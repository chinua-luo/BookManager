from __future__ import annotations

import ctypes
import errno
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bookmanager import exporting
from bookmanager.store import LibraryStore


class AllocationTests(unittest.TestCase):
    def test_shared_namespace_unicode_suffixes_and_remapped_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blob = root / 'blob'
            blob.write_text('content')
            stage = root / 'stage'
            stage.mkdir()
            folders = [('Café.txt',), ('CAFÉ.TXT (2)',), ('CAFE\u0301.TXT',),
                       ('CAFE\u0301.TXT', 'Child'), ('other',)]
            items = [((), 'cafe\u0301.txt'), ((), 'CAFÉ (2).TXT'),
                     ((), 'café.txt'), (('CAFE\u0301.TXT', 'Child'), 'nested.txt'),
                     (('other',), 'café.txt')]
            snapshot = SimpleNamespace(
                folders=[SimpleNamespace(relative_parts=p) for p in folders],
                items=[SimpleNamespace(relative_parts=p, display_name=n, stored_path=blob)
                       for p, n in items])
            with mock.patch.object(exporting, '_item_file_name', side_effect=lambda e: e.display_name):
                exporting._write_tree(snapshot, stage)
            self.assertTrue((stage / 'CAFE\u0301.TXT (3)' / 'Child' / 'nested.txt').is_file())
            for name in ['cafe\u0301 (2).txt', 'CAFÉ (2) (2).TXT', 'café (3).txt']:
                self.assertEqual((stage / name).read_text(), 'content')
            self.assertEqual((stage / 'other' / 'café.txt').read_text(), 'content')
            self.assertTrue((stage / 'Café.txt').is_dir())

    def test_existing_generated_suffix_is_reserved_for_files_and_directories(self):
        used = set()
        for name in ['Straße.txt', 'STRASSE (2).TXT']:
            self.assertEqual(exporting._unique_file_name(used, name, directory=True), name)
        self.assertEqual(exporting._unique_file_name(used, 'strasse.txt'), 'strasse (3).txt')


class PublicationTests(unittest.TestCase):
    def test_late_target_conflicts_preserve_target_and_clean_staging(self):
        kinds = ['empty', 'populated', 'file']
        if os.name != 'nt':
            kinds += ['symlink', 'dangling']
        for kind in kinds:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = LibraryStore(root / 'library')
                self.addCleanup(store.close)
                folder_id = store.create_folder(1, 'Export')
                out = root / 'out'
                out.mkdir()
                target = out / 'Export'
                original = exporting._commit_staging
                def race(stage, destination, folder):
                    if kind in ['empty', 'populated']:
                        target.mkdir()
                        if kind == 'populated':
                            (target / 'keep').write_text('keep')
                    elif kind == 'file':
                        target.write_text('keep')
                    else:
                        target.symlink_to(root / ('out' if kind == 'symlink' else 'missing'),
                                          target_is_directory=True)
                    original(stage, destination, folder)
                with mock.patch.object(exporting, '_commit_staging', side_effect=race):
                    with self.assertRaises(exporting.ExportProblem) as caught:
                        exporting.export_folder(store, folder_id, out)
                self.assertEqual(caught.exception.export_error_type, 'export_conflict')
                self.assertEqual(list(out.iterdir()), [target])
                if kind == 'file':
                    self.assertEqual(target.read_text(), 'keep')
                elif kind == 'populated':
                    self.assertEqual((target / 'keep').read_text(), 'keep')
                elif kind in ['symlink', 'dangling']:
                    self.assertTrue(target.is_symlink())
                else:
                    self.assertEqual(list(target.iterdir()), [])
                store.close()

    def test_concurrent_publish_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            stages = [root / str(i) for i in range(8)]
            for stage in stages:
                stage.mkdir()
                (stage / 'owner').write_text(stage.name)
            barrier = threading.Barrier(len(stages))
            def publish(stage):
                barrier.wait(timeout=10)
                try:
                    exporting._commit_staging(stage, target, SimpleNamespace(name='target'))
                    return stage.name
                except exporting.ExportProblem as error:
                    self.assertEqual(error.export_error_type, 'export_conflict')
                    self.assertEqual((stage / 'owner').read_text(), stage.name)
                    return None
            with ThreadPoolExecutor(max_workers=len(stages)) as pool:
                winners = [result for result in pool.map(publish, stages) if result is not None]
            self.assertEqual(winners, [(target / 'owner').read_text()])

    def test_platform_dispatch_and_exclusive_flags(self):
        for platform, symbol, flags in [('darwin', 'renamex_np', 4), ('linux', 'renameat2', 1)]:
            with self.subTest(platform=platform):
                function = mock.Mock(return_value=0)
                libc = SimpleNamespace(**{symbol: function})
                with mock.patch.object(sys, 'platform', platform), mock.patch.object(ctypes, 'CDLL', return_value=libc):
                    exporting._rename_exclusive(Path('source'), Path('target'))
                args = function.call_args.args
                self.assertEqual(args, (b'source', b'target', flags) if platform == 'darwin'
                                 else (-100, b'source', -100, b'target', flags))
                self.assertEqual(function.restype, ctypes.c_int)
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch.object(os, 'rename') as rename:
            exporting._rename_exclusive(Path('source'), Path('target'))
            rename.assert_called_once_with(Path('source'), Path('target'))

    def test_unsupported_platform_symbol_and_filesystem_fail_closed(self):
        with mock.patch.object(os, 'replace') as replace, mock.patch.object(os, 'rename') as rename:
            with mock.patch.object(sys, 'platform', 'unsupported'):
                with self.assertRaises(OSError):
                    exporting._rename_exclusive(Path('source'), Path('target'))
            for platform in ['darwin', 'linux']:
                with mock.patch.object(sys, 'platform', platform), mock.patch.object(ctypes, 'CDLL', return_value=SimpleNamespace()):
                    with self.assertRaises(OSError) as caught:
                        exporting._rename_exclusive(Path('source'), Path('target'))
                    self.assertEqual(caught.exception.errno, errno.ENOTSUP)
            for code in [errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EACCES, errno.ENOSPC]:
                libc = SimpleNamespace(renameat2=mock.Mock(return_value=-1))
                with mock.patch.object(sys, 'platform', 'linux'), mock.patch.object(ctypes, 'CDLL', return_value=libc), mock.patch.object(ctypes, 'get_errno', return_value=code):
                    with self.assertRaises(OSError) as caught:
                        exporting._rename_exclusive(Path('source'), Path('target'))
                    self.assertEqual(caught.exception.errno, code)
            replace.assert_not_called()
            rename.assert_not_called()


if __name__ == '__main__':
    unittest.main()
