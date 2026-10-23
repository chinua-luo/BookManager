from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import run


PROJECT_DIR = Path(__file__).resolve().parents[1]


class ElectronOnlyTests(unittest.TestCase):
    def test_launcher_does_not_fall_back_when_runtime_is_missing(self) -> None:
        with patch.object(run, "start_electron", return_value=False):
            self.assertEqual(run.main(), 1)

    def test_removed_rendering_dependencies_are_not_referenced(self) -> None:
        checked_paths = [
            PROJECT_DIR / "requirements.txt",
            PROJECT_DIR / "run.py",
            PROJECT_DIR / "start_windows.bat",
            PROJECT_DIR / "bookmanager" / "bridge.py",
            PROJECT_DIR / "bookmanager" / "metadata.py",
        ]
        forbidden = ("pymu" + "pdf", "fit" + "z", "tkinter" + "dnd2")
        for path in checked_paths:
            contents = path.read_text(encoding="utf-8").lower()
            for value in forbidden:
                self.assertNotIn(value, contents, f"{value} remains in {path}")

    def test_legacy_tk_preview_modules_were_removed(self) -> None:
        self.assertFalse((PROJECT_DIR / "bookmanager" / "app.py").exists())
        self.assertFalse((PROJECT_DIR / "bookmanager" / "rendering.py").exists())

if __name__ == "__main__":
    unittest.main()
