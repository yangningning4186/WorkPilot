"""构建 M1 的 120 条候选评测套件（40 human + 80 pending）。

本脚本不调用模型，也不改写现有 ``core-dev`` / ``english-dev``。它从未被既有
human gold 使用的文档版本中抽取真实字符区间，生成 60 条 dev 增量候选与 20 条
test 留出候选，并可幂等导入四个隔离 staging dataset。

test 按 document version 整体留出；固定 seed 只依赖不可变 group key。任何数量、
分层、quote、重复或跨 split 泄漏不满足约束时均在写库前失败。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from app.core.db import close_database, session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Language = Literal["zh", "en"]
Split = Literal["dev", "test"]

SUITE_NAME = "m1-core-120-candidate-v1"
SPLIT_SEED = "workpilot-m1-120-v1"
NAMESPACE = UUID("98aa74ab-e4b6-52f7-bf4d-b643d0b976bd")
HUMAN_DATASETS = ("core-dev", "english-dev")
TARGET_DATASETS: dict[tuple[Split, Language], str] = {
    ("dev", "zh"): "core-dev-candidates-zh-v1",
    ("dev", "en"): "core-dev-candidates-en-v1",
    ("test", "zh"): "core-test-candidates-zh-v1",
    ("test", "en"): "core-test-candidates-en-v1",
}
ALL_CATEGORIES = (
    "single_hop",
    "multi_hop",
    "table",
    "temporal",
    "unanswerable",
    "global",
    "agent_task",
)
TEST_CATEGORY_TARGET = Counter(
    {
        "single_hop": 5,
        "multi_hop": 4,
        "table": 2,
        "temporal": 2,
        "unanswerable": 3,
        "global": 2,
        "agent_task": 2,
    }
)
EXPECTED_CANDIDATE_COUNTS = Counter(
    {
        "single_hop": 16,
        "multi_hop": 16,
        "table": 2,
        "temporal": 12,
        "unanswerable": 10,
        "global": 12,
        "agent_task": 12,
    }
)
EXPECTED_FULL_COUNTS = Counter(
    {
        "single_hop": 30,
        "multi_hop": 24,
        "table": 12,
        "temporal": 12,
        "unanswerable": 18,
        "global": 12,
        "agent_task": 12,
    }
)
_CJK = re.compile(r"[\u3400-\u9fff]")
_QUESTION_PREFIX = re.compile(r"^(?:Q\d+[.、]?\s*|[⭐🆕\s]+)", re.IGNORECASE)


class CandidateSuiteError(RuntimeError):
    """候选套件不满足 fail-closed 前置条件。"""


@dataclass(frozen=True)
class GroupPlan:
    key: str
    title: str
    language: Language
    categories: tuple[str, ...]


@dataclass(frozen=True)
class BlockAnchor:
    version_id: str
    block_idx: int
    block_type: str
    char_start: int
    char_end: int
    quote: str
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class CandidateItem:
    item_key: str
    group_key: str
    language: Language
    split: Split
    dataset: str
    category: str
    question: str
    gold_answer: str | None
    gold_spans: tuple[dict[str, Any], ...]
    gold_tools: tuple[dict[str, Any], ...]
    constraints: dict[str, Any]
    difficulty: int
    origin: str
    temporal_ctx: str | None
    partition_version_id: str


# 每个 group 固定 5 条；split 算法在 16 个 document group 中找满足精确 test 分层的组合。
GROUPS = (
    GroupPlan(
        "zh-byte",
        "06-答案库-字节四面21题_副本",
        "zh",
        ("multi_hop", "temporal", "unanswerable", "global", "agent_task"),
    ),
    GroupPlan(
        "zh-agent",
        "07-答案库-Agent八股_副本",
        "zh",
        ("single_hop", "single_hop", "multi_hop", "table", "unanswerable"),
    ),
    GroupPlan(
        "zh-rag",
        "08-答案库-RAG与LLM基础微调_副本",
        "zh",
        ("single_hop", "multi_hop", "temporal", "unanswerable", "global"),
    ),
    GroupPlan(
        "zh-serving",
        "09-答案库-推理部署与系统设计_副本",
        "zh",
        ("single_hop", "multi_hop", "unanswerable", "global", "agent_task"),
    ),
    GroupPlan(
        "zh-code",
        "10-答案库-ML手撕代码_副本",
        "zh",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
    GroupPlan(
        "zh-recent",
        "11-近三个月新考点补遗_副本",
        "zh",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
    GroupPlan(
        "zh-ant",
        "12-答案库-蚂蚁Agent一二面_副本",
        "zh",
        ("single_hop", "multi_hop", "temporal", "unanswerable", "agent_task"),
    ),
    GroupPlan(
        "zh-vibe",
        "14-答案库-VibeCoding与Coding Agent开发经验_副本",
        "zh",
        ("single_hop", "multi_hop", "temporal", "unanswerable", "global"),
    ),
    GroupPlan(
        "en-world",
        "Agent-World: Scaling Real-World Environment Synthesis for Evolving General Agent Intelligence",
        "en",
        ("single_hop", "multi_hop", "unanswerable", "global", "agent_task"),
    ),
    GroupPlan(
        "en-anchor",
        "Anchored Self-Play for Code Repair",
        "en",
        ("single_hop", "multi_hop", "table", "unanswerable", "agent_task"),
    ),
    GroupPlan(
        "en-autogen",
        "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation",
        "en",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
    GroupPlan(
        "en-codeskill",
        "CODESKILL: Learning Self-Evolving Skills for Coding Agents",
        "en",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
    GroupPlan(
        "en-memory",
        "MemEvolve: Meta-Evolution of Agent Memory Systems",
        "en",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
    GroupPlan(
        "en-palme",
        "PaLM-E: An Embodied Multimodal Language Model",
        "en",
        ("single_hop", "multi_hop", "temporal", "unanswerable", "agent_task"),
    ),
    GroupPlan(
        "en-socratic",
        "Socratic-SWE: Self-Evolving Coding Agents via Trace-Derived Agent Skills",
        "en",
        ("single_hop", "multi_hop", "temporal", "unanswerable", "global"),
    ),
    GroupPlan(
        "en-swarm",
        "SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery",
        "en",
        ("single_hop", "multi_hop", "temporal", "global", "agent_task"),
    ),
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_test_groups(groups: tuple[GroupPlan, ...] = GROUPS) -> frozenset[str]:
    """按 group 隔离版本，并选出精确 20 条、双语各 10 条的稳定 test。"""

    matches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for chosen in itertools.combinations(groups, 4):
        if Counter(group.language for group in chosen) != {"zh": 2, "en": 2}:
            continue
        categories = Counter(
            category for group in chosen for category in group.categories
        )
        if categories != TEST_CATEGORY_TARGET:
            continue
        keys = tuple(sorted(group.key for group in chosen))
        score = tuple(sorted(_digest(f"{SPLIT_SEED}:{key}") for key in keys))
        matches.append((score, keys))
    if not matches:
        raise CandidateSuiteError("没有 group 组合满足 test 的语言/类别精确配额")
    return frozenset(min(matches)[1])


def _clean_heading(anchor: BlockAnchor) -> str:
    heading = (
        anchor.heading_path[-1] if anchor.heading_path else f"block {anchor.block_idx}"
    )
    heading = _QUESTION_PREFIX.sub("", heading).strip()
    return heading[:180] or f"block {anchor.block_idx}"


def _question(plan: GroupPlan, category: str, anchors: list[BlockAnchor]) -> str:
    labels = [_clean_heading(anchor) for anchor in anchors]
    if plan.language == "zh":
        if category == "single_hop":
            if labels[0].endswith(("?", "？")):
                return labels[0]
            return f"《{plan.title}》在“{labels[0]}”部分给出的核心结论是什么？"
        if category == "multi_hop":
            return f"综合《{plan.title}》“{labels[0]}”与“{labels[1]}”两处证据，可以得到什么结论？"
        if category == "table":
            return f"《{plan.title}》“{labels[0]}”中的表格列出了哪些关键结果？"
        if category == "temporal":
            return (
                f"在该语料版本生效时，《{plan.title}》“{labels[0]}”记录的结论是什么？"
            )
        if category == "global":
            return f"结合《{plan.title}》“{labels[0]}”和“{labels[1]}”，文档强调了哪两个要点？"
        if category == "agent_task":
            return f"检索《{plan.title}》并把与其核心方法有关的证据整理成一条笔记。"
        return f"《{plan.title}》是否给出了商业产品的精确年度订阅价格？"
    if category == "single_hop":
        return f'What core claim does "{plan.title}" make in the section "{labels[0]}"?'
    if category == "multi_hop":
        return f'What conclusion follows by combining "{labels[0]}" and "{labels[1]}" in "{plan.title}"?'
    if category == "table":
        return f'What key results are reported in the table under "{labels[0]}" in "{plan.title}"?'
    if category == "temporal":
        return f'At the time this corpus version became active, what did "{plan.title}" report in "{labels[0]}"?'
    if category == "global":
        return f'Which two points does "{plan.title}" emphasize across "{labels[0]}" and "{labels[1]}"?'
    if category == "agent_task":
        return f'Find evidence about the central method in "{plan.title}" and save it as a concise research note.'
    return f'Does "{plan.title}" specify an exact annual subscription price for its commercial product?'


def _candidate_constraints(
    item_key: str, language: Language, group_key: str
) -> dict[str, Any]:
    return {
        "must_include": [],
        "must_not_include": [],
        "candidate_review": {
            "status": "pending_human",
            "item_key": item_key,
            "language": language,
            "partition_key": group_key,
            "generator": "extractive-no-model-v1",
        },
    }


def _select_anchors(
    blocks: list[BlockAnchor], category: str, used: set[int]
) -> list[BlockAnchor]:
    if category in {"unanswerable", "agent_task"}:
        return []
    needed = 2 if category in {"multi_hop", "global"} else 1
    if category == "table":
        eligible = [block for block in blocks if block.block_type == "table"]
    else:
        eligible = [
            block
            for block in blocks
            if block.block_type in {"paragraph", "list", "figure_caption"}
            and 80 <= len(block.quote) <= 2000
        ]
    selected = [block for block in eligible if block.block_idx not in used][:needed]
    if len(selected) != needed:
        raise CandidateSuiteError(
            f"证据块不足: category={category}, needed={needed}, available={len(selected)}"
        )
    used.update(block.block_idx for block in selected)
    return selected


def build_group_items(
    plan: GroupPlan,
    *,
    split: Split,
    version_id: str,
    activated_at: str,
    blocks: list[BlockAnchor],
) -> list[CandidateItem]:
    used: set[int] = set()
    items: list[CandidateItem] = []
    category_occurrence: Counter[str] = Counter()
    for category in plan.categories:
        category_occurrence[category] += 1
        item_key = f"{plan.key}:{category}:{category_occurrence[category]}"
        anchors = _select_anchors(blocks, category, used)
        spans = tuple(
            {
                "version_id": anchor.version_id,
                "char_start": anchor.char_start,
                "char_end": anchor.char_end,
                "quote": anchor.quote,
                "note": f"{SUITE_NAME}; pending human review; {item_key}",
            }
            for anchor in anchors
        )
        gold_answer = "\n\n".join(anchor.quote for anchor in anchors) or None
        tools: tuple[dict[str, Any], ...] = ()
        if category == "agent_task":
            tools = (
                {"name": "search_knowledge", "arguments": {}},
                {"name": "write_note", "arguments": {}},
            )
            gold_answer = "A concise evidence-grounded note saved to the workspace."
        question = _question(plan, category, anchors)
        if category_occurrence[category] > 1 and anchors:
            suffix = (
                f"（证据段 {anchors[0].block_idx}）"
                if plan.language == "zh"
                else f" (evidence block {anchors[0].block_idx})"
            )
            question += suffix
        items.append(
            CandidateItem(
                item_key=item_key,
                group_key=plan.key,
                language=plan.language,
                split=split,
                dataset=TARGET_DATASETS[(split, plan.language)],
                category=category,
                question=question,
                gold_answer=gold_answer,
                gold_spans=spans,
                gold_tools=tools,
                constraints=_candidate_constraints(item_key, plan.language, plan.key),
                difficulty=3
                if category in {"multi_hop", "global", "agent_task"}
                else 2,
                origin="synthetic",
                temporal_ctx=activated_at if category == "temporal" else None,
                partition_version_id=version_id,
            )
        )
    return items


def _normalized_question(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


def _span_keys(item: CandidateItem) -> set[tuple[str, int, int]]:
    return {
        (str(span["version_id"]), int(span["char_start"]), int(span["char_end"]))
        for span in item.gold_spans
    }


def fingerprint_items(items: list[CandidateItem]) -> str:
    payload = [asdict(item) for item in sorted(items, key=lambda entry: entry.item_key)]
    return _digest(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def validate_candidate_items(
    items: list[CandidateItem],
    *,
    human_versions: set[str],
    human_questions: set[str] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    if len(items) != 80:
        failures.append(f"candidate_count={len(items)}, expected=80")
    if Counter(item.category for item in items) != EXPECTED_CANDIDATE_COUNTS:
        failures.append(
            f"candidate category distribution={Counter(item.category for item in items)}"
        )
    split_counts = Counter(item.split for item in items)
    language_counts = Counter((item.split, item.language) for item in items)
    if split_counts != {"dev": 60, "test": 20}:
        failures.append(f"split counts={split_counts}")
    if language_counts != {
        ("dev", "zh"): 30,
        ("dev", "en"): 30,
        ("test", "zh"): 10,
        ("test", "en"): 10,
    }:
        failures.append(f"language counts={language_counts}")
    test_categories = Counter(item.category for item in items if item.split == "test")
    if test_categories != TEST_CATEGORY_TARGET:
        failures.append(f"test category distribution={test_categories}")

    keys = [item.item_key for item in items]
    if len(keys) != len(set(keys)):
        failures.append("duplicate item_key")
    questions: dict[str, list[str]] = defaultdict(list)
    for item in items:
        questions[_normalized_question(item.question)].append(item.item_key)
        has_answer = bool((item.gold_answer or "").strip())
        if item.origin != "synthetic":
            failures.append(f"candidate origin must be synthetic: {item.item_key}")
        review = item.constraints.get("candidate_review") or {}
        if review.get("status") != "pending_human":
            failures.append(
                f"candidate review status must be pending_human: {item.item_key}"
            )
        if item.language == "en" and _CJK.search(item.question):
            failures.append(f"english question contains CJK: {item.item_key}")
        if item.category == "unanswerable":
            if item.gold_spans or has_answer or item.gold_tools:
                failures.append(f"unanswerable contract: {item.item_key}")
        elif item.category == "agent_task":
            if not item.gold_tools:
                failures.append(f"agent_task missing gold_tools: {item.item_key}")
        elif item.category == "global":
            if not has_answer:
                failures.append(f"global missing answer: {item.item_key}")
        elif not item.gold_spans or not has_answer:
            failures.append(f"answerable missing gold: {item.item_key}")
        if (item.category == "temporal") != (item.temporal_ctx is not None):
            failures.append(f"temporal_ctx contract: {item.item_key}")
        for span in item.gold_spans:
            start, end, quote = (
                int(span["char_start"]),
                int(span["char_end"]),
                str(span["quote"]),
            )
            if start < 0 or end <= start or len(quote) != end - start:
                failures.append(f"span range/quote length: {item.item_key}")
    duplicate_questions = {
        key: value for key, value in questions.items() if len(value) > 1
    }
    if duplicate_questions:
        failures.append(f"duplicate questions={duplicate_questions}")
    existing_overlap = sorted(set(questions) & (human_questions or set()))
    if existing_overlap:
        failures.append(
            f"existing-vs-candidate duplicate questions={existing_overlap[:10]}"
        )

    dev_items = [item for item in items if item.split == "dev"]
    test_items = [item for item in items if item.split == "test"]
    dev_versions = {item.partition_version_id for item in dev_items} | human_versions
    test_versions = {item.partition_version_id for item in test_items}
    shared_versions = sorted(dev_versions & test_versions)
    if shared_versions:
        failures.append(f"cross-split versions={shared_versions}")
    dev_spans = set().union(*(_span_keys(item) for item in dev_items))
    test_spans = set().union(*(_span_keys(item) for item in test_items))
    shared_spans = sorted(dev_spans & test_spans)
    if shared_spans:
        failures.append(f"cross-split spans={shared_spans}")
    if failures:
        raise CandidateSuiteError("; ".join(failures))
    return {
        "candidate_count": len(items),
        "split_counts": dict(sorted(split_counts.items())),
        "language_counts": {
            f"{split_}:{language}": count
            for (split_, language), count in sorted(language_counts.items())
        },
        "category_counts": dict(
            sorted(Counter(item.category for item in items).items())
        ),
        "test_category_counts": dict(sorted(test_categories.items())),
        "cross_split_question_duplicates": 0,
        "cross_split_gold_span_duplicates": 0,
        "cross_split_version_duplicates": 0,
        "existing_vs_candidate_question_duplicates": 0,
    }


async def _load_human_summary(
    session: AsyncSession,
) -> tuple[dict[str, Any], set[str], set[str]]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT ds.name, i.id, i.category, i.question, i.gold_answer,
                           i.gold_spans, i.gold_tools, i.constraints, i.difficulty,
                           i.origin, i.temporal_ctx,
                           validate_eval_spans(i.gold_spans) AS spans_valid
                    FROM eval_items i
                    JOIN eval_datasets ds ON ds.id=i.dataset_id
                    WHERE ds.name=ANY(:names) AND i.origin='human'
                    ORDER BY ds.name, i.id
                    """
                ),
                {"names": list(HUMAN_DATASETS)},
            )
        )
        .mappings()
        .all()
    )
    counts = Counter(str(row["name"]) for row in rows)
    if counts != {"core-dev": 20, "english-dev": 20}:
        raise CandidateSuiteError(
            f"既有 human 必须是 core-dev=20/english-dev=20, 实际={counts}"
        )
    invalid = [
        str(row["id"]) for row in rows if row["gold_spans"] and not row["spans_valid"]
    ]
    if invalid:
        raise CandidateSuiteError(f"既有 human 含 stale gold span: {invalid[:10]}")
    versions = {
        str(span["version_id"]) for row in rows for span in (row["gold_spans"] or [])
    }
    canonical = [
        {
            key: (
                str(row[key])
                if key in {"id", "temporal_ctx"} and row[key] is not None
                else row[key]
            )
            for key in (
                "name",
                "id",
                "category",
                "question",
                "gold_answer",
                "gold_spans",
                "gold_tools",
                "constraints",
                "difficulty",
                "origin",
                "temporal_ctx",
            )
        }
        for row in rows
    ]
    summary = {
        "item_count": len(rows),
        "origin": "human",
        "datasets": dict(sorted(counts.items())),
        "category_counts": dict(
            sorted(Counter(str(row["category"]) for row in rows).items())
        ),
        "fingerprint": _digest(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
        ),
    }
    return (
        summary,
        versions,
        {_normalized_question(str(row["question"])) for row in rows},
    )


