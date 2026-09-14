from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import shutil
import sys
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .crawler import crawl_into_library
from .metadata import extract_metadata
from .naming import StructuredFileName, build_structured_file_name, infer_structured_file_name
from .rendering import render_preview
from .store import (
    CACHE_POLICIES,
    CACHE_POLICY_NAMES,
    CacheSettings,
    Item,
    LibraryStore,
    migrate_library_home,
    normalize_tag_names,
    safe_name,
    set_configured_library_home,
)


DOCUMENT_PATH_PATTERN = re.compile(r"^/api/documents/(?P<document_id>\d+)/content$")
DJVU_PREVIEW_PATH_PATTERN = re.compile(
    r"^/api/documents/(?P<document_id>\d+)/djvu-pages/(?P<page_number>[12])$"
)
EPUB_PREVIEW_PATH_PATTERN = re.compile(r"^/api/documents/(?P<document_id>\d+)/epub-preview$")
EPUB_PAGE_PATH_PATTERN = re.compile(
    r"^/api/documents/(?P<document_id>\d+)/epub-pages/(?P<page_number>[12])$"
)
FOLDER_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/contents$")
ITEM_OPEN_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/open$")
ITEM_LOCATION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/location$")
FOLDER_ACTION_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/(?P<action>rename|move|delete)$")
ITEM_ACTION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/(?P<action>rename|move|mirror|delete|delete-underlying)$")
DOCUMENT_ACTION_PATH_PATTERN = re.compile(r"^/api/documents/(?P<document_id>\d+)/(?P<action>replace|replace-text)$")


def item_payload(item: Item) -> dict[str, Any]:
    main_title = item.name_parts.main_title.strip() or Path(item.display_name).stem
    authors = item.name_parts.authors.strip()
    return {
        "id": item.id,
        "folderId": item.folder_id,
        "documentId": item.document_id,
        "displayName": item.display_name,
        "listName": f"{main_title} _ {authors}" if authors else main_title,
        "extension": item.name_parts.extension or item.stored_path.suffix.lower(),
        "size": item.size,
        "mime": item.mime,
        "note": item.note,
        "tags": list(item.tags),
        "updatedAt": item.updated_at,
        "nameParts": {
            "seriesAbbr": item.name_parts.series_abbr,
            "number": item.name_parts.number,
            "mainTitle": item.name_parts.main_title,
            "subtitle": item.name_parts.subtitle,
            "edition": item.name_parts.edition,
            "authors": item.name_parts.authors,
            "extension": item.name_parts.extension,
            "editionLanguage": item.name_parts.edition_language,
        },
    }


def name_parts_payload(parts: StructuredFileName) -> dict[str, str]:
    return {
        "seriesAbbr": parts.series_abbr,
        "number": parts.number,
        "mainTitle": parts.main_title,
        "subtitle": parts.subtitle,
        "edition": parts.edition,
        "authors": parts.authors,
        "extension": parts.extension,
        "editionLanguage": parts.edition_language,
    }


