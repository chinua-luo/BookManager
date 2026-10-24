from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from bookmanager.bridge import BridgeServer
from bookmanager.crawler import CrawlResult
from bookmanager.operation_log import OperationLogger


class OperationLogBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = "test-token"
        self.server = BridgeServer(("127.0.0.1", 0), self.root / "data", self.token, self.root)
        fixed_now = dt.datetime(2026, 10, 22, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.server.operation_logger = OperationLogger(
            self.server.library_home,
            clock=lambda: fixed_now,
            session_id="bridge-test",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    @property
    def log_path(self) -> Path:
        return self.server.library_home / "logs" / "bookmanager-2026-10-22.jsonl"

    def request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))["data"]

    def records(self) -> list[dict[str, object]]:
        deadline = time.monotonic() + 2
        while (not self.log_path.exists() or self.log_path.stat().st_size == 0) and time.monotonic() < deadline:
            time.sleep(0.01)
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_read_only_navigation_does_not_create_a_log(self) -> None:
        self.request("/api/tree")
        self.request("/api/folders/1/contents")

        self.assertFalse(self.log_path.exists())

    def test_import_logs_the_full_display_name_with_extension(self) -> None:
        source = self.root / "unformatted-source.pdf"
        source.write_bytes(b"%PDF-1.4\n% test fixture\n")

        self.request(
            "/api/import/file",
            {
                "path": str(source),
                "folderId": 1,
                "nameParts": {
                    "seriesAbbr": "",
                    "number": "",
                    "mainTitle": "Sample Book",
                    "subtitle": "",
                    "edition": "1",
                    "authors": "Example Author",
                    "extension": ".pdf",
                    "editionLanguage": "英文",
                },
            },
        )

        record = self.records()[0]
        self.assertEqual(record["event"], "file.import")
        self.assertEqual(record["message"], "导入文件")
        self.assertEqual(record["document_name"], "Sample Book _ Example Author.pdf")
        self.assertEqual(record["result"], "success")

    def test_external_open_failure_records_message_and_error(self) -> None:
        self.request(
            "/api/logs/events",
            {
                "event": "file.open.default",
                "message": "使用系统默认程序打开文件",
                "documentName": "Broken Book.epub",
                "result": "failure",
                "error": "没有关联的默认程序",
                "durationMs": 12,
            },
        )

        record = self.records()[0]
        self.assertEqual(record["level"], "ERROR")
        self.assertEqual(record["message"], "使用系统默认程序打开文件")
        self.assertEqual(record["document_name"], "Broken Book.epub")
        self.assertEqual(record["error"], "没有关联的默认程序")

    def test_failed_operation_records_the_reported_error(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/folders", {"parentId": 999999, "name": "Invalid Parent"})

        self.assertEqual(raised.exception.code, 500)
        record = self.records()[0]
        self.assertEqual(record["event"], "folder.create")
        self.assertEqual(record["result"], "failure")
        self.assertTrue(record["error"])

    def test_crawl_only_enables_recursion_for_exact_boolean_true(self) -> None:
        for requested, expected in ((True, True), (False, False), ("true", False)):
            with self.subTest(requested=requested):
                result = CrawlResult(2, "Series", 1, [], {"total_links": 3, "success": 1, "skipped_cross_origin": 1, "skipped_safe_block": 0})
                with mock.patch("bookmanager.bridge.crawl_into_library", return_value=result) as crawl:
                    response = self.request(
                        "/api/crawl",
                        {"source": "https://example.com", "folderId": 1, "enable_recursive": requested},
                    )

                self.assertEqual(response["recursiveEnabled"], expected)
                self.assertEqual(crawl.call_args.kwargs["follow_links"], expected)
                self.assertEqual(crawl.call_args.kwargs["sandboxed_recursive_mode"], expected)


if __name__ == "__main__":
    unittest.main()
