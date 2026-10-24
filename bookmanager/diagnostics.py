"""Local exception diagnostics. Failures in logging never trigger UI or recurse."""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import functools
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Iterator

_home: contextvars.ContextVar[Path | None] = contextvars.ContextVar('diagnostic_home', default=None)
_lock = threading.RLock()
_SECRET = re.compile(r'([?&](?:token|key|secret|password|api_key)=)[^&\s\"\']+|\b(Bearer\s+)[A-Za-z0-9._~+/=-]+', re.I)


def fallback_home() -> Path:
    return Path(os.environ.get('BOOKMANAGER_DIAGNOSTIC_HOME') or Path.home() / '.bookmanager' / 'diagnostics')


@contextlib.contextmanager
def diagnostic_scope(home: Path | None) -> Iterator[None]:
    token = _home.set(home)
    try:
        yield
    finally:
        _home.reset(token)


def sanitize(value: Any) -> str:
    text = str(value)
    text = _SECRET.sub(lambda match: (match.group(1) or match.group(2)) + '<redacted>', text)
    token = os.environ.get('BOOKMANAGER_BRIDGE_TOKEN', '')
    return text.replace(token, '<redacted>') if token else text


def record_event(event: str, *, error: BaseException | None = None, data_home: Path | None = None, **details: Any) -> Path | None:
    """Daily JSONL includes full exception chains; fallback survives unusable data homes."""
    now = dt.datetime.now().astimezone()
    record: dict[str, Any] = {'timestamp': now.isoformat(timespec='milliseconds'), 'level': 'ERROR' if error else 'INFO', 'event': event, 'details': details}
    if error is not None:
        record.update(error_type=type(error).__name__, error=str(error), traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): '<redacted>' if str(key).lower() in {'token', 'password', 'secret', 'authorization', 'api_key'} else clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return sanitize(value) if isinstance(value, (str, BaseException, Path)) else value
    line = json.dumps(clean(record), ensure_ascii=False, default=str) + '\n'
    preferred = data_home or _home.get() or fallback_home()
    candidates = [preferred, fallback_home()] if preferred != fallback_home() else [preferred]
    for home in candidates:
        try:
            with _lock:
                directory = Path(home) / 'logs'
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f'diagnostics-{now.date().isoformat()}.jsonl'
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(line)
            return path
        except Exception:
            # Do not recurse if the disk is full or diagnostics are unwritable.
            continue
    try:
        sys.stderr.write(line)
    except Exception:
        pass
    return None


def record_exception(event: str, error: BaseException | None = None, **details: Any) -> Path | None:
    return record_event(event, error=error or sys.exc_info()[1] or RuntimeError('Unknown exception'), **details)


def current_home() -> Path:
    return _home.get() or fallback_home()


def silent_background(event: str):
    def decorate(function):
        @functools.wraps(function)
        def run(self, *args, **kwargs):
            try:
                return function(self, *args, **kwargs)
            except Exception as exc:
                record_exception(event, exc, data_home=self.home)
                return 0
        return run
    return decorate
