"""Pin the public JSON DTOs exposed by the local bridge.

These tests are the contract boundary between the Python bridge and the
Electron renderer. Every endpoint is asserted on its own: adding a field to one
DTO (or leaking an internal database column) must fail here until the contract
and its consumers are updated deliberately. Binary document streams are not
JSON DTOs and are intentionally out of scope.
"""
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

ENVELOPE_KEYS = {'ok', 'level', 'error_type', 'msg', 'data', 'stats'}
ITEM_KEYS = {
    'id',
    'folderId',
    'documentId',
    'displayName',
    'listName',
    'extension',
    'size',
    'mime',
    'note',
    'tags',
    'updatedAt',
    'nameParts',
}
NAME_PART_KEYS = {
    'seriesAbbr',
    'number',
    'mainTitle',
    'subtitle',
    'edition',
    'authors',
    'extension',
    'editionLanguage',
}
INTERNAL_ITEM_KEYS = {
    'stored_path',
    'storedPath',
    'sha256',
    'current_hash',
    'currentHash',
    'name_data',
    'nameData',
}
IMPORT_RESULT_KEYS = {'requestId', 'documentId', 'entries', 'automatic', 'stats'}
IMPORT_ENTRY_KEYS = {'itemId', 'folderId', 'folderPath', 'created'}
IMPORT_STATS_KEYS = {'targetCount', 'created', 'existing', 'automatic'}
SETTINGS_CACHE_KEYS = {
    'policy',
    'openDays',
    'renderDays',
    'maxBytes',
    'targetBytes',
    'clearOpenOnExit',
    'usage',
}


