from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.naming import infer_structured_file_name
from bookmanager.store import LibraryStore


class StoreQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")})
        self.env.start()
        self.store = LibraryStore(self.root / "data")
        self.series = self.store.create_folder(1, "Series")
        self.volume = self.store.create_folder(self.series, "Volume")
        for index in range(4):
            source = self.root / f"book-{index}.txt"
            source.write_text(f"content {index}", encoding="utf-8")
            self.store.add_local_file(
                source,
                1,
                infer_structured_file_name(f"Book {index}.txt"),
                tags=("--article--",) if index < 3 else (),
            )

    def tearDown(self) -> None:
        self.store.close()
        self.env.stop()
        self.temporary.cleanup()

    def test_folder_listing_index_exists_and_is_used(self) -> None:
        row = self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_items_folder_name'"
        ).fetchone()
        self.assertIsNotNone(row)
        plan = self.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM items WHERE folder_id = ? ORDER BY display_name",
            (1,),
        ).fetchall()
        self.assertTrue(any("idx_items_folder_name" in str(line["detail"]) for line in plan))

    def test_list_items_fetches_all_tags_in_one_batch(self) -> None:
        statements: list[str] = []
        self.store.conn.set_trace_callback(statements.append)
        try:
            items = self.store.list_items(1)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual(len(items), 4)
        tagged = [item for item in items if item.tags]
        self.assertEqual(len(tagged), 3)
        self.assertTrue(all(item.tags == ("--article--",) for item in tagged))
        # One query for the page, one for all of its tags; no per-row lookups.
        self.assertLessEqual(len(statements), 3, statements)

    def test_folder_stats_uses_recursive_descendants(self) -> None:
        sibling = self.store.create_folder(1, "Sibling")
        nested = self.store.create_folder(sibling, "Nested")
        stats = self.store.folder_stats(1)
        self.assertEqual(stats.folder_count, 4)  # Series, Volume, Sibling, Nested; root excluded
        self.assertEqual(stats.item_count, 4)

    def test_folder_path_resolves_nested_names_from_one_lookup(self) -> None:
        self.assertEqual(self.store.folder_path(self.volume), "Library / Series / Volume")
        lookup = self.store._folder_path_lookup()
        self.assertEqual(lookup(self.volume), "Library / Series / Volume")
        self.assertEqual(self.store.folder_path(1), "Library")


if __name__ == "__main__":
    unittest.main()