def name_parts_from_payload(payload: dict[str, Any], fallback_name: str = "") -> StructuredFileName:
    fallback = infer_structured_file_name(fallback_name) if fallback_name else StructuredFileName("", "", "", "", "1", "", "")
    return StructuredFileName(
        series_abbr=str(payload.get("seriesAbbr", fallback.series_abbr)),
        number=str(payload.get("number", fallback.number)),
        main_title=str(payload.get("mainTitle", fallback.main_title)),
        subtitle=str(payload.get("subtitle", fallback.subtitle)),
        edition=str(payload.get("edition", fallback.edition) or "1"),
        authors=str(payload.get("authors", fallback.authors)),
        extension=str(payload.get("extension", fallback.extension)),
        edition_language=str(payload.get("editionLanguage", fallback.edition_language) or "英文"),
    )


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], library_home: Path | None, token: str, static_dir: Path) -> None:
        super().__init__(address, BridgeRequestHandler)
        self.library_home = library_home
        self.token = token
        self.static_dir = static_dir.resolve()

    def open_store(self) -> LibraryStore:
        return LibraryStore(self.library_home)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Electron owns user-visible diagnostics; do not duplicate normal requests on stdout.
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api(parsed)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/") or not self._authorized(parsed):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        try:
            payload = self._read_json_body()
            self._handle_post(parsed.path, payload)
        except (OSError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # Keep unexpected errors in the Electron status area.
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求内容无效")
        return value

    def _handle_api(self, parsed: urllib.parse.ParseResult) -> None:
        if not self._authorized(parsed):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/shutdown":
            store = self.server.open_store()
            try:
                store.cleanup_caches("exit")
                store.cleanup_unreferenced_blobs()
            finally:
                store.close()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/tree":
            store = self.server.open_store()
            try:
                self._send_json(HTTPStatus.OK, {"folders": self._folder_tree(store, None)})
            finally:
                store.close()
            return
        if parsed.path == "/api/settings":
            self._send_settings()
            return
        if parsed.path == "/api/tags":
            self._send_tags()
            return
        if parsed.path == "/api/shortcuts":
            self._send_shortcuts()
            return
        folder_match = FOLDER_PATH_PATTERN.match(parsed.path)
        if folder_match:
            self._send_folder_contents(int(folder_match.group("folder_id")))
            return
        item_open_match = ITEM_OPEN_PATH_PATTERN.match(parsed.path)
        if item_open_match:
            self._send_open_copy(int(item_open_match.group("item_id")))
            return
        item_location_match = ITEM_LOCATION_PATH_PATTERN.match(parsed.path)
        if item_location_match:
            self._send_item_location(int(item_location_match.group("item_id")))
            return
        document_match = DOCUMENT_PATH_PATTERN.match(parsed.path)
        if document_match:
            self._send_document(int(document_match.group("document_id")))
            return
        djvu_match = DJVU_PREVIEW_PATH_PATTERN.match(parsed.path)
        if djvu_match:
            self._send_djvu_page(
                int(djvu_match.group("document_id")), int(djvu_match.group("page_number"))
            )
            return
        epub_preview_match = EPUB_PREVIEW_PATH_PATTERN.match(parsed.path)
        if epub_preview_match:
            self._send_epub_preview(int(epub_preview_match.group("document_id")))
            return
        epub_page_match = EPUB_PAGE_PATH_PATTERN.match(parsed.path)
        if epub_page_match:
            self._send_epub_page(
                int(epub_page_match.group("document_id")), int(epub_page_match.group("page_number"))
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_post(self, request_path: str, payload: dict[str, Any]) -> None:
        if request_path == "/api/folders":
            self._create_folder(payload)
            return
        if request_path == "/api/import/file":
            self._import_file(payload)
            return
        if request_path == "/api/import/folder":
            self._import_folder(payload)
            return
        if request_path == "/api/name-candidate":
            self._name_candidate(payload)
            return
        if request_path == "/api/name-parse":
            self._name_parse(payload)
            return
        if request_path == "/api/name-metadata":
            self._name_metadata(payload)
            return
        if request_path == "/api/search":
            self._search(payload)
            return
        if request_path == "/api/crawl":
            self._crawl(payload)
            return
        if request_path == "/api/settings/cache":
            self._save_cache_settings(payload)
            return
        if request_path == "/api/settings/cache/clear":
            self._clear_cache(payload)
            return
        if request_path == "/api/settings/data/migrate":
            self._migrate_data_home(payload)
            return
        if request_path == "/api/settings/shortcuts":
            self._save_shortcut(payload)
            return

        folder_match = FOLDER_ACTION_PATH_PATTERN.match(request_path)
        if folder_match:
            self._folder_action(int(folder_match.group("folder_id")), folder_match.group("action"), payload)
            return
        item_match = ITEM_ACTION_PATH_PATTERN.match(request_path)
        if item_match:
            self._item_action(int(item_match.group("item_id")), item_match.group("action"), payload)
            return
        document_match = DOCUMENT_ACTION_PATH_PATTERN.match(request_path)
        if document_match:
            self._document_action(int(document_match.group("document_id")), document_match.group("action"), payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _create_folder(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            folder_id = store.create_folder(int(payload["parentId"]), str(payload.get("name", "")))
            folder = store.get_folder(folder_id)
            self._send_json(HTTPStatus.CREATED, {"folder": {"id": folder.id, "parentId": folder.parent_id, "name": folder.name}})
        finally:
            store.close()

    def _folder_action(self, folder_id: int, action: str, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            if action == "rename":
                store.rename_folder(folder_id, str(payload.get("name", "")))
            elif action == "move":
                store.move_folder(folder_id, int(payload["targetFolderId"]))
            elif action == "delete":
                store.delete_folder(folder_id)
            self._send_json(HTTPStatus.OK, {"ok": True})
        finally:
            store.close()

    def _item_action(self, item_id: int, action: str, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            if action == "rename":
                item = store.get_item(item_id)
                store.rename_item(
                    item_id,
                    name_parts_from_payload(dict(payload.get("nameParts") or {}), item.display_name),
                    str(payload.get("note", "")),
                    normalize_tag_names(payload.get("tags", ())),
                )
            elif action == "move":
                store.move_item(item_id, int(payload["targetFolderId"]))
            elif action == "mirror":
                mirror_id = store.mirror_item(item_id, int(payload["targetFolderId"]))
                self._send_json(HTTPStatus.CREATED, {"itemId": mirror_id})
                return
            elif action == "delete":
                store.delete_item(item_id)
            elif action == "delete-underlying":
                self._send_json(HTTPStatus.OK, {"removedItemCount": store.delete_underlying_file(item_id)})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
        finally:
            store.close()

    def _document_action(self, document_id: int, action: str, payload: dict[str, Any]) -> None:
        if action == "replace-text":
            self._replace_document_text(document_id, payload)
            return
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取替换文件")
        store = self.server.open_store()
        try:
            result_id = store.replace_document_content(document_id, source)
            self._send_json(HTTPStatus.OK, {"documentId": result_id})
        finally:
            store.close()

    def _replace_document_text(self, document_id: int, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            name = str(payload.get("displayName", "edited.txt"))
            result_id = store.replace_document_text(document_id, str(payload.get("text", "")), name)
            self._send_json(HTTPStatus.OK, {"documentId": result_id})
        finally:
            store.close()

    def _name_candidate(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取文件")
        self._send_json(HTTPStatus.OK, {"nameParts": name_parts_payload(infer_structured_file_name(source.name))})

    def _name_parse(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", "")).strip()
        if not text:
            raise ValueError("请先输入要识别的名称")
        self._send_json(HTTPStatus.OK, {"nameParts": name_parts_payload(infer_structured_file_name(text))})

    def _name_metadata(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取文件")
        result = extract_metadata(source)
        self._send_json(HTTPStatus.OK, {"nameParts": name_parts_payload(result.parts), "extracted": result.extracted, "message": result.message})

    def _import_file(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取文件")
        store = self.server.open_store()
        try:
            parts = name_parts_from_payload(dict(payload.get("nameParts") or {}), source.name)
            item_id = store.add_local_file(
                source,
                int(payload["folderId"]),
                parts,
                str(payload.get("note", "")),
                normalize_tag_names(payload.get("tags", ())),
            )
            self._send_json(HTTPStatus.CREATED, {"item": item_payload(store.get_item(item_id))})
        finally:
            store.close()

    def _import_folder_tree(self, store: LibraryStore, source_dir: Path, parent_id: int, tags: tuple[str, ...]) -> tuple[int, int, int, list[str]]:
        root_id = store.create_folder(parent_id, source_dir.name)
        folder_ids = {source_dir: root_id}
        folder_count = 1
        file_count = 0
        errors: list[str] = []
        for directory_text, directory_names, file_names in os.walk(source_dir, topdown=True, followlinks=False):
            directory = Path(directory_text)
            target_id = folder_ids[directory]
            directory_names[:] = sorted(name for name in directory_names if not (directory / name).is_symlink())
            for name in directory_names:
                child = directory / name
                folder_ids[child] = store.create_folder(target_id, name)
                folder_count += 1
            for name in sorted(file_names):
                source = directory / name
                if source.is_symlink() or not source.is_file():
                    continue
                try:
                    store.add_local_file(source, target_id, infer_structured_file_name(source.name), tags=tags)
                    file_count += 1
                except (OSError, ValueError) as exc:
                    errors.append(f"{source.name}: {exc}")
        return root_id, folder_count, file_count, errors

    def _import_folder(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_dir():
            raise ValueError("无法读取文件夹")
        store = self.server.open_store()
        try:
            root_id, folder_count, file_count, errors = self._import_folder_tree(
                store, source, int(payload["folderId"]), normalize_tag_names(payload.get("tags", ()))
            )
            self._send_json(HTTPStatus.CREATED, {"folderId": root_id, "folderCount": folder_count, "fileCount": file_count, "errors": errors})
        finally:
            store.close()

    def _search(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            kind = str(payload.get("kind", ""))
            if kind == "files":
                results = store.search_unique_documents(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")))
            elif kind == "mirrors":
                results = store.search_mirror_items(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")))
            elif kind == "tags":
                results = store.search_tagged_items(str(payload.get("query", "")))
            else:
                raise ValueError("请选择搜索范围")
            self._send_json(HTTPStatus.OK, {"kind": kind, "results": [{"folderPath": folder_path, "item": item_payload(item)} for folder_path, item in results]})
        finally:
            store.close()

    def _crawl(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            result = crawl_into_library(store, str(payload.get("source", "")), int(payload["folderId"]))
            self._send_json(HTTPStatus.OK, {"folderId": result.folder_id, "folderName": result.folder_name, "downloaded": result.downloaded, "messages": result.messages})
        finally:
            store.close()

    def _send_settings(self) -> None:
        store = self.server.open_store()
        try:
            settings = store.cache_settings()
            usage = store.cache_usage()
            self._send_json(HTTPStatus.OK, {"dataHome": str(store.home), "cache": {"policy": settings.policy, "openDays": settings.open_days, "renderDays": settings.render_days, "maxBytes": settings.max_bytes, "targetBytes": settings.target_bytes, "clearOpenOnExit": settings.clear_open_on_exit, "usage": usage}})
        finally:
            store.close()

    def _send_tags(self) -> None:
        store = self.server.open_store()
        try:
            common, general = store.tags_by_frequency()
            self._send_json(HTTPStatus.OK, {"common": [{"name": tag.name, "count": tag.usage_count} for tag in common], "general": [{"name": tag.name, "count": tag.usage_count} for tag in general]})
        finally:
            store.close()

    def _save_cache_settings(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            settings = CacheSettings(str(payload.get("policy", "自定义")), int(payload.get("openDays", 0)), int(payload.get("renderDays", payload.get("previewDays", 0))), int(payload.get("maxBytes", 0)), int(payload.get("targetBytes", 0)), bool(payload.get("clearOpenOnExit", False)))
            store.set_cache_settings(settings)
            reclaimed = store.cleanup_caches("after_write")
            self._send_json(HTTPStatus.OK, {"reclaimed": reclaimed})
        finally:
            store.close()

    def _clear_cache(self, payload: dict[str, Any]) -> None:
        cache_name = str(payload.get("cache", ""))
        if cache_name not in {"open_cache", "render_cache"}:
            raise ValueError("未知缓存")
        store = self.server.open_store()
        try:
            self._send_json(HTTPStatus.OK, {"reclaimed": store.clear_cache(cache_name)})
        finally:
            store.close()

    def _migrate_data_home(self, payload: dict[str, Any]) -> None:
        target = Path(str(payload.get("path", ""))).expanduser().resolve()
        current_store = self.server.open_store()
        try:
            source = current_store.home.resolve()
        finally:
            current_store.close()
        if target == source:
            self._send_json(HTTPStatus.OK, {"dataHome": str(source), "migrated": False})
            return
        if target.exists() and any(target.iterdir()):
            raise ValueError("新的数据文件夹必须为空")
        set_configured_library_home(target)
        try:
            migrate_library_home(source, target)
        except Exception:
            set_configured_library_home(source)
            raise
        self.server.library_home = target
        self._send_json(HTTPStatus.OK, {"dataHome": str(target), "migrated": True})

    def _send_shortcuts(self) -> None:
        store = self.server.open_store()
        try:
            self._send_json(HTTPStatus.OK, {"shortcuts": store.shortcuts()})
        finally:
            store.close()

    def _save_shortcut(self, payload: dict[str, Any]) -> None:
        action = str(payload.get("action", ""))
        accelerator = str(payload.get("accelerator", ""))
        if not action:
            raise ValueError("快捷键操作不能为空")
        store = self.server.open_store()
        try:
            existing = store.shortcuts()
            if accelerator and any(key == accelerator and name != action for name, key in existing.items()):
                raise ValueError("该快捷键已被其他功能使用")
            store.set_shortcut(action, accelerator)
            self._send_json(HTTPStatus.OK, {"shortcuts": store.shortcuts()})
        finally:
            store.close()

    def _send_open_copy(self, item_id: int) -> None:
        store = self.server.open_store()
        try:
            item = store.get_item(item_id)
            cache_dir = store.prepare_cache_directory(
                "open_cache", f"electron-open-{uuid.uuid4().hex}"
            )
            file_name = safe_name(item.display_name)
            if not Path(file_name).suffix and item.stored_path.suffix:
                file_name += item.stored_path.suffix
            open_path = cache_dir / file_name
            shutil.copy2(item.stored_path, open_path)
            store.register_open_cache_copy(
                cache_dir, item.document_id, item.sha256, open_path.name
            )
            self._send_json(HTTPStatus.OK, {"path": str(open_path)})
        except (OSError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        finally:
            store.close()

    def _send_item_location(self, item_id: int) -> None:
        store = self.server.open_store()
        try:
            item = store.get_item(item_id)
            self._send_json(HTTPStatus.OK, {"path": str(item.stored_path)})
        except ValueError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        finally:
            store.close()

    def _folder_tree(self, store: LibraryStore, parent_id: int | None) -> list[dict[str, Any]]:
        return [
            {
                "id": folder.id,
                "parentId": folder.parent_id,
                "name": folder.name,
                "children": self._folder_tree(store, folder.id),
            }
            for folder in store.folders(parent_id)
        ]

    def _send_folder_contents(self, folder_id: int) -> None:
        store = self.server.open_store()
        try:
            folder = store.get_folder(folder_id)
            stats = store.folder_stats(folder_id)
            payload = {
                "folder": {"id": folder.id, "parentId": folder.parent_id, "name": folder.name},
                "stats": {
                    "folderCount": stats.folder_count,
                    "itemCount": stats.item_count,
                    "uniqueDocumentCount": stats.unique_document_count,
                    "uniqueSize": stats.unique_size,
                },
                "folders": [
                    {"id": child.id, "parentId": child.parent_id, "name": child.name}
                    for child in store.folders(folder_id)
                ],
                "items": [item_payload(item) for item in store.list_items(folder_id)],
            }
        except ValueError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Folder not found"})
        else:
            self._send_json(HTTPStatus.OK, payload)
        finally:
            store.close()

    def _send_document(self, document_id: int) -> None:
        store = self.server.open_store()
        try:
            row = store.conn.execute(
                """
                SELECT b.stored_path, b.mime, b.size
                FROM documents AS d
                JOIN blobs AS b ON b.sha256 = d.current_hash
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Document not found"})
                return
            path = Path(str(row["stored_path"]))
            if not path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Document content missing"})
                return
            self._stream_file(path, str(row["mime"]) or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        finally:
            store.close()

    def _stream_file(self, path: Path, mime: str) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        range_match = re.match(r"bytes=(\d*)-(\d*)$", range_header)
        status = HTTPStatus.OK
        if range_match:
            start_text, end_text = range_match.groups()
            if start_text:
                start = int(start_text)
            elif end_text:
                start = max(size - int(end_text), 0)
            if end_text and start_text:
                end = min(int(end_text), size - 1)
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self._send_cors_headers()
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_djvu_page(self, document_id: int, page_number: int) -> None:
        self._send_rendered_page(document_id, page_number)

    def _render_document_preview(self, store: LibraryStore, document_id: int):
        row = store.conn.execute(
            """
            SELECT b.sha256, b.stored_path
            FROM documents AS d
            JOIN blobs AS b ON b.sha256 = d.current_hash
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            return None, None
        result = render_preview(
            Path(str(row["stored_path"])),
            store.prepare_cache_directory("render_cache", str(row["sha256"])),
            pages=2,
        )
        return row, result

    def _send_rendered_page(self, document_id: int, page_number: int) -> None:
        store = self.server.open_store()
        try:
            row, result = self._render_document_preview(store, document_id)
            if row is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Document not found"})
                return
            if result.kind != "images" or page_number > len(result.paths):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": result.message})
                return
            self._stream_file(result.paths[page_number - 1], "image/png")
        finally:
            store.close()

    def _send_epub_preview(self, document_id: int) -> None:
        store = self.server.open_store()
        try:
            row, result = self._render_document_preview(store, document_id)
            if row is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Document not found"})
                return
            if result.kind == "images":
                self._send_json(HTTPStatus.OK, {"kind": "images", "pages": len(result.paths)})
                return
            self._send_json(HTTPStatus.OK, {"kind": "text", "text": result.text, "message": result.message})
        finally:
            store.close()

    def _send_epub_page(self, document_id: int, page_number: int) -> None:
        self._send_rendered_page(document_id, page_number)

    def _serve_static(self, url_path: str) -> None:
        requested = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
        candidate = (self.server.static_dir / requested).resolve()
        try:
            candidate.relative_to(self.server.static_dir)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = self.server.static_dir / "index.html"
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Electron frontend has not been built")
            return
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(candidate.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with candidate.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def _authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        query_token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        supplied = self.headers.get("X-BookManager-Token", query_token)
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in {"null", "http://localhost:5173", "http://127.0.0.1:5173"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "X-BookManager-Token, Range")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="BookManager Electron bridge")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-home")
    parser.add_argument("--static-dir", required=True)
    args = parser.parse_args()

    server = BridgeServer(
        ("127.0.0.1", args.port),
        Path(args.data_home).expanduser().resolve() if args.data_home else None,
        args.token,
        Path(args.static_dir).expanduser(),
    )
    print(json.dumps({"event": "ready", "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
