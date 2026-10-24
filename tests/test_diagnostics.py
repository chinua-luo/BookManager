from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.diagnostics import diagnostic_scope, record_exception
from bookmanager.store import CacheSettings, LibraryStore


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = mock.patch.dict('os.environ', {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'fallback'), 'BOOKMANAGER_BRIDGE_TOKEN': 'bridge-token-secret'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def records(self, home):
        return [json.loads(line) for path in (home / 'logs').glob('diagnostics-*.jsonl') for line in path.read_text().splitlines()]

    def test_full_chained_traceback_is_logged_but_secrets_are_redacted(self):
        with diagnostic_scope(self.root / 'data'):
            try:
                try:
                    raise ValueError('https://example.com/?token=url-secret&key=other-secret')
                except ValueError as exc:
                    raise RuntimeError('bridge-token-secret Authorization: Bearer bearer-secret') from exc
            except RuntimeError as exc:
                record_exception('request.failed', exc, token='structured-secret')
        record = self.records(self.root / 'data')[0]
        self.assertIn('Traceback', record['traceback'])
        self.assertIn('ValueError', record['traceback'])
        self.assertIn('RuntimeError', record['traceback'])
        encoded = json.dumps(record)
        for secret in ('url-secret', 'other-secret', 'bridge-token-secret', 'bearer-secret', 'structured-secret'):
            self.assertNotIn(secret, encoded)

    def test_unwritable_data_path_falls_back_and_total_log_failure_does_not_raise(self):
        blocked = self.root / 'file'
        blocked.write_text('not a directory')
        path = record_exception('failed', RuntimeError('failure'), data_home=blocked)
        self.assertTrue(path.is_relative_to(self.root / 'fallback'))
        with mock.patch('pathlib.Path.mkdir', side_effect=PermissionError('denied')), mock.patch('sys.stderr.write') as stderr:
            self.assertIsNone(record_exception('log.failed', RuntimeError('original'), data_home=blocked))
        self.assertEqual(stderr.call_count, 1)

    def test_concurrent_thread_scopes_keep_complete_records_in_their_library(self):
        def write(home):
            with diagnostic_scope(home):
                for index in range(8):
                    record_exception('thread.failed', ValueError(str(index)))
        homes = [self.root / 'one', self.root / 'two']
        threads = [threading.Thread(target=write, args=(home,)) for home in homes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for home in homes:
            self.assertEqual(len(self.records(home)), 8)

    def test_manual_cache_failure_raises_but_automatic_cleanup_only_logs(self):
        store = LibraryStore(self.root / 'data')
        cache = store.home / 'render_cache'
        cache.mkdir()
        (cache / 'entry').write_text('cache')
        try:
            with mock.patch('bookmanager.store.shutil.rmtree', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    store.clear_cache('render_cache')
            with mock.patch.object(store, 'cache_settings', side_effect=RuntimeError('cleanup failed')):
                self.assertEqual(store.cleanup_caches('startup'), 0)
            self.assertTrue(any(record['event'] == 'cache.automatic_cleanup_failed' for record in self.records(store.home)))
            self.assertTrue((cache / 'entry').exists())
        finally:
            store.close()

    def test_explicit_cache_save_failure_is_not_reported_as_no_changes(self):
        import shutil
        store = LibraryStore(self.root / 'data')
        source = self.root / 'sample.txt'
        source.write_text('original')
        item_id = store.add_local_file(source, 1)
        item = store.get_item(item_id)
        directory = store.prepare_cache_directory('open_cache', 'edited')
        cached = directory / item.display_name
        shutil.copy2(item.stored_path, cached)
        store.register_open_cache_copy(directory, item.document_id, item.sha256, cached.name)
        cached.write_text('changed')
        try:
            with mock.patch.object(store, 'replace_document_content', side_effect=PermissionError('writeback failed')):
                with self.assertRaises(PermissionError):
                    store.sync_open_cache_changes(strict=True)
                with self.assertRaises(PermissionError):
                    store.clear_cache('open_cache')
                import os
                import time
                expired = time.time() - 2 * 86400
                os.utime(directory, (expired, expired))
                policy = CacheSettings('自定义', 1, 0, 0, 0, False)
                with mock.patch.object(store, 'cache_settings', return_value=policy):
                    self.assertEqual(store.cleanup_caches('startup'), 0)
                    self.assertTrue(cached.exists())
            self.assertEqual(cached.read_text(), 'changed')
            self.assertEqual(item.stored_path.read_text(), 'original')
        finally:
            store.close()
