from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


APP_NAME = "BookManager"
BUNDLE_IDENTIFIER = "com.bookmanager.app"
LOADER_DIR = Path("electron/runtime-loader")


def build_icns(source: Path, destination: Path) -> None:
    if shutil.which("sips") is None:
        raise OSError("macOS icon tools are unavailable")
    subprocess.run(
        ["sips", "-s", "format", "icns", str(source), "--out", str(destination)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def sign_app(bundle: Path) -> None:
    if shutil.which("codesign") is None:
        raise OSError("macOS code-signing tools are unavailable")
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def runtime_bundle(project_dir: Path) -> Path:
    return project_dir / "electron" / "runtime" / "darwin-arm64" / f"{APP_NAME}.app"


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def configure_runtime(
    project_dir: Path,
    python_executable: Path,
    *,
    icon_builder: Callable[[Path, Path], None],
    signer: Callable[[Path], None],
) -> Path:
    bundle = runtime_bundle(project_dir)
    contents = bundle / "Contents"
    resources = contents / "Resources"
    loader_source = project_dir / LOADER_DIR
    required = (
        project_dir / "book.png",
        project_dir / "electron" / "dist" / "index.html",
        bundle / "Contents" / "MacOS" / "Electron",
        contents / "Info.plist",
        loader_source / "loader.mjs",
        loader_source / "package.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise OSError("Missing BookManager launcher dependency: " + ", ".join(missing))
    if not python_executable.is_file():
        raise OSError(f"Python executable not found: {python_executable}")

    app_resources = resources / "app"
    app_resources.mkdir(parents=True, exist_ok=True)
    shutil.copy2(loader_source / "loader.mjs", app_resources / "loader.mjs")
    shutil.copy2(loader_source / "package.json", app_resources / "package.json")
    (app_resources / "bookmanager-config.json").write_text(
        json.dumps(
            {
                "projectDir": str(project_dir),
                "pythonExecutable": str(python_executable),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    icon_name = "BookManager.icns"
    icon_builder(project_dir / "book.png", resources / icon_name)
    with (contents / "Info.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    plist.update(
        {
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIconFile": icon_name,
            "CFBundleIdentifier": BUNDLE_IDENTIFIER,
            "CFBundleName": APP_NAME,
            "LSApplicationCategoryType": "public.app-category.productivity",
        }
    )
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=True)
    signer(bundle)
    return bundle


def create_launcher(
    project_dir: Path,
    output: Path,
    *,
    python_executable: Path | None = None,
    icon_builder: Callable[[Path, Path], None] = build_icns,
    signer: Callable[[Path], None] = sign_app,
) -> Path:
    project_dir = Path(project_dir).expanduser().resolve()
    output = Path(output).expanduser().absolute()
    python_executable = Path(python_executable or sys.executable).expanduser().resolve()
    bundle = configure_runtime(
        project_dir,
        python_executable,
        icon_builder=icon_builder,
        signer=signer,
    )

    staging = output.with_name(output.name + ".tmp")
    remove_path(staging)
    try:
        staging.symlink_to(bundle, target_is_directory=True)
        remove_path(output)
        staging.replace(output)
    except Exception:
        remove_path(staging)
        raise
    return output


def main() -> int:
    if sys.platform != "darwin":
        print("The BookManager .app shortcut can only be generated on macOS.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="Create the BookManager macOS desktop shortcut")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path.home() / "Desktop" / "BookManager.app")
    parser.add_argument("--python", dest="python_executable", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    try:
        created = create_launcher(args.project, args.output, python_executable=args.python_executable)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not create BookManager shortcut: {exc}", file=sys.stderr)
        return 1
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