async def _load_group_blocks(
    session: AsyncSession,
) -> dict[str, tuple[str, str, list[BlockAnchor]]]:
    titles = [group.title for group in GROUPS]
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT d.title, v.id AS version_id, v.activated_at, v.full_text,
                           b.block_idx, b.block_type, b.char_start, b.char_end,
                           b.text, COALESCE(b.heading_path, ARRAY[]::text[]) AS heading_path
                    FROM documents d
                    JOIN document_versions v ON v.document_id=d.id
                      AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                    JOIN parsed_blocks b ON b.version_id=v.id
                    WHERE d.deleted_at IS NULL AND d.title=ANY(:titles)
                    ORDER BY d.title, b.block_idx
                    """
                ),
                {"titles": titles},
            )
        )
        .mappings()
        .all()
    )
    by_title: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_title[str(row["title"])].append(row)
    missing = sorted(set(titles) - set(by_title))
    if missing:
        raise CandidateSuiteError(f"缺少候选文档或激活版本: {missing}")
    loaded: dict[str, tuple[str, str, list[BlockAnchor]]] = {}
    for title in titles:
        title_rows = by_title[title]
        version_ids = {str(row["version_id"]) for row in title_rows}
        if len(version_ids) != 1:
            raise CandidateSuiteError(
                f"文档必须恰好一个激活版本: {title} -> {sorted(version_ids)}"
            )
        full_texts = {str(row["full_text"]) for row in title_rows}
        if len(full_texts) != 1:
            raise CandidateSuiteError(f"同一版本 full_text 不一致: {title}")
        full_text = next(iter(full_texts))
        blocks: list[BlockAnchor] = []
        for row in title_rows:
            start, end, quote = (
                int(row["char_start"]),
                int(row["char_end"]),
                str(row["text"]),
            )
            if full_text[start:end] != quote:
                raise CandidateSuiteError(
                    f"parsed block 无法从 full_text 回切: {title}#{row['block_idx']}"
                )
            blocks.append(
                BlockAnchor(
                    version_id=str(row["version_id"]),
                    block_idx=int(row["block_idx"]),
                    block_type=str(row["block_type"]),
                    char_start=start,
                    char_end=end,
                    quote=quote,
                    heading_path=tuple(row["heading_path"] or ()),
                )
            )
        activated_at = title_rows[0]["activated_at"].isoformat()
        loaded[title] = (next(iter(version_ids)), activated_at, blocks)
    return loaded


async def build_suite(
    session: AsyncSession,
) -> tuple[list[CandidateItem], dict[str, Any]]:
    human, human_versions, human_questions = await _load_human_summary(session)
    loaded = await _load_group_blocks(session)
    await session.rollback()
    test_groups = stable_test_groups()
    items: list[CandidateItem] = []
    for plan in GROUPS:
        version_id, activated_at, blocks = loaded[plan.title]
        if version_id in human_versions:
            raise CandidateSuiteError(f"候选文档版本已被既有 human 使用: {plan.title}")
        split: Split = "test" if plan.key in test_groups else "dev"
        items.extend(
            build_group_items(
                plan,
                split=split,
                version_id=version_id,
                activated_at=activated_at,
                blocks=blocks,
            )
        )
    validation = validate_candidate_items(
        items, human_versions=human_versions, human_questions=human_questions
    )
    full_counts = Counter(human["category_counts"]) + Counter(
        validation["category_counts"]
    )
    if full_counts != EXPECTED_FULL_COUNTS:
        raise CandidateSuiteError(f"120 条总类别分布错误: {full_counts}")
    return items, {
        "human": human,
        "validation": validation,
        "test_groups": sorted(test_groups),
        "full_category_counts": dict(sorted(full_counts.items())),
    }


def _dataset_fingerprint(items: list[CandidateItem]) -> str:
    return fingerprint_items(items)


# temporal 题必须要求跨时间点比较, 而不是"某个时刻的快照是什么"。
# 判据取"问题里出现明确的时间对照词", 与"证据跨 ≥2 个 version"二选一即可满足:
# 前者覆盖同一文档内含时间线的情况, 后者覆盖对照两个版本的情况。
# 词表刻意只收明确表达先后或变化的词——"之前 / since"这类过宽的词不收,
# 否则改写者随手加一个词就能骗过门禁, 检测器就又变回摆设。
_TEMPORAL_CONTRAST_MARKERS = (
    "相比",
    "相较",
    "对比",
    "变化",
    "变更",
    "趋势",
    "此前",
    "先前",
    "原先",
    "更新前",
    "更新后",
    "改为",
    "不再",
    "已废弃",
    "演进",
    "compared with",
    "compared to",
    "changed from",
    "changed to",
    "previously",
    "prior to",
    "no longer",
    "used to",
    "trend",
    "evolved",
    "superseded",
    "deprecated",
)


def _has_historical_contrast(item: CandidateItem) -> bool:
    """这条 temporal 题是否真的要求跨时间点比较。"""
    if len({str(span["version_id"]) for span in item.gold_spans}) >= 2:
        return True
    question = item.question.casefold()
    return any(marker in question for marker in _TEMPORAL_CONTRAST_MARKERS)


def audit_content_quality(items: list[CandidateItem]) -> dict[str, Any]:
    """阻止结构正确但语义仍是自动草稿的候选被写入 staging。

    每一条 finding 都必须是**可通过改写消除**的真实判据。按类别无条件打标的"检测器"
    会让门禁永远非空, `--apply` 永远失败, 扩集在工程上死锁——那不是 fail-closed,
    只是坏掉的闸门。
    """

    findings: dict[str, list[str]] = defaultdict(list)
    generic_markers = (
        "给出的核心结论是什么",
        "可以得到什么结论",
        "列出了哪些关键结果",
        "what core claim does",
        "what conclusion follows by combining",
        "what key results are reported",
    )
    for item in items:
        question = item.question.casefold()
        if re.search(r"\bblock\s+\d+\b", question):
            findings["placeholder_heading"].append(item.item_key)
        if any(marker in question for marker in generic_markers):
            findings["generic_question_template"].append(item.item_key)
        quoted_answer = "\n\n".join(str(span["quote"]) for span in item.gold_spans)
        if quoted_answer and (item.gold_answer or "").strip() == quoted_answer.strip():
            findings["raw_quote_as_gold_answer"].append(item.item_key)
        if item.category == "temporal" and not _has_historical_contrast(item):
            findings["temporal_without_historical_contrast"].append(item.item_key)
        if item.category == "global":
            span_versions = {str(span["version_id"]) for span in item.gold_spans}
            if len(span_versions) < 2:
                findings["global_single_document"].append(item.item_key)
        if item.category == "agent_task":
            if any(not tool.get("arguments") for tool in item.gold_tools):
                findings["agent_task_empty_arguments"].append(item.item_key)
            if item.language == "zh" and not _CJK.search(item.gold_answer or ""):
                findings["agent_task_answer_language_mismatch"].append(item.item_key)
    return {
        "status": "passed" if not findings else "rejected_content_quality",
        "finding_counts": {key: len(value) for key, value in sorted(findings.items())},
        "sample_item_keys": {
            key: value[:10] for key, value in sorted(findings.items())
        },
    }


async def import_candidates(
    session: AsyncSession,
    items: list[CandidateItem],
    *,
    datasets: dict[tuple[Split, Language], str] | None = None,
    suite_name: str = SUITE_NAME,
    version: str = "candidate-1",
) -> dict[str, Any]:
    """只向隔离 staging dataset 插入确定性 ID；冲突或漂移直接失败。

    `datasets` / `suite_name` / `version` 可换，让不同来源的候选套件共用同一套
    幂等与漂移校验，而不是各写一份导入逻辑——导入纪律只能有一处实现。
    """

    target_datasets = datasets or TARGET_DATASETS
    result: dict[str, Any] = {}
    async with session.begin():
        for (split, language), name in target_datasets.items():
            expected_items = sorted(
                [item for item in items if item.dataset == name],
                key=lambda item: item.item_key,
            )
            dataset_id = uuid5(NAMESPACE, f"dataset:{name}")
            description = (
                f"{suite_name} {language} {split} 增量候选；全部 synthetic/pending，"
                "人工逐条复核前不得用于正式结论"
            )
            await session.execute(
                text(
                    """
                    INSERT INTO eval_datasets (id, name, split, version, description)
                    VALUES (:id, :name, :split, :version, :description)
                    ON CONFLICT (name) DO NOTHING
                    """
                ),
                {
                    "id": dataset_id,
                    "name": name,
                    "split": split,
                    "version": version,
                    "description": description,
                },
            )
            stored_dataset = (
                (
                    await session.execute(
                        text(
                            "SELECT id, split, version, description FROM eval_datasets WHERE name=:name"
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .one()
            )
            if (
                stored_dataset["split"] != split
                or stored_dataset["version"] != version
                or stored_dataset["description"] != description
            ):
                raise CandidateSuiteError(f"staging dataset 元数据冲突: {name}")
            actual_dataset_id = stored_dataset["id"]
            expected_ids = {
                uuid5(NAMESPACE, f"item:{item.item_key}") for item in expected_items
            }
            stored_ids = set(
                (
                    await session.execute(
                        text("SELECT id FROM eval_items WHERE dataset_id=:dataset_id"),
                        {"dataset_id": actual_dataset_id},
                    )
                ).scalars()
            )
            unexpected = stored_ids - expected_ids
            if unexpected:
                raise CandidateSuiteError(
                    f"staging dataset 含非本 manifest 条目: {name} -> {sorted(map(str, unexpected))[:5]}"
                )
            for item in expected_items:
                item_id = uuid5(NAMESPACE, f"item:{item.item_key}")
                await session.execute(
                    text(
                        """
                        INSERT INTO eval_items
                          (id, dataset_id, category, question, gold_answer, gold_spans,
                           gold_tools, constraints, temporal_ctx, difficulty, origin)
                        VALUES
                          (:id, :dataset_id, :category, :question, :gold_answer,
                           CAST(:gold_spans AS jsonb), CAST(:gold_tools AS jsonb),
                           CAST(:constraints AS jsonb), :temporal_ctx, :difficulty, 'synthetic')
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": item_id,
                        "dataset_id": actual_dataset_id,
                        "category": item.category,
                        "question": item.question,
                        "gold_answer": item.gold_answer,
                        "gold_spans": json.dumps(item.gold_spans, ensure_ascii=False),
                        "gold_tools": json.dumps(item.gold_tools, ensure_ascii=False),
                        "constraints": json.dumps(item.constraints, ensure_ascii=False),
                        "temporal_ctx": datetime.fromisoformat(item.temporal_ctx)
                        if item.temporal_ctx
                        else None,
                        "difficulty": item.difficulty,
                    },
                )
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, category, question, gold_answer, gold_spans, gold_tools,
                                   constraints, temporal_ctx, difficulty, origin,
                                   validate_eval_spans(gold_spans) AS spans_valid
                            FROM eval_items WHERE dataset_id=:dataset_id ORDER BY id
                            """
                        ),
                        {"dataset_id": actual_dataset_id},
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) != len(expected_items) or any(
                row["origin"] != "synthetic" for row in rows
            ):
                raise CandidateSuiteError(
                    f"staging dataset 条目数量/origin 错误: {name}"
                )
            if any(row["gold_spans"] and not row["spans_valid"] for row in rows):
                raise CandidateSuiteError(f"staging dataset 含 stale span: {name}")
            expected_by_id = {
                uuid5(NAMESPACE, f"item:{item.item_key}"): item
                for item in expected_items
            }
            for row in rows:
                expected = expected_by_id[row["id"]]
                expected_row = {
                    "category": expected.category,
                    "question": expected.question,
                    "gold_answer": expected.gold_answer,
                    "gold_spans": list(expected.gold_spans),
                    "gold_tools": list(expected.gold_tools),
                    "constraints": expected.constraints,
                    "temporal_ctx": datetime.fromisoformat(expected.temporal_ctx)
                    if expected.temporal_ctx
                    else None,
                    "difficulty": expected.difficulty,
                    "origin": expected.origin,
                }
                actual_row = {key: row[key] for key in expected_row}
                if actual_row != expected_row:
                    raise CandidateSuiteError(
                        f"staging item 内容与 manifest 漂移: {name}/{row['id']}"
                    )
            result[name] = {
                "dataset_id": str(actual_dataset_id),
                "item_count": len(rows),
                "fingerprint": _dataset_fingerprint(expected_items),
                "origin": "synthetic",
                "review_status": "pending_human",
            }
    return result


def _review_record(item: CandidateItem) -> dict[str, Any]:
    return {
        "item_key": item.item_key,
        "dataset": item.dataset,
        "split": item.split,
        "language": item.language,
        "category": item.category,
        "question": item.question,
        "gold_answer": item.gold_answer,
        "gold_spans": item.gold_spans,
        "gold_tools": item.gold_tools,
        "constraints": item.constraints,
        "difficulty": item.difficulty,
        "temporal_ctx": item.temporal_ctx,
        "origin": item.origin,
        "partition_version_id": item.partition_version_id,
        "review_status": "pending_human",
        "reviewer": "",
        "reviewed_at": "",
        "review_note": "",
    }


def write_outputs(
    output_root: Path,
    items: list[CandidateItem],
    summary: dict[str, Any],
    imported: dict[str, Any] | None,
    quality: dict[str, Any] | None = None,
) -> Path:
    suite_fingerprint = fingerprint_items(items)
    output_dir = output_root / suite_fingerprint[:16]
    output_dir.mkdir(parents=True, exist_ok=True)
    by_dataset = {
        name: {
            "split": split,
            "language": language,
            "item_count": sum(item.dataset == name for item in items),
            "fingerprint": _dataset_fingerprint(
                [item for item in items if item.dataset == name]
            ),
            "origin": "synthetic",
            "review_status": "pending_human",
        }
        for (split, language), name in TARGET_DATASETS.items()
    }
    manifest = {
        "schema_version": 1,
        "suite": SUITE_NAME,
        "suite_fingerprint": suite_fingerprint,
        "split_seed": SPLIT_SEED,
        "target": {"dev": 100, "test": 20},
        "existing_human": summary["human"],
        "candidate_datasets": by_dataset,
        "candidate_validation": summary["validation"],
        "full_category_counts": summary["full_category_counts"],
        "test_partition_groups": summary["test_groups"],
        "import": {"applied": imported is not None, "datasets": imported or {}},
        "review": {
            "status": (quality or {}).get("status", "pending_human"),
            "pending_items": 80,
            "promotion_rule": "仅 reviewer/reviewed_at 完整且逐条确认后，才可转 human",
            "test_discipline": "test 仅供独立标注复核；不得用于阈值、prompt 或检索参数调优",
        },
        "content_quality": quality or {"status": "not_run"},
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for split in ("dev", "test"):
        records = [
            _review_record(item)
            for item in sorted(items, key=lambda entry: entry.item_key)
            if item.split == split
        ]
        (output_dir / f"review-{split}.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
    validation = summary["validation"]
    report = f"""# M1 120 条候选集构建报告

