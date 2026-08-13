import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument


class PdfParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPdf:
    title: str
    parser_version: str
    document: ParsedDocument


async def parse_pdf_in_subprocess(
    path: Path,
    *,
    timeout_s: float,
    max_pages: int,
    memory_mb: int,
    cpu_seconds: int,
) -> ParsedPdf:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.ingest.pdf_worker",
        str(path),
        str(max_pages),
        str(memory_mb),
        str(cpu_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise PdfParseError(f"PDF 解析超过 {timeout_s:g} 秒, 已终止子进程") from error
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[-2000:]
        raise PdfParseError(f"PDF 解析子进程失败: {detail or f'exit {process.returncode}'}")
    try:
        payload: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PdfParseError("PDF 解析子进程返回了无效结果") from error
    if not payload.get("ok"):
        raise PdfParseError(str(payload.get("error") or "PDF 解析失败"))
    return _decode_document(payload["document"])


def _decode_document(payload: dict[str, Any]) -> ParsedPdf:
    blocks = [
        ParsedBlock(
            block_idx=int(block["block_idx"]),
            block_type=str(block["block_type"]),
            text=str(block["text"]),
            char_start=int(block["char_start"]),
            char_end=int(block["char_end"]),
            heading_path=tuple(block["heading_path"]),
            locations=tuple(
                BlockLocation(
                    page_no=int(location["page_no"]),
                    page_width=float(location["page_width"]),
                    page_height=float(location["page_height"]),
                    rotation=int(location["rotation"]),
                    coord_origin=str(location["coord_origin"]),
                    bbox_norm=tuple(float(value) for value in location["bbox_norm"]),  # type: ignore[arg-type]
                )
                for location in block["locations"]
            ),
        )
        for block in payload["blocks"]
    ]
    document = ParsedDocument(
        full_text=str(payload["full_text"]),
        blocks=blocks,
        page_count=int(payload["page_count"]),
    )
    for block in blocks:
        if document.full_text[block.char_start : block.char_end] != block.text:
            raise PdfParseError("PDF block 字符区间校验失败")
    return ParsedPdf(
        title=str(payload["title"]),
        parser_version=str(payload["parser_version"]),
        document=document,
    )
