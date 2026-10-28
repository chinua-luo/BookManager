"""Multi-target import: one write transaction and durable, replayable receipts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .diagnostics import record_exception
from .file_security import validate_supported_document
from .naming import StructuredFileName, build_structured_file_name
from .responses import failure_details
from .store import LibraryStore, normalize_tag_names, sha256_file, utc_now

MAX_IMPORT_TARGETS = 100
REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_active: set[tuple[str, str]] = set()
_guard = threading.Lock()


class ImportProblem(ValueError):
    def __init__(self, code: str, message: str):
        self.import_error_type = code
        super().__init__(message)


def target_ids(payload: dict[str, Any]) -> list[int]:
    if 'folderIds' in payload and 'folderId' in payload:
        raise ImportProblem('invalid_targets', '请只使用一种目标文件夹选择方式。')
    raw = payload.get('folderIds') if 'folderIds' in payload else [payload.get('folderId')]
    if not isinstance(raw, list) or not raw:
        raise ImportProblem('invalid_targets', '请至少选择一个存放文件夹。')
    if any(type(value) is not int or value <= 0 for value in raw):
        raise ImportProblem('invalid_targets', '目标文件夹编号无效，请重新选择。')
    unique = list(dict.fromkeys(raw))
    if len(unique) > MAX_IMPORT_TARGETS:
        raise ImportProblem('invalid_targets', f'一次最多选择 {MAX_IMPORT_TARGETS} 个文件夹。')
    return unique


def validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ImportProblem('invalid_request_id', '导入请求标识无效，请重新打开导入窗口。')
    return value


def _key(store: LibraryStore, request_id: str) -> tuple[str, str]:
    return str(store.home.resolve()), request_id


def _row_result(row) -> dict[str, Any]:
    return {'requestId': row['request_id'], 'state': row['state'], **json.loads(row['result_json'])}


def import_status(store: LibraryStore, request_id: str) -> dict[str, Any]:
    validate_request_id(request_id)
    row = store.conn.execute('SELECT * FROM import_requests WHERE request_id = ?', (request_id,)).fetchone()
    if row is None:
        return {'requestId': request_id, 'state': 'not_found'}
    if row['state'] == 'pending':
        with _guard:
            active = _key(store, request_id) in _active
        alive = True
        if row['owner_pid'] != os.getpid():
            try:
                os.kill(row['owner_pid'], 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                pass
        elif not active:
            alive = False
        if not alive:
            failure = {'errorType': 'import_interrupted', 'message': '上次导入被中断，本次新增记录已回滚，请重新导入。'}
            with store.transaction():
                store.conn.execute("UPDATE import_requests SET state='failed', result_json=? WHERE request_id=? AND state='pending'", (json.dumps(failure, ensure_ascii=False), request_id))
            row = store.conn.execute('SELECT * FROM import_requests WHERE request_id=?', (request_id,)).fetchone()
    return _row_result(row)


def _check_targets(store: LibraryStore, folder_ids: list[int]) -> None:
    missing = [value for value in folder_ids if store.conn.execute('SELECT 1 FROM folders WHERE id=?', (value,)).fetchone() is None]
    if missing:
        raise ImportProblem('target_missing', f'目标文件夹已不存在（编号：{", ".join(map(str, missing))}），请重新选择。本次未新增镜像。')


def _safe_failure(error: Exception) -> dict[str, str]:
    if isinstance(error, ImportProblem):
        return {'errorType': error.import_error_type, 'message': str(error)}
    if isinstance(error, sqlite3.OperationalError) and ('locked' in str(error) or 'busy' in str(error)):
        return {'errorType': 'library_busy', 'message': '书库繁忙，本次未新增镜像，请稍后重试。'}
    details = failure_details(error, '/api/import/file')
    return {'errorType': details['error_type'], 'message': details['msg']}


def import_file(store: LibraryStore, source: Path, folder_ids: list[int], parts: StructuredFileName,
                note: str, tags, request_id: str | None = None, *, details: dict | None = None) -> dict[str, Any]:
    folder_ids = target_ids({'folderIds': folder_ids})
    request_id = validate_request_id(request_id if request_id is not None else uuid.uuid4().hex)
    details = details if details is not None else {}
    source = source.expanduser().absolute()
    tags = normalize_tag_names(tags)
    # Curated naming errors remain field-specific at the API boundary.
    build_structured_file_name(parts)
    fingerprint = hashlib.sha256(json.dumps({'path': str(source), 'folders': folder_ids, 'parts': parts.__dict__, 'note': note, 'tags': tags}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    key = _key(store, request_id)
    details.update(request_id=request_id, target_folder_ids=folder_ids, target_count=len(folder_ids), stage='validate')
    with _guard:
        if key in _active:
            details['replayed'] = True
            raise ImportProblem('import_pending', '此导入请求正在处理中，请确认结果后再操作。')
        _active.add(key)
    owned_blob: Path | None = None
    registered = False
    try:
        row = store.conn.execute('SELECT * FROM import_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is not None:
            details['replayed'] = True
            if row['fingerprint'] != fingerprint:
                raise ImportProblem('request_conflict', '同一请求标识对应的导入内容不同，请先确认原请求结果。')
            if row['state'] == 'success':
                if source.is_file():
                    if source.is_symlink() or sha256_file(source)[0] != row['content_hash']:
                        raise ImportProblem('request_conflict', '源文件内容已变化，请先确认原请求结果后重新导入。')
                details['replayed'] = True
                return json.loads(row['result_json'])['result']
            if row['state'] == 'failed':
                failure = json.loads(row['result_json'])
                raise ImportProblem(failure['errorType'], failure['message'])
            # Pending from another owner is never guessed to be a failure here.
            raise ImportProblem('import_pending', '上次导入结果仍待确认，请查询结果后再操作。')
        with store.transaction():
            _check_targets(store, folder_ids)
            store.conn.execute('INSERT INTO import_requests(request_id,fingerprint,owner_pid,state,created_at) VALUES(?,?,?,\'pending\',?)', (request_id, fingerprint, os.getpid(), utc_now()))
        registered = True
        if source.is_symlink() or not source.is_file():
            raise ImportProblem('file_unavailable', '源文件不存在、不可读取或为软链接，请重新选择。')
        validate_supported_document(source)
        details['stage'] = 'hash'
        before = source.stat()
        blob_hash, size = sha256_file(source)
        snapshot = (before.st_size, before.st_mtime_ns, before.st_ino)
        def unchanged():
            stat = source.stat()
            if snapshot != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                raise ImportProblem('source_changed', '源文件在导入期间发生变化，本次未新增镜像，请重新选择。')
        unchanged()
        details['stage'] = 'write'
        with store.transaction():
            _check_targets(store, folder_ids)
            blob = store.conn.execute('SELECT stored_path,mime,extension FROM blobs WHERE sha256=?', (blob_hash,)).fetchone()
            path = Path(blob['stored_path']) if blob else store.blob_dir / blob_hash[:2] / f'{blob_hash}{source.suffix.lower()}'
            if blob is None and not path.exists():
                owned_blob = path
            if blob is not None:
                store._ensure_blob(source, blob_hash, size, blob['mime'], blob['extension'], allow_rename=False)
            document_id = store._find_document_by_hash(blob_hash)
            original_ids = {int(row['id']) for row in store.conn.execute('SELECT id FROM items WHERE document_id=?', (document_id,))} if document_id else set()
            created = []; existing = []
            for folder_id in folder_ids:
                details['target_folder_id'] = folder_id
                found = store.conn.execute('SELECT id FROM items WHERE folder_id=? AND document_id=? ORDER BY id LIMIT 1', (folder_id, document_id)).fetchone() if document_id else None
                if found:
                    (existing if int(found['id']) in original_ids else created).append(int(found['id']))
                    continue
                # The first item also applies the existing article-tag rule, within
                # this same transaction; later targets reuse any auto-created item.
                if document_id is None or not created:
                    item_id = store.add_local_file(source, folder_id, parts, note, tags, _prepared=(blob_hash, size))
                    document_id = store.get_item(item_id).document_id
                else:
                    item_id = store.mirror_item(created[0], folder_id, parts, note, tags)
                created.append(item_id)
            # Even when all selected targets existed, article tagging follows its
            # existing semantics without replacing their names, notes or tags.
            auto = []
            if any(tag.casefold() == 'article' for tag in tags):
                articles = next((folder for folder in store.folders(1) if folder.name.casefold() == 'articles'), None)
                if articles is None:
                    article_folder = store.create_folder(1, 'Articles')
                else:
                    article_folder = articles.id
                previous = store.conn.execute('SELECT id FROM items WHERE folder_id=? AND document_id=? ORDER BY id LIMIT 1', (article_folder, document_id)).fetchone()
                if previous:
                    auto_id = int(previous['id'])
                    if not any(tag.casefold() == 'article' for tag in store.get_item(auto_id).tags):
                        store._replace_item_tags(auto_id, (*store.get_item(auto_id).tags, 'article'))
                else:
                    auto_id = store.mirror_item((created + existing)[0], article_folder, parts, note, tags)
                if article_folder not in folder_ids:
                    auto.append({'itemId': auto_id, 'folderId': article_folder, 'folderPath': store.folder_path(article_folder), 'created': auto_id not in original_ids})
            # Include the requested order, including items automatically created in
            # Articles by the first write, but distinguish pre-existing items.
            entries = []
            for folder_id in folder_ids:
                item_id = int(store.conn.execute('SELECT id FROM items WHERE folder_id=? AND document_id=? ORDER BY id LIMIT 1', (folder_id, document_id)).fetchone()['id'])
                entries.append({'itemId': item_id, 'folderId': folder_id, 'folderPath': store.folder_path(folder_id), 'created': item_id in created})
            unchanged()
            result = {'requestId': request_id, 'documentId': document_id, 'entries': entries, 'automatic': auto,
                      'stats': {'targetCount': len(folder_ids), 'created': len(created), 'existing': len(folder_ids) - len(created), 'automatic': len(auto)}}
            details.update(stage='commit', imported_file_count=1, created_mirror_count=len(created), existing_mirror_count=len(folder_ids)-len(created), automatic_mirror_count=len(auto))
            store.conn.execute("UPDATE import_requests SET state='success', content_hash=?, result_json=? WHERE request_id=?", (blob_hash, json.dumps({'result': result}, ensure_ascii=False), request_id))
        details['stage'] = 'complete'
        return result
    except Exception as error:
        # If COMMIT succeeded but the acknowledgement failed, the durable receipt
        # is authoritative. Never turn that success into a retryable failure.
        row = store.conn.execute('SELECT state,result_json FROM import_requests WHERE request_id=?', (request_id,)).fetchone()
        if registered and row is not None and row['state'] == 'success':
            return json.loads(row['result_json'])['result']
        if owned_blob is not None:
            try:
                with store.transaction():
                    if not store.conn.execute('SELECT 1 FROM blobs WHERE stored_path=?', (str(owned_blob),)).fetchone():
                        owned_blob.unlink(missing_ok=True)
            except Exception:
                record_exception('import.cleanup_failed', data_home=store.home, request_id=request_id)
        if registered:
            try:
                with store.transaction():
                    store.conn.execute("UPDATE import_requests SET state='failed', result_json=? WHERE request_id=? AND state='pending'", (json.dumps(_safe_failure(error), ensure_ascii=False), request_id))
            except Exception:
                record_exception('import.receipt_update_failed', data_home=store.home, request_id=request_id)
        if isinstance(error, sqlite3.OperationalError) and ('locked' in str(error) or 'busy' in str(error)):
            raise ImportProblem('library_busy', '书库繁忙，本次未新增镜像，请稍后重试。') from error
        raise
    finally:
        with _guard:
            _active.discard(key)
