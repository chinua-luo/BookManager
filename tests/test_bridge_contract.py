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
from bookmanager.crawler import CrawlResult, UnsafeUrlError
from bookmanager.store import LibraryStore


class BridgeContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = 'contract-token'
        self.env = mock.patch.dict(os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'fallback')})
        self.env.start()
        self.server = BridgeServer(('127.0.0.1', 0), self.root / 'data', self.token, self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temporary.cleanup()

    def request(self, path, payload=None, *, authorized=True):
        headers = {'Authorization': f'Bearer {self.token}'} if authorized else {}
        data = json.dumps(payload).encode() if payload is not None else None
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('POST' if payload is not None else 'GET', path, data, headers)
        response = connection.getresponse()
        result = response.read()
        status = response.status
        connection.close()
        return status, json.loads(result)

    def assertEnvelope(self, value):
        self.assertEqual(set(value), {'ok', 'level', 'error_type', 'msg', 'data', 'stats'})
        self.assertIsInstance(value['ok'], bool)
        self.assertIsInstance(value['stats'], dict)

    def diagnostics(self):
        return [json.loads(line) for path in (self.server.library_home / 'logs').glob('diagnostics-*.jsonl') for line in path.read_text().splitlines()]

    def test_success_get_post_and_rejections_share_one_contract(self):
        for path, payload, authorized in [('/api/health', None, True), ('/api/tree', None, True), ('/api/folders', {'parentId': 1, 'name': 'new'}, True), ('/api/missing', None, True), ('/api/health', None, False)]:
            status, result = self.request(path, payload, authorized=authorized)
            self.assertEnvelope(result)
            self.assertEqual(result['ok'], status < 400)
            if not result['ok']:
                self.assertIsNone(result['data'])
                self.assertTrue(result['msg'])
        _status, result = self.request('/api/tree')
        self.assertIn('folders', result['data'])

    def test_get_and_post_unexpected_errors_log_tracebacks_without_leaking(self):
        for path, payload, target in [('/api/tree', None, 'folders'), ('/api/search', {'kind': 'files'}, 'search_files')]:
            # Patch request entry instead of depending on internal query implementation.
            with mock.patch.object(self.server, 'open_store', side_effect=RuntimeError('Traceback secret/internal.db')):
                status, result = self.request(path, payload)
            self.assertEqual(status, 500)
            self.assertEnvelope(result)
            self.assertNotIn('secret', json.dumps(result))
            self.assertNotIn('Traceback', json.dumps(result))
        records = self.diagnostics()
        self.assertEqual(sum(record['event'] == 'bridge.request_failed' for record in records), 2)
        self.assertTrue(all('Traceback' in record['traceback'] for record in records if record['event'] == 'bridge.request_failed'))

    def test_name_validation_reports_field_and_preserves_existing_rules(self):
        cases = [
            ({'mainTitle': 'Book', 'subtitle': 'One - Two'}, 'subtitle', "' - '"),
            ({'mainTitle': 'Book', 'authors': 'A _ B'}, 'authors', "' _ '"),
            ({'mainTitle': 'Book', 'seriesAbbr': 'A - B'}, 'seriesAbbr', "' - '"),
            ({'mainTitle': 'Book', 'number': '1 _ 2'}, 'number', "' _ '"),
            ({'mainTitle': 'Book', 'extension': 'pd/f'}, 'extension', '不允许的字符'),
            ({'mainTitle': 'Bad/Title'}, 'mainTitle', '不允许的字符'),
            ({'mainTitle': 'Book', 'edition': 'second'}, 'edition', '阿拉伯数字'),
            ({'mainTitle': 'Book', 'edition': '0'}, 'edition', '大于等于 1'),
            ({}, 'mainTitle', '整体不能为空'),
        ]
        for parts, field, message in cases:
            with self.subTest(parts=parts):
                status, result = self.request('/api/name-validate', {'nameParts': parts})
                self.assertEqual(status, 200)
                self.assertEnvelope(result)
                self.assertFalse(result['data']['valid'])
                self.assertEqual(result['data']['field'], field)
                self.assertIn(message, result['data']['message'])
        status, result = self.request('/api/name-validate', {'nameParts': {'mainTitle': 'Book', 'subtitle': 'One-Two', 'extension': 'pdf'}})
        self.assertTrue(result['data']['valid'])
        self.assertTrue(result['data']['name'].endswith('.pdf'))
        self.assertEqual(self.diagnostics(), [])

    def test_invalid_import_name_crosses_error_boundary_with_specific_message(self):
        source = self.root / 'Book.txt'
        source.write_text('book')
        status, result = self.request('/api/import/file', {'path': str(source), 'folderId': 1, 'nameParts': {'mainTitle': 'Book', 'subtitle': 'One - Two'}})
        self.assertEqual(status, 400)
        self.assertEqual(result['error_type'], 'invalid_name')
        self.assertIn('副标题名', result['msg'])
        self.assertIn("' - '", result['msg'])
        self.assertIn('请删除', result['msg'])
        self.assertNotIn('One', result['msg'])
        self.assertNotIn('Traceback', result['msg'])
        status, result = self.request('/api/import/file', {'path': str(source), 'folderId': 1, 'nameParts': {'mainTitle': 'Book', 'subtitle': 'One-Two'}})
        self.assertEqual(status, 201)
        self.assertTrue(result['ok'])

    def test_multi_target_import_receipt_compatibility_and_safe_validation(self):
        store = self.server.open_store()
        folder_id = store.create_folder(1, 'Destination')
        store.close()
        source = self.root / 'Book.txt'
        source.write_text('book')
        payload = {'path': str(source), 'folderIds': [1, folder_id, 1], 'nameParts': {'mainTitle': 'Book', 'extension': '.txt'}, 'requestId': 'multi-request'}
        status, result = self.request('/api/import/file', payload)
        self.assertEqual(status, 201)
        self.assertEqual(result['stats']['created'], 2)
        self.assertEqual(len(result['data']['items']), 2)
        self.assertEqual(result['data']['item']['folderId'], 1)
        status, receipt = self.request('/api/import/requests/multi-request')
        self.assertEqual(status, 200)
        self.assertEqual(receipt['data']['state'], 'success')
        self.assertEqual(receipt['data']['result']['documentId'], result['data']['documentId'])
        self.assertEqual(self.request('/api/import/file', payload)[1]['data']['entries'], result['data']['entries'])
        status, conflict = self.request('/api/import/file', {**payload, 'folderIds': [1]})
        self.assertEqual(status, 409)
        self.assertEqual(conflict['error_type'], 'request_conflict')
        self.assertEqual(self.request('/api/import/requests/unknown-request')[1]['data']['state'], 'not_found')
        self.assertEqual(self.request('/api/import/requests/multi-request', authorized=False)[0], 401)
        for index, changes in enumerate(({'folderIds': []}, {'folderIds': [True]}, {'folderIds': [9999]}, {'folderIds': [1], 'folderId': 1}, {'requestId': '../invalid'})):
            with self.subTest(changes=changes):
                status, failure = self.request('/api/import/file', {**payload, 'requestId': f'invalid-{index}', **changes})
                self.assertEqual(status, 400)
                self.assertNotIn('Traceback', failure['msg'])
        identity = self.request('/api/tree')[1]['data']['libraryId']
        self.assertTrue(identity)
        self.assertEqual(self.request('/api/tree')[1]['data']['libraryId'], identity)

    def test_security_block_has_stable_error_type_and_business_message(self):
        with mock.patch('bookmanager.bridge.crawl_into_library', side_effect=UnsafeUrlError('private 127.0.0.1 secret')):
            status, result = self.request('/api/crawl', {'source': 'http://localhost', 'folderId': 1})
        self.assertEqual(status, 400)
        self.assertEqual(result['error_type'], 'unsafe_address')
        self.assertNotIn('127.0.0.1', result['msg'])

    def test_partial_import_logs_every_failure_and_returns_one_aggregate_warning(self):
        source = self.root / 'import'
        source.mkdir()
        for index in range(10):
            (source / f'{index}.txt').write_text('text')
        original = LibraryStore.add_local_file
        def add(store, path, *args, **kwargs):
            if int(path.stem) % 2:
                raise ValueError('invalid file secret-stack')
            return original(store, path, *args, **kwargs)
        with mock.patch.object(LibraryStore, 'add_local_file', add):
            status, result = self.request('/api/import/folder', {'path': str(source), 'folderId': 1})
        self.assertEqual(status, 201)
        self.assertEnvelope(result)
        self.assertEqual(result['level'], 'warning')
        self.assertEqual(result['stats'], {'total': 10, 'success': 5, 'failed': 5})
        self.assertEqual(result['data']['fileCount'], 5)
        self.assertNotIn('secret-stack', json.dumps(result))
        self.assertEqual(sum(record['event'] == 'import.file_skipped' for record in self.diagnostics()), 5)

    def test_crawl_result_preserves_statistics_but_not_raw_exception_messages(self):
        crawl = CrawlResult(1, 'Series', 1, ['跳过 https://site/?token=secret: Traceback', '所有下载链接均失败，已保存源页面 HTML。'], {'total_links': 2, 'success': 0, 'skipped_safe_block': 1})
        with mock.patch('bookmanager.bridge.crawl_into_library', return_value=crawl):
            _status, result = self.request('/api/crawl', {'source': 'https://example.com', 'folderId': 1})
        self.assertEqual(result['level'], 'warning')
        self.assertEqual(result['error_type'], 'crawl_fallback')
        self.assertEqual(result['stats']['skipped_safe_block'], 1)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('Traceback', json.dumps(result))

    def test_shutdown_cleanup_failure_is_logged_and_does_not_fail_the_request(self):
        with mock.patch.object(LibraryStore, 'cleanup_caches', side_effect=OSError('cleanup failed')):
            status, result = self.request('/api/shutdown')
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertTrue(any(record['event'] == 'cache.exit_cleanup_failed' for record in self.diagnostics()))

    def test_manual_cache_failure_is_a_structured_error(self):
        with mock.patch.object(LibraryStore, 'clear_cache', side_effect=PermissionError('locked /private/cache')):
            status, result = self.request('/api/settings/cache/clear', {'cache': 'render_cache'})
        self.assertEqual(status, 400)
        self.assertEqual(result['level'], 'error')
        self.assertEqual(result['error_type'], 'permission_denied')
        self.assertNotIn('/private/cache', json.dumps(result))


    def test_startup_applies_the_cache_policy_immediately(self):
        with mock.patch.object(LibraryStore, 'cleanup_caches', return_value=0) as cleanup:
            server = BridgeServer(('127.0.0.1', 0), self.root / 'cleanup-data', self.token, self.root)
            try:
                cleanup.assert_called_once_with('startup')
            finally:
                server.server_close()
        self.assertTrue((self.root / 'cleanup-data' / 'library.db').is_file())

    def test_data_migration_copies_then_writes_config_then_deletes_source(self):
        import bookmanager.store as store_module

        source = self.server.library_home
        target = self.root / 'moved-data'
        settings_path = self.root / 'settings.json'
        events = []
        real_copy = store_module.copy_library_home_atomically
        real_delete = store_module.delete_library_source_data

        def copy_spy(source_path, target_path):
            events.append('copy')
            return real_copy(source_path, target_path)

        def delete_spy(source_path):
            events.append('delete')
            return real_delete(source_path)

        def config_spy(_home):
            events.append('config')

        with (
            mock.patch('bookmanager.store.SETTINGS_PATH', settings_path),
            mock.patch('bookmanager.bridge.copy_library_home_atomically', side_effect=copy_spy),
            mock.patch('bookmanager.bridge.delete_library_source_data', side_effect=delete_spy),
            mock.patch('bookmanager.bridge.set_configured_library_home', side_effect=config_spy),
        ):
            status, result = self.request('/api/settings/data/migrate', {'path': str(target)})
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(events, ['copy', 'config', 'delete'])
        self.assertEqual(self.server.library_home, target.resolve())
        self.assertTrue((target / 'library.db').is_file())
        self.assertFalse((source / 'library.db').exists())

    def test_data_migration_rolls_back_config_and_target_when_source_removal_fails(self):
        source = self.server.library_home
        target = self.root / 'moved-data'
        settings_path = self.root / 'settings.json'

        with (
            mock.patch('bookmanager.store.SETTINGS_PATH', settings_path),
            mock.patch('bookmanager.bridge.delete_library_source_data', side_effect=OSError('cannot delete source')),
        ):
            status, result = self.request('/api/settings/data/migrate', {'path': str(target)})

        self.assertEqual(status, 400)
        self.assertEqual(result['error_type'], 'migration_failed')
        self.assertEqual(self.server.library_home, source)
        self.assertEqual(json.loads(settings_path.read_text(encoding='utf-8'))['data_home'], str(source.resolve()))
        self.assertFalse(target.exists())
        self.assertTrue((source / 'library.db').is_file())

    def test_data_migration_copy_failure_leaves_config_and_source_untouched(self):
        source = self.server.library_home
        target = self.root / 'moved-data'
        settings_path = self.root / 'settings.json'

        with (
            mock.patch('bookmanager.store.SETTINGS_PATH', settings_path),
            mock.patch('bookmanager.bridge.copy_library_home_atomically', side_effect=OSError('copy failed')),
        ):
            status, result = self.request('/api/settings/data/migrate', {'path': str(target)})

        self.assertEqual(status, 400)
        self.assertEqual(result['error_type'], 'migration_failed')
        self.assertEqual(self.server.library_home, source)
        self.assertFalse(settings_path.exists())
        self.assertFalse(target.exists())
        self.assertTrue((source / 'library.db').is_file())
