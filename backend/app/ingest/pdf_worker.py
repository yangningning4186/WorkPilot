import json
import math
import re
import resource
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf

PDF_PARSER_VERSION = f"{pymupdf.VersionBind}:adapter2"


def _limit_resources(memory_mb: int, cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
    if sys.platform.startswith("linux"):
        memory_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return unicodedata.normalize("NFC", "\n".join(line for line in lines if line)).strip()


def _reading_order(blocks: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """按跨栏标题切段；检测到左右两栏时先读完左栏再读右栏。"""

    spanning = sorted(
        (block for block in blocks if block["x1"] - block["x0"] >= page_width * 0.62),
        key=lambda block: (block["y0"], block["x0"]),
    )
    narrow = [block for block in blocks if block not in spanning]
    ordered: list[dict[str, Any]] = []
    boundaries = [-math.inf, *(block["y0"] for block in spanning), math.inf]
    for index, span in enumerate([*spanning, None]):
        lower = boundaries[index]
        upper = boundaries[index + 1]
        region = [block for block in narrow if lower <= block["y0"] < upper]
        left = [block for block in region if (block["x0"] + block["x1"]) / 2 < page_width / 2]
        right = [block for block in region if block not in left]
        has_columns = left and right and min(len(left), len(right)) >= 1
        if has_columns:
            ordered.extend(sorted(left, key=lambda block: (block["y0"], block["x0"])))
            ordered.extend(sorted(right, key=lambda block: (block["y0"], block["x0"])))
        else:
            ordered.extend(sorted(region, key=lambda block: (block["y0"], block["x0"])))
        if span is not None:
            ordered.append(span)
    return ordered


def _looks_multi_column(blocks: list[dict[str, Any]], page_width: float) -> bool:
    narrow = [block for block in blocks if block["x1"] - block["x0"] < page_width * 0.55]
    left = [block for block in narrow if (block["x0"] + block["x1"]) / 2 < page_width / 2]
    right = [block for block in narrow if (block["x0"] + block["x1"]) / 2 >= page_width / 2]
    if len(left) < 2 or len(right) < 2:
        return False
    return any(
        max(left_block["y0"], right_block["y0"]) < min(left_block["y1"], right_block["y1"])
        for left_block in left
        for right_block in right
    )


def extract_pdf(path: Path, max_pages: int) -> dict[str, Any]:
    document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
    try:
        if document.needs_pass:
            raise ValueError("PDF 已加密, 需要先解除密码保护")
        if document.page_count > max_pages:
            raise ValueError(f"PDF 页数 {document.page_count} 超过上限 {max_pages}")

        pages: list[dict[str, Any]] = []
        edge_texts: Counter[str] = Counter()
        image_count = 0
        multi_column_pages = 0
        pages_with_text = 0
        for page_index in range(document.page_count):
            page: Any = document[page_index]
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            blocks: list[dict[str, Any]] = []
            page_edge_texts: set[str] = set()
            for raw in page.get_text("blocks", sort=False):
                x0, y0, x1, y1, raw_text, _, block_type = raw[:7]
                text = _normalize_text(str(raw_text))
                if block_type != 0 or not text:
                    continue
                block: dict[str, Any] = {
                    "text": text,
                    "x0": max(0.0, float(x0)),
                    "y0": max(0.0, float(y0)),
                    "x1": min(page_width, float(x1)),
                    "y1": min(page_height, float(y1)),
                }
                blocks.append(block)
                if block["y0"] < page_height * 0.12 or block["y1"] > page_height * 0.88:
                    page_edge_texts.add(text)
            edge_texts.update(page_edge_texts)
            image_count += len(page.get_images(full=True))
            pages_with_text += bool(blocks)
            multi_column_pages += _looks_multi_column(blocks, page_width)
            pages.append(
                {
                    "page_no": page_index + 1,
                    "page_width": page_width,
                    "page_height": page_height,
                    "rotation": int(page.rotation),
                    "blocks": blocks,
                }
            )

        repeat_threshold = max(3, math.ceil(document.page_count * 0.6))
        repeated_edges = {text for text, count in edge_texts.items() if count >= repeat_threshold}
        output_blocks: list[dict[str, Any]] = []
        full_parts: list[str] = []
        cursor = 0
        for page in pages:
            page_blocks: list[dict[str, Any]] = page["blocks"]
            blocks = [block for block in page_blocks if block["text"] not in repeated_edges]
            page_width = float(page["page_width"])
            page_height = float(page["page_height"])
            for block in _reading_order(blocks, page_width):
                text = str(block["text"])
                if full_parts:
                    cursor += 2
                char_start = cursor
                cursor += len(text)
                full_parts.append(text)
                output_blocks.append(
                    {
                        "block_idx": len(output_blocks),
                        "block_type": "paragraph",
                        "text": text,
                        "char_start": char_start,
                        "char_end": cursor,
                        "heading_path": [],
                        "locations": [
                            {
                                "page_no": page["page_no"],
                                "page_width": page["page_width"],
                                "page_height": page["page_height"],
                                "rotation": page["rotation"],
                                "coord_origin": "top_left",
                                "bbox_norm": [
                                    float(block["x0"]) / page_width,
                                    float(block["y0"]) / page_height,
                                    float(block["x1"]) / page_width,
                                    float(block["y1"]) / page_height,
                                ],
                            }
                        ],
                    }
                )
        if not output_blocks:
            raise ValueError("PDF 没有可提取文本; 扫描件需要 OCR/MinerU 后再导入")
        metadata = document.metadata or {}
        metadata_title = _normalize_text(str(metadata.get("title") or ""))
        return {
            "title": metadata_title or output_blocks[0]["text"][:120],
            "full_text": "\n\n".join(full_parts),
            "page_count": document.page_count,
            "blocks": output_blocks,
            "parser_version": PDF_PARSER_VERSION,
            "source_analysis": {
                "image_count": image_count,
                "multi_column_pages": multi_column_pages,
                "pages_with_text": pages_with_text,
            },
        }
    finally:
        document.close()


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: pdf_worker PATH MAX_PAGES MEMORY_MB CPU_SECONDS")
    path = Path(sys.argv[1])
    max_pages, memory_mb, cpu_seconds = map(int, sys.argv[2:])
    _limit_resources(memory_mb, cpu_seconds)
    try:
        payload = {"ok": True, "document": extract_pdf(path, max_pages)}
    except Exception as error:
        payload = {"ok": False, "error": str(error)[:4000]}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
