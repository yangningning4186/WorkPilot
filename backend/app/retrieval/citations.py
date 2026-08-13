import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.retrieval.dense import DenseSearchHit

CITATION_RE = re.compile(r"\[(S[1-9]\d*)\]")
REFUSAL_TEXT = "资料库中未找到相关信息。"


class CitationValidationError(ValueError):
    def __init__(self, *, unknown_ids: list[str] | None = None, missing: bool = False) -> None:
        self.unknown_ids = unknown_ids or []
        self.missing = missing
        if self.unknown_ids:
            message = f"模型引用了未提供的证据: {', '.join(self.unknown_ids)}"
        else:
            message = "模型回答缺少有效引用"
        super().__init__(message)


@dataclass(frozen=True)
class EvidenceSegment:
    citation_id: str
    block_id: UUID
    version_id: UUID
    document_id: UUID
    title: str
    source_uri: str
    quote: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]


@dataclass(frozen=True)
class Citation:
    citation_id: str
    block_id: UUID
    version_id: UUID
    document_id: UUID
    title: str
    source_uri: str
    quote: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]


def build_evidence_segments(hits: list[DenseSearchHit], *, max_chars: int) -> list[EvidenceSegment]:
    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    segments: list[EvidenceSegment] = []
    seen_blocks: set[UUID] = set()
    remaining = max_chars

    candidates = [
        (hit, block) for hit in hits for block in hit.blocks if block.get("block_type") != "title"
    ]
    if not candidates:
        candidates = [(hit, block) for hit in hits for block in hit.blocks]

    for hit, block in candidates:
        block_id = UUID(str(block["block_id"]))
        if block_id in seen_blocks or remaining <= 0:
            continue
        text = str(block["text"])
        quote = text[:remaining]
        if not quote:
            continue
        char_start = int(block["char_start"])
        segments.append(
            EvidenceSegment(
                citation_id=f"S{len(segments) + 1}",
                block_id=block_id,
                version_id=hit.version_id,
                document_id=hit.document_id,
                title=hit.title,
                source_uri=hit.source_uri,
                quote=quote,
                char_start=char_start,
                char_end=char_start + len(quote),
                heading_path=[str(value) for value in block.get("heading_path") or []],
                locations=list(block.get("locations") or []),
            )
        )
        seen_blocks.add(block_id)
        remaining -= len(quote)
    return segments


def parse_citations(answer: str, evidence: list[EvidenceSegment]) -> list[Citation]:
    labels = CITATION_RE.findall(answer)
    if not labels:
        if answer.strip() == REFUSAL_TEXT:
            return []
        raise CitationValidationError(missing=True)

    evidence_by_id = {segment.citation_id: segment for segment in evidence}
    unknown = list(dict.fromkeys(label for label in labels if label not in evidence_by_id))
    if unknown:
        raise CitationValidationError(unknown_ids=unknown)

    citations: list[Citation] = []
    for label in dict.fromkeys(labels):
        segment = evidence_by_id[label]
        citations.append(
            Citation(
                citation_id=segment.citation_id,
                block_id=segment.block_id,
                version_id=segment.version_id,
                document_id=segment.document_id,
                title=segment.title,
                source_uri=segment.source_uri,
                quote=segment.quote,
                char_start=segment.char_start,
                char_end=segment.char_end,
                heading_path=segment.heading_path,
                locations=segment.locations,
            )
        )
    return citations
