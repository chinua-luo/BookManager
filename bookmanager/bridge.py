from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .crawler import crawl_into_library, new_crawl_stats
from .exporting import ExportProblem, export_folder
from .file_copy import clone_or_copy
from .file_security import ensure_safe_system_open
from .importing import ImportProblem, import_file, import_status, target_ids, validate_request_id
from .metadata import extract_metadata
from .naming import NamingValidationError, StructuredFileName, build_structured_file_name, infer_structured_file_name
from .operation_log import OperationLogger
from .diagnostics import diagnostic_scope, record_event, record_exception
from .responses import response_envelope
from .store import (
    CacheSettings,
    Item,
    LibraryStore,
    copy_library_home_atomically,
    delete_library_source_data,
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
DEFAULT_FOLDER_PAGE_SIZE = 200
MAX_FOLDER_PAGE_SIZE = 1000
FOLDER_IMPORT_BATCH_SIZE = 100
MAX_FOLDER_IMPORT_JOBS = 20
MAX_OPEN_CACHE_FILE_NAME_BYTES = 200
REQUEST_SOCKET_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 16
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
IMPORT_STATUS_PATTERN = re.compile(r"^/api/import/requests/(?P<request_id>[A-Za-z0-9_-]{1,80})$")
FOLDER_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/contents$")
ITEM_OPEN_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/open$")
ITEM_LOCATION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/location$")
FOLDER_ACTION_PATH_PATTERN = re.compile(r"^/api/folders/(?P<folder_id>\d+)/(?P<action>rename|move|delete|export)$")
ITEM_ACTION_PATH_PATTERN = re.compile(r"^/api/items/(?P<item_id>\d+)/(?P<action>rename|move|mirror|delete|delete-underlying)$")
FOLDER_IMPORT_PROGRESS_PATTERN = re.compile(r"^/api/import/folder/progress/(?P<job_id>[A-Za-z0-9_-]{1,80})$")
FOLDER_IMPORT_CANCEL_PATTERN = re.compile(r"^/api/import/folder/cancel/(?P<job_id>[A-Za-z0-9_-]{1,80})$")
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
    "export": ("folder.export", "导出文件夹"),
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


def truncate_open_cache_file_name(file_name: str, max_bytes: int = MAX_OPEN_CACHE_FILE_NAME_BYTES) -> str:
    """Keep cache copy names within filesystem byte limits without dropping the extension."""
    if len(file_name.encode("utf-8")) <= max_bytes:
        return file_name
    suffix = Path(file_name).suffix
    suffix_bytes = suffix.encode("utf-8")
    budget = max_bytes - len(suffix_bytes)
    if budget < 1:
        suffix = ""
        budget = max_bytes
    stem = file_name[: -len(suffix)] if suffix else file_name
    truncated = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").strip()
    return f"{truncated or 'bookmanager'}{suffix}"


class BridgeServer(ThreadingHTTPServer):
    # A handful of renderer windows plus startup probes never need dozens of
    # sockets; a larger listen backlog absorbs short bursts instead.
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], library_home: Path | None, token: str, static_dir: Path) -> None:
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, BridgeRequestHandler)
        self.library_home = library_home
        self.token = token
        self.static_dir = static_dir.resolve()
        try:
            initial_store = self.open_store(run_maintenance=True)
            try:
                self.library_home = initial_store.home
            finally:
                initial_store.close()
        except Exception:
            self.server_close()
            raise
        self.operation_logger = OperationLogger(self.library_home)
        self.capabilities: dict[str, bool] = {}
        self._folder_jobs: dict[str, dict[str, Any]] = {}
        self._folder_job_lock = threading.Lock()
        # Cache retention can delete gigabytes; do it after the window is usable.
        self._startup_cleanup_done = threading.Event()
        self._startup_cleanup_thread = threading.Thread(
            target=self._apply_startup_cache_policy,
            name="bookmanager-startup-cache",
            daemon=True,
        )
        self._startup_cleanup_thread.start()

    def process_request(self, request: Any, client_address: Any) -> None:
        # Backpressure: each accepted connection holds a slot until its handler
        # thread finishes, bounding memory and SQLite connections under bursts.
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def _apply_startup_cache_policy(self) -> None:
        try:
            store = self.open_store()
            try:
                store.cleanup_caches("startup")
            finally:
                store.close()
        except Exception as exc:
            record_exception('cache.startup_cleanup_failed', exc, data_home=self.library_home)
        finally:
            self._startup_cleanup_done.set()

    def wait_for_startup_cleanup(self, timeout: float | None = None) -> bool:
        return self._startup_cleanup_done.wait(timeout)

    def open_store(self, *, run_maintenance: bool = False) -> LibraryStore:
        return LibraryStore(self.library_home, run_maintenance=run_maintenance)

    def begin_folder_job(self, job_id: str) -> None:
        with self._folder_job_lock:
            finished = [key for key, job in self._folder_jobs.items() if job["state"] != "running"]
            while len(self._folder_jobs) >= MAX_FOLDER_IMPORT_JOBS and finished:
                self._folder_jobs.pop(finished.pop(0), None)
            previous = self._folder_jobs.get(job_id)
            cancel_requested = bool(previous and previous.get("state") == "running" and previous.get("cancelRequested"))
            self._folder_jobs[job_id] = {
                "jobId": job_id,
                "state": "running",
                "examined": 0,
                "imported": 0,
                "totalFiles": 0,
                "folderCount": 0,
                "errorCount": 0,
                "message": "",
                "cancelRequested": cancel_requested,
            }

    def update_folder_job(self, job_id: str, **fields: Any) -> None:
        with self._folder_job_lock:
            job = self._folder_jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def finish_folder_job(self, job_id: str, state: str, message: str = "") -> None:
        with self._folder_job_lock:
            job = self._folder_jobs.get(job_id)
            if job is not None:
                job.update(state=state, message=message)

    def folder_job_progress(self, job_id: str) -> dict[str, Any]:
        with self._folder_job_lock:
            job = self._folder_jobs.get(job_id)
            if job is None:
                return {"jobId": job_id, "state": "unknown", "examined": 0, "imported": 0,
                        "totalFiles": 0, "folderCount": 0, "errorCount": 0, "message": ""}
            return {key: value for key, value in job.items() if key != "cancelRequested"}

    def cancel_folder_job(self, job_id: str) -> bool:
        with self._folder_job_lock:
            job = self._folder_jobs.get(job_id)
            if job is None or job["state"] != "running":
                return False
            job["cancelRequested"] = True
            return True

    def folder_job_cancelled(self, job_id: str) -> bool:
        with self._folder_job_lock:
            job = self._folder_jobs.get(job_id)
            return bool(job and job.get("cancelRequested"))

    def log_operation(self, **record: Any) -> None:
        try:
            self.operation_logger.write(**record)
        except Exception as exc:
            record_exception("operation_log.write_failed", exc, data_home=self.library_home)

    def handle_error(self, request: Any, client_address: Any) -> None:
        record_exception('bridge.request_thread_failed', data_home=self.library_home)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    timeout = REQUEST_SOCKET_TIMEOUT_SECONDS

    def log_message(self, _format: str, *_args: object) -> None:
        # Electron owns user-visible diagnostics; do not duplicate normal requests on stdout.
        return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.FORBIDDEN)
        self._send_security_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._response_started = False
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            with diagnostic_scope(self.server.library_home):
                try:
                    self._handle_api(parsed)
                except (OSError, ValueError) as exc:
                    self._send_failure(exc, HTTPStatus.BAD_REQUEST)
                except Exception as exc:
                    self._send_failure(exc, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        with diagnostic_scope(self.server.library_home):
            try:
                self._serve_static(parsed.path)
            except Exception as exc:
                record_exception('bridge.static_failed', exc)
                if not getattr(self, '_response_started', False):
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, 'Frontend unavailable')

    def do_POST(self) -> None:  # noqa: N802
        self._response_started = False
        with diagnostic_scope(self.server.library_home):
            self._do_post()

    def _do_post(self) -> None:
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
            if parsed.path == '/api/import/file':
                details.update(request_id=payload.get('requestId'), target_folder_ids=payload.get('folderIds', [payload.get('folderId')]))
            document_name, context_details = self._operation_context(parsed.path, payload)
            details.update(context_details)
            self._operation_document_name = document_name
            self._operation_details = details
            self._handle_post(parsed.path, payload)
            document_name = self._operation_document_name
        except ExportProblem as exc:
            details["export_error_type"] = exc.export_error_type
            if not details.get("replayed"):
                self._log_request_operation(operation, started, "failure", document_name, details, exc)
            self._send_failure(exc, HTTPStatus(exc.http_status))
        except (OSError, ValueError) as exc:
            if not details.get("replayed"):
                self._log_request_operation(operation, started, "failure", document_name, details, exc)
            status = HTTPStatus.CONFLICT if getattr(exc, 'import_error_type', '') in {'request_conflict', 'import_pending'} else HTTPStatus.BAD_REQUEST
            self._send_failure(exc, status)
        except Exception as exc:  # Keep full diagnostics on disk; only curated messages cross IPC.
            if not details.get("replayed"):
                self._log_request_operation(operation, started, "failure", document_name, details, exc)
            self._send_failure(exc, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            failure = getattr(self, '_business_failure', '')
            if not details.get('replayed'):
                self._log_request_operation(operation, started, 'failure' if failure else 'success', document_name, details, ValueError(failure) if failure else None)

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
                try:
                    details["folder_name"] = store.get_folder(int(folder_match.group("folder_id"))).name
                except ValueError as error:
                    # Export has a dedicated 404 contract; other folder actions
                    # keep their existing generic folder failure mapping.
                    if folder_match.group("action") == "export":
                        raise ExportProblem(
                            "export_folder_missing",
                            "源文件夹不存在或已被删除，请刷新后重试。",
                            http_status=404,
                        ) from error
                    raise
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
        import_match = IMPORT_STATUS_PATTERN.match(parsed.path)
        if import_match:
            store = self.server.open_store()
            try:
                self._send_json(HTTPStatus.OK, import_status(store, import_match.group('request_id')))
            finally:
                store.close()
            return
        progress_match = FOLDER_IMPORT_PROGRESS_PATTERN.match(parsed.path)
        if progress_match:
            self._send_json(HTTPStatus.OK, self.server.folder_job_progress(progress_match.group("job_id")))
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/shutdown":
            store = self.server.open_store()
            try:
                for cleanup in (lambda: store.cleanup_caches("exit"), store.cleanup_unreferenced_blobs):
                    try:
                        cleanup()
                    except Exception as exc:
                        record_exception('cache.exit_cleanup_failed', exc)
                try:
                    store.mark_clean_shutdown()
                except OSError as exc:
                    record_exception('store.clean_shutdown_marker_failed', exc, data_home=store.home)
            finally:
                store.close()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/tree":
            store = self.server.open_store()
            try:
                self._send_json(HTTPStatus.OK, {"folders": self._folder_tree(store, None), "libraryId": store.conn.execute("SELECT library_id FROM library_identity WHERE id=1").fetchone()["library_id"]})
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
            offset, limit = self._folder_page_params(parsed.query)
            self._send_folder_contents(int(folder_match.group("folder_id")), offset, limit)
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
        cancel_match = FOLDER_IMPORT_CANCEL_PATTERN.match(request_path)
        if cancel_match:
            job_id = cancel_match.group("job_id")
            self._send_json(HTTPStatus.OK, {"jobId": job_id, "cancelRequested": self.server.cancel_folder_job(job_id)})
            return
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
        if request_path == "/api/name-validate":
            self._name_validate(payload)
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
            if action == "export":
                target_path = payload.get("targetPath")
                if not isinstance(target_path, str) or not target_path.strip():
                    raise ExportProblem("export_target_invalid", "请选择导出目标文件夹。")
                target_parent_name = Path(target_path.strip()).expanduser().name
                if target_parent_name:
                    self._operation_details["target_folder"] = target_parent_name
                result = export_folder(store, folder_id, target_path.strip())
                self._operation_details.update(
                    {
                        "file_count": result.file_count,
                        "folder_count": result.folder_count,
                        "copied_bytes": result.copied_bytes,
                    }
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "folderId": folder_id,
                        "folderName": result.target_path.name,
                        "targetPath": str(result.target_path),
                        "fileCount": result.file_count,
                        "folderCount": result.folder_count,
                        "copiedBytes": result.copied_bytes,
                    },
                )
            elif action == "rename":
                store.rename_folder(folder_id, str(payload.get("name", "")))
            elif action == "move":
                store.move_folder(folder_id, int(payload["targetFolderId"]))
            elif action == "delete":
                store.delete_folder(folder_id)
            else:
                raise ValueError("不支持的文件夹操作")
            if action != "export":
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
            saved = store.sync_open_cache_changes(strict=True)
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

    def _name_validate(self, payload: dict[str, Any]) -> None:
        parts = name_parts_from_payload(dict(payload.get("nameParts") or {}))
        try:
            name = build_structured_file_name(parts)
        except NamingValidationError as exc:
            self._send_json(HTTPStatus.OK, {"valid": False, "field": exc.field, "message": str(exc)})
        else:
            self._send_json(HTTPStatus.OK, {"valid": True, "name": name})

    def _name_metadata(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file():
            raise ValueError("无法读取文件")
        result = extract_metadata(source)
        self._send_json(HTTPStatus.OK, {"nameParts": name_parts_payload(result.parts), "extracted": result.extracted, "message": result.message})

    def _import_file(self, payload: dict[str, Any]) -> None:
        folder_ids = target_ids(payload)
        request_id = validate_request_id(payload['requestId']) if 'requestId' in payload else None
        source = Path(str(payload.get("path", ""))).expanduser()
        store = self.server.open_store()
        try:
            identity = store.conn.execute('SELECT library_id FROM library_identity WHERE id=1').fetchone()['library_id']
            if 'libraryId' in payload and payload['libraryId'] != identity:
                raise ImportProblem('library_changed', '当前书库已变化，请切回原书库确认上次导入结果。')
            parts = name_parts_from_payload(dict(payload.get("nameParts") or {}), source.name)
            result = import_file(store, source, folder_ids, parts, str(payload.get("note", "")),
                                 normalize_tag_names(payload.get("tags", ())), request_id,
                                 details=self._operation_details)
            # Receipt results are snapshots; replay remains valid even if a user
            # subsequently moved or deleted one of the returned items.
            items = []
            for entry in result['entries']:
                try:
                    items.append(item_payload(store.get_item(entry['itemId'])))
                except ValueError:
                    continue
            self._operation_document_name = build_structured_file_name(parts)
            try:
                self._send_json(HTTPStatus.CREATED, {**result, "items": items, "item": items[0] if items else None})
            except (BrokenPipeError, ConnectionResetError):
                record_exception('import.response_delivery_failed', request_id=result['requestId'])
        finally:
            store.close()

    def _scan_folder_tree(self, source_dir: Path) -> tuple[int, int]:
        total_files = 0
        total_folders = 1
        for directory_text, directory_names, file_names in os.walk(source_dir, topdown=True, followlinks=False):
            directory = Path(directory_text)
            directory_names[:] = sorted(name for name in directory_names if not (directory / name).is_symlink())
            total_folders += len(directory_names)
            total_files += sum(
                1 for name in file_names
                if not (directory / name).is_symlink() and (directory / name).is_file()
            )
            if total_folders > MAX_IMPORT_FOLDERS:
                raise ValueError(f"单次最多导入 {MAX_IMPORT_FOLDERS} 个文件夹")
            if total_files > MAX_IMPORT_FILES:
                raise ValueError(f"单次最多导入 {MAX_IMPORT_FILES} 个文件")
        return total_files, total_folders

    def _import_folder_tree(
        self,
        store: LibraryStore,
        source_dir: Path,
        parent_id: int,
        tags: tuple[str, ...],
        *,
        on_progress: Callable[..., None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[int, int, int, list[str]]:
        total_files, total_folders = self._scan_folder_tree(source_dir)
        root_id = store.create_folder(parent_id, source_dir.name)
        folder_ids = {source_dir: root_id}
        folder_count = 1
        file_count = 0
        examined_file_count = 0
        errors: list[str] = []
        pending: list[tuple[Path, int]] = []

        def progress(**fields: object) -> None:
            if on_progress is not None:
                on_progress(**fields)

        progress(totalFiles=total_files, folderCount=total_folders, examined=0, imported=0, errorCount=0)

        def flush_batch() -> None:
            nonlocal file_count
            if not pending:
                return
            batch = list(pending)
            pending.clear()
            with store.transaction():
                for source, target_id in batch:
                    try:
                        store.add_local_file(source, target_id, infer_structured_file_name(source.name), tags=tags)
                        file_count += 1
                    except (OSError, ValueError) as exc:
                        record_exception('import.file_skipped', exc, source_name=source.name)
                        errors.append('文件无法读取或内容无效，已跳过')
            progress(examined=examined_file_count, imported=file_count, errorCount=len(errors))

        try:
            for directory_text, directory_names, file_names in os.walk(source_dir, topdown=True, followlinks=False):
                directory = Path(directory_text)
                target_id = folder_ids[directory]
                directory_names[:] = sorted(name for name in directory_names if not (directory / name).is_symlink())
                for name in directory_names:
                    child = directory / name
                    folder_ids[child] = store.create_folder(target_id, name)
                    folder_count += 1
                for name in sorted(file_names):
                    if cancel_requested is not None and cancel_requested():
                        raise ImportProblem('import_cancelled', '已取消导入，本次未保留任何文件')
                    source = directory / name
                    if source.is_symlink() or not source.is_file():
                        continue
                    examined_file_count += 1
                    pending.append((source, target_id))
                    if len(pending) >= FOLDER_IMPORT_BATCH_SIZE:
                        flush_batch()
                progress(examined=examined_file_count, imported=file_count, folderCount=folder_count)
            flush_batch()
        except Exception:
            store.delete_folder(root_id)
            raise
        return root_id, folder_count, file_count, errors

    def _import_folder(self, payload: dict[str, Any]) -> None:
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_dir():
            raise ValueError("无法读取文件夹")
        job_id: str | None = None
        if "jobId" in payload:
            job_id = validate_request_id(str(payload.get("jobId")))
        on_progress = None
        cancel_requested = None
        if job_id is not None:
            self.server.begin_folder_job(job_id)
            on_progress = lambda **fields: self.server.update_folder_job(job_id, **fields)  # noqa: E731
            cancel_requested = lambda: self.server.folder_job_cancelled(job_id)  # noqa: E731
        store = self.server.open_store()
        try:
            root_id, folder_count, file_count, errors = self._import_folder_tree(
                store,
                source,
                int(payload["folderId"]),
                normalize_tag_names(payload.get("tags", ())),
                on_progress=on_progress,
                cancel_requested=cancel_requested,
            )
            self._operation_details.update({"folder_count": folder_count, "file_count": file_count, "error_count": len(errors)})
            if job_id is not None:
                self.server.finish_folder_job(job_id, "done", f"已导入 {file_count} 个文件")
            self._send_json(HTTPStatus.CREATED, {"folderId": root_id, "folderCount": folder_count, "fileCount": file_count, "errors": errors, "jobId": job_id})
        except ImportProblem as exc:
            if job_id is not None:
                state = "cancelled" if exc.import_error_type == "import_cancelled" else "failed"
                self.server.finish_folder_job(job_id, state, str(exc))
            raise
        except Exception:
            if job_id is not None:
                self.server.finish_folder_job(job_id, "failed", "导入失败")
            raise
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
                results, truncated = store.search_unique_documents(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")), MAX_SEARCH_RESULTS)
            elif kind == "mirrors":
                results, truncated = store.search_mirror_items(str(payload.get("mainTitle", "")), str(payload.get("subtitle", "")), str(payload.get("authors", "")), MAX_SEARCH_RESULTS)
            elif kind == "tags":
                results, truncated = store.search_tagged_items(str(payload.get("query", "")), MAX_SEARCH_RESULTS)
            else:
                raise ValueError("请选择搜索范围")
            results = results[:MAX_SEARCH_RESULTS]
            self._operation_details.update({"scope": kind, "match_count": len(results), "truncated": truncated})
            self._send_json(HTTPStatus.OK, {"kind": kind, "results": [{"folderPath": folder_path, "item": item_payload(item)} for folder_path, item in results], "truncated": truncated})
        finally:
            store.close()

    def _crawl(self, payload: dict[str, Any]) -> None:
        recursive_enabled = payload.get("enable_recursive") is True
        crawl_stats = new_crawl_stats()
        self._operation_details.update({"recursive_enabled": recursive_enabled, **crawl_stats})
        store = self.server.open_store()
        try:
            result = crawl_into_library(
                store,
                str(payload.get("source", "")),
                int(payload["folderId"]),
                follow_links=recursive_enabled,
                sandboxed_recursive_mode=recursive_enabled,
                crawl_stats=crawl_stats,
            )
            self._operation_details.update(
                {
                    "folder_name": result.folder_name,
                    "downloaded": result.downloaded,
                    "recursive_enabled": recursive_enabled,
                    **result.stats,
                }
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "folderId": result.folder_id,
                    "folderName": result.folder_name,
                    "downloaded": result.downloaded,
                    "messages": [
                        '所有下载链接均失败，已保存源页面 HTML。' if '所有下载链接均失败' in message
                        else '安全拦截：部分链接已跳过' if message.startswith('安全拦截')
                        else '跳过无法访问的链接' if message.startswith('跳过')
                        else '起始网址发生重定向，已按安全规则停止提取页面链接。' if message.startswith('起始网址发生重定向')
                        else '爬取处理记录已保存到本地日志'
                        for message in result.messages
                    ],
                    "stats": result.stats,
                    "recursiveEnabled": recursive_enabled,
                },
            )
        finally:
            self._operation_details.update(crawl_stats)
            store.close()

    def _send_settings(self) -> None:
        store = self.server.open_store()
        try:
            settings = store.cache_settings()
            usage = store.cache_usage()
            self._send_json(HTTPStatus.OK, {"dataHome": str(store.home), "cache": {"policy": settings.policy, "openDays": settings.open_days, "renderDays": settings.render_days, "maxBytes": settings.max_bytes, "targetBytes": settings.target_bytes, "clearOpenOnExit": settings.clear_open_on_exit, "usage": usage}, "capabilities": self.server.capabilities})
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
        target_existed = target.exists()
        copied = False
        configured = False
        try:
            # Copy first: a failure here leaves both the source and the config untouched.
            copied = copy_library_home_atomically(source, target)
            set_configured_library_home(target)
            configured = True
            # Only remove the old copy after the new library and its config are durable.
            delete_library_source_data(source)
        except Exception:
            if configured:
                try:
                    set_configured_library_home(source)
                except OSError as rollback_error:
                    record_exception('data_home.config_rollback_failed', rollback_error, data_home=source)
            if copied:
                try:
                    shutil.rmtree(target)
                except OSError as cleanup_error:
                    record_exception('data_home.target_cleanup_failed', cleanup_error, data_home=target)
                if target_existed:
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as restore_error:
                        record_exception('data_home.target_restore_failed', restore_error, data_home=target)
            raise
        self.server.library_home = target
        self.server.operation_logger.set_data_home(target)
        record_event("data_home.migrated", data_home=target)
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
            ensure_safe_system_open(item.display_name, str(item.stored_path))
            cache_dir = store.prepare_cache_directory(
                "open_cache", f"electron-open-{uuid.uuid4().hex}"
            )
            file_name = safe_name(item.display_name)
            if not Path(file_name).suffix and item.stored_path.suffix:
                file_name += item.stored_path.suffix
            file_name = truncate_open_cache_file_name(file_name)
            open_path = cache_dir / file_name
            clone_or_copy(item.stored_path, open_path)
            store.register_open_cache_copy(
                cache_dir, item.document_id, item.sha256, open_path.name
            )
            self._send_json(HTTPStatus.OK, {"path": str(open_path), "documentName": item.display_name})
        except (OSError, ValueError) as exc:
            self._send_failure(exc, HTTPStatus.BAD_REQUEST)
        finally:
            store.close()

    def _send_item_location(self, item_id: int) -> None:
        store = self.server.open_store()
        try:
            item = store.get_item(item_id)
            self._send_json(HTTPStatus.OK, {"path": str(item.stored_path), "documentName": item.display_name})
        except ValueError as exc:
            self._send_failure(exc, HTTPStatus.NOT_FOUND)
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

    def _folder_page_params(self, query: str) -> tuple[int, int]:
        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        try:
            offset = int(params.get("offset", ["0"])[0])
            limit = int(params.get("limit", [str(DEFAULT_FOLDER_PAGE_SIZE)])[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("分页参数无效") from exc
        if offset < 0 or limit < 1:
            raise ValueError("分页参数无效")
        return offset, min(limit, MAX_FOLDER_PAGE_SIZE)

    def _send_folder_contents(self, folder_id: int, offset: int = 0, limit: int = DEFAULT_FOLDER_PAGE_SIZE) -> None:
        store = self.server.open_store()
        try:
            folder = store.get_folder(folder_id)
            stats = store.folder_stats(folder_id)
            items, total = store.list_items_page(folder_id, offset, limit)
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
                "items": [item_payload(item) for item in items],
                "pagination": {
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "hasMore": offset + len(items) < total,
                },
            }
        except ValueError as exc:
            self._send_failure(exc, HTTPStatus.NOT_FOUND)
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

    def end_headers(self) -> None:
        self._response_started = True
        super().end_headers()

    def _send_failure(self, error: BaseException, status: HTTPStatus) -> None:
        export_type = getattr(error, 'export_error_type', '')
        rejection_type = getattr(error, 'import_error_type', '') or getattr(error, 'error_type', '') or export_type
        if isinstance(error, NamingValidationError) or rejection_type in {'invalid_targets', 'target_missing', 'invalid_request_id', 'request_conflict', 'import_pending', 'unsafe_file_type', 'export_conflict', 'export_target_invalid', 'export_source_missing', 'export_folder_missing'}:
            record_event('bridge.validation_rejected', data_home=self.server.library_home, request_path=urllib.parse.urlparse(self.path).path, reason=str(error))
        elif export_type == 'export_failed':
            # The service records export.failed with its traceback before raising;
            # avoid a second bridge-level record for the same failure.
            pass
        else:
            record_exception('bridge.request_failed', error, data_home=self.server.library_home, request_path=urllib.parse.urlparse(self.path).path)
        if not getattr(self, '_response_started', False):
            self._send_json(status, {}, error=error)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, error: BaseException | None = None) -> None:
        path = urllib.parse.urlparse(self.path).path
        if status >= 400 and error is None:
            record_event('bridge.request_rejected', data_home=self.server.library_home, request_path=path, status=int(status))
        envelope = response_envelope(int(status), payload, path, error=error)
        self._business_failure = envelope['msg'] if not envelope['ok'] else ''
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    from .startup import main as startup_main
    startup_main()


if __name__ == "__main__":
    main()
