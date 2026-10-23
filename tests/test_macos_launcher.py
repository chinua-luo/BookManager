from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

from tools.create_macos_launcher import BUNDLE_IDENTIFIER, create_launcher


class MacOSLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "Book Manager Project"
        (self.project / "electron" / "dist").mkdir(parents=True)
        contents = self.project / "electron" / "runtime" / "darwin-arm64" / "BookManager.app" / "Contents"
        runtime = contents / "MacOS"
        resources = contents / "Resources"
        loader = self.project / "electron" / "runtime-loader"
        runtime.mkdir(parents=True)
        resources.mkdir()
        loader.mkdir(parents=True)
        (self.project / "book.png").write_bytes(b"png")
        (self.project / "electron" / "dist" / "index.html").write_text("ready", encoding="utf-8")
        runtime_executable = runtime / "Electron"
        runtime_executable.write_text("runtime", encoding="utf-8")
        runtime_executable.chmod(0o755)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": "org.electronjs.Electron", "CFBundleExecutable": "Electron"}, handle)
        (loader / "loader.mjs").write_text("// loader", encoding="utf-8")
        (loader / "package.json").write_text('{"main":"loader.mjs"}', encoding="utf-8")
        self.signed: list[Path] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def fake_icon(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"icns")

    def fake_sign(self, bundle: Path) -> None:
        self.signed.append(bundle)

    def create(self, output: Path) -> Path:
        return create_launcher(
            self.project,
            output,
            python_executable=Path(sys.executable),
            icon_builder=self.fake_icon,
            signer=self.fake_sign,
        )

    def test_shortcut_targets_the_configured_runtime_app(self) -> None:
        output = self.root / "BookManager.app"

        self.create(output)

        runtime = (self.project / "electron" / "runtime" / "darwin-arm64" / "BookManager.app").resolve()
        self.assertTrue(output.is_symlink())
        self.assertEqual(output.resolve(), runtime.resolve())
        self.assertEqual(self.signed, [runtime])

    def test_runtime_uses_one_bundle_identity_icon_and_embedded_config(self) -> None:
        output = self.root / "BookManager.app"

        self.create(output)

        contents = output.resolve() / "Contents"
        with (contents / "Info.plist").open("rb") as handle:
            plist = plistlib.load(handle)
        config = json.loads((contents / "Resources" / "app" / "bookmanager-config.json").read_text(encoding="utf-8"))
        self.assertEqual(plist["CFBundleIdentifier"], BUNDLE_IDENTIFIER)
        self.assertEqual(plist["CFBundleExecutable"], "Electron")
        self.assertEqual(plist["CFBundleIconFile"], "BookManager.icns")
        self.assertEqual(config["projectDir"], str(self.project.resolve()))
        self.assertEqual(config["pythonExecutable"], str(Path(sys.executable).resolve()))
        self.assertTrue((contents / "Resources" / "BookManager.icns").is_file())
        self.assertEqual(
            (contents / "Resources" / "app" / "loader.mjs").read_text(encoding="utf-8"),
            "// loader",
        )

    def test_replaces_the_old_wrapper_bundle_with_a_symlink(self) -> None:
        output = self.root / "BookManager.app"
        (output / "Contents").mkdir(parents=True)
        (output / "Contents" / "old-launcher").write_text("old", encoding="utf-8")

        self.create(output)

        self.assertTrue(output.is_symlink())
        self.assertFalse((output / "Contents" / "old-launcher").exists())

    def test_rejects_an_incomplete_project_without_creating_a_shortcut(self) -> None:
        output = self.root / "BookManager.app"
        (self.project / "electron" / "dist" / "index.html").unlink()

        with self.assertRaises(OSError):
            self.create(output)

        self.assertFalse(os.path.lexists(output))


if __name__ == "__main__":
    unittest.main()
