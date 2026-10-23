from __future__ import annotations

import html
import ipaddress
import re
import socket
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


@dataclass(frozen=True)
class CrawlResult:
    folder_id: int
    folder_name: str
    downloaded: int
    messages: list[str]


class UnsafeUrlError(ValueError):
    pass


class DownloadTooLargeError(ValueError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


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


def validated_url(url: str) -> str:
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
    return normalized


class SafeHttpClient:
    def __init__(self, max_requests: int = MAX_REQUEST_COUNT) -> None:
        self.max_requests = max_requests
        self.request_count = 0
        self.opener = urllib.request.build_opener(_NoRedirectHandler())

    def open(self, url: str, method: str = "GET"):
        current = validated_url(url)
        for redirect_count in range(MAX_REDIRECTS + 1):
            if self.request_count >= self.max_requests:
                raise ValueError(f"爬取请求数量不能超过 {self.max_requests}")
            self.request_count += 1
            request = urllib.request.Request(
                current,
                method=method,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            )
            try:
                response = self.opener.open(request, timeout=REQUEST_TIMEOUT)
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                response = exc
            status = getattr(response, "status", response.getcode())
            if status not in {301, 302, 303, 307, 308}:
                return response, current
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise UnsafeUrlError("重定向缺少目标网址")
            if redirect_count >= MAX_REDIRECTS:
                raise UnsafeUrlError(f"重定向次数不能超过 {MAX_REDIRECTS}")
            current = validated_url(urllib.parse.urljoin(current, location))
        raise UnsafeUrlError("重定向失败")


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


def _preflight_download(client: SafeHttpClient, url: str) -> None:
    try:
        response, _final_url = client.open(url, method="HEAD")
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
) -> CrawlResult:
    del follow_links  # Recursive page crawling stays disabled for security.
    url = validated_url(normalize_source(source))
    parsed_source = urllib.parse.urlsplit(url)
    display_source = urllib.parse.urlunsplit((parsed_source.scheme, parsed_source.netloc, parsed_source.path, "", ""))
    messages: list[str] = [f"Source: {display_source}"]
    client = SafeHttpClient()

    response, final_url = client.open(url)
    try:
        content_type = response.headers.get_content_type()
        if content_type != "text/html":
            folder_name = safe_name(filename_from_response(final_url, response.headers) or "Downloaded Files")
            folder_id = store.create_folder(parent_folder_id, folder_name)
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir) / folder_name
                _write_limited(response, temp_path, MAX_DOWNLOAD_BYTES)
                store.add_local_file(temp_path, folder_id, folder_name)
            return CrawlResult(folder_id, folder_name, 1, messages)

        page = _read_limited(response, MAX_HTML_BYTES).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )
    finally:
        response.close()

    collector = LinkCollector()
    collector.feed(page)
    folder_name = safe_name(collector.page_title() or "Crawled Series")
    folder_id = store.create_folder(parent_folder_id, folder_name)

    candidates = list(download_candidates(final_url, collector.links, validated_url))
    if not candidates:
        messages.append("未在页面中发现安全的直接下载链接，已保存源页面 HTML。")

    downloaded = 0
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        for link_url, label in candidates[: min(max_downloads, MAX_DOWNLOADS)]:
            try:
                _preflight_download(client, link_url)
                file_response, final_file_url = client.open(link_url)
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
            except (OSError, ValueError, urllib.error.URLError) as exc:
                messages.append(f"跳过 {link_url}: {exc}")

    if downloaded == 0:
        with tempfile.TemporaryDirectory() as temp_dir:
            filename = folder_name + ".html"
            temp_path = Path(temp_dir) / filename
            temp_path.write_text(page, encoding="utf-8")
            store.add_local_file(temp_path, folder_id, filename)
            downloaded = 1
            messages.append("所有下载链接均失败，已保存源页面 HTML。")

    return CrawlResult(folder_id, folder_name, downloaded, messages)


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
