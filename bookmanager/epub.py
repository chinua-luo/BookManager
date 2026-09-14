from __future__ import annotations

import posixpath
from urllib.parse import unquote


def epub_member_index(names: list[str]) -> dict[str, str]:
    """Map normalized EPUB archive paths back to their exact ZIP member names."""
    return {_normalize_epub_path(name): name for name in names}


def resolve_epub_member(package_dir: str, href: str, members: dict[str, str]) -> str | None:
    """Resolve a manifest href relative to its OPF package using EPUB's POSIX paths."""
    target = unquote(href).split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
    if not target:
        return None
    relative = posixpath.join(package_dir, target) if package_dir else target
    return members.get(_normalize_epub_path(relative))


def epub_package_dir(opf_path: str) -> str:
    return posixpath.dirname(unquote(opf_path).replace("\\", "/"))


def _normalize_epub_path(value: str) -> str:
    normalized = posixpath.normpath(unquote(value).replace("\\", "/"))
    return normalized.lstrip("/")