class ResponseContractTests(unittest.TestCase):
    """Exercise each public DTO over real HTTP so the envelope is included."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = 'response-contract-token'
        self.env = mock.patch.dict(
            os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'fallback')}
        )
        self.env.start()
        self.server = BridgeServer(('127.0.0.1', 0), self.root / 'data', self.token, self.root)
        self.server.wait_for_startup_cleanup(5)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------

    def request(self, path, payload=None, *, authorized=True):
        headers = {'Authorization': f'Bearer {self.token}'} if authorized else {}
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_port, timeout=10
        )
        connection.request('POST' if payload is not None else 'GET', path, data, headers)
        response = connection.getresponse()
        body = response.read()
        status = response.status
        connection.close()
        return status, json.loads(body)

    def assertExactKeys(self, value, expected, label):
        self.assertIsInstance(value, dict, f'{label} must be a JSON object')
        added = sorted(set(value) - set(expected))
        missing = sorted(set(expected) - set(value))
        self.assertEqual(
            added,
            [],
            f'{label} gained public fields {added}; update this contract explicitly',
        )
        self.assertEqual(missing, [], f'{label} lost contracted fields {missing}')

    def assertEnvelope(self, result, status):
        self.assertExactKeys(result, ENVELOPE_KEYS, 'response envelope')
        self.assertEqual(result['ok'], status < 400)
        self.assertIsInstance(result['level'], str)
        self.assertIsInstance(result['error_type'], str)
        self.assertIsInstance(result['msg'], str)
        self.assertIsInstance(result['stats'], dict)
        if status >= 400:
            self.assertEqual(result['level'], 'error')
            self.assertIsNone(result['data'])
            self.assertEqual(result['stats'], {})
            self.assertTrue(result['msg'])

    def assertNoInternalItemFields(self, value, label='response'):
        if isinstance(value, dict):
            leaked = sorted(INTERNAL_ITEM_KEYS.intersection(value))
            self.assertEqual(leaked, [], f'{label} leaked internal item fields {leaked}')
            for key, child in value.items():
                self.assertNoInternalItemFields(child, f'{label}.{key}')
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.assertNoInternalItemFields(child, f'{label}[{index}]')

    def assertItemDto(self, item, label='item'):
        self.assertExactKeys(item, ITEM_KEYS, label)
        self.assertExactKeys(item['nameParts'], NAME_PART_KEYS, f'{label}.nameParts')
        self.assertIsInstance(item['id'], int)
        self.assertIsInstance(item['folderId'], int)
        self.assertIsInstance(item['documentId'], int)
        self.assertIsInstance(item['displayName'], str)
        self.assertIsInstance(item['listName'], str)
        self.assertIsInstance(item['extension'], str)
        self.assertIsInstance(item['size'], int)
        self.assertIsInstance(item['mime'], str)
        self.assertIsInstance(item['note'], str)
        self.assertIsInstance(item['tags'], list)
        self.assertTrue(all(isinstance(tag, str) for tag in item['tags']))
        self.assertIsInstance(item['updatedAt'], str)
        self.assertTrue(all(isinstance(part, str) for part in item['nameParts'].values()))

    def assertImportEntryDto(self, entry, label='import entry'):
        self.assertExactKeys(entry, IMPORT_ENTRY_KEYS, label)
        self.assertIsInstance(entry['itemId'], int)
        self.assertIsInstance(entry['folderId'], int)
        self.assertIsInstance(entry['folderPath'], str)
        self.assertIsInstance(entry['created'], bool)

    def create_folder(self, name, parent_id=1):
        store = self.server.open_store()
        try:
            return store.create_folder(parent_id, name)
        finally:
            store.close()

    def import_sample(
        self,
        *,
        folder_id=1,
        request_id='contract-request',
        name='Sample Book.txt',
        name_parts=None,
        tags=(),
        note='',
    ):
        source = self.root / name
        source.write_text('contract payload', encoding='utf-8')
        payload = {
            'path': str(source),
            'folderId': folder_id,
            'requestId': request_id,
            'nameParts': name_parts
            or {'mainTitle': 'Sample', 'authors': 'Tester', 'extension': Path(name).suffix},
            'tags': list(tags),
            'note': note,
        }
        status, result = self.request('/api/import/file', payload)
        self.assertEqual(status, 201, result)
        self.assertEnvelope(result, status)
        self.assertEqual(result['stats'], result['data']['stats'])
        return result['data']

    # -- envelope --------------------------------------------------------

    def test_response_envelope_shape_and_error_payloads_are_unchanged(self):
        status, result = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertEnvelope(result, status)
        self.assertEqual(result['data'], {})
        self.assertEqual(result['stats'], {})

        for path, payload, expected_status, expected_type in (
            ('/api/health', None, 401, 'request_rejected'),
            ('/api/missing', None, 404, 'not_found'),
            ('/api/folders/1/contents?offset=-1', None, 400, 'invalid_pagination'),
        ):
            with self.subTest(path=path, status=expected_status):
                if expected_status == 401:
                    status, failure = self.request(path, payload, authorized=False)
                else:
                    status, failure = self.request(path, payload)
                self.assertEqual(status, expected_status)
                self.assertEnvelope(failure, status)
                self.assertEqual(failure['error_type'], expected_type)

    # -- item and folder contents ---------------------------------------

    def test_folder_contents_dto_has_exact_keys_and_no_internal_item_fields(self):
        folder_id = self.create_folder('Contract Folder')
        imported = self.import_sample(
            folder_id=folder_id, request_id='contents-request', tags=('contract-tag',)
        )
        status, result = self.request(f'/api/folders/{folder_id}/contents')
        self.assertEqual(status, 200)
        self.assertEnvelope(result, status)
        contents = result['data']
        self.assertExactKeys(
            contents, {'folder', 'stats', 'folders', 'items', 'pagination'}, 'folder contents'
        )
        self.assertExactKeys(contents['folder'], {'id', 'parentId', 'name'}, 'contents.folder')
        self.assertEqual(contents['folder']['id'], folder_id)
        self.assertEqual(contents['folder']['parentId'], 1)
        self.assertEqual(contents['folder']['name'], 'Contract Folder')
        self.assertExactKeys(
            contents['stats'],
            {'folderCount', 'itemCount', 'uniqueDocumentCount', 'uniqueSize'},
            'contents.stats',
        )
        self.assertEqual(contents['stats']['folderCount'], 0)
        self.assertEqual(contents['stats']['itemCount'], 1)
        self.assertEqual(contents['stats']['uniqueDocumentCount'], 1)
        self.assertEqual(contents['stats']['uniqueSize'], imported['item']['size'])
        self.assertEqual(contents['folders'], [])
        self.assertEqual(len(contents['items']), 1)
        self.assertItemDto(contents['items'][0], 'contents.items[0]')
        self.assertEqual(contents['items'][0]['id'], imported['item']['id'])
        self.assertEqual(contents['items'][0]['folderId'], folder_id)
        self.assertEqual(contents['items'][0]['displayName'], imported['item']['displayName'])
        self.assertEqual(
            contents['items'][0]['nameParts'], imported['item']['nameParts']
        )
        self.assertExactKeys(
            contents['pagination'], {'offset', 'limit', 'total', 'hasMore'}, 'contents.pagination'
        )
        self.assertEqual(
            contents['pagination'],
            {'offset': 0, 'limit': 200, 'total': 1, 'hasMore': False},
        )
        self.assertNoInternalItemFields(result)

        status, page = self.request(f'/api/folders/{folder_id}/contents?offset=1&limit=1')
        self.assertEqual(status, 200)
        self.assertExactKeys(
            page['data']['pagination'], {'offset', 'limit', 'total', 'hasMore'}, 'paged pagination'
        )
        self.assertEqual(page['data']['items'], [])
        self.assertEqual(
            page['data']['pagination'],
            {'offset': 1, 'limit': 1, 'total': 1, 'hasMore': False},
        )

    def test_folder_tree_dto_nests_exact_node_keys(self):
        alpha_id = self.create_folder('Alpha')
        beta_id = self.create_folder('Beta', alpha_id)
        status, result = self.request('/api/tree')
        self.assertEqual(status, 200)
        self.assertEnvelope(result, status)
        tree = result['data']
        self.assertExactKeys(tree, {'folders', 'libraryId'}, 'tree')
        self.assertIsInstance(tree['libraryId'], str)
        self.assertTrue(tree['libraryId'])

        nodes = {}
        stack = list(tree['folders'])
        while stack:
            node = stack.pop()
            self.assertExactKeys(node, {'id', 'parentId', 'name', 'children'}, 'tree node')
            self.assertIsInstance(node['id'], int)
            if node['id'] == 1:
                self.assertIsNone(node['parentId'])
            else:
                self.assertIsInstance(node['parentId'], int)
            self.assertIsInstance(node['name'], str)
            self.assertIsInstance(node['children'], list)
            nodes[node['id']] = node
            stack.extend(node['children'])

        self.assertIn(1, nodes)
        self.assertEqual(nodes[1]['parentId'], None)
        self.assertEqual(nodes[alpha_id]['parentId'], 1)
        self.assertEqual(nodes[beta_id]['parentId'], alpha_id)
        self.assertEqual(nodes[beta_id]['children'], [])
        self.assertIn(beta_id, [child['id'] for child in nodes[alpha_id]['children']])
        self.assertNoInternalItemFields(result)

    # -- import receipt and status --------------------------------------

    def test_import_receipt_and_status_dtos_are_exact(self):
        imported = self.import_sample(
            request_id='contract-receipt',
            name_parts={
                'mainTitle': 'Sample',
                'subtitle': 'Receipt',
                'authors': 'Tester',
                'extension': '.txt',
            },
            tags=('article',),
            note='contract note',
        )
        self.assertExactKeys(
            imported,
            {'requestId', 'documentId', 'entries', 'automatic', 'stats', 'items', 'item'},
            'import receipt',
        )
        self.assertEqual(imported['requestId'], 'contract-receipt')
        self.assertIsInstance(imported['documentId'], int)
        self.assertIsInstance(imported['entries'], list)
        self.assertIsInstance(imported['automatic'], list)
        self.assertExactKeys(imported['stats'], IMPORT_STATS_KEYS, 'import stats')
        self.assertEqual(
            imported['stats'],
            {'targetCount': 1, 'created': 1, 'existing': 0, 'automatic': 1},
        )
        self.assertEqual(len(imported['entries']), 1)
        self.assertImportEntryDto(imported['entries'][0], 'import entries[0]')
        self.assertEqual(imported['entries'][0]['folderId'], 1)
        self.assertEqual(imported['entries'][0]['created'], True)
        self.assertEqual(len(imported['automatic']), 1)
        self.assertImportEntryDto(imported['automatic'][0], 'import automatic[0]')
        self.assertEqual(imported['automatic'][0]['created'], True)
        self.assertEqual(len(imported['items']), 1)
        self.assertItemDto(imported['items'][0], 'import items[0]')
        self.assertEqual(imported['item'], imported['items'][0])
        self.assertNoInternalItemFields(imported)

        status, receipt = self.request('/api/import/requests/contract-receipt')
        self.assertEqual(status, 200)
        self.assertEnvelope(receipt, status)
        self.assertExactKeys(receipt['data'], {'requestId', 'state', 'result'}, 'success status')
        self.assertEqual(receipt['data']['state'], 'success')
        self.assertExactKeys(receipt['data']['result'], IMPORT_RESULT_KEYS, 'receipt result')
        self.assertEqual(receipt['data']['result']['requestId'], 'contract-receipt')
        self.assertEqual(
            receipt['data']['result']['documentId'], imported['documentId']
        )
        self.assertImportEntryDto(
            receipt['data']['result']['entries'][0], 'receipt result entries[0]'
        )
        self.assertExactKeys(
            receipt['data']['result']['stats'], IMPORT_STATS_KEYS, 'receipt result stats'
        )
        self.assertEqual(receipt['stats'], {})

        status, missing = self.request('/api/import/requests/contract-missing')
        self.assertEqual(status, 200)
        self.assertExactKeys(missing['data'], {'requestId', 'state'}, 'not_found status')
        self.assertEqual(
            missing['data'],
            {'requestId': 'contract-missing', 'state': 'not_found'},
        )

        broken = self.root / 'Broken.pdf'
        broken.write_bytes(b'not a pdf')
        status, failure = self.request(
            '/api/import/file',
            {
                'path': str(broken),
                'folderId': 1,
                'requestId': 'contract-failed',
                'nameParts': {'mainTitle': 'Broken', 'extension': '.pdf'},
            },
        )
        self.assertEqual(status, 400)
        self.assertEnvelope(failure, status)
        self.assertEqual(failure['error_type'], 'import_failed')
        self.assertNotIn(str(self.root), json.dumps(failure))
        status, failed = self.request('/api/import/requests/contract-failed')
        self.assertEqual(status, 200)
        self.assertEnvelope(failed, status)
        self.assertExactKeys(
            failed['data'], {'requestId', 'state', 'errorType', 'message'}, 'failed status'
        )
        self.assertEqual(failed['data']['state'], 'failed')
        self.assertEqual(failed['data']['errorType'], 'import_failed')
        self.assertTrue(failed['data']['message'])
        self.assertNotIn(str(self.root), json.dumps(failed))
        self.assertNotIn('Traceback', json.dumps(failed))

    # -- search ----------------------------------------------------------

    def test_search_results_dto_per_kind_uses_public_item_projection(self):
        imported = self.import_sample(
            request_id='search-request', name='Search Book.txt', tags=('contract-tag',)
        )
        for kind, payload in (
            ('files', {'kind': 'files'}),
            ('mirrors', {'kind': 'mirrors'}),
            ('tags', {'kind': 'tags', 'query': 'contract-tag'}),
        ):
            with self.subTest(kind=kind):
                status, result = self.request('/api/search', payload)
                self.assertEqual(status, 200)
                self.assertEnvelope(result, status)
                data = result['data']
                self.assertExactKeys(data, {'kind', 'results', 'truncated'}, f'{kind} search')
                self.assertEqual(data['kind'], kind)
                self.assertIsInstance(data['truncated'], bool)
                self.assertTrue(data['results'])
                for hit in data['results']:
                    self.assertExactKeys(hit, {'folderPath', 'item'}, f'{kind} hit')
                    self.assertIsInstance(hit['folderPath'], str)
                    self.assertItemDto(hit['item'], f'{kind} hit.item')
                self.assertIn(imported['item']['id'], [hit['item']['id'] for hit in data['results']])
                self.assertNoInternalItemFields(result)

        status, failure = self.request('/api/search', {'kind': 'everything'})
        self.assertEqual(status, 400)
        self.assertEnvelope(failure, status)
        self.assertEqual(failure['error_type'], 'search_failed')

    # -- settings --------------------------------------------------------

    def test_settings_dto_keeps_the_intentional_data_home_path(self):
        self.server.capabilities = {'pdf': True, 'epub': False}
        status, result = self.request('/api/settings')
        self.assertEqual(status, 200)
        self.assertEnvelope(result, status)
        settings = result['data']
        self.assertExactKeys(settings, {'dataHome', 'cache', 'capabilities'}, 'settings')
        self.assertIsInstance(settings['dataHome'], str)
        self.assertEqual(settings['dataHome'], str(self.server.library_home))
        self.assertExactKeys(settings['cache'], SETTINGS_CACHE_KEYS, 'settings.cache')
        self.assertIsInstance(settings['cache']['policy'], str)
        for key in ('openDays', 'renderDays', 'maxBytes', 'targetBytes'):
            self.assertIsInstance(settings['cache'][key], int)
        self.assertIsInstance(settings['cache']['clearOpenOnExit'], bool)
        self.assertExactKeys(
            settings['cache']['usage'], {'open_cache', 'render_cache'}, 'settings.cache.usage'
        )
        self.assertTrue(all(isinstance(size, int) for size in settings['cache']['usage'].values()))
        self.assertEqual(settings['capabilities'], {'pdf': True, 'epub': False})

    # -- export receipt --------------------------------------------------

    def test_export_receipt_keeps_the_intentional_target_path(self):
        folder_id = self.create_folder('Contract Export')
        imported = self.import_sample(folder_id=folder_id, request_id='export-request')
        target = self.root / 'export-target'
        target.mkdir()
        status, result = self.request(
            f'/api/folders/{folder_id}/export', {'targetPath': str(target)}
        )
        self.assertEqual(status, 200)
        self.assertEnvelope(result, status)
        receipt = result['data']
        self.assertExactKeys(
            receipt,
            {'folderId', 'folderName', 'targetPath', 'fileCount', 'folderCount', 'copiedBytes'},
            'export receipt',
        )
        self.assertEqual(receipt['folderId'], folder_id)
        self.assertEqual(receipt['folderName'], 'Contract Export')
        # Export deliberately tells the main process where the exported folder
        # was created; this local path is part of the public export receipt.
        self.assertEqual(
            Path(receipt['targetPath']), (target / 'Contract Export').resolve()
        )
        self.assertEqual(receipt['fileCount'], 1)
        self.assertEqual(receipt['folderCount'], 0)
        self.assertEqual(receipt['copiedBytes'], imported['item']['size'])
        self.assertTrue(Path(receipt['targetPath']).is_dir())
        self.assertNoInternalItemFields(result)

    # -- main-process-only local paths ----------------------------------

    def test_open_and_location_paths_stay_outside_the_public_item_dto(self):
        imported = self.import_sample(request_id='path-request')
        item_id = imported['item']['id']
        display_name = imported['item']['displayName']
        store = self.server.open_store()
        try:
            stored_path = str(store.get_item(item_id).stored_path)
        finally:
            store.close()

        status, location = self.request(f'/api/items/{item_id}/location')
        self.assertEqual(status, 200)
        self.assertEnvelope(location, status)
        self.assertExactKeys(location['data'], {'path', 'documentName'}, 'location')
        self.assertEqual(location['data'], {'path': stored_path, 'documentName': display_name})
        # The renderer must reach these through main-process IPC handlers, never
        # through its generic GET allowlist (pinned in ipc-policy.test.mjs).

        status, opened = self.request(f'/api/items/{item_id}/open')
        self.assertEqual(status, 200)
        self.assertEnvelope(opened, status)
        self.assertExactKeys(opened['data'], {'path', 'documentName'}, 'open')
        open_path = Path(opened['data']['path'])
        self.assertTrue(open_path.is_absolute())
        self.assertTrue(open_path.is_file())
        self.assertTrue(open_path.is_relative_to(self.server.library_home))
        self.assertEqual(opened['data']['documentName'], display_name)

    # -- small auxiliary DTOs -------------------------------------------

    def test_auxiliary_lookup_and_folder_create_dtos_are_exact(self):
        self.import_sample(request_id='tags-request', tags=('contract-tag',))
        status, tags = self.request('/api/tags')
        self.assertEqual(status, 200)
        self.assertEnvelope(tags, status)
        self.assertExactKeys(tags['data'], {'common', 'general'}, 'tags')
        for bucket in ('common', 'general'):
            self.assertIsInstance(tags['data'][bucket], list)
            for tag in tags['data'][bucket]:
                self.assertExactKeys(tag, {'name', 'count'}, f'tags.{bucket} entry')
                self.assertIsInstance(tag['name'], str)
                self.assertIsInstance(tag['count'], int)

        status, shortcuts = self.request('/api/shortcuts')
        self.assertEqual(status, 200)
        self.assertEnvelope(shortcuts, status)
        self.assertExactKeys(shortcuts['data'], {'shortcuts'}, 'shortcuts')
        self.assertIsInstance(shortcuts['data']['shortcuts'], dict)
        self.assertTrue(
            all(
                isinstance(action, str) and isinstance(accelerator, str)
                for action, accelerator in shortcuts['data']['shortcuts'].items()
            )
        )

        status, created = self.request(
            '/api/folders', {'parentId': 1, 'name': 'Contract Created'}
        )
        self.assertEqual(status, 201)
        self.assertEnvelope(created, status)
        self.assertExactKeys(created['data'], {'folder'}, 'folder create')
        folder = created['data']['folder']
        self.assertExactKeys(folder, {'id', 'parentId', 'name'}, 'folder create.folder')
        self.assertIsInstance(folder['id'], int)
        self.assertEqual(folder['parentId'], 1)
        self.assertEqual(folder['name'], 'Contract Created')


if __name__ == '__main__':
    unittest.main()
