"""Fast file copies for disposable cache copies.

Opening an external application needs an independent copy of the blob, but a
plain `shutil.copy2` rewrites every byte. macOS APFS and Linux filesystems with
reflink support can create a copy-on-write clone instead; the fallback stays a
ordinary copy so behaviour is unchanged elsewhere.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run_clone_command(command: list[str]) -> bool:
    try:
        completed = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def clone_or_copy(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target``, preferring a copy-on-write clone."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        if _run_clone_command(["cp", "-c", "-p", str(source), str(target)]):
            return
    elif sys.platform.startswith("linux"):
        if _run_clone_command(["cp", "--reflink=auto", "-p", str(source), str(target)]):
            return
    target.unlink(missing_ok=True)
    shutil.copy2(source, target)
