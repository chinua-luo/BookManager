from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.metadata import extract_metadata


class MetadataSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "Book.pdf"
        self.path.write_bytes(b"%PDF-1.7\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_metadata_parser_runs_in_a_bounded_worker_process(self) -> None:
        payload = {
            "parts": {
                "series_abbr": "",
                "number": "",
                "main_title": "Book",
                "subtitle": "",
                "edition": "1",
                "authors": "",
                "extension": ".pdf",
                "edition_language": "英文",
            },
            "extracted": True,
            "message": "worker result",
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch("bookmanager.metadata.subprocess.run", return_value=completed) as run:
            result = extract_metadata(self.path)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-m", "bookmanager.metadata_worker"])
        self.assertEqual(run.call_args.kwargs["timeout"], 40)
        self.assertEqual(result.parts.main_title, "Book")
        self.assertTrue(result.extracted)

    def test_worker_failure_falls_back_without_crashing_the_bridge(self) -> None:
        with mock.patch(
            "bookmanager.metadata.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["worker"], 40),
        ):
            result = extract_metadata(self.path)
        self.assertFalse(result.extracted)
        self.assertIn("内容提取失败", result.message)


if __name__ == "__main__":
    unittest.main()
