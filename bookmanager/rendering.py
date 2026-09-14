from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

from .epub import epub_member_index, epub_package_dir, resolve_epub_member

@dataclass(frozen=True)
class RenderResult:
    kind: str
    paths: list[Path]
    text: str
    message: str


def render_preview(path: Path, cache_dir: Path, pages: int = 2) -> RenderResult:
    suffix = path.suffix.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if suffix == ".pdf":
        return render_pdf(path, cache_dir, pages)
    if suffix in {".djvu", ".djv"}:
        return render_djvu(path, cache_dir, pages)
    if suffix == ".epub":
        return render_epub(path, cache_dir, pages)
    return RenderResult("none", [], "", "该格式没有专用渲染器。")


def render_pdf(path: Path, cache_dir: Path, pages: int) -> RenderResult:
    return _render_pymupdf_pages(path, cache_dir, pages, "pdf-page", "PDF")


def _render_pymupdf_pages(
    path: Path,
    cache_dir: Path,
    pages: int,
    output_prefix: str,
    document_label: str,
) -> RenderResult:
    try:
        import pymupdf  # type: ignore
    except Exception:
        return RenderResult("none", [], "", f"未安装 PyMuPDF，无法内嵌渲染 {document_label} 前两页。")

    cached_paths = [cache_dir / f"{output_prefix}-{index + 1}.png" for index in range(pages)]
    existing_paths = [path for path in cached_paths if path.is_file()]
    if existing_paths:
        return RenderResult("images", existing_paths, "", f"已读取缓存的 {document_label} 预览。")

    output_paths: list[Path] = []
    try:
        with pymupdf.open(str(path)) as document:
            for index in range(min(pages, document.page_count)):
                page = document[index]
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                output = cache_dir / f"{output_prefix}-{index + 1}.png"
                pixmap.save(str(output))
                output_paths.append(output)
    except Exception as exc:
        return RenderResult("none", [], "", f"{document_label} 渲染失败：{exc}")
    if not output_paths:
        return RenderResult("none", [], "", f"{document_label} 没有可渲染的页面。")
    return RenderResult("images", output_paths, "", f"已渲染 {document_label} 前 {len(output_paths)} 页。")


def render_djvu(path: Path, cache_dir: Path, pages: int) -> RenderResult:
    ddjvu = shutil.which("ddjvu")
    if not ddjvu:
        return RenderResult("none", [], "", "未找到 ddjvu，无法内嵌渲染 DJVU 前两页。")

    output_paths: list[Path] = []
    for page_number in range(1, pages + 1):
        output = cache_dir / f"page-{page_number}.ppm"
        command = [ddjvu, "-format=ppm", f"-page={page_number}", str(path), str(output)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        if completed.returncode != 0:
            if page_number == 1:
                return RenderResult("none", [], "", "DJVU 渲染失败；请确认文件可读或安装完整 DjVuLibre。")
            break
        output_paths.append(output)
    return RenderResult("images", output_paths, "", f"已渲染 DJVU 前 {len(output_paths)} 页。")


def render_epub(path: Path, cache_dir: Path, pages: int) -> RenderResult:
    rendered = _render_pymupdf_pages(path, cache_dir, pages, "epub-page", "EPUB")
    if rendered.kind == "images":
        return rendered

    try:
        with zipfile.ZipFile(path) as archive:
            opf_path = _epub_opf_path(archive)
            if not opf_path:
                return RenderResult("none", [], "", "EPUB 未找到 OPF 目录，无法渲染前两页。")
            root = ElementTree.fromstring(archive.read(opf_path))
            package_dir = epub_package_dir(opf_path)
            blocks = _epub_first_text_blocks(archive, root, package_dir, pages)
    except Exception as exc:
        return RenderResult("none", [], "", f"{rendered.message}\nEPUB 文本回退失败：{exc}")

    if not blocks:
        return RenderResult("none", [], "", f"{rendered.message}\nEPUB 前两页没有可显示的文本内容。")
    text = "\n\n".join(f"第 {index + 1} 页\n{block}" for index, block in enumerate(blocks))
    return RenderResult("text", [], text, f"{rendered.message}\n已提取并显示 EPUB 前 {len(blocks)} 页。")


def _epub_opf_path(archive: zipfile.ZipFile) -> str | None:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except Exception:
        return None
    for node in container.iter():
        if node.tag.endswith("rootfile") and node.attrib.get("full-path"):
            return node.attrib["full-path"]
    return None


def _epub_first_text_blocks(
    archive: zipfile.ZipFile,
    root: ElementTree.Element,
    package_dir: str,
    pages: int,
) -> list[str]:
    manifest: dict[str, str] = {}
    spine_ids: list[str] = []
    for node in root.iter():
        tag = _local_name(node.tag)
        if tag == "item" and node.attrib.get("id") and node.attrib.get("href"):
            manifest[node.attrib["id"]] = node.attrib["href"]
        elif tag == "itemref" and node.attrib.get("idref"):
            spine_ids.append(node.attrib["idref"])

    blocks: list[str] = []
    members = epub_member_index(archive.namelist())
    for item_id in spine_ids:
        href = manifest.get(item_id)
        if not href:
            continue
        member = resolve_epub_member(package_dir, href, members)
        if member is None or Path(member).suffix.lower() not in {".html", ".htm", ".xhtml"}:
            continue
        raw = archive.read(member).decode("utf-8", errors="replace")
        text = _strip_html(raw)
        if text:
            blocks.append(text[:5000])
        if len(blocks) >= pages:
            break
    return blocks


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", "\n", value)
    lines = [" ".join(unescape(line).split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
