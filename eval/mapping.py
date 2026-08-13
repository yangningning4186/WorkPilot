from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class GoldSpan:
    version_id: UUID
    char_start: int
    char_end: int
    quote: str = ""

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("gold span 字符区间无效")


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    version_id: UUID
    char_start: int
    char_end: int
    content_tokens: int
    score: float

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("chunk 字符区间无效")
        if self.content_tokens < 0:
            raise ValueError("chunk token 数不能为负数")


def overlap_characters(chunk: RetrievedChunk, span: GoldSpan) -> int:
    if chunk.version_id != span.version_id:
        return 0
    return max(
        0, min(chunk.char_end, span.char_end) - max(chunk.char_start, span.char_start)
    )


def overlap_ratio(chunk: RetrievedChunk, span: GoldSpan) -> float:
    return overlap_characters(chunk, span) / (span.char_end - span.char_start)


def hits(chunk: RetrievedChunk, span: GoldSpan, *, theta: float = 0.5) -> bool:
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 (0,1]")
    return overlap_ratio(chunk, span) >= theta
