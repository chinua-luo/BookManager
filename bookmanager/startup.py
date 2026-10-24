"""Bootstrap with stdlib-only imports, before loading the bridge or its dependencies."""
from __future__ import annotations

import argparse
import errno
import importlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .diagnostics import diagnostic_scope, record_event, record_exception

MIN_FREE_BYTES = 32 * 1024 * 1024
REQUIRED_MODULES = ('sqlite3', 'ssl', 'zlib', 'hashlib', 'bookmanager.bridge')


class StartupFailure(RuntimeError):
    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(error_type)


def check_dependencies() -> dict[str, bool]:
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            raise StartupFailure('dependency_missing') from exc
    optional = {}
    try:
        importlib.import_module('pypdf')
        optional['pdfMetadata'] = True
    except Exception as exc:
        optional['pdfMetadata'] = False
        record_exception('startup.optional_pdf_metadata', exc)
    optional['djvuText'] = shutil.which('djvutxt') is not None
    if not optional['djvuText']:
        record_event('startup.optional_djvu_text_unavailable')
    return optional


def _probe_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError('Directory has no write permission bits')
    # access() alone is insufficient: ACLs and read-only volumes need a real write.
    with tempfile.NamedTemporaryFile(prefix='.bookmanager-preflight-', dir=directory) as handle:
        handle.write(b'BookManager startup check\n')
        handle.flush()
        os.fsync(handle.fileno())


def check_storage(home: Path) -> None:
    try:
        _probe_directory(home)
        if shutil.disk_usage(home).free < MIN_FREE_BYTES:
            raise StartupFailure('disk_full')
        for name in ('blobs', 'open_cache', 'render_cache', 'tmp', 'logs'):
            directory = home / name
            if not directory.exists():
                continue
            _probe_directory(directory)
            if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
                raise StartupFailure('disk_full')
        database = home / 'library.db'
        if database.exists():
            if not database.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                raise PermissionError('Database has no write permission bits')
            with database.open('r+b'):
                pass
            sqlite3 = importlib.import_module('sqlite3')
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise StartupFailure('data_corrupt')
            finally:
                connection.close()
    except StartupFailure:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            kind = 'disk_full'
        elif isinstance(exc, PermissionError) or exc.errno in {errno.EPERM, errno.EACCES, errno.EROFS}:
            kind = 'data_permission'
        else:
            kind = 'data_unavailable'
        raise StartupFailure(kind) from exc
    except Exception as exc:
        sqlite3 = importlib.import_module('sqlite3')
        kind = 'data_unavailable' if isinstance(exc, sqlite3.OperationalError) else 'data_corrupt'
        raise StartupFailure(kind) from exc


def preflight(data_home: Path | None, static_dir: Path) -> tuple[Path, dict[str, bool]]:
    optional = check_dependencies()
    if not (static_dir / 'index.html').is_file():
        raise StartupFailure('frontend_missing')
    from .store import DEFAULT_LIBRARY_HOME, PROJECT_LIBRARY_HOME, LEGACY_LIBRARY_HOME, configured_library_home, resolve_default_library_home
    target = data_home or (Path(os.environ['BOOK_MANAGER_HOME']).expanduser() if os.environ.get('BOOK_MANAGER_HOME') else configured_library_home() or DEFAULT_LIBRARY_HOME)
    check_storage(target)
    # Check an old library before migration can move it; reject corruption in place.
    if data_home is None and not os.environ.get('BOOK_MANAGER_HOME') and not (target / 'library.db').exists():
        for source in (PROJECT_LIBRARY_HOME, LEGACY_LIBRARY_HOME):
            if source.resolve() != target.resolve() and (source / 'library.db').exists():
                check_storage(source)
                break
    resolved, migrated = (data_home, False) if data_home else resolve_default_library_home()
    if migrated or resolved.resolve() != target.resolve():
        check_storage(resolved)
    return resolved, optional


def main() -> None:
    parser = argparse.ArgumentParser(description='BookManager Electron bridge')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--token', help=argparse.SUPPRESS)
    parser.add_argument('--data-home')
    parser.add_argument('--static-dir', required=True)
    args = parser.parse_args()
    token = args.token or os.environ.get('BOOKMANAGER_BRIDGE_TOKEN', '')
    if len(token) < 32:
        parser.error('bridge token is missing or too short')
    server = None
    try:
        home, capabilities = preflight(Path(args.data_home).expanduser().resolve() if args.data_home else None, Path(args.static_dir).expanduser())
        with diagnostic_scope(home):
            from .bridge import BridgeServer
            server = BridgeServer(('127.0.0.1', args.port), home, token, Path(args.static_dir).expanduser())
            server.capabilities = capabilities
            record_event('startup.ready', data_home=home, capabilities=capabilities)
            print(json.dumps({'event': 'ready', 'port': server.server_port}), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log_path = record_exception('startup.failed', exc)
        print(json.dumps({'event': 'startup-error', 'error_type': exc.error_type if isinstance(exc, StartupFailure) else 'startup_failed', 'log_path': str(log_path) if log_path else None}), flush=True)
        raise SystemExit(1) from None
    finally:
        if server is not None:
            server.server_close()


if __name__ == '__main__':
    main()
