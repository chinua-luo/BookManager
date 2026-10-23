from __future__ import annotations

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from bookmanager.file_security import validate_supported_document
from bookmanager.naming import StructuredFileName
from bookmanager.store import LibraryStore, sha256_file


class FileSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rejects_mislabeled_parser_inputs(self) -> None:
        for extension in (".pdf", ".epub", ".djvu"):
            path = self.root / f"bad{extension}"
            path.write_bytes(b"not a document")
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                validate_supported_document(path)

    def test_accepts_expected_pdf_epub_and_djvu_headers(self) -> None:
        pdf = self.root / "good.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        epub = self.root / "good.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", "<container/>")
        djvu = self.root / "good.djvu"
        djvu.write_bytes(b"AT&TFORMDJVU")
        for path in (pdf, epub, djvu):
            validate_supported_document(path)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_store_rejects_symlink_imports(self) -> None:
        source = self.root / "source.txt"
        source.write_text("content", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(source)
        store = LibraryStore(self.root / "data")
        try:
            with self.assertRaises(ValueError):
                store.add_local_file(link, 1)
        finally:
            store.close()

    def test_blob_copy_is_hash_verified_and_stale_temps_are_removed(self) -> None:
        data = self.root / "data"
        store = LibraryStore(data)
        source = self.root / "source.txt"
        source.write_text("verified", encoding="utf-8")
        try:
            item_id = store.add_local_file(source, 1)
            item = store.get_item(item_id)
            self.assertEqual(sha256_file(item.stored_path)[0], item.sha256)
        finally:
            store.close()
        stale = next((data / "blobs").iterdir()) / ".bookmanager-tmp-stale"
        stale.write_text("partial", encoding="utf-8")
        stale_tmp = data / "tmp" / "abandoned.tmp"
        stale_tmp.parent.mkdir()
        stale_tmp.write_text("partial", encoding="utf-8")
        old = time.time() - 25 * 60 * 60
        os.utime(stale, (old, old))
        os.utime(stale_tmp, (old, old))
        reopened = LibraryStore(data)
        reopened.close()
        self.assertFalse(stale.exists())
        self.assertFalse(stale_tmp.exists())

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not meaningful on Windows")
    def test_library_database_and_directories_are_owner_only(self) -> None:
        data = self.root / "permissions"
        store = LibraryStore(data)
        store.close()
        self.assertEqual(data.stat().st_mode & 0o777, 0o700)
        self.assertEqual((data / "blobs").stat().st_mode & 0o777, 0o700)
        self.assertEqual((data / "library.db").stat().st_mode & 0o777, 0o600)

    def test_search_results_obey_the_requested_limit(self) -> None:
        store = LibraryStore(self.root / "search-data")
        try:
            for index in range(5):
                source = self.root / f"Security Book {index}.txt"
                source.write_text(f"book {index}", encoding="utf-8")
                store.add_local_file(
                    source,
                    1,
                    StructuredFileName("", "", f"Security Book {index}", "", "1", "", ".txt"),
                )
            self.assertEqual(len(store.search_mirror_items("Security", limit=2)), 2)
            self.assertEqual(len(store.search_unique_documents("Security", limit=3)), 3)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
