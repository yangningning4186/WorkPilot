from dataclasses import dataclass
from typing import Any
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
class GoldEvidenceGroup:
    fact_id: str
    alternatives: tuple[GoldSpan, ...]

    def __post_init__(self) -> None:
        if not self.fact_id.strip():
            raise ValueError("fact_id 不能为空")
        if not self.alternatives:
            raise ValueError("事实组至少需要一个等价证据")


def singleton_evidence_groups(spans: list[GoldSpan]) -> list[GoldEvidenceGroup]:
    return [
        GoldEvidenceGroup(fact_id=f"R{index}", alternatives=(span,))
        for index, span in enumerate(spans, start=1)
    ]


def parse_evidence_groups(
    value: object, *, fallback_spans: list[GoldSpan]
) -> list[GoldEvidenceGroup]:
    if not isinstance(value, list) or not value:
        return singleton_evidence_groups(fallback_spans)
    groups: list[GoldEvidenceGroup] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError("gold_evidence_groups 元素必须是对象")
        alternatives = raw.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            raise ValueError("事实组 alternatives 必须是非空数组")
        spans = tuple(_span_from_json(item) for item in alternatives)
        groups.append(
            GoldEvidenceGroup(
                fact_id=str(raw.get("fact_id") or f"R{index}"),
                alternatives=spans,
            )
        )
    if len({group.fact_id for group in groups}) != len(groups):
        raise ValueError("fact_id 不能重复")
    return groups


def flatten_evidence_groups(groups: list[GoldEvidenceGroup]) -> list[GoldSpan]:
    seen: set[tuple[UUID, int, int]] = set()
    spans: list[GoldSpan] = []
    for group in groups:
        for span in group.alternatives:
            key = (span.version_id, span.char_start, span.char_end)
            if key in seen:
                continue
            seen.add(key)
            spans.append(span)
    return spans


def _span_from_json(value: Any) -> GoldSpan:
    if not isinstance(value, dict):
        raise ValueError("alternative 必须是 gold span 对象")
    return GoldSpan(
        version_id=UUID(str(value["version_id"])),
        char_start=int(value["char_start"]),
        char_end=int(value["char_end"]),
        quote=str(value.get("quote") or ""),
    )


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
    return max(0, min(chunk.char_end, span.char_end) - max(chunk.char_start, span.char_start))


def overlap_ratio(chunk: RetrievedChunk, span: GoldSpan) -> float:
    return overlap_characters(chunk, span) / (span.char_end - span.char_start)


def hits(chunk: RetrievedChunk, span: GoldSpan, *, theta: float = 0.5) -> bool:
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 (0,1]")
    return overlap_ratio(chunk, span) >= theta
