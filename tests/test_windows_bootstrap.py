from pathlib import Path
import os
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'start_windows.bat'


class WindowsBootstrapContractTests(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding='utf-8')

    def test_checks_required_tools_and_versions_before_install(self):
        for part in ('where py', 'sys.version_info >= (3,10)', 'where node',
                     'a===22 && b>=12', 'where npx.cmd'):
            self.assertIn(part, self.source)
        self.assertLess(self.source.index('where npx.cmd'), self.source.index('-m venv'))

    def test_installs_project_local_dependencies_and_frozen_lockfile(self):
        for part in ('py -3 -m venv "%~dp0.venv"', '.venv\\Scripts\\python.exe',
                     '-m pip install -r "%~dp0requirements.txt"',
                     'call npx.cmd --yes pnpm@9.15.9 install --frozen-lockfile',
                     'call npx.cmd --yes pnpm@9.15.9 run build'):
            self.assertIn(part, self.source)
        self.assertNotIn(' -m pip install --user', self.source)

    def test_launch_uses_venv_and_project_without_old_runtime(self):
        self.assertIn('set "BOOKMANAGER_PYTHON=%~dp0.venv\\Scripts\\python.exe"', self.source)
        self.assertIn('set "BOOKMANAGER_PROJECT_DIR=%~dp0"', self.source)
        self.assertIn('call "%~dp0electron\\node_modules\\.bin\\electron.cmd" .', self.source)
        self.assertNotIn('run.py', self.source)
        self.assertLess(self.source.index('run build'), self.source.index('electron.cmd" .'))

    def test_failures_stop_and_setup_only_does_not_pause(self):
        for label in ('error_directory', 'error_python', 'error_node', 'error_npx',
                      'error_venv', 'error_pip', 'error_frontend', 'error_build', 'error_electron'):
            self.assertIn(f':{label}', self.source)
        self.assertIn('if not "%~1"=="--setup-only" pause', self.source)
        self.assertIn('exit /b 1', self.source)
        self.assertIn('if "%~1"=="--setup-only" goto success', self.source)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows cmd.exe')
    def test_invalid_option_exits_nonzero_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c',
                                     str(SCRIPT), '--invalid'], cwd=temp,
                                    capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown option', result.stdout)


if __name__ == '__main__':
    unittest.main()
