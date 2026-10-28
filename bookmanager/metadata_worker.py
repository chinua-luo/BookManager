from __future__ import annotations

import json
import sys
from pathlib import Path

from .diagnostics import record_event

from .metadata import _extract_metadata_in_process


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:
        record_event('metadata_worker.limit_unavailable', resource='resource', reason='platform_unsupported')
        return
    limits = [("RLIMIT_CPU", 25), ("RLIMIT_NOFILE", 64)]
    # Darwin rejects low address-space ceilings against existing VM mappings.
    # CPU/descriptor limits and the parent process timeout remain in force.
    if sys.platform != "darwin":
        limits.append(("RLIMIT_AS", 768 * 1024 * 1024))
    else:
        record_event('metadata_worker.limit_unavailable', resource='RLIMIT_AS', reason='platform_unsupported')
    for name, target in limits:
        resource_id = getattr(resource, name, None)
        if resource_id is None:
            record_event('metadata_worker.limit_unavailable', resource=name, reason='platform_unsupported')
            continue
        try:
            current_soft, current_hard = resource.getrlimit(resource_id)
            hard = target if current_hard == resource.RLIM_INFINITY else min(target, current_hard)
            soft = hard if current_soft == resource.RLIM_INFINITY else min(hard, current_soft)
            resource.setrlimit(resource_id, (soft, hard))
        except (OSError, ValueError) as exc:
            record_event('metadata_worker.limit_unavailable', resource=name, target=target, error=exc)


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
