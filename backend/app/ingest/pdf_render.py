"""把 PDF 的某一页渲染成 PNG。

放在共享的 `app/ingest/` 而不是任一产品包内：RAG 的标注台要它来画 gold 区间，Cowork 的
阅读器面板要它来画模型引用的高亮，而这两个包按 ADR-0011 互不 import。渲染一页图和解析
一份文档是同一类事情——都只依赖 PDF 本身，不依赖任何产品语义。

只开文档取一页位图，不抽文字、不跑版面分析，所以不必走解析子进程的资源上限（那条约束防
的是 MinerU 遇畸形 PDF 时的 OOM）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf


class PdfRenderError(ValueError):
    """页面渲染失败。调用方负责翻译成对应协议的错误。"""


# 1.5 倍是可读性与体积的折中：视网膜屏上不糊，一页 A4 的 PNG 仍在几百 KB 量级。
DEFAULT_SCALE = 1.5


def render_pdf_page(path: Path, page_no: int, scale: float = DEFAULT_SCALE) -> bytes:
    if path.suffix.lower() != ".pdf":
        raise PdfRenderError("只有 PDF 支持页面预览")
    document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
    try:
        if not 1 <= page_no <= document.page_count:
            raise PdfRenderError("PDF 页码越界")
        matrix = pymupdf.Matrix(scale, scale)  # type: ignore[no-untyped-call]
        pixmap: Any = document[page_no - 1].get_pixmap(matrix=matrix, alpha=False)
        return bytes(pixmap.tobytes("png"))
    finally:
        document.close()


__all__ = ["DEFAULT_SCALE", "PdfRenderError", "render_pdf_page"]
