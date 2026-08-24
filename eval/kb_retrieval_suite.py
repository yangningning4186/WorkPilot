"""本地知识库检索评测集：gold 锚在内容，而不是会随切块变化的节点上。

旧检索轨把 gold span 锚到 PostgreSQL 的 document version。数据库退役后，那组 UUID
不再存在；继续沿用只会让一份看似完整的评测集在运行时失去参照物。这里的新格式使用
``(content_hash, page_no, char_start, char_end)``：

* ``content_hash`` 标识不可变的文件内容；
* ``page_no`` 把 PDF 字符区间限定在物理页内，文本类文件为 ``null``；
* 字符区间相对该页（或整份文本）起算，不依赖任何 chunk id。

runner 会在查询前把每个 anchor 逐字映射回实际索引。内容换了、页码错了、区间漂了，
都会 fail-closed，而不是悄悄把未命中算成模型质量下降。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from eval.mapping import GoldEvidenceGroup, GoldSpan

SCHEMA_VERSION = 1
SOURCE_NAMESPACE = UUID("91a44766-d3cb-4e69-94e0-cf90051d7a68")
# 指标层沿用“同一 version_id 下字符区间可比较”的历史契约。新索引的字符区间按页起算，
# 因此给每页一个互不重叠的确定性区段；这样 version_id 仍能表示整篇内容哈希，文档多样性
# 不会把 3 页误算成 3 篇文档。单页十亿字符远高于解析器允许的文件上限。
PAGE_CHAR_STRIDE = 1_000_000_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

Split = Literal["dev", "test"]
Origin = Literal["human", "synthetic"]


@dataclass(frozen=True)
class StableGoldSpan:
    content_hash: str
    page_no: int | None
    char_start: int
    char_end: int
    quote: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.content_hash):
            raise ValueError("content_hash 必须是 64 位小写 SHA-256")
        if self.page_no is not None and (isinstance(self.page_no, bool) or self.page_no < 1):
            raise ValueError("page_no 必须是正整数或 null")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("gold span 字符区间无效")
        if not self.quote:
            raise ValueError("gold span quote 不能为空")
        if len(self.quote) != self.char_end - self.char_start:
            raise ValueError("quote 字符数必须等于 char_end - char_start")
        if self.char_end >= PAGE_CHAR_STRIDE:
            raise ValueError(f"单页字符区间不能达到 {PAGE_CHAR_STRIDE}")

    @property
    def source_id(self) -> UUID:
        return stable_source_id(self.content_hash, self.page_no)

    @property
    def document_id(self) -> UUID:
        return stable_document_id(self.content_hash)

    @property
    def metric_char_start(self) -> int:
        return metric_char_offset(self.page_no, self.char_start)

    @property
    def metric_char_end(self) -> int:
        return metric_char_offset(self.page_no, self.char_end)

    def to_metric_span(self) -> GoldSpan:
        # RetrievalMetrics 里的 version_id 实际表达“同一篇文档”。新索引的字符区间按页
        # 起算，因此 version_id 用 content hash 派生，字符坐标再编码页码；既复用成熟指标，
        # 也不会把索引版本、页面或 chunk 身份误当成文档身份。
        return GoldSpan(
            version_id=self.document_id,
            char_start=self.metric_char_start,
            char_end=self.metric_char_end,
            quote=self.quote,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "page_no": self.page_no,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "source_id": str(self.source_id),
            "document_id": str(self.document_id),
        }


@dataclass(frozen=True)
class StableEvidenceGroup:
    fact_id: str
    alternatives: tuple[StableGoldSpan, ...]

    def __post_init__(self) -> None:
        if not self.fact_id.strip():
            raise ValueError("fact_id 不能为空")
        if not self.alternatives:
            raise ValueError("事实组至少需要一个等价证据")

    def to_metric_group(self) -> GoldEvidenceGroup:
        return GoldEvidenceGroup(
            fact_id=self.fact_id,
            alternatives=tuple(span.to_metric_span() for span in self.alternatives),
        )


@dataclass(frozen=True)
class KbRetrievalItem:
    item_id: str
    split: Split
    category: str
    question: str
    answerable: bool
    evidence_groups: tuple[StableEvidenceGroup, ...]

    @property
    def spans(self) -> tuple[StableGoldSpan, ...]:
        seen: set[tuple[str, int | None, int, int]] = set()
        result: list[StableGoldSpan] = []
        for group in self.evidence_groups:
            for span in group.alternatives:
                key = (span.content_hash, span.page_no, span.char_start, span.char_end)
                if key not in seen:
                    seen.add(key)
                    result.append(span)
        return tuple(result)


@dataclass(frozen=True)
class KbRetrievalSuite:
    name: str
    description: str
    origin: Origin
    review_status: str
    reviewer: str | None
    reviewed_at: str | None
    items: tuple[KbRetrievalItem, ...]
    source_path: Path
    sha256: str


def stable_source_id(content_hash: str, page_no: int | None) -> UUID:
    locator = "text" if page_no is None else f"page:{page_no}"
    return uuid5(SOURCE_NAMESPACE, f"{content_hash}:{locator}")


def stable_document_id(content_hash: str) -> UUID:
    return uuid5(SOURCE_NAMESPACE, f"{content_hash}:document")


def metric_char_offset(page_no: int | None, page_char_offset: int) -> int:
    page_base = 0 if page_no is None else page_no * PAGE_CHAR_STRIDE
    return page_base + page_char_offset


def load_kb_retrieval_suite(
    path: Path,
    *,
    allow_synthetic: bool = False,
) -> KbRetrievalSuite:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("评测集根节点必须是对象")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError(f"schema_version 必须为 {SCHEMA_VERSION}")

    name = _required_text(payload, "name")
    description = str(payload.get("description") or "").strip()
    origin = str(payload.get("origin") or "")
    if origin not in {"human", "synthetic"}:
        raise ValueError("origin 必须是 human 或 synthetic")
    if origin == "synthetic" and not allow_synthetic:
        raise ValueError("synthetic 评测集只能用于工程打通；显式传 --allow-synthetic 才能运行")

    review = payload.get("review")
    review_dict = review if isinstance(review, dict) else {}
    review_status = str(review_dict.get("status") or "pending_human_review")
    reviewer = _optional_text(review_dict.get("reviewer"))
    reviewed_at = _optional_text(review_dict.get("reviewed_at"))
    if review_status not in {"pending_human_review", "approved"}:
        raise ValueError("review.status 必须是 pending_human_review 或 approved")
    if review_status == "pending_human_review":
        if reviewer is not None or reviewed_at is not None:
            raise ValueError("pending_human_review 不能提前填写 reviewer/reviewed_at")
    else:
        if reviewer is None or reviewed_at is None:
            raise ValueError("approved 评测集必须有 reviewer 和 reviewed_at")
        try:
            parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("reviewed_at 必须是 ISO-8601 时间") from error
        if parsed_reviewed_at.tzinfo is None:
            raise ValueError("reviewed_at 必须包含时区")
    if origin == "human" and review_status != "approved":
        raise ValueError("human 评测集必须是 approved")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("评测集 items 必须是非空数组")
    items = tuple(_parse_item(item, index=index) for index, item in enumerate(raw_items, 1))
    item_ids = [item.item_id for item in items]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("item_id 不能重复")
    return KbRetrievalSuite(
        name=name,
        description=description,
        origin=cast("Origin", origin),
        review_status=review_status,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        items=items,
        source_path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def select_suite_items(
    suite: KbRetrievalSuite,
    *,
    include_test: bool,
    test_access_note: str | None,
) -> tuple[KbRetrievalItem, ...]:
    if include_test and not (test_access_note or "").strip():
        raise ValueError("读取冻结 test split 必须提供 --test-access-note")
    selected = (
        suite.items if include_test else tuple(item for item in suite.items if item.split == "dev")
    )
    if not selected:
        raise ValueError("当前选择没有样本；默认只跑 dev，test 需显式 --include-test")
    return selected


def _parse_item(value: object, *, index: int) -> KbRetrievalItem:
    if not isinstance(value, dict):
        raise TypeError(f"items[{index}] 必须是对象")
    item_id = _required_text(value, "item_id")
    split = str(value.get("split") or "dev")
    if split not in {"dev", "test"}:
        raise ValueError(f"{item_id}: split 必须是 dev 或 test")
    category = _required_text(value, "category")
    question = _required_text(value, "question")
    answerable = value.get("answerable")
    if not isinstance(answerable, bool):
        raise TypeError(f"{item_id}: answerable 必须是布尔值")

    raw_groups = value.get("gold_evidence_groups")
    if raw_groups is None:
        raw_groups = []
    if not isinstance(raw_groups, list):
        raise TypeError(f"{item_id}: gold_evidence_groups 必须是数组")
    groups = tuple(
        _parse_group(group, item_id=item_id, index=i) for i, group in enumerate(raw_groups, 1)
    )
    if answerable and not groups:
        raise ValueError(f"{item_id}: 可答题必须包含 gold_evidence_groups")
    if not answerable and groups:
        raise ValueError(f"{item_id}: 不可答题不能伪造 gold evidence")
    fact_ids = [group.fact_id for group in groups]
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError(f"{item_id}: fact_id 不能重复")
    return KbRetrievalItem(
        item_id=item_id,
        split=cast("Split", split),
        category=category,
        question=question,
        answerable=answerable,
        evidence_groups=groups,
    )


def _parse_group(value: object, *, item_id: str, index: int) -> StableEvidenceGroup:
    if not isinstance(value, dict):
        raise TypeError(f"{item_id}: gold_evidence_groups[{index}] 必须是对象")
    alternatives = value.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError(f"{item_id}: 事实组 alternatives 必须是非空数组")
    return StableEvidenceGroup(
        fact_id=_required_text(value, "fact_id"),
        alternatives=tuple(_parse_span(span, item_id=item_id) for span in alternatives),
    )


def _parse_span(value: object, *, item_id: str) -> StableGoldSpan:
    if not isinstance(value, dict):
        raise TypeError(f"{item_id}: gold alternative 必须是对象")
    page_no = value.get("page_no")
    if page_no is not None and (isinstance(page_no, bool) or not isinstance(page_no, int)):
        raise ValueError(f"{item_id}: page_no 必须是正整数或 null")
    try:
        return StableGoldSpan(
            content_hash=str(value["content_hash"]),
            page_no=page_no,
            char_start=int(value["char_start"]),
            char_end=int(value["char_end"]),
            quote=str(value["quote"]),
        )
    except KeyError as error:
        raise ValueError(f"{item_id}: gold span 缺字段 {error.args[0]}") from error


def _required_text(value: dict[str, Any], key: str) -> str:
    text = str(value.get(key) or "").strip()
    if not text:
        raise ValueError(f"{key} 不能为空")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "PAGE_CHAR_STRIDE",
    "SCHEMA_VERSION",
    "KbRetrievalItem",
    "KbRetrievalSuite",
    "StableEvidenceGroup",
    "StableGoldSpan",
    "load_kb_retrieval_suite",
    "metric_char_offset",
    "select_suite_items",
    "stable_document_id",
    "stable_source_id",
]
