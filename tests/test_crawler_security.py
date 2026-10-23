from __future__ import annotations

import io
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from bookmanager.crawler import (
    DownloadTooLargeError,
    SafeHttpClient,
    UnsafeUrlError,
    _read_limited,
    download_candidates,
    filename_from_response,
    normalize_url,
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


class CrawlerSecurityTests(unittest.TestCase):
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
        self.assertEqual(
            list(download_candidates("https://example.com/catalog", links, validator)),
            [("https://example.com/book.pdf", "one")],
        )

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


if __name__ == "__main__":
    unittest.main()
