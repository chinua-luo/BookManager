from __future__ import annotations

import errno
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.importing import ImportProblem, import_file, import_status, target_ids
from bookmanager.naming import infer_structured_file_name
from bookmanager.store import LibraryStore, sha256_file


class MultiImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LibraryStore(self.root / 'data')
        self.source = self.root / 'Book.txt'
        self.source.write_text('book contents')
        self.parts = infer_structured_file_name(self.source.name)
        self.a = self.store.create_folder(1, 'A')
        self.b = self.store.create_folder(self.a, 'B')
        self.env = mock.patch.dict(os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'diagnostics')})
        self.env.start()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temporary.cleanup()

    def run_import(self, ids=None, request='request-1', **kwargs):
        return import_file(self.store, self.source, ids or [1, self.a, self.b], self.parts, kwargs.get('note', 'note'), kwargs.get('tags', ['tag']), request)

    def counts(self):
        return [self.store.conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('items', 'documents', 'blobs')]

    def test_targets_are_strict_bounded_and_deduplicated(self):
        self.assertEqual(target_ids({'folderIds': [1, self.a, 1]}), [1, self.a])
        self.assertEqual(target_ids({'folderId': 1}), [1])
        for payload in ({}, {'folderIds': []}, {'folderIds': '1'}, {'folderIds': [True]}, {'folderIds': ['1']}, {'folderIds': [0]}, {'folderIds': list(range(1, 102))}, {'folderId': 1, 'folderIds': [1]}):
            with self.subTest(payload=payload), self.assertRaises(ImportProblem):
                target_ids(payload)

    def test_parent_child_and_root_share_one_document_hash_and_copy(self):
        with (
            mock.patch.object(LibraryStore, 'prepare_blob', autospec=True, wraps=LibraryStore.prepare_blob) as prepare,
            mock.patch.object(LibraryStore, 'install_prepared_blob', autospec=True, wraps=LibraryStore.install_prepared_blob) as install,
        ):
            result = self.run_import([1, self.a, self.b, self.a])
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(install.call_count, 1)
        self.assertEqual(result['stats'], {'targetCount': 3, 'created': 3, 'existing': 0, 'automatic': 0})
        self.assertEqual(self.counts(), [3, 1, 1])
        items = [self.store.get_item(entry['itemId']) for entry in result['entries']]
        self.assertEqual(len({item.document_id for item in items}), 1)
        self.assertTrue(all(item.note == 'note' and item.tags == ('tag',) for item in items))
        self.assertEqual(result['entries'][-1]['folderPath'], 'Library / A / B')

    def test_existing_mirrors_keep_metadata_and_same_name_other_content_is_allowed(self):
        first = self.run_import([self.a])
        item = self.store.get_item(first['entries'][0]['itemId'])
        changed = infer_structured_file_name('Changed.txt')
        self.store.rename_item(item.id, changed, 'old note', ['old'])
        result = self.run_import([self.a, self.b], 'request-2', note='new', tags=['new'])
        self.assertEqual(result['stats']['existing'], 1)
        self.assertEqual(result['stats']['created'], 1)
        self.assertEqual(self.store.get_item(item.id).note, 'old note')
        self.assertEqual(self.store.get_item(item.id).display_name, 'Changed.txt')
        self.assertEqual(self.store.get_item(item.id).tags, ('old',))
        result = self.run_import([self.a, self.b], 'request-3')
        self.assertEqual(result['stats']['created'], 0)
        self.source.write_text('different content')
        self.run_import([self.a], 'request-4')
        self.assertEqual(self.counts(), [3, 2, 2])

    def test_existing_mirror_with_missing_blob_is_repaired_without_duplication(self):
        self.run_import([self.a])
        item = self.store.list_items(self.a)[0]
        item.stored_path.unlink()
        result = self.run_import([self.a], 'repair')
        self.assertEqual(result['stats']['created'], 0)
        self.assertEqual(item.stored_path.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.counts(), [1, 1, 1])

    def test_articles_is_not_duplicated_when_also_selected(self):
        articles = self.store.create_folder(1, 'Articles')
        result = self.run_import([self.a, articles, self.b], tags=['article'])
        self.assertEqual(self.counts(), [3, 1, 1])
        self.assertEqual(result['stats']['created'], 3)
        self.assertEqual(result['automatic'], [])
        result = self.run_import([self.a], 'request-2', tags=['article'])
        self.assertEqual(result['stats']['existing'], 1)
        self.assertEqual(result['automatic'][0]['folderId'], articles)
        self.assertEqual(self.counts(), [3, 1, 1])

    def test_articles_folder_and_tags_roll_back_on_later_target_failure(self):
        with mock.patch.object(self.store, 'mirror_item', side_effect=OSError(errno.ENOSPC, 'disk full')):
            with self.assertRaises(OSError):
                self.run_import(tags=['article'])
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertNotIn('Articles', [folder.name for folder in self.store.folders(1)])
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM tags').fetchone()[0], 0)
        self.assertEqual(list(self.store.blob_dir.rglob('*.txt')), [])
        status = import_status(self.store, 'request-1')
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['errorType'], 'disk_full')

    def test_second_mirror_or_tag_failure_leaves_no_partial_import(self):
        for failure in ('mirror', 'tags'):
            with self.subTest(failure=failure):
                target = 'mirror_item' if failure == 'mirror' else '_replace_item_tags'
                with mock.patch.object(self.store, target, side_effect=RuntimeError('private failure')):
                    with self.assertRaises(RuntimeError):
                        self.run_import(request=failure)
                self.assertEqual(self.counts(), [0, 0, 0])
                self.assertNotIn('private failure', import_status(self.store, failure)['message'])

    def test_failure_preserves_preexisting_shared_blob_and_mirrors(self):
        self.run_import([self.a], 'original')
        item = self.store.list_items(self.a)[0]
        content = item.stored_path.read_bytes()
        with mock.patch.object(self.store, 'mirror_item', side_effect=PermissionError('private details')):
            with self.assertRaises(PermissionError):
                self.run_import([self.b, 1], 'failed')
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertEqual(item.stored_path.read_bytes(), content)

    def test_failed_import_does_not_rename_legacy_shared_blob(self):
        self.run_import([self.a], 'original')
        item = self.store.list_items(self.a)[0]
        legacy = item.stored_path.with_suffix('')
        item.stored_path.rename(legacy)
        with self.store.transaction():
            self.store.conn.execute("UPDATE blobs SET stored_path=?, extension=''", (str(legacy),))
        with mock.patch.object(self.store, 'mirror_item', side_effect=RuntimeError('mirror failure')):
            with self.assertRaises(RuntimeError):
                self.run_import([self.b, 1], 'legacy-failure')
        self.assertTrue(legacy.is_file())
        self.assertEqual(self.store.get_item(item.id).stored_path, legacy)
        self.assertEqual(self.counts(), [1, 1, 1])

    def test_blob_cleanup_holds_writer_lock_before_touching_files(self):
        digest, size = sha256_file(self.source)
        self.store._ensure_blob(self.source, digest, size, 'text/plain', '.txt')
        original = Path.unlink
        store = self.store
        def checked_unlink(path, *args, **kwargs):
            self.assertTrue(store.conn.in_transaction)
            return original(path, *args, **kwargs)
        with mock.patch('pathlib.Path.unlink', checked_unlink):
            self.assertEqual(self.store.cleanup_unreferenced_blobs(), size)
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_missing_target_unreadable_source_and_source_changes(self):
        with self.assertRaises(ImportProblem):
            self.run_import([9999])
        self.assertEqual(self.counts(), [0, 0, 0])
        with mock.patch.object(LibraryStore, 'prepare_blob', autospec=True, side_effect=PermissionError('cannot read')):
            with self.assertRaises(PermissionError):
                self.run_import(request='unreadable')
        original_prepare = LibraryStore.prepare_blob
        def changed(store, path):
            result = original_prepare(store, path)
            path.write_text('changed during hash')
            return result
        with mock.patch.object(LibraryStore, 'prepare_blob', autospec=True, side_effect=changed):
            with self.assertRaisesRegex(ImportProblem, '变化'):
                self.run_import(request='changed')
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_target_deleted_between_precheck_and_write_is_rejected(self):
        original_prepare = LibraryStore.prepare_blob
        def deleted(store, path):
            result = original_prepare(store, path)
            with self.store.transaction():
                self.store.conn.execute('DELETE FROM folders WHERE id=?', (self.b,))
            return result
        with mock.patch.object(LibraryStore, 'prepare_blob', autospec=True, side_effect=deleted), self.assertRaisesRegex(ImportProblem, '已不存在'):
            self.run_import()
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_receipt_survives_reopen_replays_and_rejects_changed_request(self):
        first = self.run_import()
        self.store.close()
        self.store = LibraryStore(self.root / 'data')
        self.assertEqual(import_status(self.store, 'request-1')['result'], first)
        self.assertEqual(self.run_import(), first)
        self.assertEqual(self.counts(), [3, 1, 1])
        with self.assertRaisesRegex(ImportProblem, '内容不同'):
            self.run_import([self.a])
        self.source.write_text('modified content')
        with self.assertRaisesRegex(ImportProblem, '内容已变化'):
            self.run_import()
        self.source.unlink()
        self.assertEqual(self.run_import(), first)

    def test_receipt_and_items_commit_together_and_commit_acknowledgement_is_authoritative(self):
        connection = self.store.conn
        class ConnectionProxy:
            def __init__(self, fail_after_commit):
                self.commits = 0
                self.fail_after_commit = fail_after_commit
            def __getattr__(self, name):
                return getattr(connection, name)
            def commit(self):
                self.commits += 1
                # 1: pending receipt, 2: staged blob registration, 3: mirror write.
                if self.commits == 3:
                    if self.fail_after_commit:
                        connection.commit()
                    raise RuntimeError('commit acknowledgement failed')
                connection.commit()
        self.store.conn = ConnectionProxy(False)
        with self.assertRaises(RuntimeError):
            self.run_import(request='commit-failed')
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertEqual(import_status(self.store, 'commit-failed')['state'], 'failed')
        self.store.conn = ConnectionProxy(True)
        result = self.run_import(request='committed')
        self.assertEqual(import_status(self.store, 'committed')['result'], result)
        self.assertEqual(self.counts(), [3, 1, 1])
        self.store.conn = connection

    def test_schema_upgrade_preserves_existing_library(self):
        self.run_import([self.a], 'old-import')
        item = self.store.list_items(self.a)[0]
        with self.store.transaction():
            self.store.conn.execute('DROP TABLE import_requests')
            self.store.conn.execute('DROP TABLE library_identity')
        self.store.close()
        self.store = LibraryStore(self.root / 'data')
        self.assertEqual(self.store.get_item(item.id).note, 'note')
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertEqual(import_status(self.store, 'old-import')['state'], 'not_found')
        identity = self.store.conn.execute('SELECT library_id FROM library_identity').fetchone()[0]
        self.store.close(); self.store = LibraryStore(self.root / 'data')
        self.assertEqual(self.store.conn.execute('SELECT library_id FROM library_identity').fetchone()[0], identity)

    def test_interrupted_pending_is_failed_but_live_owner_and_missing_are_not_guessed(self):
        self.store.conn.execute("INSERT INTO import_requests VALUES(?,?,?,?,?,?,?)", ('dead', 'fingerprint', '', os.getpid(), 'pending', '{}', 'now'))
        self.store.conn.commit()
        self.assertEqual(import_status(self.store, 'dead')['state'], 'failed')
        self.store.conn.execute("INSERT INTO import_requests VALUES(?,?,?,?,?,?,?)", ('live', 'fingerprint', '', 12345, 'pending', '{}', 'now'))
        self.store.conn.commit()
        with mock.patch('bookmanager.importing.os.kill'):
            self.assertEqual(import_status(self.store, 'live')['state'], 'pending')
        self.assertEqual(import_status(self.store, 'missing')['state'], 'not_found')

    def test_concurrent_same_request_does_not_duplicate_and_reports_pending(self):
        entered = threading.Event(); release = threading.Event(); results = []; errors = []
        original_prepare = LibraryStore.prepare_blob
        def paused(store, path):
            entered.set(); release.wait(4)
            return original_prepare(store, path)
        def worker():
            store = LibraryStore(self.root / 'data')
            try:
                results.append(import_file(store, self.source, [self.a, self.b], self.parts, 'note', ['tag'], 'concurrent'))
            except Exception as error:
                errors.append(error)
            finally:
                store.close()
        with mock.patch.object(LibraryStore, 'prepare_blob', autospec=True, side_effect=paused):
            thread = threading.Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(import_status(self.store, 'concurrent')['state'], 'pending')
                with self.assertRaisesRegex(ImportProblem, '正在处理'):
                    self.run_import([self.a, self.b], 'concurrent')
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.counts(), [2, 1, 1])

    def test_busy_database_has_specific_error_and_finite_timeout(self):
        other = sqlite3.connect(self.store.db_path)
        other.execute('BEGIN IMMEDIATE')
        self.store.conn.execute('PRAGMA busy_timeout=20')
        try:
            with self.assertRaises(ImportProblem) as raised:
                self.run_import()
            self.assertEqual(raised.exception.import_error_type, 'library_busy')
        finally:
            other.rollback(); other.close()
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_cleanup_failure_keeps_original_error_and_records_diagnostic(self):
        with mock.patch.object(self.store, 'mirror_item', side_effect=RuntimeError('original failure')), mock.patch('pathlib.Path.unlink', side_effect=PermissionError('cleanup denied')), mock.patch('bookmanager.store.record_exception') as log:
            with self.assertRaisesRegex(RuntimeError, 'original failure'):
                self.run_import()
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertTrue(any(call.args and call.args[0] == 'store.blob_unlink_failed' for call in log.call_args_list), log.call_args_list)


    def test_delete_underlying_file_rolls_back_blob_when_database_fails(self):
        self.run_import([self.a], 'original')
        item = self.store.list_items(self.a)[0]
        blob_path = item.stored_path
        connection = self.store.conn

        class FailingConnection:
            def execute(self, sql, *parameters):
                if 'DELETE FROM items' in sql:
                    raise sqlite3.OperationalError('simulated write failure')
                return connection.execute(sql, *parameters)

            def __getattr__(self, name):
                return getattr(connection, name)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return connection.__exit__(exc_type, exc, traceback)

        self.store.conn = FailingConnection()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.delete_underlying_file(item.id)
        finally:
            self.store.conn = connection
        self.assertTrue(blob_path.is_file())
        self.assertEqual(list(blob_path.parent.glob('.bookmanager-tmp-*')), [])
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertEqual(self.store.get_item(item.id).sha256, item.sha256)

    def test_delete_underlying_file_removes_blob_and_every_mirror(self):
        self.run_import([self.a, self.b], 'original')
        item = self.store.list_items(self.a)[0]
        blob_path = item.stored_path

        self.assertEqual(self.store.delete_underlying_file(item.id), 2)

        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertFalse(blob_path.exists())
        self.assertEqual(list(blob_path.parent.glob('.bookmanager-tmp-*')), [])

    def test_delete_underlying_file_hands_locked_temporary_files_to_startup_cleanup(self):
        self.run_import([self.a], 'original')
        item = self.store.list_items(self.a)[0]
        blob_path = item.stored_path
        original_unlink = Path.unlink

        def locked_temp_unlink(path, *args, **kwargs):
            if path.name.startswith('.bookmanager-tmp-'):
                raise PermissionError('temporary file locked')
            return original_unlink(path, *args, **kwargs)

        with mock.patch('pathlib.Path.unlink', locked_temp_unlink), mock.patch('bookmanager.store.record_exception') as log:
            self.assertEqual(self.store.delete_underlying_file(item.id), 1)
        self.assertEqual(self.counts(), [0, 0, 0])
        leftovers = list(blob_path.parent.glob('.bookmanager-tmp-*'))
        self.assertEqual(len(leftovers), 1)
        log.assert_any_call('store.blob_unlink_failed', mock.ANY, data_home=self.store.home)

        old = time.time() - 25 * 60 * 60
        os.utime(leftovers[0], (old, old))
        self.store.close()
        self.store = LibraryStore(self.root / 'data')
        self.assertFalse(leftovers[0].exists())

    def test_delete_underlying_file_accepts_already_missing_blob(self):
        self.run_import([self.a], 'original')
        item = self.store.list_items(self.a)[0]
        item.stored_path.unlink()

        with mock.patch('bookmanager.store.record_event') as log:
            self.assertEqual(self.store.delete_underlying_file(item.id), 1)

        self.assertEqual(self.counts(), [0, 0, 0])
        log.assert_any_call('store.blob_already_missing', data_home=self.store.home)



if __name__ == '__main__':
    unittest.main()
