from __future__ import annotations

import json
import sys
from pathlib import Path

from .metadata import _extract_metadata_in_process


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:
        return
    limits = (
        (resource.RLIMIT_CPU, 25, 25),
        (resource.RLIMIT_AS, 768 * 1024 * 1024, 768 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64, 64),
    )
    for resource_id, soft, hard in limits:
        try:
            resource.setrlimit(resource_id, (soft, hard))
        except (OSError, ValueError):
            pass


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: metadata_worker PATH MAX_PAGES")
    _limit_resources()
    result = _extract_metadata_in_process(Path(sys.argv[1]), int(sys.argv[2]))
    print(
        json.dumps(
            {
                "parts": result.parts.__dict__,
                "extracted": result.extracted,
                "message": result.message,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
