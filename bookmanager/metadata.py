from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from .epub import epub_member_index, epub_package_dir, resolve_epub_member
from .naming import StructuredFileName, infer_structured_file_name, parse_edition_number, split_series_number


SUPPORTED_EXTENSIONS = {".pdf", ".epub", ".djvu", ".djv"}


@dataclass(frozen=True)
class MetadataResult:
    parts: StructuredFileName
    extracted: bool
    message: str


def extract_metadata(path: Path, max_pages: int = 5) -> MetadataResult:
    path = Path(path)
    fallback = infer_structured_file_name(path.name)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return MetadataResult(fallback, False, "当前格式暂不支持内容提取，已使用文件名识别。")

    try:
        if suffix == ".epub":
            return _extract_epub(path, fallback, max_pages)
        if suffix == ".pdf":
            return _extract_pdf(path, fallback, max_pages)
        if suffix in {".djvu", ".djv"}:
            return _extract_djvu(path, fallback, max_pages)
    except Exception as exc:
        return MetadataResult(fallback, False, f"内容提取失败，已使用文件名识别：{exc}")

    return MetadataResult(fallback, False, "未能提取内容，已使用文件名识别。")


def _extract_epub(path: Path, fallback: StructuredFileName, max_pages: int) -> MetadataResult:
    with zipfile.ZipFile(path) as archive:
        opf_path = _epub_opf_path(archive)
        if not opf_path:
            return MetadataResult(fallback, False, "EPUB 未找到 OPF 元数据，已使用文件名识别。")
        root = ElementTree.fromstring(archive.read(opf_path))
        package_dir = epub_package_dir(opf_path)
        metadata = _epub_metadata(root)
        text = "\n".join(_epub_first_text_blocks(archive, root, package_dir, max_pages))

    parts = _parts_from_metadata_text(
        fallback=fallback,
        series=metadata.get("series", ""),
        number=metadata.get("number", ""),
        title=metadata.get("title", ""),
        authors=metadata.get("creator", ""),
        text=text,
    )
    extracted = _has_useful_metadata(parts, fallback)
    message = "已从 EPUB 元数据和前几页内容提取候选字段。" if extracted else "未从 EPUB 中识别出可用字段，请手动输入。"
    return MetadataResult(parts, extracted, message)


def _epub_opf_path(archive: zipfile.ZipFile) -> Optional[str]:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except Exception:
        return None
    for node in container.iter():
        if node.tag.endswith("rootfile") and node.attrib.get("full-path"):
            return node.attrib["full-path"]
    return None


def _epub_metadata(root: ElementTree.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in root.iter():
        tag = _local_name(node.tag)
        if tag in {"title", "creator"} and (node.text or "").strip():
            values.setdefault(tag, " ".join((node.text or "").split()))
        if tag == "meta":
            name = node.attrib.get("name", "").lower()
            content = node.attrib.get("content", "").strip()
            if name in {"calibre:series", "series"} and content:
                values.setdefault("series", content)
            elif name in {"calibre:series_index", "series_index"} and content:
                values.setdefault("number", content)
    return values


def _epub_first_text_blocks(
    archive: zipfile.ZipFile,
    root: ElementTree.Element,
    package_dir: str,
    max_pages: int,
) -> list[str]:
    manifest: dict[str, str] = {}
    spine_ids: list[str] = []
    for node in root.iter():
        tag = _local_name(node.tag)
        if tag == "item" and node.attrib.get("id") and node.attrib.get("href"):
            manifest[node.attrib["id"]] = node.attrib["href"]
        elif tag == "itemref" and node.attrib.get("idref"):
            spine_ids.append(node.attrib["idref"])

    texts: list[str] = []
    members = epub_member_index(archive.namelist())
    for item_id in spine_ids:
        href = manifest.get(item_id)
        if not href:
            continue
        member = resolve_epub_member(package_dir, href, members)
        if member is None or Path(member).suffix.lower() not in {".html", ".htm", ".xhtml"}:
            continue
        raw = archive.read(member).decode("utf-8", errors="replace")
        texts.append(_strip_html(raw))
        if len(texts) >= max_pages:
            break
    return texts


def _extract_pdf(path: Path, fallback: StructuredFileName, max_pages: int) -> MetadataResult:
    text = ""
    backend = ""

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:max_pages])
        backend = "pypdf"
    except Exception:
        try:
            import pymupdf  # type: ignore

            with pymupdf.open(str(path)) as document:
                text = "\n".join(document[index].get_text() for index in range(min(max_pages, document.page_count)))
            backend = "PyMuPDF"
        except Exception:
            return MetadataResult(fallback, False, "未安装 pypdf/PyMuPDF，或 PDF 没有可提取文本；请手动输入。")

    parts = _parts_from_metadata_text(fallback=fallback, text=text)
    if not text.strip():
        return MetadataResult(parts, False, f"{backend} 未从 PDF 前 {max_pages} 页提取到文本；请手动输入。")
    extracted = _has_useful_metadata(parts, fallback)
    message = (
        f"已用 {backend} 从 PDF 前 {max_pages} 页提取候选字段。"
        if extracted
        else f"已读取 PDF 前 {max_pages} 页文本，但未识别出可用字段；请手动输入。"
    )
    return MetadataResult(parts, extracted, message)


