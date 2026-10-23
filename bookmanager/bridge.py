from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .crawler import crawl_into_library
from .metadata import extract_metadata
from .naming import StructuredFileName, build_structured_file_name, infer_structured_file_name
from .operation_log import OperationLogger
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


MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_INPUT_STRING_LENGTH = 8192
MAX_SEARCH_QUERY_LENGTH = 256
MAX_IMPORT_FILES = 10_000
MAX_IMPORT_FOLDERS = 5_000
MAX_SEARCH_RESULTS = 500
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: bookmanager-document:",
        "font-src 'self' data:",
        "connect-src 'self' blob: bookmanager-document:",
        "worker-src 'self' blob:",
        "media-src 'self' blob: bookmanager-document:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    )
)


DOCUMENT_PATH_PATTERN = re.compile(r"^/api/documents/(?P<document_id>\d+)/content$")
FOLDER_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/contents$")
ITEM_OPEN_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/open$")
ITEM_LOCATION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/location$")
FOLDER_ACTION_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/(?P<action>rename|move|delete)$")
ITEM_ACTION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/(?P<action>rename|move|mirror|delete|delete-underlying)$")
DOCUMENT_ACTION_PATH_PATTERN = re.compile(r"^/api/documents/(?P<document_id>\d+)/(?P<action>replace)$")

