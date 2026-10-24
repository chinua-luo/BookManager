from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.startup import MIN_FREE_BYTES, StartupFailure, check_dependencies, check_storage, preflight
from bookmanager.store import LibraryStore


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / 'data'
        self.static = self.root / 'static'
        self.static.mkdir()
        (self.static / 'index.html').write_text('<!doctype html>')
        self.env = mock.patch.dict(os.environ, {'BOOKMANAGER_DIAGNOSTIC_HOME': str(self.root / 'diagnostics')})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def test_storage_probe_preserves_database_and_removes_temporary_files(self):
        store = LibraryStore(self.home)
        store.close()
        before = (self.home / 'library.db').read_bytes()
        check_storage(self.home)
        self.assertEqual((self.home / 'library.db').read_bytes(), before)
        self.assertEqual(list(self.home.rglob('.bookmanager-preflight-*')), [])

    def test_readonly_database_or_existing_cache_directory_blocks_startup(self):
        store = LibraryStore(self.home)
        store.close()
        for target in (self.home / 'library.db', self.home / 'blobs'):
            mode = target.stat().st_mode
            try:
                target.chmod(0o400 if target.is_file() else 0o500)
                with self.assertRaises(StartupFailure) as raised:
                    check_storage(self.home)
                self.assertEqual(raised.exception.error_type, 'data_permission')
            finally:
                target.chmod(mode)

    def test_low_disk_space_and_corrupt_database_have_distinct_failures(self):
        usage = type('Usage', (), {'free': MIN_FREE_BYTES - 1})()
        with mock.patch('bookmanager.startup.shutil.disk_usage', return_value=usage):
            with self.assertRaises(StartupFailure) as raised:
                check_storage(self.home)
        self.assertEqual(raised.exception.error_type, 'disk_full')
        (self.home / 'library.db').write_bytes(b'corrupt database fixture')
        with self.assertRaises(StartupFailure) as raised:
            check_storage(self.home)
        self.assertEqual(raised.exception.error_type, 'data_corrupt')

    def test_core_dependency_failure_blocks_and_optional_dependency_only_degrades(self):
        with mock.patch('bookmanager.startup.importlib.import_module', side_effect=ImportError('broken ssl')):
            with self.assertRaises(StartupFailure) as raised:
                check_dependencies()
        self.assertEqual(raised.exception.error_type, 'dependency_missing')
        real_import = __import__('importlib').import_module
        def load(name):
            if name == 'pypdf':
                raise ImportError('optional pypdf missing')
            return real_import(name)
        with mock.patch('bookmanager.startup.importlib.import_module', side_effect=load), mock.patch('bookmanager.startup.shutil.which', return_value=None):
            self.assertEqual(check_dependencies(), {'pdfMetadata': False, 'djvuText': False})

    def test_preflight_does_not_pollute_empty_migration_target(self):
        source = self.root / 'legacy'
        store = LibraryStore(source)
        store.close()
        with mock.patch('bookmanager.store.DEFAULT_LIBRARY_HOME', self.home), mock.patch('bookmanager.store.PROJECT_LIBRARY_HOME', source), mock.patch('bookmanager.store.configured_library_home', return_value=None):
            with mock.patch('bookmanager.startup.check_dependencies', return_value={}):
                home, _capabilities = preflight(None, self.static)
        self.assertEqual(home, self.home)
        self.assertTrue((self.home / 'library.db').is_file())
        self.assertFalse((source / 'library.db').exists())

    def test_real_bootstrap_outputs_structured_failure_and_persistent_traceback(self):
        # A real subprocess validates the preflight/lifecycle boundary without a GUI.
        missing = self.root / 'missing-static'
        result = subprocess.run([sys.executable, '-m', 'bookmanager.startup', '--static-dir', str(missing), '--data-home', str(self.home)], env={**os.environ, 'BOOKMANAGER_BRIDGE_TOKEN': 'x' * 64}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        event = json.loads(result.stdout)
        self.assertEqual(event['event'], 'startup-error')
        self.assertEqual(event['error_type'], 'frontend_missing')
        self.assertNotIn('Traceback', result.stdout)
        self.assertIn('Traceback', Path(event['log_path']).read_text())

    def test_real_bootstrap_passes_preflight_then_serves_enveloped_health(self):
        import queue
        import threading
        import urllib.request
        child = subprocess.Popen([sys.executable, '-m', 'bookmanager.startup', '--static-dir', str(self.static), '--data-home', str(self.home)], env={**os.environ, 'BOOKMANAGER_BRIDGE_TOKEN': 'x' * 64}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            event = json.loads(lines.get(timeout=15))
            self.assertEqual(event['event'], 'ready')
            request = urllib.request.Request(f"http://127.0.0.1:{event['port']}/api/health", headers={'Authorization': 'Bearer ' + 'x' * 64})
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['data'], {})
            self.assertTrue(list((self.home / 'logs').glob('diagnostics-*.jsonl')))
        finally:
            child.terminate()
            child.communicate(timeout=5)
            reader.join(timeout=1)

    def test_corrupt_legacy_library_is_rejected_before_migration_can_move_it(self):
        source = self.root / 'legacy'
        source.mkdir()
        database = source / 'library.db'
        database.write_bytes(b'corrupt legacy library')
        with mock.patch('bookmanager.store.DEFAULT_LIBRARY_HOME', self.home), mock.patch('bookmanager.store.PROJECT_LIBRARY_HOME', source), mock.patch('bookmanager.store.configured_library_home', return_value=None):
            with mock.patch('bookmanager.startup.check_dependencies', return_value={}):
                with self.assertRaises(StartupFailure) as raised:
                    preflight(None, self.static)
        self.assertEqual(raised.exception.error_type, 'data_corrupt')
        self.assertEqual(database.read_bytes(), b'corrupt legacy library')
        self.assertFalse((self.home / 'library.db').exists())