def _extract_djvu(path: Path, fallback: StructuredFileName, max_pages: int) -> MetadataResult:
    djvutxt = shutil.which("djvutxt")
    if not djvutxt:
        return MetadataResult(fallback, False, "未找到 djvutxt；请安装 DjVuLibre 或手动输入。")

    command = [djvutxt, "--page=1-" + str(max_pages), str(path)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        return MetadataResult(fallback, False, "djvutxt 未能提取 DJVU 文本；请手动输入。")
    parts = _parts_from_metadata_text(fallback=fallback, text=completed.stdout)
    extracted = _has_useful_metadata(parts, fallback)
    message = (
        f"已用 djvutxt 从 DJVU 前 {max_pages} 页提取候选字段。"
        if extracted
        else f"已读取 DJVU 前 {max_pages} 页文本，但未识别出可用字段；请手动输入。"
    )
    return MetadataResult(parts, extracted, message)


def _parts_from_metadata_text(
    fallback: StructuredFileName,
    series: str = "",
    number: str = "",
    title: str = "",
    authors: str = "",
    text: str = "",
) -> StructuredFileName:
    clean_text = _clean_text(text)
    text_series_abbr, text_number = _extract_series_number(clean_text)
    metadata_series_abbr, metadata_number = split_series_number(series)
    edition = _extract_edition(clean_text)
    text_title, text_subtitle = _extract_title_lines(clean_text)
    text_authors = _extract_authors(clean_text)

    title = title.strip() or text_title
    main_title, subtitle = _split_title(title)
    if not subtitle:
        subtitle = text_subtitle

    return StructuredFileName(
        series_abbr=metadata_series_abbr or text_series_abbr or fallback.series_abbr,
        number=number.strip() or metadata_number or text_number or fallback.number,
        main_title=main_title or fallback.main_title,
        subtitle=subtitle or fallback.subtitle,
        edition=edition or fallback.edition or "1",
        authors=authors.strip() or text_authors or fallback.authors,
        extension=fallback.extension,
    )


def _extract_series_number(text: str) -> tuple[str, str]:
    labeled = re.search(
        r"\b(?:series|volume|vol\.?|book)\s*[:#]?\s*([A-Z][A-Z0-9]{1,12})\s*[- ]?\s*(\d+[A-Za-z]?)\b",
        text,
        flags=re.I,
    )
    if labeled:
        return labeled.group(1).upper(), labeled.group(2)
    compact = re.search(r"\b([A-Z][A-Z0-9]{1,10})\s*[- ]?(\d{1,4}[A-Za-z]?)\b", text)
    if compact and compact.group(1).upper() not in {"ISBN", "ISSN", "DOI"}:
        return compact.group(1).upper(), compact.group(2)
    return "", ""


def _extract_edition(text: str) -> str:
    numeric = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+Edition\b", text, flags=re.I)
    if numeric:
        return numeric.group(1)
    words = {
        "first": "1",
        "second": "2",
        "third": "3",
        "fourth": "4",
        "fifth": "5",
        "sixth": "6",
        "seventh": "7",
        "eighth": "8",
        "ninth": "9",
        "tenth": "10",
    }
    word = re.search(r"\b(" + "|".join(words) + r")\s+Edition\b", text, flags=re.I)
    if word:
        return words[word.group(1).lower()]
    chinese = re.search(r"第\s*([0-9]+)\s*版", text)
    if chinese:
        return chinese.group(1)
    return "1"


def _extract_title_lines(text: str) -> tuple[str, str]:
    candidates = []
    for line in text.splitlines()[:80]:
        cleaned = line.strip()
        if not cleaned or len(cleaned) < 4 or len(cleaned) > 140:
            continue
        if re.search(r"\b(ISBN|ISSN|DOI|Springer|Copyright|Contents|Preface)\b", cleaned, flags=re.I):
            continue
        if len(re.findall(r"[A-Za-z]", cleaned)) < 3:
            continue
        candidates.append(cleaned)
    if not candidates:
        return "", ""
    main_title, subtitle = _split_title(candidates[0])
    if not subtitle and len(candidates) > 1 and not _looks_like_author_line(candidates[1]):
        subtitle = candidates[1]
    return main_title, subtitle


def _extract_authors(text: str) -> str:
    by_line = re.search(r"\bby\s+([A-Z][A-Za-z .,'-]+(?:\s+(?:and|&)\s+[A-Z][A-Za-z .,'-]+)*)", text)
    if by_line:
        return _clean_author_value(by_line.group(1))
    for line in text.splitlines()[:80]:
        if _looks_like_author_line(line):
            return _clean_author_value(line)
    return ""


def _split_title(value: str) -> tuple[str, str]:
    value = " ".join(value.split()).strip()
    if ":" in value:
        main, subtitle = value.split(":", 1)
        return main.strip(), subtitle.strip()
    return value, ""


def _has_useful_metadata(parts: StructuredFileName, fallback: StructuredFileName) -> bool:
    return any(
        getattr(parts, field) and getattr(parts, field) != getattr(fallback, field)
        for field in ("series_abbr", "number", "main_title", "subtitle", "edition", "authors")
    )


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", "\n", value)
    return unescape(value)


def _clean_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _clean_author_value(value: str) -> str:
    value = re.sub(r"\b(edited by|editor|author|authors)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ,;:-")


def _looks_like_author_line(value: str) -> bool:
    line = value.strip()
    if not line or len(line) > 100:
        return False
    if any(word.lower() in line.lower() for word in ("edition", "springer", "contents", "chapter")):
        return False
    names = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+)+\b", line)
    return bool(names)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
