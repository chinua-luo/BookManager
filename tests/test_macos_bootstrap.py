import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'start_macos.command'


class MacOSBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        (self.root / 'electron').mkdir()
        (self.root / 'requirements.txt').write_text('pypdf\n', encoding='utf-8')
        shutil.copy2(SCRIPT, self.root / SCRIPT.name)
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        self.log = Path(self.temp.name) / 'calls'
        self.command('uname', 'echo Darwin')
        self.command('python3', '''
if [ "$1" = -c ]; then exit "${BAD_PYTHON:-0}"; fi
if [ "$1" = -m ] && [ "$2" = venv ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\necho pip >> "$CALL_LOG"\\nexit "${PIP_FAIL:-0}"\\n' > "$3/bin/python"
  chmod +x "$3/bin/python"
fi
''')
        self.command('node', 'exit "${BAD_NODE:-0}"')
        self.command('npx', '''
echo "$*" >> "$CALL_LOG"
if [ "${FAIL_NPX:-}" = install ] && [ "$3" = install ]; then exit 1; fi
if [ "$3" = install ]; then
  mkdir -p node_modules/.bin
  printf '#!/bin/sh\\necho launched >> "$CALL_LOG"\\n' > node_modules/.bin/electron
  chmod +x node_modules/.bin/electron
fi
''')
        self.env = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin', CALL_LOG=str(self.log))

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n', encoding='utf-8')
        path.chmod(0o755)

    def run_script(self, *args, **environment):
        return subprocess.run(['/bin/bash', str(self.root / SCRIPT.name), *args],
                              cwd=self.temp.name, env={**self.env, **environment},
                              text=True, capture_output=True, timeout=15)

    def test_setup_and_repeat_preserve_environment(self):
        for _ in range(2):
            result = self.run_script('--setup-only')
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text(encoding='utf-8')
        self.assertEqual(calls.count('install --frozen-lockfile'), 2)
        self.assertEqual(calls.count('run build'), 2)
        self.assertNotIn('launched', calls)

    def test_launch_after_setup(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('launched', self.log.read_text(encoding='utf-8'))

    def test_unsupported_node_stops_before_install(self):
        result = self.run_script(BAD_NODE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node.js 版本过低', result.stderr)
        self.assertFalse(self.log.exists())

    def test_failed_dependency_install_never_launches(self):
        result = self.run_script(FAIL_NPX='install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('前端依赖安装失败', result.stderr)
        self.assertNotIn('launched', self.log.read_text(encoding='utf-8'))

    def test_invalid_existing_venv_not_overwritten(self):
        (self.root / '.venv').mkdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不会自动覆盖', result.stderr)
        self.assertTrue((self.root / '.venv').is_dir())


if __name__ == '__main__':
    unittest.main()
