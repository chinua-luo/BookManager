from pathlib import Path
import tempfile
import unittest

from bookmanager.exporting import ExportProblem, export_folder
from bookmanager.store import LibraryStore


class BlobBoundaryTests(unittest.TestCase):
    def test_invalid_database_paths_never_read_export_or_delete_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'example.txt'
            source.write_bytes(b'example')
            store = LibraryStore(root / 'library')
            try:
                item = store.get_item(store.add_local_file(source, 1))
                path = item.stored_path
                for bad in (source, store.db_path, path.parent / ('a' * 64 + '.txt'), Path('relative.txt')):
                    with self.subTest(path=bad):
                        with store.conn:
                            store.conn.execute('UPDATE blobs SET stored_path=?', (str(bad),))
                        for get in (lambda: store.document_blob(item.document_id), lambda: store.get_item(item.id), lambda: store.delete_underlying_file(item.id)):
                            with self.assertRaises(ValueError):
                                get()
                        with self.assertRaises(ExportProblem):
                            export_folder(store, 1, root)
                        self.assertEqual(source.read_bytes(), b'example')
                        self.assertEqual(path.read_bytes(), b'example')
                        self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)
            finally:
                store.close()
