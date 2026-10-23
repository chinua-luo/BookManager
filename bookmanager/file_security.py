from __future__ import annotations

import zipfile
from pathlib import Path


MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024


def validate_supported_document(path: Path) -> None:
    """Reject mislabeled parser inputs before they reach third-party libraries."""
    path = Path(path)
    suffix = path.suffix.lower()
    if path.is_symlink() or not path.is_file():
        raise ValueError("文件路径无效或为软链接")
    if suffix == ".pdf":
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ValueError("PDF 文件头无效")
    elif suffix == ".epub":
        _validate_epub(path)
    elif suffix in {".djvu", ".djv"}:
        with path.open("rb") as source:
            header = source.read(12)
        if len(header) < 12 or not header.startswith(b"AT&TFORM") or header[8:12] not in {b"DJVU", b"DJVM"}:
            raise ValueError("DJVU 文件头无效")


def _validate_epub(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            total_size = sum(info.file_size for info in infos)
            if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError("EPUB 解压后体积过大")
            if any(info.file_size > MAX_ARCHIVE_MEMBER_BYTES for info in infos):
                raise ValueError("EPUB 内部文件体积过大")
            mimetype = archive.read("mimetype")
            if mimetype.strip() != b"application/epub+zip":
                raise ValueError("EPUB mimetype 无效")
            if "META-INF/container.xml" not in archive.namelist():
                raise ValueError("EPUB 缺少 container.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("EPUB 文件结构无效") from exc
