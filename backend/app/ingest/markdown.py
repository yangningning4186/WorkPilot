import re
import unicodedata

from app.ingest.types import ParsedBlock, ParsedDocument

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
LIST_RE = re.compile(r"^(?:[-+*]|\d+[.)])[ \t]+")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


ParsedMarkdown = ParsedDocument


def normalize_markdown(content: str) -> str:
    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def parse_markdown(content: str) -> ParsedMarkdown:
    """保留 NFC 全文的 code-point offset，产出可稳定引用的块。"""

    full_text = normalize_markdown(content)
    lines = full_text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip("\n")
        if not line.strip():
            index += 1
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings = headings[: level - 1]
            headings.append(title)
            _append_block(
                blocks, full_text, offsets[index], offsets[index] + len(line), "title", headings
            )
            index += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            end_index = index + 1
            while end_index < len(lines):
                if lines[end_index].lstrip().startswith(marker):
                    end_index += 1
                    break
                end_index += 1
            start = offsets[index]
            end = _trimmed_line_end(lines, offsets, end_index - 1)
            _append_block(blocks, full_text, start, end, "code", headings)
            index = end_index
            continue

        end_index = index + 1
        while end_index < len(lines):
            candidate = lines[end_index].rstrip("\n")
            if not candidate.strip() or HEADING_RE.match(candidate) or FENCE_RE.match(candidate):
                break
            end_index += 1
        start = offsets[index]
        end = _trimmed_line_end(lines, offsets, end_index - 1)
        block_text = full_text[start:end]
        block_type = _classify_block(block_text)
        _append_block(blocks, full_text, start, end, block_type, headings)
        index = end_index

    if not blocks:
        raise ValueError("Markdown 文档没有可索引内容")
    return ParsedMarkdown(full_text=full_text, blocks=blocks)


def _trimmed_line_end(lines: list[str], offsets: list[int], index: int) -> int:
    return offsets[index] + len(lines[index].rstrip("\n"))


def _classify_block(text: str) -> str:
    first = text.lstrip().splitlines()[0]
    if LIST_RE.match(first):
        return "list"
    lines = text.splitlines()
    if len(lines) >= 2 and all("|" in line for line in lines[:2]):
        return "table"
    return "paragraph"


def _append_block(
    blocks: list[ParsedBlock],
    full_text: str,
    start: int,
    end: int,
    block_type: str,
    headings: list[str],
) -> None:
    if end <= start:
        return
    blocks.append(
        ParsedBlock(
            block_idx=len(blocks),
            block_type=block_type,
            text=full_text[start:end],
            char_start=start,
            char_end=end,
            heading_path=tuple(headings),
        )
    )
