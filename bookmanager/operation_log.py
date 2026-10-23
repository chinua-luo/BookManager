from __future__ import annotations

import datetime as dt
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable


TOKEN_PATTERN = re.compile(r"([?&](?:token|key|secret|password)=)[^&\s]+", re.IGNORECASE)


class OperationLogger:
    """Append structured, daily operation records without creating empty logs."""

    def __init__(
        self,
        data_home: Path,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._data_home = Path(data_home)
        self._clock = clock or (lambda: dt.datetime.now().astimezone())
        self._session_id = session_id or uuid.uuid4().hex
        self._lock = threading.Lock()

    @property
    def data_home(self) -> Path:
        with self._lock:
            return self._data_home

    def set_data_home(self, data_home: Path) -> None:
        with self._lock:
            self._data_home = Path(data_home)

    def write(
        self,
        *,
        event: str,
        message: str,
        result: str,
        duration_ms: int | float,
        document_name: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Path:
        if result not in {"success", "failure"}:
            raise ValueError("result must be success or failure")
        if not event.strip() or not message.strip():
            raise ValueError("event and message are required")

        now = self._clock()
        if now.tzinfo is None:
            now = now.astimezone()
        record: dict[str, Any] = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "level": "ERROR" if result == "failure" else "INFO",
            "event": event.strip(),
            "message": message.strip(),
            "session_id": self._session_id,
            "result": result,
            "duration_ms": max(0, round(float(duration_ms))),
        }
        if document_name:
            record["document_name"] = str(document_name)
        if result == "failure":
            record["error"] = self._sanitize_error(error or "未知错误")
        if details:
            record["details"] = details

        with self._lock:
            logs_dir = self._data_home / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"bookmanager-{now.date().isoformat()}.jsonl"
            with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            return log_path

    def _sanitize_error(self, value: str) -> str:
        text = TOKEN_PATTERN.sub(r"\1<redacted>", str(value).replace("\r", " ").replace("\n", " "))
        home = str(self._data_home)
        if home:
            text = text.replace(home, "<data_home>")
        return text[:2000]
