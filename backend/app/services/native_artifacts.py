"""原生 DOCX、XLSX、PDF 交付物生成与原子替换。"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import fitz  # type: ignore[import-untyped]
from docx import Document
from openpyxl import Workbook  # type: ignore[import-untyped]

from app.services.cowork_files import create_file_backup


@dataclass(frozen=True)
class NativeArtifactResult:
    path: Path
    sha256: str
    size_bytes: int
    mime_type: str
    backup_path: Path | None


def create_native_artifact(
    path: Path,
    *,
    format: Literal["docx", "xlsx", "pdf"],
    title: str,
    content: str,
    sheets: list[dict[str, Any]],
    baseline_sha256: str | None,
    backup_versions: int = 5,
) -> NativeArtifactResult:
    expected_suffix = f".{format}"
    if path.suffix.casefold() != expected_suffix:
        raise ValueError(f"{format} 交付物路径必须以 {expected_suffix} 结尾")
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_baseline(path, baseline_sha256)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if format == "docx":
            _write_docx(temporary, title=title, content=content)
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif format == "xlsx":
            _write_xlsx(temporary, title=title, sheets=sheets)
            mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            _write_pdf(temporary, title=title, content=content)
            mime_type = "application/pdf"
        payload = temporary.read_bytes()
        # 生成可能耗时；替换前再次核对 baseline，防止覆盖期间的并发修改。
        _check_baseline(path, baseline_sha256)
        previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        backup_path = create_file_backup(path, backup_versions) if path.exists() else None
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return NativeArtifactResult(
            path=path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            mime_type=mime_type,
            backup_path=backup_path,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _check_baseline(path: Path, baseline: str | None) -> None:
    if not path.exists():
        if baseline is not None:
            raise ValueError("原生交付物尚不存在，baseline_sha256 必须省略")
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("原生交付物目标必须是普通文件")
    if baseline is None:
        raise ValueError("覆盖已有原生交付物必须提供 baseline_sha256")
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != baseline:
        raise ValueError("原生交付物已被修改；baseline_sha256 不匹配")


def _write_docx(path: Path, *, title: str, content: str) -> None:
    document = Document()
    document.core_properties.title = title
    document.add_heading(title, level=0)
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            document.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            document.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            document.add_heading(line[2:], level=1)
        elif line.startswith(("- ", "* ")):
            document.add_paragraph(line[2:], style="List Bullet")
        else:
            document.add_paragraph(line)
    document.save(str(path))


def _write_xlsx(path: Path, *, title: str, sheets: list[dict[str, Any]]) -> None:
    if not sheets:
        sheets = [{"name": title[:31] or "Sheet1", "rows": []}]
    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)
    total_cells = 0
    for index, raw_sheet in enumerate(sheets):
        name = str(raw_sheet.get("name") or f"Sheet{index + 1}")[:31]
        sheet = workbook.create_sheet(name)
        rows = raw_sheet.get("rows") or []
        if not isinstance(rows, list):
            raise ValueError("XLSX sheets.rows 必须是二维数组")
        for row in rows:
            if not isinstance(row, list):
                raise ValueError("XLSX sheets.rows 必须是二维数组")
            total_cells += len(row)
            if total_cells > 100_000:
                raise ValueError("XLSX 单次生成不能超过 100000 个单元格")
            sheet.append(row)
    workbook.save(path)


def _write_pdf(path: Path, *, title: str, content: str) -> None:
    document = fitz.open()
    try:
        page = document.new_page(width=595, height=842)
        font = fitz.Font(fontname="china-s")
        y = 52.0
        for index, paragraph in enumerate([title, *content.splitlines()]):
            text = paragraph.strip()
            if not text:
                y += 8
                continue
            size = 18 if index == 0 else 10.5
            line_height = size * 1.35
            for line in _wrap_pdf_lines(text, font=font, fontsize=size, max_width=499):
                if y + line_height > 800:
                    page = document.new_page(width=595, height=842)
                    y = 48
                page.insert_text(
                    (48, y),
                    line,
                    fontname="china-s",
                    fontsize=size,
                )
                y += line_height
            y += 5
        document.set_metadata({"title": title})
        document.save(path)
    finally:
        document.close()


def _wrap_pdf_lines(
    text: str,
    *,
    font: Any,
    fontsize: float,
    max_width: float,
) -> list[str]:
    """按字体真实宽度折行，避免 insert_textbox 负返回值导致内容静默消失。"""

    lines: list[str] = []
    current = ""
    for character in text:
        candidate = f"{current}{character}"
        if current and font.text_length(candidate, fontsize=fontsize) > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current or not lines:
        lines.append(current.rstrip())
    return [line for line in lines if line]
