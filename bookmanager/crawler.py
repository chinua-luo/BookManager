from __future__ import annotations

import html
import http.client
import ipaddress
import re
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Optional

from .store import LibraryStore, safe_name


USER_AGENT = "BookManager/0.1 (+local desktop crawler)"
SPRINGER_SERIES_URL = "https://link.springer.com/bookseries/{series_id}"
DOWNLOAD_EXTENSIONS = {".pdf", ".epub", ".zip", ".csv", ".xlsx", ".txt", ".xml"}
REQUEST_TIMEOUT = 15
MAX_REDIRECTS = 2
MAX_REQUEST_COUNT = 100
MAX_DOWNLOADS = 40
MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
SANDBOX_MAX_DEPTH = 1
SANDBOX_MAX_REQUEST_COUNT = 20
SANDBOX_MAX_DOWNLOADS = 8
SANDBOX_MAX_PAGE_REQUESTS = 5


@dataclass(frozen=True)
class CrawlResult:
    folder_id: int
    folder_name: str
    downloaded: int
    messages: list[str]
    stats: dict[str, int]


class UnsafeUrlError(ValueError):
    pass


class DownloadTooLargeError(ValueError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int) -> None:
        super().__init__(hostname, port=port, timeout=REQUEST_TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedResponse:
    def __init__(self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection) -> None:
        self.response = response
        self.connection = connection
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers

    def read(self, size: int = -1) -> bytes:
        return self.response.read(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.in_title = False
        self.h1_parts: list[str] = []
        self.in_h1 = False
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "h1":
            self.in_h1 = True
        elif tag.lower() == "a" and attrs_dict.get("href"):
            label = attrs_dict.get("title") or attrs_dict.get("aria-label") or ""
            self.links.append((attrs_dict["href"], label))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
        elif tag.lower() == "h1":
            self.in_h1 = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_h1:
            self.h1_parts.append(data)

    def page_title(self) -> str:
        h1 = " ".join(part.strip() for part in self.h1_parts if part.strip())
        title = " ".join(part.strip() for part in self.title_parts if part.strip())
        title = re.sub(r"\s+", " ", h1 or title).strip()
        title = title.replace(" | SpringerLink", "").replace(" | Springer", "")
        return html.unescape(title)


def normalize_source(source: str) -> str:
    value = source.strip()
    if not value:
        raise ValueError("请输入网址或 Springer 系列编号")
    if len(value) > 2048:
        raise ValueError("网址过长")
    if re.fullmatch(r"\d+", value):
        return SPRINGER_SERIES_URL.format(series_id=value)
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.I) and not re.match(r"^https?://", value, flags=re.I):
        raise UnsafeUrlError("仅允许 http 或 https 网址")
    if not re.match(r"^https?://", value, flags=re.I):
        return "https://" + value
    return value


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("仅允许 http 或 https 网址")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("网址主机无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("网址端口无效") from exc
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def is_url_safe(url: str) -> bool:
    try:
        validated_url(url)
    except (OSError, UnsafeUrlError):
        return False
    return True


def _public_addresses(url: str) -> tuple[str, tuple[str, ...]]:
    normalized = normalize_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    hostname = parsed.hostname or ""
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("禁止访问本机或内网地址")
    try:
        addresses = {
            ipaddress.ip_address(sockaddr[0])
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"无法解析网址主机：{hostname}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeUrlError("禁止访问本机、内网、链路本地或保留地址")
    return normalized, tuple(sorted(str(address) for address in addresses))


def validated_url(url: str) -> str:
    return _public_addresses(url)[0]


def url_origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(normalize_url(url))
    return parsed.scheme, parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)


class SafeHttpClient:
    def __init__(self, max_requests: int = MAX_REQUEST_COUNT, pin_dns: bool = False) -> None:
        self.max_requests = max_requests
        self.request_count = 0
        self.pin_dns = pin_dns
        self.address_cache: dict[tuple[str, str, int], tuple[str, ...]] = {}
        self.opener = urllib.request.build_opener(_NoRedirectHandler())

    def open(
        self,
        url: str,
        method: str = "GET",
        allowed_origin: tuple[str, str, int] | None = None,
    ):
        current = self._prepare_url(url, allowed_origin)
        for redirect_count in range(MAX_REDIRECTS + 1):
            if self.request_count >= self.max_requests:
                raise ValueError(f"爬取请求数量不能超过 {self.max_requests}")
            self.request_count += 1
            try:
                if self.pin_dns:
                    response = self._open_pinned(current, method)
                else:
                    request = urllib.request.Request(
                        current,
                        method=method,
                        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                    )
                    response = self.opener.open(request, timeout=REQUEST_TIMEOUT)
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                response = exc
            status = getattr(response, "status", response.getcode())
            if status not in {301, 302, 303, 307, 308}:
                if status >= 400:
                    reason = getattr(response, "reason", "HTTP error")
                    raise urllib.error.HTTPError(current, status, reason, response.headers, response)
                return response, current
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise UnsafeUrlError("重定向缺少目标网址")
            if redirect_count >= MAX_REDIRECTS:
                raise UnsafeUrlError(f"重定向次数不能超过 {MAX_REDIRECTS}")
            current = self._prepare_url(urllib.parse.urljoin(current, location), allowed_origin)
        raise UnsafeUrlError("重定向失败")

    def _prepare_url(self, url: str, allowed_origin: tuple[str, str, int] | None) -> str:
        normalized = normalize_url(url)
        if allowed_origin is not None and url_origin(normalized) != allowed_origin:
            raise UnsafeUrlError("递归模式禁止访问跨源链接")
        if not self.pin_dns:
            return validated_url(normalized)
        origin = url_origin(normalized)
        if origin not in self.address_cache:
            normalized, addresses = _public_addresses(normalized)
            self.address_cache[origin] = addresses
        return normalized

    def _open_pinned(self, url: str, method: str):
        parsed = urllib.parse.urlsplit(url)
        origin = url_origin(url)
        addresses = self.address_cache[origin]
        port = origin[2]
        host = parsed.hostname or ""
        authority = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            authority += f":{parsed.port}"
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_error: BaseException | None = None
        for address in addresses:
            connection: http.client.HTTPConnection | None = None
            try:
                if parsed.scheme == "https":
                    connection = _PinnedHTTPSConnection(host, address, port)
                else:
                    connection = http.client.HTTPConnection(address, port=port, timeout=REQUEST_TIMEOUT)
                connection.request(
                    method,
                    target,
                    headers={"Host": authority, "User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                )
                return _PinnedResponse(connection.getresponse(), connection)
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
        raise urllib.error.URLError(last_error or "无法连接已验证地址")


def _content_length(headers) -> int | None:  # noqa: ANN001
    value = headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ensure_size_allowed(headers, max_bytes: int) -> None:  # noqa: ANN001
    length = _content_length(headers)
    if length is not None and length > max_bytes:
        raise DownloadTooLargeError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限")


def _read_limited(response, max_bytes: int) -> bytes:  # noqa: ANN001
    _ensure_size_allowed(response.headers, max_bytes)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = response.read(min(1024 * 1024, max_bytes - size + 1))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise DownloadTooLargeError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限")
        chunks.append(chunk)


def _write_limited(response, target: Path, max_bytes: int) -> None:  # noqa: ANN001
    _ensure_size_allowed(response.headers, max_bytes)
    size = 0
    with target.open("xb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                return
            size += len(chunk)
            if size > max_bytes:
                raise DownloadTooLargeError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限")
            output.write(chunk)


def _crawl_temporary_root(store: LibraryStore) -> Path:
    root = store.home / "tmp"
    if root.is_symlink():
        raise ValueError("爬取临时目录不能是软链接")
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _preflight_download(
    client: SafeHttpClient,
    url: str,
    allowed_origin: tuple[str, str, int] | None = None,
) -> None:
    try:
        response, _final_url = client.open(url, method="HEAD", allowed_origin=allowed_origin)
    except urllib.error.HTTPError as exc:
        if exc.code in {405, 501}:
            return
        raise
    try:
        _ensure_size_allowed(response.headers, MAX_DOWNLOAD_BYTES)
    finally:
        response.close()


def crawl_into_library(
    store: LibraryStore,
    source: str,
    parent_folder_id: int,
    max_downloads: int = MAX_DOWNLOADS,
    follow_links: bool = False,
    sandboxed_recursive_mode: bool = False,
) -> CrawlResult:
    recursive_enabled = follow_links and sandboxed_recursive_mode
    url = validated_url(normalize_source(source))
    origin = url_origin(url)
    parsed_source = urllib.parse.urlsplit(url)
    display_source = urllib.parse.urlunsplit((parsed_source.scheme, parsed_source.netloc, parsed_source.path, "", ""))
    messages: list[str] = [f"Source: {display_source}"]
    stats = {"total_links": 0, "success": 0, "skipped_cross_origin": 0, "skipped_safe_block": 0}
    request_limit = SANDBOX_MAX_REQUEST_COUNT if recursive_enabled else MAX_REQUEST_COUNT
    download_limit = SANDBOX_MAX_DOWNLOADS if recursive_enabled else min(max_downloads, MAX_DOWNLOADS)
    client = SafeHttpClient(max_requests=request_limit, pin_dns=recursive_enabled)
    temporary_root = _crawl_temporary_root(store)

    response, final_url = client.open(url)
    try:
        content_type = response.headers.get_content_type()
        if content_type != "text/html":
            folder_name = safe_name(filename_from_response(final_url, response.headers) or "Downloaded Files")
            folder_id = store.create_folder(parent_folder_id, folder_name)
            with tempfile.TemporaryDirectory(prefix="crawl-", dir=temporary_root) as temp_dir:
                temp_path = Path(temp_dir) / folder_name
                _write_limited(response, temp_path, MAX_DOWNLOAD_BYTES)
                store.add_local_file(temp_path, folder_id, folder_name)
            stats["success"] = 1
            return CrawlResult(folder_id, folder_name, 1, messages, stats)

        page = _read_limited(response, MAX_HTML_BYTES).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )
    finally:
        response.close()

    collector = LinkCollector()
    collector.feed(page)
    folder_name = safe_name(collector.page_title() or "Crawled Series")
    folder_id = store.create_folder(parent_folder_id, folder_name)

    if recursive_enabled and final_url != url:
        downloads: list[tuple[str, str]] = []
        page_links: list[str] = []
        messages.append("起始网址发生重定向，已按安全规则停止提取页面链接。")
    elif recursive_enabled:
        downloads, page_links = recursive_link_candidates(final_url, collector.links, origin, stats)
    else:
        downloads = list(download_candidates(final_url, collector.links, validated_url))
        page_links = []

    downloaded = 0
    seen_downloads: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="crawl-", dir=temporary_root) as temp_dir:
        temp_root = Path(temp_dir)
        if recursive_enabled and SANDBOX_MAX_DEPTH >= 1:
            for page_url in page_links[:SANDBOX_MAX_PAGE_REQUESTS]:
                if client.request_count >= request_limit:
                    break
                try:
                    child_response, child_final_url = client.open(page_url, allowed_origin=origin)
                    try:
                        if child_response.headers.get_content_type() != "text/html":
                            continue
                        child_page = _read_limited(child_response, MAX_HTML_BYTES).decode(
                            child_response.headers.get_content_charset() or "utf-8", errors="replace"
                        )
                    finally:
                        child_response.close()
                    child_collector = LinkCollector()
                    child_collector.feed(child_page)
                    child_downloads, _ignored_pages = recursive_link_candidates(
                        child_final_url, child_collector.links, origin, stats
                    )
                    downloads.extend(child_downloads)
                except UnsafeUrlError as exc:
                    stats["skipped_safe_block"] += 1
                    messages.append(f"安全拦截一级链接：{exc}")
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    messages.append(f"跳过一级链接 {page_url}: {exc}")

        for link_url, label in downloads:
            if downloaded >= download_limit or link_url in seen_downloads:
                continue
            seen_downloads.add(link_url)
            try:
                _preflight_download(client, link_url, origin if recursive_enabled else None)
                file_response, final_file_url = client.open(
                    link_url, allowed_origin=origin if recursive_enabled else None
                )
                try:
                    filename = safe_name(
                        filename_from_response(final_file_url, file_response.headers)
                        or label
                        or Path(urllib.parse.urlparse(final_file_url).path).name,
                        f"download-{downloaded + 1}",
                    )
                    if "." not in filename and file_response.headers.get_content_type() == "application/pdf":
                        filename += ".pdf"
                    temp_path = temp_root / filename
                    _write_limited(file_response, temp_path, MAX_DOWNLOAD_BYTES)
                finally:
                    file_response.close()
                store.add_local_file(temp_path, folder_id, filename)
                downloaded += 1
                stats["success"] += 1
            except UnsafeUrlError as exc:
                stats["skipped_safe_block"] += 1
                messages.append(f"安全拦截下载链接：{exc}")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                messages.append(f"跳过 {link_url}: {exc}")

    if downloaded == 0:
        with tempfile.TemporaryDirectory(prefix="crawl-", dir=temporary_root) as temp_dir:
            filename = folder_name + ".html"
            temp_path = Path(temp_dir) / filename
            temp_path.write_text(page, encoding="utf-8")
            store.add_local_file(temp_path, folder_id, filename)
            downloaded = 1
        messages.append("所有下载链接均失败，已保存源页面 HTML。")

    if recursive_enabled:
        messages.append(
            "受控递归统计："
            f"链接 {stats['total_links']}，导入 {stats['success']}，"
            f"跨源丢弃 {stats['skipped_cross_origin']}，安全拦截 {stats['skipped_safe_block']}。"
        )
    return CrawlResult(folder_id, folder_name, downloaded, messages, stats)


def recursive_link_candidates(
    base_url: str,
    links: list[tuple[str, str]],
    origin: tuple[str, str, int],
    stats: dict[str, int],
) -> tuple[list[tuple[str, str]], list[str]]:
    downloads: list[tuple[str, str]] = []
    pages: list[str] = []
    seen: set[str] = set()
    for href, label in links:
        stats["total_links"] += 1
        try:
            absolute = normalize_url(urllib.parse.urljoin(base_url, href))
        except (OSError, ValueError):
            stats["skipped_safe_block"] += 1
            continue
        if url_origin(absolute) != origin:
            stats["skipped_cross_origin"] += 1
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        parsed = urllib.parse.urlsplit(absolute)
        suffix = Path(parsed.path).suffix.lower()
        is_download = suffix in DOWNLOAD_EXTENSIONS or "download" in parsed.path.lower() or "/content/pdf/" in parsed.path
        if is_download:
            downloads.append((absolute, label))
        else:
            pages.append(absolute)
    return downloads, pages


def download_candidates(
    base_url: str,
    links: list[tuple[str, str]],
    validator: Callable[[str], str] | None = None,
) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    for href, label in links:
        try:
            absolute = normalize_url(urllib.parse.urljoin(base_url, href))
            if validator is not None:
                absolute = validator(absolute)
        except (OSError, ValueError):
            continue
        parsed = urllib.parse.urlparse(absolute)
        suffix = Path(parsed.path).suffix.lower()
        is_springer_pdf = "/content/pdf/" in parsed.path or parsed.path.endswith(".pdf")
        is_download = suffix in DOWNLOAD_EXTENSIONS or "download" in parsed.path.lower() or is_springer_pdf
        if not is_download or absolute in seen:
            continue
        seen.add(absolute)
        yield absolute, label


def filename_from_response(url: str, headers) -> Optional[str]:  # noqa: ANN001
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, flags=re.I)
    if match:
        return urllib.parse.unquote(match.group(1))
    name = Path(urllib.parse.urlparse(url).path).name
    return urllib.parse.unquote(name) if name else None