POST_OPERATION_NAMES = {
    "/api/folders": ("folder.create", "新建文件夹"),
    "/api/import/file": ("file.import", "导入文件"),
    "/api/import/folder": ("folder.import", "导入文件夹"),
    "/api/search": ("library.search", "搜索资料库"),
    "/api/crawl": ("series.crawl", "爬取网络系列数据"),
    "/api/settings/cache": ("settings.cache.update", "修改缓存设置"),
    "/api/settings/cache/clear": ("cache.clear", "手动清理缓存"),
    "/api/open-cache/sync": ("open_cache.sync", "保存打开缓存中的文件改动"),
    "/api/settings/data/migrate": ("settings.data.migrate", "迁移书库数据位置"),
    "/api/settings/shortcuts": ("settings.shortcut.update", "修改快捷键设置"),
}
FOLDER_OPERATION_NAMES = {
    "rename": ("folder.rename", "重命名文件夹"),
    "move": ("folder.move", "移动文件夹"),
    "delete": ("folder.delete", "删除文件夹"),
}
ITEM_OPERATION_NAMES = {
    "rename": ("file.rename", "重命名文件"),
    "move": ("file.move", "移动镜像文件"),
    "mirror": ("file.mirror", "创建镜像文件"),
    "delete": ("file.delete", "删除镜像文件"),
    "delete-underlying": ("file.delete_underlying", "删除底层文件及全部镜像"),
}


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
        initial_store = self.open_store()
        try:
            self.library_home = initial_store.home
        finally:
            initial_store.close()
        self.operation_logger = OperationLogger(self.library_home)

    def open_store(self) -> LibraryStore:
        return LibraryStore(self.library_home)

    def log_operation(self, **record: Any) -> None:
        try:
            self.operation_logger.write(**record)
        except Exception as exc:
            print(f"BookManager operation log: {exc}", file=sys.stderr, flush=True)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Electron owns user-visible diagnostics; do not duplicate normal requests on stdout.
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.FORBIDDEN)
        self._send_security_headers()
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
        started = time.perf_counter()
        operation = self._operation_for_path(parsed.path)
        document_name = None
        details: dict[str, Any] = {}
        try:
            payload = self._read_json_body()
            document_name, details = self._operation_context(parsed.path, payload)
            self._operation_document_name = document_name
            self._operation_details = details
            self._handle_post(parsed.path, payload)
            document_name = self._operation_document_name
        except (OSError, ValueError) as exc:
            self._log_request_operation(operation, started, "failure", document_name, details, exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # Keep unexpected errors in the Electron status area.
            self._log_request_operation(operation, started, "failure", document_name, details, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        else:
            self._log_request_operation(operation, started, "success", document_name, details)

    def _operation_for_path(self, request_path: str) -> tuple[str, str] | None:
        if request_path == "/api/logs/events":
            return None
        if request_path in POST_OPERATION_NAMES:
            return POST_OPERATION_NAMES[request_path]
        folder_match = FOLDER_ACTION_PATH_PATTERN.match(request_path)
        if folder_match:
            return FOLDER_OPERATION_NAMES.get(folder_match.group("action"))
        item_match = ITEM_ACTION_PATH_PATTERN.match(request_path)
        if item_match:
            return ITEM_OPERATION_NAMES.get(item_match.group("action"))
        if DOCUMENT_ACTION_PATH_PATTERN.match(request_path):
            return "file.replace_underlying", "替换底层文件"
        return None

    def _operation_context(self, request_path: str, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        details: dict[str, Any] = {}
        item_match = ITEM_ACTION_PATH_PATTERN.match(request_path)
        if item_match:
            store = self.server.open_store()
            try:
                item = store.get_item(int(item_match.group("item_id")))
                return item.display_name, details
            finally:
                store.close()
        document_match = DOCUMENT_ACTION_PATH_PATTERN.match(request_path)
        if document_match:
            store = self.server.open_store()
            try:
                row = store.conn.execute(
                    "SELECT display_name FROM items WHERE document_id = ? ORDER BY id LIMIT 1",
                    (int(document_match.group("document_id")),),
                ).fetchone()
                return (str(row["display_name"]) if row else None), details
            finally:
                store.close()
        folder_match = FOLDER_ACTION_PATH_PATTERN.match(request_path)
        if folder_match:
            store = self.server.open_store()
            try:
                details["folder_name"] = store.get_folder(int(folder_match.group("folder_id"))).name
            finally:
                store.close()
        elif request_path == "/api/import/file":
            source = Path(str(payload.get("path", ""))).expanduser()
            parts = name_parts_from_payload(dict(payload.get("nameParts") or {}), source.name)
            return build_structured_file_name(parts), details
        elif request_path == "/api/import/folder":
            details["folder_name"] = Path(str(payload.get("path", ""))).name
        elif request_path == "/api/settings/cache/clear":
            details["cache"] = str(payload.get("cache", ""))
        return None, details

    def _log_request_operation(
        self,
        operation: tuple[str, str] | None,
        started: float,
        result: str,
        document_name: str | None,
        details: dict[str, Any],
        error: Exception | None = None,
    ) -> None:
        if operation is None:
            return
        event, message = operation
        self.server.log_operation(
            event=event,
            message=message,
            result=result,
            duration_ms=(time.perf_counter() - started) * 1000,
            document_name=document_name,
            error=str(error) if error else None,
            details=details or None,
        )

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("请求内容过大")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求内容无效")
        self._validate_json_value(value)
        return value

    def _validate_json_value(self, value: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError("请求内容嵌套过深")
        if isinstance(value, str):
            if len(value) > MAX_INPUT_STRING_LENGTH:
                raise ValueError("输入内容过长")
            return
        if isinstance(value, dict):
            if len(value) > 200:
                raise ValueError("请求字段过多")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 100:
                    raise ValueError("请求字段无效")
                self._validate_json_value(child, depth + 1)
            return
        if isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("请求条目过多")
            for child in value:
                self._validate_json_value(child, depth + 1)

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
        if request_path == "/api/open-cache/sync":
            self._sync_open_cache()
            return
        if request_path == "/api/settings/data/migrate":
            self._migrate_data_home(payload)
            return
        if request_path == "/api/settings/shortcuts":
            self._save_shortcut(payload)
            return
        if request_path == "/api/logs/events":
            self._record_external_operation(payload)
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

    def _record_external_operation(self, payload: dict[str, Any]) -> None:
        self.server.log_operation(
            event=str(payload.get("event", "")),
            message=str(payload.get("message", "")),
            result=str(payload.get("result", "")),
            duration_ms=float(payload.get("durationMs", 0)),
            document_name=str(payload.get("documentName", "")) or None,
            error=str(payload.get("error", "")) or None,
        )
        self._send_json(HTTPStatus.CREATED, {"ok": True})

    def _create_folder(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            folder_id = store.create_folder(int(payload["parentId"]), str(payload.get("name", "")))
            folder = store.get_folder(folder_id)
            self._operation_details["folder_name"] = folder.name
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
                self._operation_document_name = store.get_item(item_id).display_name
            elif action == "move":
                store.move_item(item_id, int(payload["targetFolderId"]))
                self._operation_document_name = store.get_item(item_id).display_name
            elif action == "mirror":
                mirror_id = store.mirror_item(item_id, int(payload["targetFolderId"]))
                self._operation_document_name = store.get_item(mirror_id).display_name
                self._operation_details["created_mirror_count"] = 1
                self._send_json(HTTPStatus.CREATED, {"itemId": mirror_id})
                return
            elif action == "delete":
                store.delete_item(item_id)
            elif action == "delete-underlying":
                removed = store.delete_underlying_file(item_id)
                self._operation_details["removed_mirror_count"] = removed
                self._send_json(HTTPStatus.OK, {"removedItemCount": removed})
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
        finally:
            store.close()

    def _document_action(self, document_id: int, action: str, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取替换文件")
        store = self.server.open_store()
        try:
            result_id = store.replace_document_content(document_id, source)
            self._send_json(HTTPStatus.OK, {"documentId": result_id})
        finally:
            store.close()

    def _sync_open_cache(self) -> None:
        store = self.server.open_store()
        try:
            saved = store.sync_open_cache_changes()
            reclaimed = store.cleanup_unreferenced_blobs()
            self._operation_details.update({"saved_file_count": saved, "reclaimed_bytes": reclaimed})
            self._send_json(HTTPStatus.OK, {"saved": saved, "reclaimed": reclaimed})
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
            item = store.get_item(item_id)
            self._operation_document_name = item.display_name
            self._operation_details["imported_file_count"] = 1
            self._send_json(HTTPStatus.CREATED, {"item": item_payload(item)})
        finally:
            store.close()

    def _import_folder_tree(self, store: LibraryStore, source_dir: Path, parent_id: int, tags: tuple[str, ...]) -> tuple[int, int, int, list[str]]:
        root_id = store.create_folder(parent_id, source_dir.name)
        folder_ids = {source_dir: root_id}
        folder_count = 1
        file_count = 0
        examined_file_count = 0
        errors: list[str] = []
        try:
            for directory_text, directory_names, file_names in os.walk(source_dir, topdown=True, followlinks=False):
                directory = Path(directory_text)
                target_id = folder_ids[directory]
                directory_names[:] = sorted(name for name in directory_names if not (directory / name).is_symlink())
                if folder_count + len(directory_names) > MAX_IMPORT_FOLDERS:
                    raise ValueError(f"单次最多导入 {MAX_IMPORT_FOLDERS} 个文件夹")
                if examined_file_count + len(file_names) > MAX_IMPORT_FILES:
                    raise ValueError(f"单次最多导入 {MAX_IMPORT_FILES} 个文件")
                examined_file_count += len(file_names)
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
        except Exception:
            store.delete_folder(root_id)
            raise
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
            self._operation_details.update({"folder_count": folder_count, "file_count": file_count, "error_count": len(errors)})
            self._send_json(HTTPStatus.CREATED, {"folderId": root_id, "folderCount": folder_count, "fileCount": file_count, "errors": errors})
        finally:
            store.close()

    def _search(self, payload: dict[str, Any]) -> None:
        for key in ("mainTitle", "subtitle", "authors", "query"):
            if len(str(payload.get(key, ""))) > MAX_SEARCH_QUERY_LENGTH:
                raise ValueError("搜索关键词不能超过 256 个字符")
        store = self.server.open_store()
        try:
            kind = str(payload.get("kind", ""))
            if kind == "files":
                results = store.search_unique_documents(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")), MAX_SEARCH_RESULTS)
            elif kind == "mirrors":
                results = store.search_mirror_items(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")), MAX_SEARCH_RESULTS)
            elif kind == "tags":
                results = store.search_tagged_items(str(payload.get("query", "")), MAX_SEARCH_RESULTS)
            else:
                raise ValueError("请选择搜索范围")
            results = results[:MAX_SEARCH_RESULTS]
            self._operation_details.update({"scope": kind, "match_count": len(results)})
            self._send_json(HTTPStatus.OK, {"kind": kind, "results": [{"folderPath": folder_path, "item": item_payload(item)} for folder_path, item in results]})
        finally:
            store.close()

    def _crawl(self, payload: dict[str, Any]) -> None:
        store = self.server.open_store()
        try:
            result = crawl_into_library(store, str(payload.get("source", "")), int(payload["folderId"]))
            self._operation_details.update({"folder_name": result.folder_name, "downloaded": result.downloaded})
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
            self._operation_details.update({"policy": settings.policy, "reclaimed_bytes": reclaimed})
            self._send_json(HTTPStatus.OK, {"reclaimed": reclaimed})
        finally:
            store.close()

    def _clear_cache(self, payload: dict[str, Any]) -> None:
        cache_name = str(payload.get("cache", ""))
        if cache_name not in {"open_cache", "render_cache"}:
            raise ValueError("未知缓存")
        store = self.server.open_store()
        try:
            reclaimed = store.clear_cache(cache_name)
            self._operation_details["reclaimed_bytes"] = reclaimed
            self._send_json(HTTPStatus.OK, {"reclaimed": reclaimed})
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
        self.server.operation_logger.set_data_home(target)
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
            self._send_json(HTTPStatus.OK, {"path": str(open_path), "documentName": item.display_name})
        except (OSError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        finally:
            store.close()

    def _send_item_location(self, item_id: int) -> None:
        store = self.server.open_store()
        try:
            item = store.get_item(item_id)
            self._send_json(HTTPStatus.OK, {"path": str(item.stored_path), "documentName": item.display_name})
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
                self._send_security_headers()
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
        self._send_security_headers()
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
        self._send_security_headers(include_csp=True)
        self.end_headers()
        with candidate.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def _authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        del parsed
        authorization = self.headers.get("Authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return False
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _send_security_headers(self, include_csp: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if include_csp:
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="BookManager Electron bridge")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--data-home")
    parser.add_argument("--static-dir", required=True)
    args = parser.parse_args()

    token = args.token or os.environ.get("BOOKMANAGER_BRIDGE_TOKEN", "")
    if len(token) < 32:
        parser.error("bridge token is missing or too short")

    server = BridgeServer(
        ("127.0.0.1", args.port),
        Path(args.data_home).expanduser().resolve() if args.data_home else None,
        token,
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
