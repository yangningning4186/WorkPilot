from dataclasses import dataclass

from app.ingest.markdown import ParsedBlock, ParsedMarkdown


@dataclass(frozen=True)
class HeadingChunk:
    chunk_index: int
    content: str
    content_tokens: int
    block_start_idx: int
    block_end_idx: int
    char_start: int
    char_end: int
    dominant_block_type: str
    heading_path: tuple[str, ...]


def chunk_by_heading(parsed: ParsedMarkdown, *, max_chars: int = 2000) -> list[HeadingChunk]:
    """标题边界优先；章节过长时仅在 block 边界切分。"""

    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    groups: list[list[ParsedBlock]] = []
    current: list[ParsedBlock] = []
    for block in parsed.blocks:
        has_body = any(item.block_type != "title" for item in current)
        if block.block_type == "title" and current and has_body:
            groups.append(current)
            current = []
        if current and has_body and block.char_end - current[0].char_start > max_chars:
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)

    chunks: list[HeadingChunk] = []
    for blocks in groups:
        start = blocks[0].char_start
        end = blocks[-1].char_end
        content = parsed.full_text[start:end]
        dominant = next(
            (block.block_type for block in blocks if block.block_type != "title"),
            blocks[0].block_type,
        )
        chunks.append(
            HeadingChunk(
                chunk_index=len(chunks),
                content=content,
                content_tokens=max(1, (len(content) + 3) // 4),
                block_start_idx=blocks[0].block_idx,
                block_end_idx=blocks[-1].block_idx,
                char_start=start,
                char_end=end,
                dominant_block_type=dominant,
                heading_path=blocks[-1].heading_path,
            )
        )
    return chunks
