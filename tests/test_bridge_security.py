from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from bookmanager.bridge import BridgeServer, CONTENT_SECURITY_POLICY, MAX_REQUEST_BODY_BYTES


class BridgeSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>BookManager</title>", encoding="utf-8")
        self.token = "s" * 64
        self.server = BridgeServer(("127.0.0.1", 0), self.root / "data", self.token, self.static)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, body

    def test_query_token_is_rejected_and_bearer_header_is_required(self) -> None:
        status, _headers, _body = self.request("GET", f"/api/health?token={self.token}")
        self.assertEqual(status, 401)
        status, _headers, _body = self.request(
            "GET", "/api/health", {"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(status, 200)

    def test_static_content_has_csp_and_null_origin_gets_no_cors_access(self) -> None:
        status, headers, _body = self.request("GET", "/", {"Origin": "null"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], CONTENT_SECURITY_POLICY)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_oversized_json_body_is_rejected_before_reading(self) -> None:
        status, _headers, body = self.request(
            "POST",
            "/api/search",
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("请求内容过大".encode(), body)


if __name__ == "__main__":
    unittest.main()
