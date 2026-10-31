from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.store import SCHEMA_VERSION, LibraryStore


class SchemaVersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "data"
        self.env = mock.patch.dict(os.environ, {"BOOKMANAGER_DIAGNOSTIC_HOME": str(self.root / "diag")})
        self.env.start()
        self.store = LibraryStore(self.home)
        self.store.close()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def version(self, home: Path | None = None) -> int:
        store = LibraryStore(home or self.home)
        try:
            return int(store.conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            store.close()

    def test_fresh_library_is_stamped_with_the_current_schema_version(self) -> None:
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_reopening_a_current_library_skips_base_migrations_and_maintenance(self) -> None:
        with (
            mock.patch.object(LibraryStore, "_init_schema") as init_schema,
            mock.patch.object(LibraryStore, "_migrate_item_name_data") as migrate_names,
            mock.patch.object(LibraryStore, "_cleanup_stale_temporary_files") as cleanup,
        ):
            store = LibraryStore(self.home, run_maintenance=False)
            store.close()
        init_schema.assert_not_called()
        migrate_names.assert_not_called()
        cleanup.assert_not_called()

    def test_run_maintenance_controls_the_full_tree_scan(self) -> None:
        blob_subdir = self.home / "blobs" / "ab"
        blob_subdir.mkdir(parents=True, exist_ok=True)
        stale = blob_subdir / ".bookmanager-tmp-stale"
        stale.write_bytes(b"stale")
        old = time.time() - 48 * 60 * 60
        os.utime(stale, (old, old))

        store = LibraryStore(self.home, run_maintenance=False)
        store.close()
        self.assertTrue(stale.exists())

        store = LibraryStore(self.home, run_maintenance=True)
        store.close()
        self.assertFalse(stale.exists())

    def test_legacy_unversioned_library_is_upgraded_once(self) -> None:
        store = LibraryStore(self.home)
        try:
            store.conn.execute("PRAGMA user_version = 0")
            store.conn.commit()
        finally:
            store.close()
        with mock.patch.object(LibraryStore, "_migrate_item_name_data") as migrate_names:
            store = LibraryStore(self.home)
            store.close()
        self.assertEqual(migrate_names.call_count, 1)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_missing_required_table_triggers_a_schema_rebuild(self) -> None:
        store = LibraryStore(self.home)
        try:
            with store.conn:
                store.conn.execute("DROP TABLE import_requests")
        finally:
            store.close()
        store = LibraryStore(self.home)
        try:
            row = store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'import_requests'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            store.close()
        self.assertEqual(self.version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
