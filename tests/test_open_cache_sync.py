from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from bookmanager.store import LibraryStore


PROJECT_DIR = Path(__file__).resolve().parents[1]


class OpenCacheSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LibraryStore(self.root / "BookManagerData")
        self.source = self.root / "Sample.txt"
        self.source.write_text("original", encoding="utf-8")
        self.item_id = self.store.add_local_file(self.source, 1)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def open_copy(self, entry_name: str) -> Path:
        item = self.store.get_item(self.item_id)
        cache_dir = self.store.prepare_cache_directory("open_cache", entry_name)
        cached_path = cache_dir / item.display_name
        shutil.copy2(item.stored_path, cached_path)
        self.store.register_open_cache_copy(
            cache_dir, item.document_id, item.sha256, cached_path.name
        )
        return cached_path

    def test_modified_copy_updates_every_mirror(self) -> None:
        mirror_folder_id = self.store.create_folder(1, "Mirror")
        mirror_id = self.store.mirror_item(self.item_id, mirror_folder_id)
        cached_path = self.open_copy("first")
        cached_path.write_text("changed", encoding="utf-8")

        self.assertEqual(self.store.sync_open_cache_changes(), 1)

        item = self.store.get_item(self.item_id)
        mirror = self.store.get_item(mirror_id)
        self.assertEqual(item.sha256, mirror.sha256)
        self.assertEqual(item.stored_path.read_text(encoding="utf-8"), "changed")
        self.assertEqual(self.store.sync_open_cache_changes(), 0)

    def test_last_modified_copy_wins_and_can_restore_original_content(self) -> None:
        older = self.open_copy("older")
        newer = self.open_copy("newer")
        older.write_text("older edit", encoding="utf-8")
        newer.write_text("newer edit", encoding="utf-8")
        now = time.time()
        os.utime(older, (now - 10, now - 10))
        os.utime(newer, (now, now))

        self.assertEqual(self.store.sync_open_cache_changes(), 2)
        self.assertEqual(
            self.store.get_item(self.item_id).stored_path.read_text(encoding="utf-8"),
            "newer edit",
        )

        older.write_text("original", encoding="utf-8")
        os.utime(older, (now + 10, now + 10))
        self.assertEqual(self.store.sync_open_cache_changes(), 1)
        self.assertEqual(
            self.store.get_item(self.item_id).stored_path.read_text(encoding="utf-8"),
            "original",
        )

    def test_menu_saves_open_cache_instead_of_inline_text(self) -> None:
        renderer = (PROJECT_DIR / "electron" / "src" / "main.js").read_text(encoding="utf-8")
        bridge = (PROJECT_DIR / "bookmanager" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn("/api/open-cache/sync", renderer)
        self.assertIn("/api/open-cache/sync", bridge)
        self.assertNotIn("replace-text", renderer)
        self.assertNotIn("replace-text", bridge)
