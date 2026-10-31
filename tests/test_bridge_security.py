from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from bookmanager.bridge import (
    BridgeRequestHandler,
    BridgeServer,
    CONTENT_SECURITY_POLICY,
    MAX_OPEN_CACHE_FILE_NAME_BYTES,
    MAX_REQUEST_BODY_BYTES,
)
from bookmanager.naming import StructuredFileName


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


    def authorized(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def test_handler_enforces_a_thirty_second_socket_timeout(self) -> None:
        self.assertEqual(BridgeRequestHandler.timeout, 30)

    def test_open_rejects_dangerous_display_extension_before_copying(self) -> None:
        store = self.server.open_store()
        try:
            source = self.root / "Safe.txt"
            source.write_text("content", encoding="utf-8")
            item_id = store.add_local_file(source, 1)
            store.rename_item(item_id, StructuredFileName("", "", "Book", "", "1", "", ".exe"))
        finally:
            store.close()

        status, _headers, body = self.request(
            "GET", f"/api/items/{item_id}/open", self.authorized()
        )

        payload = json.loads(body)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_type"], "unsafe_file_type")
        self.assertIn("不允许用系统默认程序打开", payload["msg"])
        cache_root = self.server.library_home / "open_cache"
        self.assertFalse(cache_root.exists() and any(cache_root.iterdir()))

    def test_open_rejects_dangerous_underlying_extension(self) -> None:
        store = self.server.open_store()
        try:
            source = self.root / "Book.exe"
            source.write_text("content", encoding="utf-8")
            item_id = store.add_local_file(source, 1)
            store.rename_item(item_id, StructuredFileName("", "", "Book", "", "1", "", ".txt"))
            item = store.get_item(item_id)
        finally:
            store.close()
        self.assertEqual(item.stored_path.suffix.lower(), ".exe")
        self.assertEqual(item.display_name, "Book.txt")

        status, _headers, body = self.request(
            "GET", f"/api/items/{item_id}/open", self.authorized()
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error_type"], "unsafe_file_type")

    def test_long_chinese_names_are_truncated_but_keep_their_extension(self) -> None:
        title = "超长中文书名" * 30
        store = self.server.open_store()
        try:
            source = self.root / "Long.txt"
            source.write_text("long content", encoding="utf-8")
            item_id = store.add_local_file(
                source, 1, StructuredFileName("", "", title, "", "1", "", ".txt")
            )
        finally:
            store.close()

        status, _headers, body = self.request(
            "GET", f"/api/items/{item_id}/open", self.authorized()
        )

        payload = json.loads(body)
        self.assertEqual(status, 200)
        open_path = Path(payload["data"]["path"])
        self.assertTrue(open_path.is_file())
        self.assertLessEqual(len(open_path.name.encode("utf-8")), MAX_OPEN_CACHE_FILE_NAME_BYTES)
        self.assertTrue(open_path.name.endswith(".txt"))
        self.assertEqual(open_path.read_text(encoding="utf-8"), "long content")


if __name__ == "__main__":
    unittest.main()
