"""HtmlReportSpec → 无脚本、单文件 HTML。"""

from __future__ import annotations

import html
from pathlib import Path

from app.cowork.artifact_renderers.contracts import DocumentBlock, HtmlReportSpec, PdfSpec
from app.cowork.artifact_renderers.image_assets import image_data_uri

_STYLE = """
:root{color-scheme:light;--ink:#17211d;--muted:#5f6d66;--accent:#167a5b;--line:#dce3df;--surface:#f4f7f5}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.65 system-ui,-apple-system,sans-serif}
main{max-width:980px;margin:auto;padding:64px 56px 80px}header{padding-bottom:30px;border-bottom:1px solid var(--line)}
h1{max-width:820px;margin:0;font-size:42px;line-height:1.12;letter-spacing:-.025em}header p{max-width:760px;color:var(--muted)}
section{padding:34px 0;border-bottom:1px solid var(--line)}h2{margin:0 0 18px;font-size:26px;line-height:1.25}p{margin:0 0 14px}
ul{margin:8px 0 18px;padding-left:23px}.callout{padding:16px 18px;border-left:4px solid var(--accent);background:var(--surface);font-weight:650}
blockquote{margin:20px 0;padding:8px 22px;border-left:3px solid var(--line);color:var(--muted);font-size:19px}
.figure{margin:24px 0}.figure img{display:block;max-width:100%;height:auto;margin:auto}.figure figcaption{margin-top:8px;text-align:center;color:var(--muted);font-size:14px}
.table-wrap{max-width:100%;overflow:auto;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--surface)}tr:last-child td{border-bottom:0}
@media(max-width:680px){main{padding:32px 22px 54px}h1{font-size:32px}}
@media print{main{max-width:none;padding:20mm}section{break-inside:avoid}}
""".strip()


def _block_html(block: DocumentBlock) -> str:
    if block.type == "paragraph":
        class_name = ' class="lead"' if block.style == "lead" else ""
        return f"<p{class_name}>{html.escape(block.text or '')}</p>"
    if block.type == "bullets":
        return "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in block.items) + "</ul>"
    if block.type == "quote":
        return f"<blockquote>{html.escape(block.text or '')}</blockquote>"
    if block.type == "callout":
        return f'<p class="callout">{html.escape(block.text or "")}</p>'
    if block.type == "image":
        if block.image_path is None:  # guarded by DocumentBlock validation
            raise ValueError("image block 缺少 image_path")
        uri = image_data_uri(Path(block.image_path))
        alt = html.escape(block.image_alt or "", quote=True)
        caption = (
            f"<figcaption>{html.escape(block.image_caption)}</figcaption>"
            if block.image_caption
            else ""
        )
        width = (
            f' style="width:min(100%,{block.image_width_inches:g}in)"'
            if block.image_width_inches is not None
            else ""
        )
        return f'<figure class="figure"><img src="{uri}" alt="{alt}"{width}>{caption}</figure>'
    headers = "".join(f"<th>{html.escape(value)}</th>" for value in block.headers)
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape('' if value is None else str(value))}</td>" for value in row)
        + "</tr>"
        for row in block.rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>'


def report_html(spec: HtmlReportSpec | PdfSpec) -> str:
    summary = f"<p>{html.escape(spec.summary)}</p>" if spec.summary else ""
    sections = "".join(
        f'<section id="{html.escape(section.id)}"><h2>{html.escape(section.heading)}</h2>'
        + "".join(_block_html(block) for block in section.blocks)
        + "</section>"
        for section in spec.sections
    )
    purpose = f"<p>{html.escape(spec.purpose)}</p>" if spec.purpose else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(spec.title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><header><h1>{html.escape(spec.title)}</h1>{purpose}{summary}</header>"
        f"{sections}</main></body></html>"
    )


def render_html_report(spec: HtmlReportSpec, target: Path) -> None:
    target.write_text(report_html(spec), encoding="utf-8")


__all__ = ["render_html_report", "report_html"]
