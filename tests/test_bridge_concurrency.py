from __future__ import annotations

import http.client
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.bridge import MAX_CONCURRENT_REQUESTS, BridgeServer


class BridgeConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = "concurrency-token"
        self.env = mock.patch.dict(os.environ, {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")})
        self.env.start()
        self.server = BridgeServer(("127.0.0.1", 0), self.root / "data", self.token, self.root)
        self.server.wait_for_startup_cleanup(5)
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        original = self.server.process_request_thread

        def tracked(request, client_address):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.05)
                return original(request, client_address)
            finally:
                with self.lock:
                    self.active -= 1

        self.server.process_request_thread = tracked
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temporary.cleanup()

    def request_health(self) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET", "/api/health", headers={"Authorization": f"Bearer {self.token}"})
        response = connection.getresponse()
        response.read()
        status = response.status
        connection.close()
        return status

    def test_burst_of_requests_is_capped_but_all_succeed(self) -> None:
        statuses: list[int] = []
        status_lock = threading.Lock()

        def worker() -> None:
            status = self.request_health()
            with status_lock:
                statuses.append(status)

        workers = [threading.Thread(target=worker) for _ in range(MAX_CONCURRENT_REQUESTS * 2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        self.assertEqual(len(statuses), MAX_CONCURRENT_REQUESTS * 2)
        self.assertTrue(all(status == 200 for status in statuses), statuses)
        self.assertLessEqual(self.peak, MAX_CONCURRENT_REQUESTS)
        self.assertGreaterEqual(self.peak, 2)
        # Every handler released its slot, so a later request still gets through.
        self.assertEqual(self.request_health(), 200)


if __name__ == "__main__":
    unittest.main()
