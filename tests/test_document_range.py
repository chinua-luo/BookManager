from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.bridge import BridgeServer
from bookmanager.naming import infer_structured_file_name


class DocumentRangeTests(unittest.TestCase):
    """Exercise the real HTTP binary document stream, not a JSON DTO."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = 'document-range-token'
        self.env = mock.patch.dict(
            os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'fallback')}
        )
        self.env.start()
        self.server = BridgeServer(('127.0.0.1', 0), self.root / 'data', self.token, self.root)
        self.server.wait_for_startup_cleanup(5)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # 4096 deterministic bytes make every requested Range slice unambiguous.
        self.content = bytes(range(256)) * 16
        source = self.root / 'range-content.bin'
        source.write_bytes(self.content)
        store = self.server.open_store()
        try:
            item_id = store.add_local_file(
                source, 1, infer_structured_file_name(source.name)
            )
            self.item = store.get_item(item_id)
        finally:
            store.close()
        self.document_id = self.item.document_id

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temporary.cleanup()

    def fetch(self, range_header: str | None = None, document_id: int | None = None):
        headers = {'Authorization': f'Bearer {self.token}'}
        if range_header is not None:
            headers['Range'] = range_header
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_port, timeout=5
        )
        connection.request(
            'GET',
            f'/api/documents/{self.document_id if document_id is None else document_id}/content',
            headers=headers,
        )
        response = connection.getresponse()
        body = response.read()
        status = response.status
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        connection.close()
        return status, response_headers, body

    def test_full_document_stream_without_range_returns_complete_content(self) -> None:
        status, headers, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.content)
        self.assertEqual(headers['content-length'], str(len(self.content)))
        self.assertEqual(headers['content-type'], 'application/octet-stream')
        self.assertEqual(headers['accept-ranges'], 'bytes')
        self.assertNotIn('content-range', headers)

    def test_range_zero_to_1023_returns_exactly_1024_bytes(self) -> None:
        status, headers, body = self.fetch('bytes=0-1023')
        self.assertEqual(status, 206)
        self.assertEqual(headers['content-range'], f'bytes 0-1023/{len(self.content)}')
        self.assertEqual(headers['content-length'], '1024')
        self.assertEqual(len(body), 1024)
        self.assertEqual(body, self.content[:1024])

    def test_range_zero_to_zero_returns_only_the_first_byte(self) -> None:
        status, headers, body = self.fetch('bytes=0-0')
        self.assertEqual(status, 206)
        self.assertEqual(headers['content-range'], f'bytes 0-0/{len(self.content)}')
        self.assertEqual(headers['content-length'], '1')
        self.assertEqual(body, self.content[:1])

    def test_range_beyond_end_is_requested_range_not_satisfiable(self) -> None:
        status, headers, body = self.fetch('bytes=999999999-')
        self.assertEqual(status, 416)
        self.assertEqual(headers['content-range'], f'bytes */{len(self.content)}')
        self.assertEqual(body, b'')

    def test_missing_document_keeps_404(self) -> None:
        status, _headers, body = self.fetch(document_id=self.document_id + 1000)
        self.assertEqual(status, 404)
        result = json.loads(body)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'not_found')
        self.assertIsNone(result['data'])

    def test_existing_document_with_missing_blob_file_keeps_404(self) -> None:
        self.item.stored_path.unlink()
        status, _headers, body = self.fetch()
        self.assertEqual(status, 404)
        result = json.loads(body)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'not_found')
        self.assertIsNone(result['data'])


if __name__ == '__main__':
    unittest.main()
