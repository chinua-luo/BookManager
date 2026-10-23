from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path

from bookmanager.operation_log import OperationLogger
from bookmanager.store import LibraryStore, migrate_library_home


class OperationLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = dt.datetime(2026, 10, 22, 14, 35, 21, 123000, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.logger = OperationLogger(self.root / "BookManagerData", clock=lambda: self.now, session_id="test-session")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def records(self, home: Path | None = None) -> list[dict[str, object]]:
        path = (home or self.root / "BookManagerData") / "logs" / "bookmanager-2026-10-22.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_is_lazy_and_writes_standard_daily_jsonl(self) -> None:
        self.assertFalse((self.root / "BookManagerData" / "logs").exists())

        path = self.logger.write(
            event="file.open.default",
            message="使用系统默认程序打开文件",
            document_name="Sample Book.pdf",
            result="success",
            duration_ms=38.4,
        )

        self.assertEqual(path.name, "bookmanager-2026-10-22.jsonl")
        self.assertEqual(
            self.records(),
            [{
                "timestamp": "2026-10-22T14:35:21.123+08:00",
                "level": "INFO",
                "event": "file.open.default",
                "message": "使用系统默认程序打开文件",
                "session_id": "test-session",
                "result": "success",
                "duration_ms": 38,
                "document_name": "Sample Book.pdf",
            }],
        )

    def test_failure_includes_sanitized_error_but_success_omits_it(self) -> None:
        home = self.root / "BookManagerData"
        self.logger.write(event="file.import", message="导入文件", result="success", duration_ms=1)
        self.logger.write(
            event="file.open.default",
            message="使用系统默认程序打开文件",
            document_name="Broken.pdf",
            result="failure",
            duration_ms=2,
            error=f"cannot read {home}/blobs/a.pdf?token=secret-value\nretry failed",
        )

        success, failure = self.records()
        self.assertNotIn("error", success)
        self.assertEqual(failure["level"], "ERROR")
        self.assertEqual(failure["result"], "failure")
        self.assertIn("<data_home>", failure["error"])
        self.assertIn("token=<redacted>", failure["error"])
        self.assertNotIn("secret-value", failure["error"])
        self.assertNotIn("\n", failure["error"])

    def test_concurrent_records_remain_complete_json_lines(self) -> None:
        threads = [
            threading.Thread(
                target=self.logger.write,
                kwargs={"event": "file.move", "message": "移动镜像文件", "result": "success", "duration_ms": index},
            )
            for index in range(30)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        records = self.records()
        self.assertEqual(len(records), 30)
        self.assertEqual({record["duration_ms"] for record in records}, set(range(30)))

    def test_date_change_rotates_to_a_new_daily_file(self) -> None:
        self.logger.write(event="file.import", message="导入文件", result="success", duration_ms=1)
        self.now += dt.timedelta(days=1)

        next_path = self.logger.write(event="file.move", message="移动镜像文件", result="success", duration_ms=2)

        self.assertEqual(next_path.name, "bookmanager-2026-10-23.jsonl")
        self.assertTrue((self.root / "BookManagerData" / "logs" / "bookmanager-2026-10-22.jsonl").is_file())

    def test_data_migration_moves_existing_logs(self) -> None:
        source = self.root / "source"
        target = self.root / "target"
        store = LibraryStore(source)
        store.close()
        self.logger.set_data_home(source)
        self.logger.write(event="folder.create", message="新建文件夹", result="success", duration_ms=3)

        migrate_library_home(source, target)

        self.assertFalse((source / "logs").exists())
        self.assertEqual(self.records(target)[0]["event"], "folder.create")


if __name__ == "__main__":
    unittest.main()
