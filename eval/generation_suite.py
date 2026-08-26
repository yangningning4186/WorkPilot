"""文件型 KB 的 grounded-generation 评测集契约。

PostgreSQL 退役后，旧 ``eval_items`` 里的 UUID 不再是可复现锚点。v2 把 gold 固定到
``(content_hash, page_no, char_start, char_end)``，并把 70 条题目、答案和约束一起纳入
suite 文件；runner 不再依赖一份本机数据库才能知道自己在测什么。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class GenerationGoldSpan:
    content_hash: str
    filename: str
    page_no: int | None
    char_start: int
    char_end: int
    quote: str
    migration_match_score: float

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.content_hash):
            raise ValueError("gold content_hash 必须是 64 位小写 SHA-256")
        if not self.filename.strip():
            raise ValueError("gold filename 不能为空")
        if self.page_no is not None and (isinstance(self.page_no, bool) or self.page_no < 1):
            raise ValueError("gold page_no 必须为正整数或 null")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("gold 字符区间无效")
        if len(self.quote) != self.char_end - self.char_start:
            raise ValueError("gold quote 长度必须等于 char_end - char_start")
        if not 0.0 <= self.migration_match_score <= 1.0:
            raise ValueError("migration_match_score 必须位于 [0, 1]")


@dataclass(frozen=True)
class GenerationEvidenceGroup:
    fact_id: str
    alternatives: tuple[GenerationGoldSpan, ...]

    def __post_init__(self) -> None:
        if not self.fact_id.strip() or not self.alternatives:
            raise ValueError("事实组必须有 fact_id 和至少一个等价证据")


@dataclass(frozen=True)
class GenerationItem:
    item_id: str
    dataset_name: str
    split: str
    category: str
    difficulty: int
    question: str
    gold_answer: str
    constraints: dict[str, tuple[str, ...]]
    temporal_ctx: str | None
    evidence_groups: tuple[GenerationEvidenceGroup, ...]

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"


@dataclass(frozen=True)
class GenerationCorpusDocument:
    filename: str
    content_hash: str
    source_kb: str


@dataclass(frozen=True)
class GenerationSuite:
    name: str
    description: str
    origin: str
    reviewer: str
    reviewed_at: str
    kb_slug: str
    corpus: tuple[GenerationCorpusDocument, ...]
    items: tuple[GenerationItem, ...]
    source_path: Path
    sha256: str


def load_generation_suite(path: Path) -> GenerationSuite:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("generation suite 根节点必须是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"generation suite schema_version 必须为 {SCHEMA_VERSION}")
    name = _text(payload, "name")
    origin = _text(payload, "origin")
    if origin != "human":
        raise ValueError("正式 generation suite 必须是 human")
    review = _mapping(payload.get("review"), "review")
    if review.get("status") != "approved":
        raise ValueError("正式 generation suite 必须人工 approved")
    reviewer = _text(review, "reviewer")
    reviewed_at = _text(review, "reviewed_at")
    parsed_review = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    if parsed_review.tzinfo is None:
        raise ValueError("reviewed_at 必须包含时区")

    corpus_raw = _mapping(payload.get("corpus"), "corpus")
    kb_slug = _text(corpus_raw, "kb_slug")
    documents_raw = corpus_raw.get("documents")
    if not isinstance(documents_raw, list) or not documents_raw:
        raise ValueError("corpus.documents 必须是非空数组")
    corpus = tuple(_parse_document(value, index) for index, value in enumerate(documents_raw))
    filenames = [item.filename for item in corpus]
    hashes = [item.content_hash for item in corpus]
    if len(set(filenames)) != len(filenames) or len(set(hashes)) != len(hashes):
        raise ValueError("corpus filename/content_hash 不能重复")

    items_raw = payload.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("items 必须是非空数组")
    items = tuple(_parse_item(value, index) for index, value in enumerate(items_raw))
    if int(payload.get("item_count") or 0) != len(items):
        raise ValueError("item_count 与 items 数量不一致")
    if len({item.item_id for item in items}) != len(items):
        raise ValueError("item_id 不能重复")
    corpus_pairs = {(item.filename, item.content_hash) for item in corpus}
    for item in items:
        if item.answerable and not item.evidence_groups:
            raise ValueError(f"{item.item_id}: 可答题必须有 gold evidence")
        if not item.answerable and item.evidence_groups:
            raise ValueError(f"{item.item_id}: 不可答题不得携带 gold evidence")
        for group in item.evidence_groups:
            for span in group.alternatives:
                if (span.filename, span.content_hash) not in corpus_pairs:
                    raise ValueError(f"{item.item_id}: gold evidence 不在冻结 corpus 中")

    return GenerationSuite(
        name=name,
        description=str(payload.get("description") or "").strip(),
        origin=origin,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        kb_slug=kb_slug,
        corpus=corpus,
        items=items,
        source_path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _parse_document(value: object, index: int) -> GenerationCorpusDocument:
    raw = _mapping(value, f"corpus.documents[{index}]")
    content_hash = _text(raw, "content_hash")
    if not _SHA256.fullmatch(content_hash):
        raise ValueError(f"corpus.documents[{index}].content_hash 非法")
    return GenerationCorpusDocument(
        filename=_text(raw, "filename"),
        content_hash=content_hash,
        source_kb=_text(raw, "source_kb"),
    )


def _parse_item(value: object, index: int) -> GenerationItem:
    raw = _mapping(value, f"items[{index}]")
    constraints_raw = _mapping(raw.get("constraints", {}), f"items[{index}].constraints")
    constraints: dict[str, tuple[str, ...]] = {}
    for key in ("must_include", "must_not_include"):
        values = constraints_raw.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item for item in values
        ):
            raise TypeError(f"items[{index}].constraints.{key} 必须是非空字符串数组")
        constraints[key] = tuple(values)
    groups_raw = raw.get("gold_evidence_groups", [])
    if not isinstance(groups_raw, list):
        raise TypeError(f"items[{index}].gold_evidence_groups 必须是数组")
    groups = tuple(_parse_group(group, index, offset) for offset, group in enumerate(groups_raw))
    return GenerationItem(
        item_id=_text(raw, "item_id"),
        dataset_name=_text(raw, "dataset_name"),
        split=_text(raw, "split"),
        category=_text(raw, "category"),
        difficulty=int(raw.get("difficulty") or 0),
        question=_text(raw, "question"),
        gold_answer=_text(raw, "gold_answer"),
        constraints=constraints,
        temporal_ctx=str(raw["temporal_ctx"]) if raw.get("temporal_ctx") is not None else None,
        evidence_groups=groups,
    )


def _parse_group(value: object, item_index: int, group_index: int) -> GenerationEvidenceGroup:
    raw = _mapping(value, f"items[{item_index}].gold_evidence_groups[{group_index}]")
    alternatives = raw.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("gold evidence group alternatives 必须是非空数组")
    return GenerationEvidenceGroup(
        fact_id=_text(raw, "fact_id"),
        alternatives=tuple(_parse_span(item) for item in alternatives),
    )


def _parse_span(value: object) -> GenerationGoldSpan:
    raw = _mapping(value, "gold span")
    page_no = raw.get("page_no")
    return GenerationGoldSpan(
        content_hash=_text(raw, "content_hash"),
        filename=_text(raw, "filename"),
        page_no=int(page_no) if page_no is not None else None,
        char_start=int(raw["char_start"]),
        char_end=int(raw["char_end"]),
        quote=_text(raw, "quote"),
        migration_match_score=float(raw.get("migration_match_score", 1.0)),
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} 必须是对象")
    return value


def _text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()


__all__ = [
    "GenerationCorpusDocument",
    "GenerationEvidenceGroup",
    "GenerationGoldSpan",
    "GenerationItem",
    "GenerationSuite",
    "load_generation_suite",
]
