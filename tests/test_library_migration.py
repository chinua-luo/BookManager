from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.naming import build_structured_file_name
from bookmanager.store import (
    MIGRATION_COMPLETE_MARKER,
    MIGRATION_STAGING_PREFIX,
    LibraryStore,
    _legacy_name_parts,
    migrate_library_home,
    resolve_default_library_home,
)


class LibraryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict(os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'diagnostics')})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def make_library(self, home: Path, *, content: str = 'book contents'):
        store = LibraryStore(home)
        try:
            source = self.root / 'Book.txt'
            source.write_text(content, encoding='utf-8')
            item_id = store.add_local_file(source, 1)
            item = store.get_item(item_id)
        finally:
            store.close()
        return item

    def staging_paths(self):
        return [path for path in self.root.iterdir() if path.name.startswith(MIGRATION_STAGING_PREFIX)]

    def test_successful_migration_moves_the_library_and_removes_the_source(self):
        source = self.root / 'old'
        target = self.root / 'new'
        target.mkdir()  # preflight creates the empty target before migrating
        item = self.make_library(source)

        migrate_library_home(source, target)

        self.assertTrue((target / 'library.db').is_file())
        self.assertFalse((source / 'library.db').exists())
        self.assertFalse((source / 'blobs').exists())
        self.assertEqual(self.staging_paths(), [])
        self.assertFalse((target / MIGRATION_COMPLETE_MARKER).exists())
        store = LibraryStore(target)
        try:
            restored = store.get_item(item.id)
            self.assertEqual(restored.display_name, item.display_name)
            self.assertTrue(restored.stored_path.is_file())
            self.assertEqual(restored.stored_path.read_bytes(), b'book contents')
            self.assertTrue(restored.stored_path.is_relative_to(target.resolve() / 'blobs'))
        finally:
            store.close()

    def test_migration_retries_after_a_partial_copy_failure(self):
        source = self.root / 'old'
        target = self.root / 'new'
        item = self.make_library(source)

        with mock.patch('bookmanager.store.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, '迁移资料库失败'):
                migrate_library_home(source, target)

        self.assertFalse(target.exists())
        self.assertTrue((source / 'library.db').is_file())
        self.assertEqual(self.staging_paths(), [])

        migrate_library_home(source, target)
        store = LibraryStore(target)
        try:
            self.assertEqual(store.get_item(item.id).stored_path.read_bytes(), b'book contents')
        finally:
            store.close()

    def test_database_rewrite_failure_never_publishes_a_partial_target(self):
        source = self.root / 'old'
        target = self.root / 'new'
        self.make_library(source)

        with mock.patch(
            'bookmanager.store._rewrite_blob_paths_in_database',
            side_effect=sqlite3.OperationalError('disk I/O error'),
        ):
            with self.assertRaisesRegex(OSError, '迁移资料库失败'):
                migrate_library_home(source, target)

        self.assertFalse(target.exists())
        self.assertTrue((source / 'library.db').is_file())
        self.assertEqual(self.staging_paths(), [])

    def test_failed_atomic_rename_leaves_no_target_and_cleans_staging(self):
        source = self.root / 'old'
        target = self.root / 'new'
        target.mkdir()
        self.make_library(source)

        with mock.patch('bookmanager.store.os.replace', side_effect=OSError('rename failed')):
            with self.assertRaisesRegex(OSError, '迁移资料库失败'):
                migrate_library_home(source, target)

        self.assertFalse(target.exists())
        self.assertTrue((source / 'library.db').is_file())
        self.assertEqual(self.staging_paths(), [])

    def test_stale_staging_directory_from_a_crash_is_discarded(self):
        source = self.root / 'old'
        target = self.root / 'new'
        item = self.make_library(source)
        stale = self.root / f'{MIGRATION_STAGING_PREFIX}{target.name}-stale'
        stale.mkdir()
        (stale / 'library.db').write_text('abandoned partial copy', encoding='utf-8')

        migrate_library_home(source, target)

        self.assertFalse(stale.exists())
        store = LibraryStore(target)
        try:
            self.assertEqual(store.get_item(item.id).stored_path.read_bytes(), b'book contents')
        finally:
            store.close()

    def test_non_empty_target_and_same_source_are_rejected_without_moving_data(self):
        source = self.root / 'old'
        self.make_library(source)
        same = self.make_library(self.root / 'other')
        migrate_library_home(source, source)
        non_empty = self.root / 'occupied'
        non_empty.mkdir()
        (non_empty / 'keep.txt').write_text('keep', encoding='utf-8')

        with self.assertRaisesRegex(OSError, '必须为空'):
            migrate_library_home(source, non_empty)

        self.assertTrue((source / 'library.db').is_file())
        self.assertTrue((non_empty / 'keep.txt').is_file())
        self.assertTrue((self.root / 'other' / 'library.db').is_file())
        self.assertEqual(same.display_name, 'Book.txt')

    def test_historical_dirty_display_names_survive_startup_migration(self):
        source = self.root / 'old'
        target = self.root / 'new'
        store = LibraryStore(source)
        try:
            original = self.root / 'Clean Book.txt'
            original.write_text('dirty contents', encoding='utf-8')
            item_id = store.add_local_file(original, 1)
            dirty_name = 'Bad\x01/Title: <Book>.txt'
            with store.conn:
                store.conn.execute(
                    'UPDATE items SET display_name = ?, name_data = ? WHERE id = ?',
                    (dirty_name, 'not-json{', item_id),
                )
        finally:
            store.close()

        migrate_library_home(source, target)

        # Opening the migrated library must sanitize the historical row, not crash.
        store = LibraryStore(target)
        try:
            item = store.get_item(item_id)
            parts = item.name_parts
            self.assertEqual(item.display_name, dirty_name)
            self.assertNotIn('\x01', parts.main_title)
            self.assertNotIn('/', parts.main_title)
            self.assertNotIn('<', parts.main_title)
            self.assertEqual(parts.extension, '.txt')
        finally:
            store.close()

    def test_default_library_resolution_migrates_legacy_dirty_data(self):
        source = self.root / 'legacy'
        target = self.root / 'installed'
        store = LibraryStore(source)
        try:
            original = self.root / 'Clean Book.txt'
            original.write_text('dirty contents', encoding='utf-8')
            item_id = store.add_local_file(original, 1)
            with store.conn:
                store.conn.execute(
                    'UPDATE items SET display_name = ?, name_data = ? WHERE id = ?',
                    ('Bad\x02/Title<>.txt', '[]', item_id),
                )
        finally:
            store.close()

        with (
            mock.patch('bookmanager.store.DEFAULT_LIBRARY_HOME', target),
            mock.patch('bookmanager.store.PROJECT_LIBRARY_HOME', source),
            mock.patch('bookmanager.store.configured_library_home', return_value=None),
            mock.patch.dict(os.environ, {'BOOK_MANAGER_HOME': ''}),
        ):
            resolved, migrated = resolve_default_library_home()

        self.assertEqual(resolved, target)
        self.assertTrue(migrated)
        self.assertTrue((target / 'library.db').is_file())
        self.assertFalse((source / 'library.db').exists())
        store = LibraryStore(resolved)
        try:
            self.assertEqual(store.get_item(item_id).name_parts.extension, '.txt')
        finally:
            store.close()

    def test_legacy_name_parts_cleans_unsupported_characters(self):
        for dirty in (
            'Bad\x00\x01Name.txt',
            'Chapter/One: <Intro>.pdf',
            'A _ B - C _ D.txt',
            'Book\twith\nnewlines.pdf',
        ):
            with self.subTest(dirty=dirty):
                parts = _legacy_name_parts(dirty)
                self.assertTrue(build_structured_file_name(parts))


if __name__ == '__main__':
    unittest.main()
