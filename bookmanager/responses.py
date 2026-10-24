"""The sole JSON response contract; raw exceptions belong only in diagnostics."""
from __future__ import annotations

import errno
from typing import Any


def failure_details(error: BaseException | str, path: str = '', status: int = 400) -> dict[str, str]:
    # Inspect types and known validations, never expose arbitrary exception text.
    text = str(error)
    code = getattr(error, 'errno', None)
    if code == errno.ENOSPC:
        kind, msg = 'disk_full', '剩余空间不足，无法完成本次操作'
    elif isinstance(error, PermissionError) or code in {errno.EACCES, errno.EPERM}:
        kind, msg = 'permission_denied', '无法访问所需文件，请检查文件夹权限'
    elif status == 401:
        kind, msg = 'request_rejected', '本地请求未通过安全检查，请重启软件后重试'
    elif status == 404:
        kind, msg = 'not_found', '请求的文件、文件夹或功能不存在，请刷新后重试'
    elif '请求内容过大' in text:
        kind, msg = 'request_too_large', '请求内容过大，请减少输入内容后重试'
    elif 'UnsafeUrl' in type(error).__name__:
        kind, msg = 'unsafe_address', '目标地址属于内网 / 回环地址或不安全地址，已被安全策略拦截'
    elif '不支持' in text and '格式' in text:
        kind, msg = 'unsupported_format', '该文件格式不支持，无法导入书库'
    elif isinstance(error, FileNotFoundError) or text in {'无法读取文件', '无法读取文件夹'}:
        kind, msg = 'file_unavailable', '文件或文件夹不存在或无法读取，请检查后重试'
    elif '/import' in path:
        kind, msg = 'import_failed', '文件导入失败，请检查文件是否可读取且内容完整'
    elif '/search' in path:
        kind, msg = 'search_failed', '搜索执行失败，请检查搜索条件后重试'
    elif '/crawl' in path:
        kind, msg = 'crawl_failed', '爬取失败，请检查网址、网络连接和站点访问限制'
    elif '/settings/cache/clear' in path:
        kind, msg = 'cache_clear_failed', '缓存清理失败，请关闭正在使用缓存文件的程序后重试'
    elif '/settings/data' in path:
        kind, msg = 'migration_failed', '数据迁移失败，请检查目标文件夹为空且具有写入权限'
    elif '/settings' in path:
        kind, msg = 'settings_failed', '设置保存失败，请检查输入内容和文件夹权限后重试'
    elif '/folders' in path:
        kind, msg = 'folder_operation_failed', '文件夹操作失败，请检查名称、目标位置及文件夹状态'
    elif '/items' in path or '/documents' in path:
        kind, msg = 'document_operation_failed', '文件操作失败，请检查文件状态及访问权限后重试'
    else:
        kind, msg = 'operation_failed', '未能完成操作，请检查输入和文件状态后重试'
    return {'level': 'error', 'error_type': kind, 'msg': msg}


def response_envelope(status: int, payload: dict[str, Any], path: str = '', *, error: BaseException | str | None = None) -> dict[str, Any]:
    if status >= 400 or error is not None:
        return {'ok': False, **failure_details(error if error is not None else str(payload.get('error', '')), path, status), 'data': None, 'stats': {}}
    data = {key: value for key, value in payload.items() if key != 'ok'}
    stats = dict(data.get('stats') or {})
    result: dict[str, Any] = {'ok': True, 'level': 'success', 'error_type': '', 'msg': '', 'data': data, 'stats': stats}
    if path == '/api/import/folder':
        failed = len(data.get('errors') or [])
        succeeded = int(data.get('fileCount', 0))
        stats.update(total=succeeded + failed, success=succeeded, failed=failed)
        if failed:
            if not succeeded:
                result['data'] = None
            result.update(level='warning' if succeeded else 'error', ok=bool(succeeded), error_type='import_partial' if succeeded else 'import_failed', msg=f'共 {succeeded + failed} 个文件，成功导入 {succeeded} 个，{failed} 个因无法读取或导入失败被跳过')
    elif path == '/api/crawl':
        messages = data.get('messages') or []
        if any('所有下载链接均失败' in str(message) for message in messages):
            result.update(level='warning', error_type='crawl_fallback', msg='文档下载失败，已保存源页面供后续查看。请检查站点访问限制或更换网址。')
        elif stats.get('skipped_cross_origin', 0) or stats.get('skipped_safe_block', 0) or any(str(message).startswith(('跳过', '安全拦截', '起始网址发生重定向')) for message in messages):
            result.update(level='warning', error_type='crawl_partial', msg='部分链接因访问失败、安全限制或跨源限制已跳过，请检查导入结果')
    elif path == '/api/settings/data/migrate' and data.get('migrated'):
        result['msg'] = '书库数据已成功迁移到目标目录'
    return result
