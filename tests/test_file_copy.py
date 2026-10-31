from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bookmanager import file_copy


class FastCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"cache copy payload" * 1024)
        self.target = self.root / "nested" / "target.bin"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_copy_preserves_content_and_creates_parent(self) -> None:
        file_copy.clone_or_copy(self.source, self.target)
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_darwin_uses_apfs_clone_command(self) -> None:
        with (
            mock.patch.object(file_copy.sys, "platform", "darwin"),
            mock.patch.object(file_copy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            file_copy.clone_or_copy(self.source, self.target)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["cp", "-c", "-p"])
        self.assertEqual(command[-2:], [str(self.source), str(self.target)])

    def test_linux_uses_reflink_with_fallback(self) -> None:
        with (
            mock.patch.object(file_copy.sys, "platform", "linux"),
            mock.patch.object(file_copy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            file_copy.clone_or_copy(self.source, self.target)
        self.assertIn("--reflink=auto", run.call_args.args[0])

        self.target.unlink(missing_ok=True)
        with (
            mock.patch.object(file_copy.sys, "platform", "linux"),
            mock.patch.object(file_copy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
            mock.patch.object(file_copy.shutil, "copy2", wraps=file_copy.shutil.copy2) as fallback,
        ):
            file_copy.clone_or_copy(self.source, self.target)
        fallback.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_failed_clone_does_not_leave_a_partial_target(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"partial")
        with (
            mock.patch.object(file_copy.sys, "platform", "darwin"),
            mock.patch.object(file_copy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
        ):
            file_copy.clone_or_copy(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
