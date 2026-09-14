from __future__ import annotations

import html
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

from .store import LibraryStore, safe_name


USER_AGENT = "BookManager/0.1 (+local desktop crawler)"
SPRINGER_SERIES_URL = "https://link.springer.com/bookseries/{series_id}"
DOWNLOAD_EXTENSIONS = {".pdf", ".epub", ".zip", ".csv", ".xlsx", ".txt", ".xml"}


@dataclass(frozen=True)
class CrawlResult:
    folder_id: int
    folder_name: str
    downloaded: int
    messages: list[str]


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
    if re.fullmatch(r"\d+", value):
        return SPRINGER_SERIES_URL.format(series_id=value)
    if not re.match(r"^https?://", value, flags=re.I):
        return "https://" + value
    return value


def crawl_into_library(
    store: LibraryStore,
    source: str,
    parent_folder_id: int,
    max_downloads: int = 80,
) -> CrawlResult:
    url = normalize_source(source)
    messages: list[str] = [f"Source: {url}"]
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", USER_AGENT)]

    response = opener.open(url, timeout=30)
    content_type = response.headers.get_content_type()
    final_url = response.geturl()

    if content_type != "text/html":
        folder_name = safe_name(filename_from_response(final_url, response.headers) or "Downloaded Files")
        folder_id = store.create_folder(parent_folder_id, folder_name)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / folder_name
            temp_path.write_bytes(response.read())
            store.add_local_file(temp_path, folder_id, folder_name)
        return CrawlResult(folder_id, folder_name, 1, messages)

    page = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    collector = LinkCollector()
    collector.feed(page)
    folder_name = safe_name(collector.page_title() or "Crawled Series")
    folder_id = store.create_folder(parent_folder_id, folder_name)

    candidates = list(download_candidates(final_url, collector.links))
    if not candidates:
        messages.append("未在页面中发现可直接下载的文件链接，已保存源页面 HTML。")
        candidates = [(final_url, folder_name + ".html")]

    downloaded = 0
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        for link_url, label in candidates[:max_downloads]:
            try:
                file_response = opener.open(link_url, timeout=45)
                filename = safe_name(
                    filename_from_response(link_url, file_response.headers) or label or Path(urllib.parse.urlparse(link_url).path).name,
                    f"download-{downloaded + 1}",
                )
                if "." not in filename and file_response.headers.get_content_type() == "application/pdf":
                    filename += ".pdf"
                temp_path = temp_root / filename
                temp_path.write_bytes(file_response.read())
                store.add_local_file(temp_path, folder_id, filename)
                downloaded += 1
            except Exception as exc:  # Network pages vary heavily; keep the import best-effort.
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


def download_candidates(base_url: str, links: list[tuple[str, str]]) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    for href, label in links:
        absolute = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(absolute)
        suffix = Path(parsed.path).suffix.lower()
        is_springer_pdf = "/content/pdf/" in parsed.path or parsed.path.endswith(".pdf")
        is_download = suffix in DOWNLOAD_EXTENSIONS or "download" in parsed.path.lower() or is_springer_pdf
        if not is_download or absolute in seen:
            continue
        seen.add(absolute)
        yield absolute, label


def filename_from_response(url: str, headers) -> Optional[str]:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, flags=re.I)
    if match:
        return urllib.parse.unquote(match.group(1))
    name = Path(urllib.parse.urlparse(url).path).name
    return urllib.parse.unquote(name) if name else None
