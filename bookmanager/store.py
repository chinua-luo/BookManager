from __future__ import annotations

import datetime as _dt
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .naming import (
    StructuredFileName,
    build_structured_file_name,
    normalize_structured_file_name,
    parse_structured_file_name,
)


PROJECT_LIBRARY_HOME = Path(__file__).resolve().parent.parent
DEFAULT_LIBRARY_HOME = PROJECT_LIBRARY_HOME / "BookManagerData"
SETTINGS_PATH = PROJECT_LIBRARY_HOME / "bookmanager_settings.json"
LEGACY_LIBRARY_HOME = Path.home() / "BookManagerData"
LIBRARY_DATA_NAMES = (
    "library.db",
    "library.db-wal",
    "library.db-shm",
    "library.db-journal",
    "blobs",
    "open_cache",
    "render_cache",
    "tmp",
)

CACHE_POLICIES = {
    "平衡模式": {
        "open_days": 7,
        "render_days": 30,
        "max_bytes": 4 * 1024**3,
        "target_bytes": 3 * 1024**3,
        "clear_open_on_exit": False,
    },
    "节省磁盘模式": {
        "open_days": 0,
        "render_days": 7,
        "max_bytes": 1024**3,
        "target_bytes": 768 * 1024**2,
        "clear_open_on_exit": True,
    },
    "预览优先模式": {
        "open_days": 14,
        "render_days": 90,
        "max_bytes": 10 * 1024**3,
        "target_bytes": 8 * 1024**3,
        "clear_open_on_exit": False,
    },
    "仅容量控制": {
        "open_days": 0,
        "render_days": 0,
        "max_bytes": 4 * 1024**3,
        "target_bytes": 3 * 1024**3,
        "clear_open_on_exit": False,
    },
    "完全手动": {
        "open_days": 0,
        "render_days": 0,
        "max_bytes": 0,
        "target_bytes": 0,
        "clear_open_on_exit": False,
    },
}
CACHE_POLICY_NAMES = tuple(CACHE_POLICIES)
OPEN_CACHE_MANIFEST = ".bookmanager-open.json"


def resolve_default_library_home() -> tuple[Path, bool]:
    """Use the installation data directory and migrate earlier library layouts once."""
    configured_home = os.environ.get("BOOK_MANAGER_HOME")
    if configured_home:
        return Path(configured_home).expanduser(), False

    target = configured_library_home() or DEFAULT_LIBRARY_HOME
    if target.joinpath("library.db").exists():
        return target, False

    for source in (PROJECT_LIBRARY_HOME, LEGACY_LIBRARY_HOME):
        if source == target or not source.joinpath("library.db").exists():
            continue
        try:
            migrate_library_home(source, target)
        except OSError:
            # Keep using the intact old library instead of opening an empty new one.
            # A later launch can retry the migration without changing the source.
            return source, False
        return target, True
    return target, False


def configured_library_home() -> Path | None:
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        configured_path = str(settings.get("data_home", "")).strip()
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return Path(configured_path).expanduser() if configured_path else None


def set_configured_library_home(home: Path) -> None:
    settings = {"data_home": str(home.expanduser().resolve())}
    temporary_path = SETTINGS_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(SETTINGS_PATH)


def migrate_library_home(source: Path, target: Path) -> None:
    """Move the complete data directory while preserving the installation files around it."""
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        return
    if not source.joinpath("library.db").exists():
        raise OSError(f"找不到资料库数据库：{source / 'library.db'}")
    if target.exists() and any(target.iterdir()):
        raise OSError(f"目标数据文件夹必须为空：{target}")

    target.mkdir(parents=True, exist_ok=True)
    copied_paths: list[Path] = []
    try:
        for name in LIBRARY_DATA_NAMES:
            source_path = source / name
            target_path = target / name
            if not source_path.exists():
                continue
            if source_path.is_dir():
                shutil.copytree(source_path, target_path)
            else:
                shutil.copy2(source_path, target_path)
            copied_paths.append(target_path)
        _rewrite_blob_paths_in_database(target / "library.db", source / "blobs", target / "blobs")
    except (OSError, sqlite3.Error) as exc:
        for copied_path in reversed(copied_paths):
            if copied_path.is_dir():
                shutil.rmtree(copied_path, ignore_errors=True)
            else:
                copied_path.unlink(missing_ok=True)
        try:
            target.rmdir()
        except OSError:
            pass
        raise OSError(f"迁移资料库失败：{exc}") from exc

    for name in LIBRARY_DATA_NAMES:
        source_path = source / name
        if source_path.is_dir():
            shutil.rmtree(source_path, ignore_errors=True)
        else:
            try:
                source_path.unlink(missing_ok=True)
            except OSError:
                pass


