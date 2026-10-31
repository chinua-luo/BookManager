from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from bookmanager.crawler import (
    DownloadTooLargeError,
    SafeHttpClient,
    UnsafeUrlError,
    _read_limited,
    _write_limited,
    crawl_into_library,
    download_candidates,
    filename_from_response,
    new_crawl_stats,
    normalize_source,
    normalize_url,
    recursive_link_candidates,
    sanitize_download_filename,
    url_origin,
    validated_url,
)
from bookmanager.store import safe_name


class _Response:
    def __init__(self, body: bytes = b"", status: int = 200, headers: Message | None = None) -> None:
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or Message()
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


def _headers(content_type: str) -> Message:
    headers = Message()
    headers["Content-Type"] = content_type
    return headers


def _file_headers(content_type: str, disposition: str = "", content_length: int | None = None) -> Message:
    headers = _headers(content_type)
    if disposition:
        headers["Content-Disposition"] = disposition
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return headers


class _Store:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.added: list[tuple[str, bytes]] = []
        self.source_paths: list[Path] = []
        self.folders: list[str] = []

    def close(self) -> None:
        self.temporary.cleanup()

    def create_folder(self, parent_folder_id: int, name: str) -> int:
        del parent_folder_id
        self.folders.append(name)
        return 2

    def add_local_file(self, path: Path, folder_id: int, display_name: str) -> None:
        del folder_id
        self.source_paths.append(path)
        self.added.append((display_name, path.read_bytes()))


def _direct_client(body: bytes, headers: Message) -> type:
    """A SafeHttpClient double whose single response is a non-HTML download."""

    class _DirectClient:
        latest: "_DirectClient | None" = None

        def __init__(self, max_requests: int, pin_dns: bool) -> None:
            self.max_requests = max_requests
            self.pin_dns = pin_dns
            self.request_count = 0
            self.calls: list[tuple[str, str]] = []
            type(self).latest = self

        def open(self, url: str, method: str = "GET", allowed_origin=None):  # noqa: ANN001
            del allowed_origin
            self.request_count += 1
            self.calls.append((method, url))
            return _Response(b"" if method == "HEAD" else body, headers=headers), url

    return _DirectClient


def _static_client(root_body: bytes, files: dict[str, tuple[bytes, Message]]) -> type:
    """A SafeHttpClient double serving one HTML page plus named download URLs."""

    class _StaticClient:
        latest: "_StaticClient | None" = None

        def __init__(self, max_requests: int, pin_dns: bool) -> None:
            self.max_requests = max_requests
            self.pin_dns = pin_dns
            self.request_count = 0
            self.calls: list[tuple[str, str]] = []
            type(self).latest = self

        def open(self, url: str, method: str = "GET", allowed_origin=None):  # noqa: ANN001
            del allowed_origin
            self.request_count += 1
            self.calls.append((method, url))
            if url in files:
                body, headers = files[url]
                return _Response(b"" if method == "HEAD" else body, headers=headers), url
            if url.endswith("/root"):
                return _Response(root_body, headers=_headers("text/html; charset=utf-8")), url
            raise AssertionError(f"Unexpected request: {method} {url}")

    return _StaticClient


class _RecursiveClient:
    latest: "_RecursiveClient | None" = None
    redirect_start = False
    root_download_count: int | None = None
    include_foreign = False

    def __init__(self, max_requests: int, pin_dns: bool) -> None:
        self.max_requests = max_requests
        self.pin_dns = pin_dns
        self.request_count = 0
        self.calls: list[tuple[str, str]] = []
        type(self).latest = self

    def open(self, url: str, method: str = "GET", allowed_origin=None):  # noqa: ANN001
        del allowed_origin
        self.request_count += 1
        self.calls.append((method, url))
        if url.endswith("/root"):
            if self.root_download_count is not None:
                body = b"<title>Test Series</title>" + b"".join(
                    f'<a href="/book-{index}.pdf">Book</a>'.encode() for index in range(self.root_download_count)
                )
            else:
                body = (
                    b"<title>Test Series</title>"
                    b'<a href="/child">Child</a>'
                    b'<a href="/root.pdf">Root PDF</a>'
                )
                if self.include_foreign:
                    body += b'<a href="https://example.org/foreign.pdf">Foreign</a>'
            final_url = url + "/redirected" if self.redirect_start else url
            return _Response(body, headers=_headers("text/html; charset=utf-8")), final_url
        if url.endswith("/child"):
            body = b'<a href="/book.pdf">Book</a><a href="/deep">Too deep</a>'
            return _Response(body, headers=_headers("text/html; charset=utf-8")), url
        if url.endswith(".pdf"):
            body = b"" if method == "HEAD" else b"%PDF-test"
            return _Response(body, headers=_headers("application/pdf")), url
        raise AssertionError(f"Unexpected request: {method} {url}")


