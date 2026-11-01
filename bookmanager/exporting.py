"""Export a virtual folder tree to a real directory on disk.

The library keeps one content-addressed blob per document and virtual folder
tree in SQLite, so an export has to materialise the tree: every subfolder
(empty ones included) becomes a real directory and every item becomes a file
named by the full naming rule. A folder is exported first into a hidden
staging directory next to the target and renamed into place only when complete,
so an interrupted export never leaves a half-written folder behind.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import record_exception
from .file_copy import clone_or_copy
from .naming import build_structured_file_name
from .store import ExportItem, FolderExportSnapshot, Folder, LibraryStore


STAGING_PREFIX = ".bookmanager-export-"
STAGING_ATTEMPTS = 5
FALLBACK_FILE_NAME = "未命名"
MAX_FILE_NAME_LENGTH = 180
_WINDOWS_FORBIDDEN_CHARS = set('<>:"/\\|?*\0')


class ExportProblem(ValueError):
    """A curated export failure whose message is safe to show in the UI."""

    def __init__(self, error_type: str, message: str, *, http_status: int = 400) -> None:
        self.export_error_type = str(error_type)
        self.http_status = int(http_status)
        super().__init__(message)


@dataclass(frozen=True)
class ExportResult:
    target_path: Path
    file_count: int
    folder_count: int
    copied_bytes: int


def export_folder(store: LibraryStore, folder_id: int, target_parent: str | Path) -> ExportResult:
    """Recursively export ``folder_id`` into ``target_parent/<folder.name>``.

    Never overwrites an existing target folder, never follows a source symlink
    and never copies anything outside the library data directory. Expected
    problems raise :class:`ExportProblem`; resource errors keep their native
    ``OSError``/``PermissionError`` type so the HTTP layer can map them.
    """
    folder = _source_folder(store, folder_id)
    snapshot = store.folder_export_snapshot(folder.id)
    parent = _target_parent(store, target_parent)
    # The stored folder name becomes the exported directory name, so it gets the
    # same traversal checks as every nested folder name.
    target = _inside(parent, (folder.name,))
    if target.exists() or target.is_symlink():
        raise _conflict(folder)
    for entry in snapshot.folders:
        _inside(parent, entry.relative_parts)  # Validates every exported folder name.
    if snapshot.missing_files:
        raise ExportProblem(
            "export_source_missing",
            f"书库中有 {len(snapshot.missing_files)} 个底层文件缺失，本次未导出，请先修复书库后重试。",
        )
    _validate_sources(store, snapshot)

    staging: Path | None = None
    try:
        staging = _create_staging(parent)
        _write_tree(snapshot, staging)
        _commit_staging(staging, target, folder)
    except BaseException as error:
        if staging is not None:
            _discard_staging(staging, store=store, folder_id=folder.id, target_name=folder.name)
        if not isinstance(error, Exception):
            raise
        _raise_export_failure(store, folder, error)

    return ExportResult(
        target_path=target,
        file_count=len(snapshot.items),
        folder_count=len(snapshot.folders),
        copied_bytes=sum(entry.size for entry in snapshot.items),
    )


def _source_folder(store: LibraryStore, folder_id: int) -> Folder:
    try:
        return store.get_folder(int(folder_id))
    except (TypeError, ValueError) as error:
        raise ExportProblem(
            "export_folder_missing",
            "源文件夹不存在或已被删除，请刷新后重试。",
            http_status=404,
        ) from error


def _target_parent(store: LibraryStore, value: str | Path) -> Path:
    raw = str(value).strip()
    if not raw:
        raise ExportProblem("export_target_invalid", "请选择导出目标文件夹。")
    try:
        parent = Path(raw).expanduser()
        parent = parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ExportProblem("export_target_invalid", "导出目标文件夹不存在或无法访问，请重新选择。") from error
    if not parent.is_dir():
        raise ExportProblem("export_target_invalid", "导出目标必须是文件夹，请重新选择。")
    home = store.home.expanduser().resolve()
    if parent == home or parent.is_relative_to(home):
        raise ExportProblem("export_target_invalid", "导出目标不能位于书库数据目录内，请选择其他文件夹。")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ExportProblem("export_target_invalid", "导出目标文件夹不可写入，请检查访问权限后重试。")
    return parent


def _validate_sources(store: LibraryStore, snapshot: FolderExportSnapshot) -> None:
    """Refuse symlinked, missing or out-of-library blobs before anything is copied."""
    home = store.home.expanduser().resolve()
    for entry in snapshot.items:
        path = entry.stored_path
        if path.is_symlink():
            raise ExportProblem("export_source_missing", "书库中有底层文件为软链接，已停止导出以保护数据。")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ExportProblem(
                "export_source_missing",
                "书库中有底层文件缺失或不可读取，本次未导出，请先修复书库后重试。",
            ) from error
        if resolved != home and not resolved.is_relative_to(home):
            raise ExportProblem("export_source_missing", "书库文件位置异常，已停止导出以保护数据。")
        if not resolved.is_file():
            raise ExportProblem(
                "export_source_missing",
                "书库中有底层文件缺失或不可读取，本次未导出，请先修复书库后重试。",
            )


def _create_staging(parent: Path) -> Path:
    for _attempt in range(STAGING_ATTEMPTS):
        staging = parent / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        try:
            staging.mkdir()
        except FileExistsError:
            continue
        return staging
    raise ExportProblem("export_failed", "导出失败，请在目标文件夹中清理残留文件后重试。", http_status=500)


def _write_tree(snapshot: FolderExportSnapshot, staging: Path) -> None:
    """Materialise empty folders first, then copy every item exactly once."""
    for entry in snapshot.folders:
        _inside(staging, entry.relative_parts).mkdir(parents=True, exist_ok=True)
    used_names: dict[tuple[str, ...], set[str]] = {}
    for entry in snapshot.items:
        names = used_names.setdefault(entry.relative_parts, set())
        name = _unique_file_name(names, _item_file_name(entry))
        clone_or_copy(entry.stored_path, _inside(staging, entry.relative_parts) / name)


def _inside(root: Path, relative_parts: tuple[str, ...]) -> Path:
    """Join a stored folder path while refusing anything that could escape root."""
    path = root
    for part in relative_parts:
        path = path / _safe_folder_name(part)
    return path


def _safe_folder_name(part: str) -> str:
    if part in {"", ".", ".."} or any(character in _WINDOWS_FORBIDDEN_CHARS for character in part):
        raise ExportProblem("export_failed", "书库中的文件夹名称无法用于导出，请先重命名后重试。", http_status=500)
    return part


def _item_file_name(entry: ExportItem) -> str:
    """Prefer the full naming rule; fall back to the stored display name."""
    try:
        name = build_structured_file_name(entry.name_parts)
    except ValueError:
        name = ""
    if not name:
        name = _fallback_file_name(entry.display_name, entry.stored_path.suffix)
    return name


def _fallback_file_name(display_name: str, suffix: str) -> str:
    cleaned = "".join(
        "_" if ord(character) < 32 or character in _WINDOWS_FORBIDDEN_CHARS else character
        for character in str(display_name or "")
    )
    name = " ".join(cleaned.split())[:MAX_FILE_NAME_LENGTH]
    suffix = str(suffix or "")
    if suffix and not name.casefold().endswith(suffix.casefold()):
        name = f"{name}{suffix}"
    if name in {"", ".", ".."}:
        # Never let a degenerate legacy display name address the directory itself.
        return f"{FALLBACK_FILE_NAME}{suffix}"
    return name


def _unique_file_name(used: set[str], name: str) -> str:
    """Append `` (2)``, `` (3)`` … before the extension until the name is free."""
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    path = Path(name)
    stem, suffix = (path.stem, path.suffix) if path.stem else (name, "")
    index = 2
    while True:
        candidate = f"{stem} ({index}){suffix}"
        if candidate.casefold() not in used:
            used.add(candidate.casefold())
            return candidate
        index += 1


def _commit_staging(staging: Path, target: Path, folder: Folder) -> None:
    try:
        os.replace(staging, target)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR}:
            raise _conflict(folder) from error
        raise


def _conflict(folder: Folder) -> ExportProblem:
    return ExportProblem(
        "export_conflict",
        f"目标位置已存在同名文件夹「{folder.name}」，请更换位置或先重命名后重试。",
        http_status=409,
    )


def _discard_staging(staging: Path, *, store: LibraryStore, folder_id: int, target_name: str) -> None:
    if not staging.exists() and not staging.is_symlink():
        # Already renamed into place or removed by the filesystem; nothing to clean.
        return
    try:
        if staging.is_symlink() or not staging.is_dir():
            staging.unlink(missing_ok=True)
        else:
            shutil.rmtree(staging)
    except OSError as error:
        # Only the folder id and the target name are safe to record; the staging
        # path itself may contain the user's chosen target directory.
        record_exception("export.cleanup_failed", error, data_home=store.home, folder_id=folder_id, target_name=target_name)


def _raise_export_failure(store: LibraryStore, folder: Folder, error: BaseException) -> None:
    if isinstance(error, ExportProblem):
        raise error
    # Keep resource errors typed so the existing response mapping can turn them
    # into "disk full" / "permission denied" without exposing raw text.
    if isinstance(error, PermissionError):
        raise error
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        raise error
    record_exception("export.failed", error, data_home=store.home, folder_id=folder.id, folder_name=folder.name)
    raise ExportProblem("export_failed", "导出失败，请稍后重试或更换目标文件夹。", http_status=500) from error