def _rewrite_blob_paths_in_database(db_path: Path, old_blob_dir: Path, new_blob_dir: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT sha256, stored_path FROM blobs").fetchall()
        updates: list[tuple[str, str]] = []
        for sha256, stored_path in rows:
            stored_blob_path = Path(stored_path)
            try:
                relative_path = stored_blob_path.relative_to(old_blob_dir)
            except ValueError:
                try:
                    relative_path = stored_blob_path.resolve().relative_to(old_blob_dir.resolve())
                except ValueError:
                    continue
            updates.append((str(new_blob_dir / relative_path), str(sha256)))
        if updates:
            conn.executemany("UPDATE blobs SET stored_path = ? WHERE sha256 = ?", updates)
            conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def safe_name(value: str, fallback: str = "Untitled") -> str:
    cleaned = "".join(ch if ch not in '<>:"/\\|?*\0' else "_" for ch in value).strip()
    cleaned = " ".join(cleaned.split())
    return cleaned[:180] or fallback


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class Folder:
    id: int
    parent_id: Optional[int]
    name: str


@dataclass(frozen=True)
class Item:
    id: int
    folder_id: int
    document_id: int
    display_name: str
    name_parts: StructuredFileName
    note: str
    tags: tuple[str, ...]
    sha256: str
    size: int
    mime: str
    stored_path: Path
    updated_at: str


@dataclass(frozen=True)
class FolderStats:
    folder_count: int
    item_count: int
    unique_document_count: int
    unique_size: int


@dataclass(frozen=True)
class CacheSettings:
    policy: str
    open_days: int
    render_days: int
    max_bytes: int
    target_bytes: int
    clear_open_on_exit: bool


@dataclass(frozen=True)
class TagInfo:
    name: str
    usage_count: int


def _normalize_note(value: str) -> str:
    return str(value).strip()[:2000]


def normalize_tag_names(values: Iterable[str] | str) -> tuple[str, ...]:
    raw_values = [values] if isinstance(values, str) else values
    tags: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        text = str(raw_value).replace("，", ",").replace("；", ",").replace(";", ",").replace("\n", ",")
        for part in text.split(","):
            name = " ".join(part.split())[:100]
            key = name.casefold()
            if name and key not in seen:
                tags.append(name)
                seen.add(key)
    return tuple(tags)


def _name_parts_to_json(parts: StructuredFileName, note: str = "") -> str:
    parts = normalize_structured_file_name(parts)
    return json.dumps(
        {
            "series_abbr": parts.series_abbr,
            "number": parts.number,
            "main_title": parts.main_title,
            "subtitle": parts.subtitle,
            "edition": parts.edition,
            "edition_language": parts.edition_language,
            "authors": parts.authors,
            "extension": parts.extension,
            "note": _normalize_note(note),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _name_parts_from_json(value: str) -> StructuredFileName:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        data = {}
    return StructuredFileName(
        series_abbr=str(data.get("series_abbr", "")),
        number=str(data.get("number", "")),
        main_title=str(data.get("main_title", "")),
        subtitle=str(data.get("subtitle", "")),
        edition=str(data.get("edition", "1")) or "1",
        authors=str(data.get("authors", "")),
        extension=str(data.get("extension", "")),
        edition_language=str(data.get("edition_language", "英文")),
    )


def _note_from_json(value: str) -> str:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ""
    return _normalize_note(data.get("note", ""))


def _legacy_name_parts(display_name: str) -> StructuredFileName:
    parsed = parse_structured_file_name(display_name)
    if parsed is not None:
        try:
            build_structured_file_name(parsed)
            return parsed
        except ValueError:
            pass
    path = Path(display_name)
    fallback = StructuredFileName(
        series_abbr="",
        number="",
        main_title=safe_name(path.stem or display_name, "Untitled").replace(" - ", " ").replace(" _ ", " "),
        subtitle="",
        edition="1",
        authors="",
        extension=path.suffix,
    )
    build_structured_file_name(fallback)
    return fallback


class LibraryStore:
    """SQLite-backed virtual folder tree over content-addressed local files."""

    def __init__(self, home: Optional[Path] = None) -> None:
        if home is None:
            default_home, self.migrated_legacy_library = resolve_default_library_home()
            self.home = default_home.expanduser()
        else:
            self.home = Path(home).expanduser()
            self.migrated_legacy_library = False
        self.blob_dir = self.home / "blobs"
        self.db_path = self.home / "library.db"
        self.home.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        if self.migrated_legacy_library:
            self._rewrite_legacy_blob_paths()

    def close(self) -> None:
        self.conn.close()

    def shortcuts(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT action_id, accelerator FROM shortcuts").fetchall()
        return {str(row["action_id"]): str(row["accelerator"]) for row in rows}

    def set_shortcut(self, action_id: str, accelerator: str) -> None:
        with self.conn:
            if accelerator:
                self.conn.execute(
                    "INSERT INTO shortcuts(action_id, accelerator) VALUES(?, ?) "
                    "ON CONFLICT(action_id) DO UPDATE SET accelerator = excluded.accelerator",
                    (action_id, accelerator),
                )
            else:
                self.conn.execute("DELETE FROM shortcuts WHERE action_id = ?", (action_id,))

    def tags_by_frequency(self, common_threshold: int = 3) -> tuple[list[TagInfo], list[TagInfo]]:
        rows = self.conn.execute(
            """
            SELECT t.name, COUNT(it.item_id) AS usage_count
            FROM tags t
            JOIN item_tags it ON it.tag_id = t.id
            GROUP BY t.id
            ORDER BY usage_count DESC, lower(t.name), t.name
            """
        ).fetchall()
        common: list[TagInfo] = []
        general: list[TagInfo] = []
        for row in rows:
            tag = TagInfo(str(row["name"]), int(row["usage_count"]))
            (common if tag.usage_count >= common_threshold else general).append(tag)
        return common, general

    def tag_names_for_item(self, item_id: int) -> tuple[str, ...]:
        rows = self.conn.execute(
            """
            SELECT t.name
            FROM tags t
            JOIN item_tags it ON it.tag_id = t.id
            WHERE it.item_id = ?
            ORDER BY lower(t.name), t.name
            """,
            (item_id,),
        ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    def _replace_item_tags(self, item_id: int, values: Iterable[str] | str) -> None:
        tag_names = normalize_tag_names(values)
        tag_ids: list[int] = []
        for name in tag_names:
            normalized_name = name.casefold()
            row = self.conn.execute("SELECT id FROM tags WHERE normalized_name = ?", (normalized_name,)).fetchone()
            if row is None:
                cursor = self.conn.execute(
                    "INSERT INTO tags(name, normalized_name) VALUES(?, ?)",
                    (name, normalized_name),
                )
                tag_ids.append(int(cursor.lastrowid))
            else:
                tag_ids.append(int(row["id"]))
        self.conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        self.conn.executemany(
            "INSERT INTO item_tags(item_id, tag_id) VALUES(?, ?)",
            [(item_id, tag_id) for tag_id in tag_ids],
        )
        self.conn.execute("DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM item_tags WHERE item_tags.tag_id = tags.id)")

    def cache_settings(self) -> CacheSettings:
        row = self.conn.execute(
            "SELECT policy, open_days, preview_days, max_bytes, target_bytes, clear_open_on_exit "
            "FROM cache_settings WHERE id = 1"
        ).fetchone()
        if row is None:
            defaults = CACHE_POLICIES["平衡模式"]
            return CacheSettings("平衡模式", **defaults)
        return CacheSettings(
            policy=str(row["policy"]),
            open_days=int(row["open_days"]),
            render_days=int(row["preview_days"]),
            max_bytes=int(row["max_bytes"]),
            target_bytes=int(row["target_bytes"]),
            clear_open_on_exit=bool(row["clear_open_on_exit"]),
        )

    def set_cache_settings(self, settings: CacheSettings) -> None:
        if settings.policy not in (*CACHE_POLICY_NAMES, "自定义"):
            raise ValueError("未知的缓存方案")
        values = (settings.open_days, settings.render_days, settings.max_bytes, settings.target_bytes)
        if any(value < 0 for value in values):
            raise ValueError("缓存天数和容量不能小于零")
        if settings.max_bytes and settings.target_bytes > settings.max_bytes:
            raise ValueError("回收目标容量不能大于缓存上限")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO cache_settings(
                    id, policy, open_days, preview_days, max_bytes, target_bytes, clear_open_on_exit
                ) VALUES(1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    policy = excluded.policy,
                    open_days = excluded.open_days,
                    preview_days = excluded.preview_days,
                    max_bytes = excluded.max_bytes,
                    target_bytes = excluded.target_bytes,
                    clear_open_on_exit = excluded.clear_open_on_exit
                """,
                (
                    settings.policy,
                    settings.open_days,
                    settings.render_days,
                    settings.max_bytes,
                    settings.target_bytes,
                    int(settings.clear_open_on_exit),
                ),
            )

    def cache_usage(self) -> dict[str, int]:
        return {
            name: self._cache_entry_size(self.home / name)
            for name in ("open_cache", "render_cache")
        }

    def prepare_cache_directory(self, cache_name: str, entry_name: str) -> Path:
        if cache_name not in {"open_cache", "render_cache"}:
            raise ValueError("未知的缓存目录")
        cache_dir = self.home / cache_name / entry_name
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.touch_cache_directory(cache_dir)
        return cache_dir

    def register_open_cache_copy(
        self,
        cache_dir: Path,
        document_id: int,
        source_hash: str,
        file_name: str,
    ) -> None:
        """Record which document an externally opened cache copy belongs to."""
        cache_dir = Path(cache_dir)
        manifest_path = cache_dir / OPEN_CACHE_MANIFEST
        manifest_path.write_text(
            json.dumps(
                {
                    "document_id": document_id,
                    "source_hash": source_hash,
                    "file_name": file_name,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def sync_open_cache_changes(self) -> int:
        """Write changed default-app copies back to their underlying documents."""
        cache_root = self.home / "open_cache"
        if not cache_root.is_dir():
            return 0

        changed_count = 0
        entries: list[tuple[float, Path]] = []
        for entry in cache_root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                entries.append((entry.stat().st_mtime, entry))
            except OSError:
                continue

        # Later-edited open copies win when the same document was opened twice.
        for _modified_at, entry in sorted(entries, key=lambda value: value[0]):
            if self._sync_open_cache_entry(entry):
                changed_count += 1
        return changed_count

    def _sync_open_cache_entry(self, cache_dir: Path) -> bool:
        try:
            manifest = json.loads((cache_dir / OPEN_CACHE_MANIFEST).read_text(encoding="utf-8"))
            document_id = int(manifest["document_id"])
            source_hash = str(manifest["source_hash"])
            file_name = str(manifest["file_name"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

        if Path(file_name).name != file_name:
            return False
        cached_path = cache_dir / file_name
        if not cached_path.is_file():
            return False
        try:
            cached_hash, _size = sha256_file(cached_path)
        except OSError:
            return False
        if cached_hash == source_hash:
            return False

        row = self.conn.execute(
            "SELECT current_hash FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None or cached_hash == str(row["current_hash"]):
            return False
        try:
            self.replace_document_content(document_id, cached_path)
        except (OSError, ValueError):
            return False
        return True

    def touch_cache_directory(self, cache_dir: Path) -> None:
        try:
            os.utime(cache_dir, None)
        except OSError:
            pass

    def clear_cache(self, cache_name: str) -> int:
        if cache_name not in {"open_cache", "render_cache"}:
            raise ValueError("未知的缓存目录")
        if cache_name == "open_cache":
            self.sync_open_cache_changes()
        cache_dir = self.home / cache_name
        freed_bytes = self._cache_entry_size(cache_dir)
        shutil.rmtree(cache_dir, ignore_errors=True)
        return freed_bytes

    def cleanup_caches(self, trigger: str) -> int:
        """Apply the configured automatic policy and return reclaimed bytes."""
        settings = self.cache_settings()
        if trigger == "exit":
            self.sync_open_cache_changes()
            return self.clear_cache("open_cache") if settings.clear_open_on_exit else 0
        if trigger not in {"startup", "after_write"} or settings.policy == "完全手动":
            return 0
        if trigger == "startup":
            self.sync_open_cache_changes()

        freed_bytes = 0
        now = time.time()
        for cache_name, retention_days in (
            ("open_cache", settings.open_days),
            ("render_cache", settings.render_days),
        ):
            if retention_days <= 0:
                continue
            cutoff = now - retention_days * 24 * 60 * 60
            for path, modified_at, size in self._cache_entries(cache_name):
                if modified_at < cutoff:
                    if cache_name == "open_cache":
                        self._sync_open_cache_entry(path)
                    freed_bytes += self._remove_cache_entry(path, size)

        usage = sum(self.cache_usage().values())
        if settings.max_bytes <= 0 or usage <= settings.max_bytes:
            return freed_bytes
        target_bytes = settings.target_bytes or settings.max_bytes
        for path, _modified_at, size in sorted(self._all_cache_entries(), key=lambda entry: entry[1]):
            if usage <= target_bytes:
                break
            if path.parent == self.home / "open_cache":
                self._sync_open_cache_entry(path)
            reclaimed = self._remove_cache_entry(path, size)
            freed_bytes += reclaimed
            usage -= reclaimed
        return freed_bytes

    def cleanup_unreferenced_blobs(self) -> int:
        """Remove Blob records and files that no current document references."""
        rows = self.conn.execute(
            """
            SELECT b.sha256, b.size, b.stored_path
            FROM blobs AS b
            LEFT JOIN documents AS d ON d.current_hash = b.sha256
            WHERE d.id IS NULL
            """
        ).fetchall()

        reclaimed_bytes = 0
        for row in rows:
            blob_hash = str(row["sha256"])
            stored_path = Path(str(row["stored_path"]))
            try:
                stored_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Keep the database entry if its file cannot be removed.
                continue

            with self.conn:
                deleted = self.conn.execute(
                    """
                    DELETE FROM blobs
                    WHERE sha256 = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM documents WHERE current_hash = ?
                      )
                    """,
                    (blob_hash, blob_hash),
                ).rowcount
            if deleted:
                reclaimed_bytes += int(row["size"])
        return reclaimed_bytes

    def _all_cache_entries(self) -> list[tuple[Path, float, int]]:
        return self._cache_entries("open_cache") + self._cache_entries("render_cache")

    def _cache_entries(self, cache_name: str) -> list[tuple[Path, float, int]]:
        root = self.home / cache_name
        if not root.is_dir():
            return []
        entries: list[tuple[Path, float, int]] = []
        for path in root.iterdir():
            if path.is_symlink():
                continue
            try:
                entries.append((path, path.stat().st_mtime, self._cache_entry_size(path)))
            except OSError:
                continue
        return entries

    @staticmethod
    def _cache_entry_size(path: Path) -> int:
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        if not path.is_dir():
            return 0
        size = 0
        for child in path.rglob("*"):
            if child.is_symlink() or not child.is_file():
                continue
            try:
                size += child.stat().st_size
            except OSError:
                continue
        return size

    @staticmethod
    def _remove_cache_entry(path: Path, size: int) -> int:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            return 0
        return size

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(parent_id, name)
            );

            CREATE TABLE IF NOT EXISTS blobs (
                sha256 TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mime TEXT NOT NULL,
                extension TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                current_hash TEXT NOT NULL REFERENCES blobs(sha256),
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(current_hash);

            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                display_name TEXT NOT NULL,
                name_data TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_items_document ON items(document_id);

            CREATE TABLE IF NOT EXISTS shortcuts (
                action_id TEXT PRIMARY KEY,
                accelerator TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cache_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                policy TEXT NOT NULL,
                open_days INTEGER NOT NULL,
                preview_days INTEGER NOT NULL,
                max_bytes INTEGER NOT NULL,
                target_bytes INTEGER NOT NULL,
                clear_open_on_exit INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS item_tags (
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(item_id, tag_id)
            );

            CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
            """
        )
        self._migrate_items_allow_duplicate_display_names()
        self._migrate_item_name_data()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO folders(id, parent_id, name, created_at)
            VALUES(1, NULL, 'Library', ?)
            """,
            (utc_now(),),
        )
        defaults = CACHE_POLICIES["平衡模式"]
        self.conn.execute(
            """
            INSERT OR IGNORE INTO cache_settings(
                id, policy, open_days, preview_days, max_bytes, target_bytes, clear_open_on_exit
            ) VALUES(1, '平衡模式', ?, ?, ?, ?, ?)
            """,
            (
                defaults["open_days"],
                defaults["render_days"],
                defaults["max_bytes"],
                defaults["target_bytes"],
                int(defaults["clear_open_on_exit"]),
            ),
        )
        self.conn.commit()

    def _migrate_item_name_data(self) -> None:
        columns = {str(row["name"]) for row in self.conn.execute("PRAGMA table_info(items)")}
        if "name_data" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN name_data TEXT NOT NULL DEFAULT ''")

        rows = self.conn.execute("SELECT id, display_name, name_data FROM items").fetchall()
        updates: list[tuple[str, int]] = []
        for row in rows:
            parts = _legacy_name_parts(row["display_name"]) if not row["name_data"] else _name_parts_from_json(row["name_data"])
            try:
                normalized = normalize_structured_file_name(parts)
                build_structured_file_name(normalized)
            except ValueError:
                normalized = _legacy_name_parts(row["display_name"])
            serialized = _name_parts_to_json(normalized, _note_from_json(row["name_data"]))
            if serialized != row["name_data"]:
                updates.append((serialized, row["id"]))
        if not updates:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE items SET name_data = ? WHERE id = ?",
                updates,
            )

    def _migrate_items_allow_duplicate_display_names(self) -> None:
        table_sql_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
        ).fetchone()
        table_sql = "" if table_sql_row is None else str(table_sql_row["sql"] or "")
        if "UNIQUE(folder_id, display_name)" not in table_sql.replace("\n", " "):
            return

        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.executescript(
                """
                BEGIN;
                CREATE TABLE items_rebuilt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    display_name TEXT NOT NULL,
                    name_data TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO items_rebuilt(id, folder_id, document_id, display_name, name_data, created_at, updated_at)
                SELECT id, folder_id, document_id, display_name, name_data, created_at, updated_at FROM items;
                DROP TABLE items;
                ALTER TABLE items_rebuilt RENAME TO items;
                CREATE INDEX idx_items_document ON items(document_id);
                COMMIT;
                """
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _rewrite_legacy_blob_paths(self) -> None:
        """Update absolute paths saved by the old user-home library after migration."""
        old_blob_dir = LEGACY_LIBRARY_HOME / "blobs"
        rows = self.conn.execute("SELECT sha256, stored_path FROM blobs").fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            stored_path = Path(row["stored_path"])
            try:
                relative_path = stored_path.relative_to(old_blob_dir)
            except ValueError:
                continue
            updates.append((str(self.blob_dir / relative_path), row["sha256"]))
        if updates:
            with self.conn:
                self.conn.executemany("UPDATE blobs SET stored_path = ? WHERE sha256 = ?", updates)

    def folders(self, parent_id: Optional[int]) -> list[Folder]:
        if parent_id is None:
            rows = self.conn.execute("SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM folders WHERE parent_id = ? ORDER BY name", (parent_id,)
            ).fetchall()
        return [Folder(row["id"], row["parent_id"], row["name"]) for row in rows]

    def get_folder(self, folder_id: int) -> Folder:
        row = self.conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if row is None:
            raise ValueError(f"Folder {folder_id} does not exist")
        return Folder(row["id"], row["parent_id"], row["name"])

    def folder_path(self, folder_id: int) -> str:
        names: list[str] = []
        current = self.get_folder(folder_id)
        while current:
            names.append(current.name)
            if current.parent_id is None:
                break
            current = self.get_folder(current.parent_id)
        return " / ".join(reversed(names))

    def folder_stats(self, folder_id: int) -> FolderStats:
        folder_ids = self._descendant_folder_ids(folder_id)
        placeholders = ",".join("?" for _ in folder_ids)
        item_count = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM items WHERE folder_id IN ({placeholders})",
            tuple(folder_ids),
        ).fetchone()["count"]
        rows = self.conn.execute(
            f"""
            SELECT d.id AS document_id, b.size
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            WHERE i.folder_id IN ({placeholders})
            GROUP BY d.id
            """,
            tuple(folder_ids),
        ).fetchall()
        return FolderStats(
            folder_count=max(len(folder_ids) - 1, 0),
            item_count=int(item_count),
            unique_document_count=len(rows),
            unique_size=sum(int(row["size"]) for row in rows),
        )

    def _descendant_folder_ids(self, folder_id: int) -> list[int]:
        ids = [folder_id]
        for child in self.folders(folder_id):
            ids.extend(self._descendant_folder_ids(child.id))
        return ids

    def create_folder(self, parent_id: int, name: str) -> int:
        name = safe_name(name, "New Folder")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO folders(parent_id, name, created_at) VALUES(?, ?, ?)",
                (parent_id, self._unique_folder_name(parent_id, name), now),
            )
        return int(cursor.lastrowid)

    def rename_folder(self, folder_id: int, new_name: str) -> None:
        folder = self.get_folder(folder_id)
        name = safe_name(new_name, "Folder")
        with self.conn:
            self.conn.execute(
                "UPDATE folders SET name = ? WHERE id = ?",
                (self._unique_folder_name(folder.parent_id, name, exclude_folder_id=folder_id), folder_id),
            )

    def move_folder(self, folder_id: int, new_parent_id: int) -> None:
        folder = self.get_folder(folder_id)
        self.get_folder(new_parent_id)
        if folder.parent_id is None:
            raise ValueError("根文件夹不能移动")
        if folder_id == new_parent_id or new_parent_id in self._descendant_folder_ids(folder_id):
            raise ValueError("不能将文件夹移动到自身或其子文件夹中")
        if folder.parent_id == new_parent_id:
            return
        name = self._unique_folder_name(new_parent_id, folder.name, exclude_folder_id=folder_id)
        with self.conn:
            self.conn.execute(
                "UPDATE folders SET parent_id = ?, name = ? WHERE id = ?",
                (new_parent_id, name, folder_id),
            )

    def delete_folder(self, folder_id: int) -> None:
        folder = self.get_folder(folder_id)
        if folder.parent_id is None:
            raise ValueError("根文件夹不能删除")
        with self.conn:
            self.conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    def _unique_folder_name(
        self,
        parent_id: Optional[int],
        wanted: str,
        exclude_folder_id: Optional[int] = None,
    ) -> str:
        if parent_id is None and exclude_folder_id is None:
            rows = self.conn.execute("SELECT name FROM folders WHERE parent_id IS NULL")
        elif parent_id is None:
            rows = self.conn.execute(
                "SELECT name FROM folders WHERE parent_id IS NULL AND id <> ?",
                (exclude_folder_id,),
            )
        elif exclude_folder_id is None:
            rows = self.conn.execute("SELECT name FROM folders WHERE parent_id = ?", (parent_id,))
        else:
            rows = self.conn.execute(
                "SELECT name FROM folders WHERE parent_id = ? AND id <> ?",
                (parent_id, exclude_folder_id),
            )
        existing = {row["name"] for row in rows}
        if wanted not in existing:
            return wanted
        base = wanted
        index = 2
        while f"{base} ({index})" in existing:
            index += 1
        return f"{base} ({index})"

    def _unique_item_name_parts(
        self,
        folder_id: int,
        parts: StructuredFileName,
        exclude_item_id: Optional[int] = None,
    ) -> tuple[str, StructuredFileName]:
        parts = normalize_structured_file_name(parts)
        return build_structured_file_name(parts), parts

    def list_items(self, folder_id: int) -> list[Item]:
        rows = self.conn.execute(
            """
            SELECT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size, b.mime,
                   b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            WHERE i.folder_id = ?
            ORDER BY i.display_name
            """,
            (folder_id,),
        ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def get_item(self, item_id: int) -> Item:
        row = self.conn.execute(
            """
            SELECT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size, b.mime,
                   b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            WHERE i.id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Item {item_id} does not exist")
        return self._item_from_row(row)

    def _item_from_row(self, row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            folder_id=row["folder_id"],
            document_id=row["document_id"],
            display_name=row["display_name"],
            name_parts=_name_parts_from_json(row["name_data"]),
            note=_note_from_json(row["name_data"]),
            tags=self.tag_names_for_item(int(row["id"])),
            sha256=row["sha256"],
            size=row["size"],
            mime=row["mime"],
            stored_path=Path(row["stored_path"]),
            updated_at=row["updated_at"],
        )

    def _coerce_name_parts(
        self,
        value: StructuredFileName | str | None,
        fallback_name: str,
    ) -> StructuredFileName:
        if isinstance(value, StructuredFileName):
            return value
        return _legacy_name_parts(value if isinstance(value, str) else fallback_name)

    def add_local_file(
        self,
        source_path: Path,
        folder_id: int,
        name_parts: StructuredFileName | str | None = None,
        note: str = "",
        tags: Iterable[str] | str = (),
    ) -> int:
        source_path = Path(source_path)
        blob_hash, size = sha256_file(source_path)
        mime = mimetypes.guess_type(str(source_path))[0] or "application/octet-stream"
        extension = source_path.suffix.lower()
        stored_path = self._ensure_blob(source_path, blob_hash, size, mime, extension)
        now = utc_now()

        with self.conn:
            document_id = self._find_document_by_hash(blob_hash)
            if document_id is None:
                cursor = self.conn.execute(
                    """
                    INSERT INTO documents(current_hash, original_name, created_at, updated_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (blob_hash, source_path.name, now, now),
                )
                document_id = int(cursor.lastrowid)
            parts = self._coerce_name_parts(name_parts, source_path.name)
            name, parts = self._unique_item_name_parts(folder_id, parts)
            cursor = self.conn.execute(
                """
                INSERT INTO items(folder_id, document_id, display_name, name_data, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (folder_id, document_id, name, _name_parts_to_json(parts, note), now, now),
            )
            self._replace_item_tags(int(cursor.lastrowid), tags)
        item_id = int(cursor.lastrowid)
        self._ensure_article_mirror(item_id)
        return item_id

    def _ensure_blob(self, source_path: Path, blob_hash: str, size: int, mime: str, extension: str) -> Path:
        existing = self.conn.execute(
            "SELECT stored_path FROM blobs WHERE sha256 = ?",
            (blob_hash,),
        ).fetchone()
        if existing is not None:
            stored_path = Path(existing["stored_path"])
            if extension and stored_path.exists() and not stored_path.suffix:
                renamed_path = stored_path.with_name(f"{stored_path.name}{extension}")
                if not renamed_path.exists():
                    stored_path.rename(renamed_path)
                    stored_path = renamed_path
                    with self.conn:
                        self.conn.execute(
                            "UPDATE blobs SET stored_path = ?, extension = ?, mime = ? WHERE sha256 = ?",
                            (str(stored_path), extension, mime, blob_hash),
                        )
            elif not stored_path.exists():
                stored_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, stored_path)
            return stored_path

        subdir = self.blob_dir / blob_hash[:2]
        subdir.mkdir(parents=True, exist_ok=True)
        stored_path = subdir / f"{blob_hash}{extension}"
        if not stored_path.exists():
            shutil.copy2(source_path, stored_path)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO blobs(sha256, size, mime, extension, stored_path, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (blob_hash, size, mime, extension, str(stored_path), utc_now()),
            )
        return stored_path

    def _find_document_by_hash(self, blob_hash: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM documents WHERE current_hash = ? ORDER BY id LIMIT 1", (blob_hash,)
        ).fetchone()
        return None if row is None else int(row["id"])

    def mirror_item(
        self,
        item_id: int,
        target_folder_id: int,
        name_parts: StructuredFileName | str | None = None,
        note: str | None = None,
        tags: Iterable[str] | str | None = None,
    ) -> int:
        item = self.get_item(item_id)
        parts = self._coerce_name_parts(name_parts, item.display_name) if name_parts is not None else item.name_parts
        now = utc_now()
        with self.conn:
            name, parts = self._unique_item_name_parts(target_folder_id, parts)
            cursor = self.conn.execute(
                """
                INSERT INTO items(folder_id, document_id, display_name, name_data, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    target_folder_id,
                    item.document_id,
                    name,
                    _name_parts_to_json(parts, item.note if note is None else note),
                    now,
                    now,
                ),
            )
            self._replace_item_tags(int(cursor.lastrowid), item.tags if tags is None else tags)
        return int(cursor.lastrowid)

    def _ensure_article_mirror(self, item_id: int) -> int | None:
        item = self.get_item(item_id)
        if not any(tag.casefold() == "article" for tag in item.tags):
            return None

        articles_folder = next(
            (folder for folder in self.folders(1) if folder.name.casefold() == "articles"),
            None,
        )
        articles_folder_id = articles_folder.id if articles_folder is not None else self.create_folder(1, "Articles")
        if item.folder_id == articles_folder_id:
            return None

        row = self.conn.execute(
            "SELECT id FROM items WHERE folder_id = ? AND document_id = ? ORDER BY id LIMIT 1",
            (articles_folder_id, item.document_id),
        ).fetchone()
        if row is not None:
            article_item_id = int(row["id"])
            article_item = self.get_item(article_item_id)
            if not any(tag.casefold() == "article" for tag in article_item.tags):
                with self.conn:
                    self._replace_item_tags(article_item_id, (*article_item.tags, "article"))
            return article_item_id
        return self.mirror_item(item.id, articles_folder_id)

    def rename_item(
        self,
        item_id: int,
        name_parts: StructuredFileName | str,
        note: str | None = None,
        tags: Iterable[str] | str | None = None,
    ) -> None:
        item = self.get_item(item_id)
        parts = self._coerce_name_parts(name_parts, item.display_name)
        with self.conn:
            name, parts = self._unique_item_name_parts(item.folder_id, parts, exclude_item_id=item_id)
            self.conn.execute(
                "UPDATE items SET display_name = ?, name_data = ?, updated_at = ? WHERE id = ?",
                (name, _name_parts_to_json(parts, item.note if note is None else note), utc_now(), item_id),
            )
            if tags is not None:
                self._replace_item_tags(item_id, tags)
        self._ensure_article_mirror(item_id)

    def delete_item(self, item_id: int) -> None:
        self.get_item(item_id)
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def delete_underlying_file(self, item_id: int) -> int:
        """Permanently remove a hash blob and every mirror that references it."""
        item = self.get_item(item_id)
        document_rows = self.conn.execute(
            "SELECT id FROM documents WHERE current_hash = ?",
            (item.sha256,),
        ).fetchall()
        document_ids = [int(row["id"]) for row in document_rows]
        if not document_ids:
            raise ValueError("未找到要删除的底层文档")

        placeholders = ",".join("?" for _ in document_ids)
        item_count = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM items WHERE document_id IN ({placeholders})",
                tuple(document_ids),
            ).fetchone()[0]
        )
        try:
            item.stored_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(f"无法删除底层文件：{exc}") from exc

        with self.conn:
            self.conn.execute(
                f"DELETE FROM items WHERE document_id IN ({placeholders})",
                tuple(document_ids),
            )
            self.conn.execute(
                f"DELETE FROM documents WHERE id IN ({placeholders})",
                tuple(document_ids),
            )
            self.conn.execute("DELETE FROM blobs WHERE sha256 = ?", (item.sha256,))
        return item_count

    def move_item(self, item_id: int, target_folder_id: int) -> None:
        item = self.get_item(item_id)
        name, parts = self._unique_item_name_parts(target_folder_id, item.name_parts, exclude_item_id=item_id)
        with self.conn:
            self.conn.execute(
                "UPDATE items SET folder_id = ?, display_name = ?, name_data = ?, updated_at = ? WHERE id = ?",
                (target_folder_id, name, _name_parts_to_json(parts, item.note), utc_now(), item_id),
            )

    def replace_document_content(self, document_id: int, source_path: Path) -> int:
        source_path = Path(source_path)
        blob_hash, size = sha256_file(source_path)
        mime = mimetypes.guess_type(str(source_path))[0] or "application/octet-stream"
        extension = source_path.suffix.lower()
        self._ensure_blob(source_path, blob_hash, size, mime, extension)
        existing_document_id = self._find_document_by_hash(blob_hash)
        now = utc_now()

        with self.conn:
            if existing_document_id is not None and existing_document_id != document_id:
                self.conn.execute(
                    "UPDATE items SET document_id = ?, updated_at = ? WHERE document_id = ?",
                    (existing_document_id, now, document_id),
                )
                self.conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
                return existing_document_id

            self.conn.execute(
                "UPDATE documents SET current_hash = ?, updated_at = ? WHERE id = ?",
                (blob_hash, now, document_id),
            )
            self.conn.execute(
                "UPDATE items SET updated_at = ? WHERE document_id = ?",
                (now, document_id),
            )
        return document_id

    def replace_document_text(self, document_id: int, text: str, preferred_name: str) -> int:
        suffix = Path(preferred_name).suffix or ".txt"
        tmp = self.home / "tmp"
        tmp.mkdir(exist_ok=True)
        path = tmp / safe_name(f"edited{suffix}")
        path.write_text(text, encoding="utf-8")
        try:
            return self.replace_document_content(document_id, path)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def search(self, keyword: str) -> list[tuple[str, Item]]:
        keyword = keyword.strip()
        if not keyword:
            return []
        pattern = f"%{keyword}%"
        rows = self.conn.execute(
            """
            SELECT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size, b.mime,
                   b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            WHERE i.display_name LIKE ?
            ORDER BY i.display_name
            """,
            (pattern,),
        ).fetchall()
        results: list[tuple[str, Item]] = [(self.folder_path(row["folder_id"]), self._item_from_row(row)) for row in rows]

        for item in self._all_unique_documents():
            if item.mime.startswith("text/") or item.stored_path.suffix.lower() in {".md", ".py", ".txt", ".csv", ".json", ".xml", ".html"}:
                try:
                    text = item.stored_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if keyword.lower() in text.lower():
                    path = self.folder_path(item.folder_id)
                    if not any(existing.id == item.id for _, existing in results):
                        results.append((path, item))
        return results

    def search_unique_documents(
        self,
        main_title: str = "",
        subtitle: str = "",
        authors: str = "",
    ) -> list[tuple[str, Item]]:
        filters = (main_title.strip().lower(), subtitle.strip().lower(), authors.strip().lower())
        rows = self.conn.execute(
            """
            SELECT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size, b.mime,
                   b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            ORDER BY i.display_name
            """
        ).fetchall()
        grouped: dict[int, list[Item]] = {}
        for row in rows:
            item = self._item_from_row(row)
            grouped.setdefault(item.document_id, []).append(item)

        results: list[tuple[str, Item]] = []
        for mirrors in grouped.values():
            matching_mirrors = [item for item in mirrors if structured_name_matches(item.name_parts, filters)]
            if not matching_mirrors:
                continue
            results.extend((self.folder_path(item.folder_id), item) for item in mirrors)

        return sorted(results, key=lambda result: (result[1].display_name.lower(), result[0].lower()))

    def search_mirror_items(
        self,
        main_title: str = "",
        subtitle: str = "",
        authors: str = "",
    ) -> list[tuple[str, Item]]:
        filters = (main_title.strip().lower(), subtitle.strip().lower(), authors.strip().lower())
        rows = self.conn.execute(
            """
            SELECT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size, b.mime,
                   b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            ORDER BY i.display_name
            """
        ).fetchall()
        results = [
            (self.folder_path(item.folder_id), item)
            for row in rows
            for item in [self._item_from_row(row)]
            if structured_name_matches(item.name_parts, filters)
        ]
        return sorted(results, key=lambda result: (result[1].display_name.lower(), result[0].lower()))

    def search_tagged_items(self, tag_query: str) -> list[tuple[str, Item]]:
        query = tag_query.strip()
        if not query:
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT i.id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size,
                   b.mime, b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            JOIN item_tags it ON it.item_id = i.id
            JOIN tags t ON t.id = it.tag_id
            WHERE lower(t.name) LIKE ?
            ORDER BY i.display_name
            """,
            (f"%{query.lower()}%",),
        ).fetchall()
        results = [(self.folder_path(row["folder_id"]), self._item_from_row(row)) for row in rows]
        return sorted(results, key=lambda result: (result[1].display_name.lower(), result[0].lower()))

    def _all_unique_documents(self) -> Iterable[Item]:
        rows = self.conn.execute(
            """
            SELECT MIN(i.id) AS id, i.folder_id, i.document_id, i.display_name, i.name_data, b.sha256, b.size,
                   b.mime, b.stored_path, d.updated_at
            FROM items i
            JOIN documents d ON d.id = i.document_id
            JOIN blobs b ON b.sha256 = d.current_hash
            GROUP BY i.document_id
            ORDER BY i.display_name
            """
        ).fetchall()
        return [self._item_from_row(row) for row in rows]


def structured_name_matches(parts: StructuredFileName, filters: tuple[str, str, str]) -> bool:
    values = (parts.main_title.lower(), parts.subtitle.lower(), parts.authors.lower())
    return all(not needle or needle in value for needle, value in zip(filters, values))
