from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.naming import infer_structured_file_name
from bookmanager.store import LibraryStore


class DocumentBlobQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(
            os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'diag')}
        )
        self.env.start()
        self.store = LibraryStore(self.root / 'data')
        self.content = b'local document content'
        source = self.root / 'Local Document.txt'
        source.write_bytes(self.content)
        item_id = self.store.add_local_file(
            source, 1, infer_structured_file_name(source.name)
        )
        self.item = self.store.get_item(item_id)

    def tearDown(self) -> None:
        self.store.close()
        self.env.stop()
        self.temporary.cleanup()

    def test_valid_document_reference_resolves_its_local_blob(self) -> None:
        blob = self.store.document_blob(self.item.document_id)
        self.assertIsNotNone(blob)
        assert blob is not None
        self.assertEqual(blob.stored_path, self.item.stored_path)
        self.assertEqual(blob.mime, self.item.mime)
        self.assertEqual(blob.size, self.item.size)
        self.assertTrue(blob.stored_path.is_file())
        self.assertEqual(blob.stored_path.read_bytes(), self.content)

    def test_unknown_document_has_no_blob_reference(self) -> None:
        self.assertIsNone(self.store.document_blob(0))
        self.assertIsNone(self.store.document_blob(self.item.document_id + 1000))


if __name__ == '__main__':
    unittest.main()
