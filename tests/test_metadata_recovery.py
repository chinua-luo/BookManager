from __future__ import annotations

import builtins
import json
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from bookmanager.metadata import _extract_pdf, extract_metadata
from bookmanager.metadata_worker import _limit_resources
from bookmanager.naming import infer_structured_file_name
from bookmanager.startup import check_dependencies


class MetadataRecoveryTests(unittest.TestCase):
    def test_missing_and_broken_pdf_dependencies_have_distinct_messages(self):
        original = builtins.__import__
        def load_with(error):
            def load(name, *args, **kwargs):
                if name == 'pypdf':
                    raise error
                return original(name, *args, **kwargs)
            return load
        for error, expected, logged in [
            (ModuleNotFoundError("missing", name='pypdf'), '未安装 pypdf', False),
            (ModuleNotFoundError("private detail", name='internal_dependency'), '依赖加载失败', True),
            (ImportError('private detail'), '依赖加载失败', True),
        ]:
            with self.subTest(error=error), mock.patch('builtins.__import__', side_effect=load_with(error)), mock.patch('bookmanager.metadata.record_event') as event, mock.patch('bookmanager.metadata.record_exception') as exception:
                result = _extract_pdf(Path('Book.pdf'), infer_structured_file_name('Book.pdf'), 5)
                self.assertFalse(result.extracted)
                self.assertIn(expected, result.message)
                self.assertNotIn('private detail', result.message)
                self.assertEqual(exception.called, logged)
                self.assertEqual(event.called, not logged)

    def test_corrupt_pdf_and_empty_text_are_not_reported_as_missing_dependencies(self):
        fallback = infer_structured_file_name('Book.pdf')
        for reader, expected in [
            (mock.Mock(side_effect=ValueError('private parser detail')), '内容解析失败'),
            (mock.Mock(return_value=types.SimpleNamespace(pages=[mock.Mock(extract_text=lambda: '')])), '未从 PDF 前 5 页提取到文本'),
        ]:
            with self.subTest(expected=expected), mock.patch.dict(sys.modules, {'pypdf': types.SimpleNamespace(PdfReader=reader)}), mock.patch('bookmanager.metadata.record_exception'):
                result = _extract_pdf(Path('Book.pdf'), fallback, 5)
                self.assertIn(expected, result.message)
                self.assertNotIn('未安装', result.message)
                self.assertNotIn('private parser detail', result.message)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "optional pypdf dependency unavailable")
    def test_real_worker_can_read_pdf_with_installed_dependency(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'Book.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.write(path)
            with mock.patch.dict('os.environ', {'BOOKMANAGER_DIAGNOSTIC_HOME': temporary}):
                result = extract_metadata(path)
            self.assertFalse(result.extracted)
            self.assertIn('未从 PDF 前 5 页提取到文本', result.message)
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
            page = writer.pages[0]
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 10 50 Td (Useful Book Title) Tj ET')
            page[NameObject('/Contents')] = stream
            writer.write(path)
            with mock.patch.dict('os.environ', {'BOOKMANAGER_DIAGNOSTIC_HOME': temporary}):
                result = extract_metadata(path)
            self.assertTrue(result.extracted, result.message)
            self.assertEqual(result.parts.main_title, 'Useful Book Title')
            records = [json.loads(line) for log in Path(temporary).glob('logs/diagnostics-*.jsonl') for line in log.read_text().splitlines()]
            self.assertFalse(any(record['level'] == 'ERROR' for record in records), records)

    def test_missing_optional_pdf_dependency_is_a_capability_event(self):
        import importlib
        original = importlib.import_module
        def load(name):
            if name == 'pypdf':
                raise ModuleNotFoundError('missing', name='pypdf')
            return original(name)
        with mock.patch('bookmanager.startup.importlib.import_module', side_effect=load), mock.patch('bookmanager.startup.record_event') as event, mock.patch('bookmanager.startup.record_exception') as exception:
            self.assertFalse(check_dependencies()['pdfMetadata'])
            event.assert_any_call('startup.optional_pdf_metadata_unavailable', module='pypdf')
            exception.assert_not_called()


class ResourceLimitTests(unittest.TestCase):
    def fake_resource(self, limits):
        return types.SimpleNamespace(RLIMIT_CPU=0, RLIMIT_NOFILE=1, RLIMIT_AS=2, RLIM_INFINITY=-1, getrlimit=mock.Mock(side_effect=lambda resource: limits[resource]), setrlimit=mock.Mock())

    def test_resource_module_absence_is_a_capability_event(self):
        with mock.patch.dict(sys.modules, {'resource': None}), mock.patch('bookmanager.metadata_worker.record_event') as event:
            _limit_resources()
        event.assert_called_once_with('metadata_worker.limit_skipped', resource='resource', reason='resource_module_not_supported', message='当前平台未提供资源限制模块，按平台能力跳过设置；解析超时保护继续生效。')

    def test_inherited_limits_are_never_raised_and_infinity_is_supported(self):
        resource = self.fake_resource({0: (3, 10), 1: (12, 16), 2: (-1, -1)})
        with mock.patch.dict(sys.modules, {'resource': resource}), mock.patch('bookmanager.metadata_worker.sys.platform', 'linux'):
            _limit_resources()
        self.assertEqual(resource.setrlimit.call_args_list, [mock.call(0, (3, 10)), mock.call(1, (12, 16)), mock.call(2, (768 * 1024 * 1024,) * 2)])

    def test_darwin_skips_address_space_and_keeps_other_limits(self):
        resource = self.fake_resource({0: (-1, -1), 1: (-1, -1)})
        with mock.patch.dict(sys.modules, {'resource': resource}), mock.patch('bookmanager.metadata_worker.sys.platform', 'darwin'), mock.patch('bookmanager.metadata_worker.record_event') as event:
            _limit_resources()
        self.assertEqual(resource.setrlimit.call_args_list, [mock.call(0, (25, 25)), mock.call(1, (64, 64))])
        event.assert_called_once_with('metadata_worker.limit_skipped', resource='RLIMIT_AS', reason='macos_compatibility_policy', message='按 macOS 兼容性策略跳过地址空间限制；CPU、文件描述符限制和解析超时保护继续生效。')

    def test_unsupported_limit_is_skipped_without_hiding_actual_failures(self):
        resource = self.fake_resource({0: (-1, -1), 2: (-1, -1)})
        del resource.RLIMIT_NOFILE
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(sys.modules, {'resource': resource}), mock.patch('bookmanager.metadata_worker.sys.platform', 'linux'), mock.patch.dict('os.environ', {'BOOKMANAGER_DIAGNOSTIC_HOME': temporary}):
                resource.setrlimit.side_effect = [ValueError('system ceiling'), None]
                _limit_resources()
            records = [json.loads(line) for log in Path(temporary).glob('logs/diagnostics-*.jsonl') for line in log.read_text().splitlines()]
        skipped = next(record for record in records if record['event'] == 'metadata_worker.limit_skipped')
        self.assertEqual(skipped['level'], 'INFO')
        self.assertEqual(skipped['details']['resource'], 'RLIMIT_NOFILE')
        self.assertEqual(skipped['details']['reason'], 'resource_not_supported')
        self.assertIn('按平台能力跳过', skipped['details']['message'])
        self.assertNotIn('error_type', skipped)
        failed = next(record for record in records if record['event'] == 'metadata_worker.limit_unavailable')
        self.assertEqual(failed['level'], 'ERROR')
        self.assertEqual(failed['details']['resource'], 'RLIMIT_CPU')
        self.assertEqual(failed['error_type'], 'ValueError')
        self.assertIn('system ceiling', failed['traceback'])

    def test_failure_records_resource_and_continues_with_other_limits(self):
        resource = self.fake_resource({0: (-1, -1), 1: (-1, -1), 2: (-1, -1)})
        failure = ValueError('system ceiling')
        resource.setrlimit.side_effect = [failure, None, None]
        with mock.patch.dict(sys.modules, {'resource': resource}), mock.patch('bookmanager.metadata_worker.sys.platform', 'linux'), mock.patch('bookmanager.metadata_worker.record_event') as event:
            _limit_resources()
        self.assertEqual(resource.setrlimit.call_count, 3)
        event.assert_called_once_with('metadata_worker.limit_unavailable', resource='RLIMIT_CPU', target=25, error=failure)


if __name__ == '__main__':
    unittest.main()