class CrawlerSecurityTests(unittest.TestCase):
    def new_store(self) -> _Store:
        store = _Store()
        self.addCleanup(store.close)
        return store

    def test_rejects_private_loopback_credentials_and_non_http_urls(self) -> None:
        for url in (
            "http://127.0.0.1/admin",
            "http://10.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://user:secret@example.com/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(UnsafeUrlError):
                validated_url(url)

        for source in ("file:///etc/passwd", "ftp://example.com/book.pdf"):
            with self.subTest(source=source), self.assertRaises(UnsafeUrlError):
                normalize_source(source)

    def test_dns_resolution_rejects_any_private_answer(self) -> None:
        answers = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("192.168.1.10", 443)),
        ]
        with mock.patch("bookmanager.crawler.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(UnsafeUrlError):
                validated_url("https://example.com/book.pdf")

    def test_redirect_target_is_validated_and_private_redirect_is_blocked(self) -> None:
        headers = Message()
        headers["Location"] = "http://127.0.0.1/private"
        redirect = urllib.error.HTTPError("https://93.184.216.34/start", 302, "Found", headers, io.BytesIO())
        client = SafeHttpClient()
        client.opener.open = mock.Mock(side_effect=redirect)
        with self.assertRaises(UnsafeUrlError):
            client.open("https://93.184.216.34/start")
        self.assertEqual(client.request_count, 1)

    def test_download_candidates_drop_fragments_duplicates_and_unsafe_links(self) -> None:
        links = [
            ("/book.pdf#page=2", "one"),
            ("/book.pdf", "duplicate"),
            ("http://127.0.0.1/private.pdf", "private"),
            ("javascript:alert(1)", "script"),
        ]
        validator = lambda url: url if "127.0.0.1" not in url else (_ for _ in ()).throw(UnsafeUrlError())
        stats = new_crawl_stats()
        self.assertEqual(
            list(download_candidates("https://example.com/catalog", links, validator, stats=stats)),
            [("https://example.com/book.pdf", "one")],
        )
        self.assertEqual(stats["total_links"], 4)
        self.assertEqual(stats["skipped_duplicate"], 1)
        self.assertEqual(stats["skipped_safe_block"], 2)
        self.assertEqual(stats["success"], 0)

    def test_response_size_is_enforced_with_and_without_content_length(self) -> None:
        headers = Message()
        headers["Content-Length"] = "11"
        with self.assertRaises(DownloadTooLargeError):
            _read_limited(_Response(b"small", headers=headers), 10)
        with self.assertRaises(DownloadTooLargeError):
            _read_limited(_Response(b"01234567890"), 10)

    def test_request_budget_stops_additional_requests(self) -> None:
        client = SafeHttpClient(max_requests=1)
        client.opener.open = mock.Mock(return_value=_Response())
        response, _url = client.open("https://93.184.216.34/one")
        response.close()
        with self.assertRaises(ValueError):
            client.open("https://93.184.216.34/two")

    def test_normalization_removes_fragments(self) -> None:
        self.assertEqual(normalize_url("HTTPS://Example.com/a.pdf#page=2"), "https://example.com/a.pdf")

    def test_download_filename_can_be_safely_reduced_to_a_basename(self) -> None:
        headers = Message()
        headers["Content-Disposition"] = 'attachment; filename="../../escape.pdf"'
        filename = safe_name(filename_from_response("https://example.com/file", headers) or "")
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)

    def test_recursive_candidates_keep_same_origin_and_count_cross_origin(self) -> None:
        stats = {"total_links": 0, "success": 0, "skipped_cross_origin": 0, "skipped_safe_block": 0}
        origin = url_origin("https://example.com/catalog")

        downloads, pages = recursive_link_candidates(
            "https://example.com/catalog",
            [("/book.pdf", "Book"), ("/series", "Series"), ("https://other.example/file.pdf", "Other")],
            origin,
            stats,
        )

        self.assertEqual(downloads, [("https://example.com/book.pdf", "Book")])
        self.assertEqual(pages, ["https://example.com/series"])
        self.assertEqual(stats["total_links"], 3)
        self.assertEqual(stats["skipped_cross_origin"], 1)

    def test_recursive_candidates_count_duplicates(self) -> None:
        stats = new_crawl_stats()
        origin = url_origin("https://example.com/catalog")

        downloads, pages = recursive_link_candidates(
            "https://example.com/catalog",
            [("/book.pdf", "Book"), ("/book.pdf", "Again"), ("/series", "Series"), ("/series", "Series again")],
            origin,
            stats,
        )

        self.assertEqual(downloads, [("https://example.com/book.pdf", "Book")])
        self.assertEqual(pages, ["https://example.com/series"])
        self.assertEqual(stats["total_links"], 4)
        self.assertEqual(stats["skipped_duplicate"], 2)

    def test_recursive_crawl_only_reads_first_level_same_origin_pages(self) -> None:
        store = self.new_store()
        stats = new_crawl_stats()
        _RecursiveClient.redirect_start = False
        _RecursiveClient.root_download_count = None
        _RecursiveClient.include_foreign = True
        try:
            with mock.patch("bookmanager.crawler.SafeHttpClient", _RecursiveClient):
                result = crawl_into_library(
                    store,
                    "https://93.184.216.34/root",
                    1,
                    follow_links=True,
                    sandboxed_recursive_mode=True,
                    crawl_stats=stats,
                )
        finally:
            _RecursiveClient.include_foreign = False

        client = _RecursiveClient.latest
        self.assertIsNotNone(client)
        self.assertTrue(client.pin_dns)
        self.assertIn(("GET", "https://93.184.216.34/child"), client.calls)
        self.assertNotIn(("GET", "https://93.184.216.34/deep"), client.calls)
        self.assertFalse(any("example.org" in url for _method, url in client.calls))
        self.assertEqual(result.downloaded, 2)
        self.assertEqual(result.stats["success"], 2)
        self.assertEqual(result.stats["skipped_cross_origin"], 1)
        self.assertIs(result.stats, stats)
        self.assertTrue(all(str(path).startswith(str(store.home / "tmp")) for path in store.source_paths))

    def test_recursive_flag_downgrades_without_sandbox_confirmation(self) -> None:
        store = self.new_store()
        _RecursiveClient.redirect_start = False
        _RecursiveClient.root_download_count = None
        _RecursiveClient.include_foreign = False
        with mock.patch("bookmanager.crawler.SafeHttpClient", _RecursiveClient):
            result = crawl_into_library(
                store,
                "https://93.184.216.34/root",
                1,
                follow_links=True,
                sandboxed_recursive_mode=False,
            )

        client = _RecursiveClient.latest
        # DNS pinning is enforced even when recursive mode is downgraded.
        self.assertTrue(client.pin_dns)
        self.assertNotIn(("GET", "https://93.184.216.34/child"), client.calls)
        self.assertNotIn(("GET", "https://example.org/foreign.pdf"), client.calls)
        # Non-recursive mode now records candidate links and successful downloads too.
        self.assertEqual(result.stats["total_links"], 2)
        self.assertEqual(result.stats["success"], 1)
        self.assertEqual(result.downloaded, 1)

    def test_recursive_start_redirect_disables_link_extraction(self) -> None:
        store = self.new_store()
        _RecursiveClient.redirect_start = True
        _RecursiveClient.root_download_count = None
        with mock.patch("bookmanager.crawler.SafeHttpClient", _RecursiveClient):
            result = crawl_into_library(
                store,
                "https://93.184.216.34/root",
                1,
                follow_links=True,
                sandboxed_recursive_mode=True,
            )

        client = _RecursiveClient.latest
        self.assertEqual(client.calls, [("GET", "https://93.184.216.34/root")])
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.stats["success"], 0)
        self.assertTrue(any("重定向" in message for message in result.messages))

    def test_recursive_downloads_are_limited_to_eight_files(self) -> None:
        store = self.new_store()
        _RecursiveClient.redirect_start = False
        _RecursiveClient.root_download_count = 10
        try:
            with mock.patch("bookmanager.crawler.SafeHttpClient", _RecursiveClient):
                result = crawl_into_library(
                    store,
                    "https://93.184.216.34/root",
                    1,
                    follow_links=True,
                    sandboxed_recursive_mode=True,
                )
        finally:
            _RecursiveClient.root_download_count = None

        self.assertEqual(result.downloaded, 8)
        self.assertEqual(result.stats["success"], 8)
        self.assertEqual(result.stats["skipped_quota"], 2)
        self.assertEqual(len(store.added), 8)

    def test_sanitize_download_filename_preserves_allowed_and_neutralizes_unsafe_names(self) -> None:
        cases = [
            ("https://example.com/file", _file_headers("application/pdf", 'attachment; filename="book.pdf"'), "download", "application/pdf", "book.pdf"),
            ("https://example.com/report.csv", _headers("text/csv"), "download", "text/csv", "report.csv"),
            ("https://example.com/download/12345", _headers("application/pdf"), "download", "application/pdf", "12345.pdf"),
            ("https://example.com/file", _file_headers("application/octet-stream", 'attachment; filename="setup.command"'), "download", "application/octet-stream", "setup.command.download"),
            ("https://example.com/", _headers("application/octet-stream"), "download", "application/octet-stream", "download.download"),
        ]
        for url, headers, fallback, content_type, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(sanitize_download_filename(url, headers, fallback, content_type=content_type), expected)

    def test_direct_source_download_keeps_allowed_extension(self) -> None:
        store = self.new_store()
        client = _direct_client(b"%PDF-test", _file_headers("application/pdf", 'attachment; filename="book.pdf"'))
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            result = crawl_into_library(store, "https://93.184.216.34/content", 1)

        self.assertTrue(client.latest.pin_dns)
        self.assertEqual(store.added[0][0], "book.pdf")
        self.assertEqual(store.folders, ["book.pdf"])
        self.assertEqual(result.downloaded, 1)

    def test_direct_source_download_appends_inert_suffix_for_executable_content(self) -> None:
        store = self.new_store()
        payload = b"#!/bin/sh\nexit 0\n"
        client = _direct_client(
            payload,
            _file_headers("application/octet-stream", 'attachment; filename="setup.command"'),
        )
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            result = crawl_into_library(store, "https://93.184.216.34/installer", 1)

        self.assertEqual(store.added, [("setup.command.download", payload)])
        self.assertEqual(store.folders, ["setup.command.download"])
        self.assertEqual(result.downloaded, 1)

    def test_direct_source_pdf_without_filename_gets_pdf_suffix(self) -> None:
        store = self.new_store()
        client = _direct_client(b"%PDF-test", _file_headers("application/pdf"))
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            crawl_into_library(store, "https://93.184.216.34/download/12345", 1)

        self.assertEqual(store.added[0][0], "12345.pdf")

    def test_link_download_keeps_allowed_content_disposition_filename(self) -> None:
        store = self.new_store()
        root = b'<title>Series</title><a href="/download/1">Book</a>'
        files = {"https://93.184.216.34/download/1": (b"%PDF-test", _file_headers("application/pdf", 'attachment; filename="paper.pdf"'))}
        client = _static_client(root, files)
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            result = crawl_into_library(store, "https://93.184.216.34/root", 1)

        self.assertEqual(store.added[0][0], "paper.pdf")
        self.assertEqual(result.downloaded, 1)

    def test_link_download_appends_inert_suffix_for_executable_content_disposition(self) -> None:
        store = self.new_store()
        root = b'<title>Series</title><a href="/book.pdf">Book</a>'
        files = {"https://93.184.216.34/book.pdf": (b"%PDF-test", _file_headers("application/pdf", 'attachment; filename="run.exe"'))}
        client = _static_client(root, files)
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            result = crawl_into_library(store, "https://93.184.216.34/root", 1)

        self.assertEqual(store.added[0][0], "run.exe.download")
        self.assertEqual(result.downloaded, 1)

    def test_link_download_pdf_without_extension_gets_pdf_suffix(self) -> None:
        store = self.new_store()
        root = b'<title>Series</title><a href="/content/pdf/12345">Book</a>'
        files = {"https://93.184.216.34/content/pdf/12345": (b"%PDF-test", _file_headers("application/pdf"))}
        client = _static_client(root, files)
        with mock.patch("bookmanager.crawler.SafeHttpClient", client):
            crawl_into_library(store, "https://93.184.216.34/root", 1)

        self.assertEqual(store.added[0][0], "12345.pdf")

    def test_total_byte_budget_stops_remaining_downloads(self) -> None:
        store = self.new_store()
        root = b"<title>Budget</title>" + b"".join(
            f'<a href="/book-{index}.pdf">Book</a>'.encode() for index in range(3)
        )
        files = {}
        for index in range(3):
            files[f"https://93.184.216.34/book-{index}.pdf"] = (
                b"0123456789",
                _file_headers("application/pdf", content_length=10),
            )
        client = _static_client(root, files)
        with (
            mock.patch("bookmanager.crawler.SafeHttpClient", client),
            mock.patch("bookmanager.crawler.MAX_CRAWL_TOTAL_BYTES", 12),
        ):
            result = crawl_into_library(store, "https://93.184.216.34/root", 1)

        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.stats["success"], 1)
        self.assertEqual(result.stats["skipped_quota"], 2)
        self.assertTrue(any("本次抓取总大小已达上限" in message for message in result.messages))
        self.assertEqual(len(store.added), 1)

    def test_write_limited_returns_bytes_actually_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "file.download"
            written = _write_limited(_Response(b"abcdef"), target, 10)
            self.assertEqual(written, 6)
            self.assertEqual(target.read_bytes(), b"abcdef")

    def test_recursive_dns_results_are_cached_for_the_origin(self) -> None:
        client = SafeHttpClient(pin_dns=True)
        response = _Response(headers=_headers("text/html"))
        with (
            mock.patch(
                "bookmanager.crawler._public_addresses",
                return_value=("https://example.com/one", ("93.184.216.34",)),
            ) as resolve,
            mock.patch.object(client, "_open_pinned", return_value=response) as open_pinned,
        ):
            client.open("https://example.com/one")
            client.open("https://example.com/two")

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(open_pinned.call_count, 2)


if __name__ == "__main__":
    unittest.main()
