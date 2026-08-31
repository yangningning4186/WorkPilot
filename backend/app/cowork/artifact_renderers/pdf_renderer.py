"""PdfSpec → paginated PDF using PyMuPDF Story; no browser or script execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from app.cowork.artifact_renderers.contracts import PdfSpec
from app.cowork.artifact_renderers.html_renderer import report_html

_pymupdf: Any = pymupdf


def render_pdf(spec: PdfSpec, target: Path) -> None:
    page_rect = _pymupdf.Rect(0, 0, 595, 842)
    content_rect = _pymupdf.Rect(54, 50, 541, 792)
    story = _pymupdf.Story(
        report_html(spec),
        user_css=(
            "@page{size:A4;margin:0}*{box-sizing:border-box}"
            "body{margin:0;color:#17211d;font-family:sans-serif;font-size:10.5pt;line-height:1.45}"
            "main{max-width:none;margin:0;padding:0}header{padding:0 0 18pt;border-bottom:1px solid #dce3df}"
            "h1{margin:0 0 10pt;font-size:25pt;line-height:1.15}"
            "h2{margin:0 0 9pt;font-size:16pt;line-height:1.25}"
            "section{padding:16pt 0;border-bottom:1px solid #dce3df}"
            "p{margin:0 0 8pt}ul{margin:5pt 0 10pt;padding-left:18pt}"
            "blockquote{margin:10pt 0;padding:5pt 12pt;border-left:2pt solid #dce3df}"
            ".callout{padding:8pt 10pt;border-left:3pt solid #167a5b;background:#f4f7f5}"
            ".table-wrap{border:1px solid #dce3df;border-radius:0;overflow:visible}"
            "table{width:100%;border-collapse:collapse;font-size:9pt}"
            "th,td{padding:5pt 6pt;border-bottom:1px solid #dce3df;vertical-align:top}"
            "th{background:#f4f7f5}tr{break-inside:avoid}"
        ),
        em=10.5,
    )
    writer = _pymupdf.DocumentWriter(str(target))
    page_count = 0
    try:
        more = 1
        while more:
            page_count += 1
            if page_count > 200:
                raise ValueError("PDF 内容超过 200 页安全上限")
            device = writer.begin_page(page_rect)
            more, _ = story.place(content_rect)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()
    document = _pymupdf.open(target)
    try:
        document.set_metadata(
            {
                "title": spec.title,
                "subject": spec.purpose or "",
                "producer": "WorkPilot",
            }
        )
        document.saveIncr()
    finally:
        document.close()


__all__ = ["render_pdf"]