- Suite: `{SUITE_NAME}`
- Fingerprint: `{suite_fingerprint}`
- 既有 human: 40（`core-dev` 20 + `english-dev` 20），未复制、未改写
- 新增候选: 80（dev 60 / test 20），全部 `synthetic` / `pending_human`
- 组合规模: dev 100 / test 20
- 语言: dev zh/en 各 30 个新增候选；test zh/en 各 10
- 新增类别: `{json.dumps(validation["category_counts"], ensure_ascii=False, sort_keys=True)}`
- 120 条总类别: `{json.dumps(summary["full_category_counts"], ensure_ascii=False, sort_keys=True)}`
- 跨 split 重复: question=0 / gold span=0 / document version=0
- 既有 human vs candidate 问题重复: 0
- 导入: `{"applied" if imported is not None else "dry-run"}`
- 内容质量: `{(quality or {}).get("status", "not_run")}`

## 人工复核门槛

1. `review-dev.jsonl` 的 60 条与 `review-test.jsonl` 的 20 条必须逐条核对问题、答案、证据和类别。
2. 新增条目在 reviewer / reviewed_at / review_note 完整前不得改为 `human`，也不得形成正式质量结论。
3. test 只用于独立标注复核和里程碑验收，不得用于阈值、prompt、检索或 Judge 调参。
4. unanswerable 必须人工确认语料确实不含答案；agent_task 必须核对 gold_tools 序列与必要参数。
5. 候选问题由真实章节和证据块确定性生成，人工需要改掉过宽或不自然的措辞，但不得漂移 gold 事实。

## 自动草稿质量门禁

`{json.dumps((quality or {}).get("finding_counts", {}), ensure_ascii=False, sort_keys=True)}`

只要这里非空，本批次就是被拒绝的证据草稿，不算“已扩充到 120 条”，也禁止 `--apply`。
"""
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    return output_dir / "manifest.json"


async def run(*, apply: bool, output_root: Path) -> Path:
    async with session_factory() as session:
        items, summary = await build_suite(session)
        quality = audit_content_quality(items)
        imported = None
        if apply and quality["status"] == "passed":
            imported = await import_candidates(session, items)
    await close_database()
    manifest = write_outputs(output_root, items, summary, imported, quality)
    if apply and quality["status"] != "passed":
        raise CandidateSuiteError(f"内容质量门禁拒绝导入；审计报告: {manifest}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="构建/导入 M1 120 条候选评测套件")
    parser.add_argument(
        "--apply", action="store_true", help="写入四个隔离 staging dataset"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("eval/outputs/dataset-candidates"),
    )
    args = parser.parse_args()
    manifest = asyncio.run(run(apply=args.apply, output_root=args.output_root))
    print(
        json.dumps(
            {"manifest": str(manifest), "applied": args.apply}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
