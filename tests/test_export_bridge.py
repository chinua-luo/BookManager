from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.bridge import BridgeServer
from bookmanager.naming import StructuredFileName, build_structured_file_name
from bookmanager.operation_log import OperationLogger
from bookmanager.store import LibraryStore


class ExportBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = "export-token"
        self.env = mock.patch.dict(
            os.environ,
            {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")},
        )
        self.env.start()
        self.server = BridgeServer(("127.0.0.1", 0), self.root / "data", self.token, self.root)
        self.server.wait_for_startup_cleanup(5)
        fixed_now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.server.operation_logger = OperationLogger(
            self.server.library_home,
            clock=lambda: fixed_now,
            session_id="export-bridge-test",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        store = LibraryStore(self.root / "data")
        try:
            self.folder_id = store.create_folder(1, "Series")
            self.source = self.root / "source.txt"
            self.source.write_text("book content", encoding="utf-8")
            self.name_parts = StructuredFileName(
                series_abbr="ABC",
                number="7",
                main_title="Main Title",
                subtitle="Sub",
                edition="2",
                authors="Author",
                extension=".txt",
            )
            store.add_local_file(self.source, self.folder_id, self.name_parts)
        finally:
            store.close()

        self.target = self.root / "target"
        self.target.mkdir()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temporary.cleanup()

    def request(self, path: str, payload: dict[str, object] | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("POST", path, data, headers)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, json.loads(body)

    def diagnostics(self) -> list[dict[str, object]]:
        log_dir = self.server.library_home / "logs"
        return [
            json.loads(line)
            for path in log_dir.glob("diagnostics-*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def operation_records(self) -> list[dict[str, object]]:
        path = self.server.library_home / "logs" / "bookmanager-2026-11-01.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_export_returns_curated_data_and_writes_one_operation_record(self) -> None:
        status, result = self.request(
            f"/api/folders/{self.folder_id}/export",
            {"targetPath": str(self.target)},
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["error_type"], "")
        exported = self.target / "Series"
        self.assertTrue(exported.is_dir())
        expected_name = build_structured_file_name(self.name_parts)
        self.assertTrue((exported / expected_name).is_file())
        self.assertEqual((exported / expected_name).read_text(encoding="utf-8"), "book content")
        self.assertEqual(result["data"]["folderId"], self.folder_id)
        self.assertEqual(result["data"]["folderName"], "Series")
        self.assertEqual(result["data"]["fileCount"], 1)
        self.assertEqual(result["data"]["folderCount"], 0)
        self.assertEqual(result["data"]["copiedBytes"], len(b"book content"))

        record = self.operation_records()[0]
        self.assertEqual(record["event"], "folder.export")
        self.assertEqual(record["message"], "导出文件夹")
        self.assertEqual(record["result"], "success")
        self.assertEqual(record["details"]["folder_name"], "Series")
        self.assertEqual(record["details"]["target_folder"], "target")
        self.assertEqual(record["details"]["file_count"], 1)
        self.assertEqual(record["details"]["folder_count"], 0)
        self.assertNotIn(str(self.target), json.dumps(record, ensure_ascii=False))

    def test_conflict_is_a_409_without_absolute_paths_or_tracebacks(self) -> None:
        (self.target / "Series").mkdir()
        status, result = self.request(
            f"/api/folders/{self.folder_id}/export",
            {"targetPath": str(self.target)},
        )

        self.assertEqual(status, 409)
        self.assertEqual(result["error_type"], "export_conflict")
        self.assertNotIn(str(self.target), json.dumps(result, ensure_ascii=False))
        records = self.diagnostics()
        self.assertEqual(
            sum(record["event"] == "bridge.validation_rejected" for record in records),
            1,
        )
        self.assertFalse(any(record["event"] == "bridge.request_failed" for record in records))

        operation = self.operation_records()[0]
        self.assertEqual(operation["event"], "folder.export")
        self.assertEqual(operation["result"], "failure")
        self.assertEqual(operation["details"]["export_error_type"], "export_conflict")
        self.assertEqual(operation["details"]["folder_name"], "Series")
        self.assertEqual(operation["details"]["target_folder"], "target")
        self.assertNotIn(str(self.target), json.dumps(operation, ensure_ascii=False))

    def test_missing_folder_is_a_404_with_the_export_error_type(self) -> None:
        status, result = self.request(
            "/api/folders/999999/export",
            {"targetPath": str(self.target)},
        )

        self.assertEqual(status, 404)
        self.assertEqual(result["error_type"], "export_folder_missing")
        self.assertTrue(result["msg"])
        self.assertIsNone(result["data"])

    def test_target_inside_library_is_rejected_before_copying(self) -> None:
        status, result = self.request(
            f"/api/folders/{self.folder_id}/export",
            {"targetPath": str(self.root / "data")},
        )

        self.assertEqual(status, 400)
        self.assertEqual(result["error_type"], "export_target_invalid")
        self.assertFalse((self.root / "data" / "Series").exists())

    def test_missing_source_file_maps_to_export_source_missing(self) -> None:
        store = LibraryStore(self.root / "data")
        try:
            row = store.conn.execute("SELECT stored_path FROM blobs").fetchone()
        finally:
            store.close()
        Path(row["stored_path"]).unlink()

        status, result = self.request(
            f"/api/folders/{self.folder_id}/export",
            {"targetPath": str(self.target)},
        )

        self.assertEqual(status, 400)
        self.assertEqual(result["error_type"], "export_source_missing")
        self.assertFalse((self.target / "Series").exists())

    def test_unexpected_export_error_is_500_without_leaking_details(self) -> None:
        with mock.patch("bookmanager.bridge.export_folder", side_effect=RuntimeError("Traceback /secret")):
            status, result = self.request(
                f"/api/folders/{self.folder_id}/export",
                {"targetPath": str(self.target)},
            )

        self.assertEqual(status, 500)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("Traceback", json.dumps(result))
        records = self.diagnostics()
        self.assertTrue(any(record["event"] == "bridge.request_failed" for record in records))
        self.assertTrue(any("Traceback" in str(record.get("traceback", "")) for record in records if record["event"] == "bridge.request_failed"))


if __name__ == "__main__":
    unittest.main()
