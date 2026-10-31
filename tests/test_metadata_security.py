from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from bookmanager.metadata import MAX_PARALLEL_METADATA_WORKERS, extract_metadata


def _worker_payload() -> dict:
    return {
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


class MetadataSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "Book.pdf"
        self.path.write_bytes(b"%PDF-1.7\n")
        self.diagnostics = mock.patch.dict('os.environ', {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.path.parent / 'diagnostics')})
        self.diagnostics.start()

    def tearDown(self) -> None:
        self.diagnostics.stop()
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

    def test_worker_environment_omits_bridge_token_and_keeps_diagnostics_home(self) -> None:
        completed = subprocess.CompletedProcess([], 0, json.dumps(_worker_payload()), "")
        with (
            mock.patch.dict("os.environ", {"BOOKMANAGER_BRIDGE_TOKEN": "secret-bridge-token"}),
            mock.patch("bookmanager.metadata.subprocess.run", return_value=completed) as run,
        ):
            extract_metadata(self.path)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("BOOKMANAGER_BRIDGE_TOKEN", environment)
        self.assertEqual(environment["BOOKMANAGER_DIAGNOSTIC_HOME"], str(self.path.parent / "diagnostics"))

    def test_parallel_worker_processes_are_capped(self) -> None:
        completed = subprocess.CompletedProcess([], 0, json.dumps(_worker_payload()), "")
        active = 0
        peak = 0
        gate = threading.Lock()

        def fake_run(*args, **kwargs):  # noqa: ANN001, ARG001
            nonlocal active, peak
            with gate:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.05)
            finally:
                with gate:
                    active -= 1
            return completed

        with mock.patch("bookmanager.metadata.subprocess.run", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _index: extract_metadata(self.path), range(4)))

        self.assertEqual(len(results), 4)
        self.assertTrue(all(result.extracted for result in results))
        self.assertLessEqual(peak, MAX_PARALLEL_METADATA_WORKERS)
        self.assertGreaterEqual(peak, 2)

    def test_worker_failure_falls_back_without_crashing_the_bridge(self) -> None:
        with mock.patch(
            "bookmanager.metadata.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["worker"], 40),
        ):
            result = extract_metadata(self.path)
        self.assertFalse(result.extracted)
        self.assertIn("内容提取失败", result.message)


    def test_worker_failure_logs_full_stderr_but_returns_only_business_text(self):
        completed = subprocess.CompletedProcess([], 1, '', 'Traceback (most recent call last):\nRuntimeError: private-parser-detail')
        with mock.patch('bookmanager.metadata.subprocess.run', return_value=completed):
            result = extract_metadata(self.path)
        self.assertFalse(result.extracted)
        self.assertNotIn('private-parser-detail', result.message)
        self.assertNotIn('Traceback', result.message)
        paths = list((self.path.parent / 'diagnostics' / 'logs').glob('diagnostics-*.jsonl'))
        self.assertTrue(paths)
        self.assertIn('private-parser-detail', paths[0].read_text())


if __name__ == "__main__":
    unittest.main()
