"""格式分发；候选提交与验证由更外层 Artifact pipeline 负责。"""

from __future__ import annotations

from pathlib import Path

from app.cowork.artifact_renderers.contracts import (
    ArtifactSpec,
    DocumentSpec,
    HtmlReportSpec,
    PdfSpec,
    PresentationSpec,
    WorkbookSpec,
)
from app.cowork.artifact_renderers.docx_renderer import render_document
from app.cowork.artifact_renderers.html_renderer import render_html_report
from app.cowork.artifact_renderers.pdf_renderer import render_pdf
from app.cowork.artifact_renderers.xlsx_renderer import render_workbook
from app.cowork.skills.builtin.pptx.scripts.render_pptx import render_presentation

_SUFFIX_BY_TYPE = {
    "docx": ".docx",
    "xlsx": ".xlsx",
    "pptx": ".pptx",
    "pdf": ".pdf",
    "html": ".html",
}


def render_candidate(spec: ArtifactSpec, target: Path) -> None:
    expected = _SUFFIX_BY_TYPE[spec.artifact_type]
    if target.suffix.casefold() != expected:
        raise ValueError(f"{spec.artifact_type} Artifact 必须写入 {expected} 文件")
    if isinstance(spec, PresentationSpec):
        render_presentation(spec, target)
    elif isinstance(spec, DocumentSpec):
        render_document(spec, target)
    elif isinstance(spec, WorkbookSpec):
        render_workbook(spec, target)
    elif isinstance(spec, HtmlReportSpec):
        render_html_report(spec, target)
    elif isinstance(spec, PdfSpec):
        render_pdf(spec, target)
    else:  # pragma: no cover - discriminated union 已封闭
        raise TypeError("未知 ArtifactSpec")


__all__ = ["render_candidate"]
