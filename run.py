from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ELECTRON_DIR = PROJECT_DIR / "electron"


def bundled_electron() -> Path | None:
    """Return the matching Electron executable shipped with this project."""
    system = sys.platform
    machine = platform.machine().lower()
    if system == "win32":
        names = ["win32-arm64", "win32-x64"] if "arm" in machine else ["win32-x64", "win32-arm64"]
        candidates = [ELECTRON_DIR / "runtime" / name / "electron.exe" for name in names]
    elif system == "darwin":
        names = ["darwin-arm64", "darwin-x64"] if "arm" in machine else ["darwin-x64", "darwin-arm64"]
        candidates = [
            ELECTRON_DIR / "runtime" / name / bundle / "Contents" / "MacOS" / "Electron"
            for name in names
            for bundle in ("BookManager.app", "Electron.app")
        ]
    else:
        candidates = [ELECTRON_DIR / "runtime" / "linux-x64" / "electron"]

    return next((candidate for candidate in candidates if candidate.is_file()), None)


def start_electron() -> bool:
    executable = bundled_electron()
    if executable is None:
        return False

    environment = os.environ.copy()
    # The bridge must run in the same Python environment as this launcher.
    environment["BOOKMANAGER_PYTHON"] = sys.executable
    environment["BOOKMANAGER_PROJECT_DIR"] = str(PROJECT_DIR)
    try:
        # macOS application bundles must be launched through LaunchServices.
        # Windows and Linux can execute the bundled runtime directly.
        if sys.platform == "darwin":
            bundle = executable.parents[2]
            subprocess.Popen(
                ["open", "-n", str(bundle), "--args", str(ELECTRON_DIR)],
                cwd=ELECTRON_DIR,
                env=environment,
            )
            return True
        result = subprocess.run([str(executable), str(ELECTRON_DIR)], cwd=ELECTRON_DIR, env=environment)
    except OSError as error:
        print(f"Could not start the bundled Electron interface: {error}")
        return False
    if result.returncode != 0:
        print(f"BookManager Electron exited with status {result.returncode}.")
    # A discovered Electron runtime is authoritative.  Do not launch the old
    # Tk interface after an Electron failure, which would create a second UI.
    return True


if __name__ == "__main__":
    if not start_electron():
        from bookmanager.app import main as tkinter_main

        tkinter_main()
