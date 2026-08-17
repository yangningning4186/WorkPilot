"""修复 m1-dev-70 benchmark；只允许四个 dev dataset，先快照再原子更新。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import close_database, session_factory
from eval.metrics.generation import evaluate_constraints

DEV_DATASETS = (
    "core-dev",
    "english-dev",
    "core-dev-handwritten-zh-v1",
    "core-dev-handwritten-en-v1",
)

RECLASSIFY = "019ffd37-e50e-707c-aab2-679a84c03b7c"
CROSS_BLOCK = "2fb9c356-2467-568b-b738-c58d2ecd67a1"
TEMPORAL_SNAPSHOT = datetime.fromisoformat("2026-08-14T12:00:00+00:00")

ANSWER_REWRITES = {
    "019ffd27-973f-7b6e-9668-6d87c916676f": (
        "LLM 基础题主要覆盖：Self-Attention 的缩放与多头机制；temperature、top-k、"
        "top-p 等采样参数；KV Cache 的加速原理和显存估算；绝对位置编码与 RoPE 及长度"
        "外推；base/instruct 模型和 CoT；context rot；Transformer 参数与显存估算；以及 "
        "MoE 的总参数、激活参数和路由机制。"
    ),
    "019ffd2a-5430-76cf-a775-1e4c2d798112": (
        "WorkPilot 采用分层架构：Next.js 前端通过 SSE 展示流式对话、引用、Agent 时间线和 "
        "HITL；FastAPI 负责鉴权、限流与会话；业务层分为 LangGraph Agent、RAG 知识链路和"
        "评测层。底层以 Postgres/pgvector 保存业务与向量数据，Redis 和任务队列承载异步执行，"
        "并预留对象存储与全链路观测。"
    ),
    "019ffd2c-4d00-7fa2-9c20-f70a4aeb9945": (
        "成本与性能治理包括四部分：按任务复杂度做模型路由并测质量—成本曲线；采用结果、语义"
        "和 Prompt 三级缓存；通过流式首 token、并发检索和 embedding 批处理降低延迟；最后用"
        "看板持续监控 P50/P95、单会话成本、缓存命中率和各模型调用占比。"
    ),
    "019ffd34-3949-7b6d-b109-1e4feef649ce": (
        "核心选型为：LangGraph 编排可恢复状态机；Postgres + pgvector 以较低运维成本同时提供"
        "事务和向量检索；PG 全文索引承担关键词召回；bge-m3 支持中英与稀疏/稠密表示，"
        "bge-reranker-v2-m3 做交叉编码精排；FastAPI 提供异步 API 与 Pydantic 契约；Next.js"
        "负责流式交互，Langfuse 统一记录 trace、成本和评测。"
    ),
}

QUESTION_REWRITES = {
    "24b880a2-2ef3-50b9-8369-789760dc85fd": (
        "How much does CODESKILL improve relative to each reported baseline, and what gain "
        "does Anchored Self-Play achieve versus its unanchored counterpart?"
    ),
    "7f03fc76-b952-5633-9b8c-b2bcddeded4c": (
        "How does Socratic-SWE turn a one-shot synthetic-task pipeline into an adaptive "
        "training loop?"
    ),
    "8e7cc6a9-bf4c-5f9e-a66f-33d284d71536": (
        "Self-play SWE-RL is described as an earlier open RL approach. Which limitation does "
        "Socratic-SWE argue remains after that work?"
    ),
}

# item_id -> (canonical group index, title, phrase identifying an equivalent parsed block)
ALTERNATIVES = (
    (
        "019ffd37-e52c-7b82-a4e5-151d6563daff",
        0,
        "SimpleMem: Efficient Lifelong Memory for LLM Agents",
        "To address these limitations, we introduce SimpleMem",
    ),
    (
        "4edc1f15-4961-5e37-8f27-39e3699d8548",
        1,
        "Socratic-SWE: Self-Evolving Coding Agents via Trace-Derived Agent Skills",
        "Existing synthetic pipelines attempt to alleviate this bottleneck",
    ),
    (
        "90e184ee-f01f-572f-b8da-6d94edbf2bc5",
        1,
        "CODESKILL: Learning Self-Evolving Skills for Coding Agents",
        "We presented CODESKILL, an LLM-based framework that learns to extract",
    ),
    (
        "9b8dd40e-78b1-5cfa-9ffa-d600471b72cd",
        0,
        "Anchored Self-Play for Code Repair",
        "BUGSOURCEBENCH lets us test whether synthetic bug",
    ),
    (
        "9b8dd40e-78b1-5cfa-9ffa-d600471b72cd",
        1,
        "Socratic-SWE: Self-Evolving Coding Agents via Trace-Derived Agent Skills",
        "We presented Socratic-SWE, a practical closed-loop framework",
    ),
)


async def repair(*, output_dir: Path, apply: bool) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with session_factory() as session:
            rows = await _load_rows(session)
            _preflight(rows)
            snapshot = {
                "schema_version": "m1-dev-70.pre-benchmark-v2",
                "created_at": datetime.now(UTC).isoformat(),
                "datasets": list(DEV_DATASETS),
                "items": [_json_row(row) for row in rows],
            }
            encoded = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n"
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            snapshot_path = output_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{digest[:12]}.json"
            snapshot_path.write_text(encoded, encoding="utf-8")
            if not apply:
                await session.rollback()
                return {"applied": False, "snapshot": str(snapshot_path), "sha256": digest}

            async with session.begin():
                await _apply_rewrites(session)
                await _fix_temporal_snapshot(session)
                await _split_cross_block_span(session)
                await _append_alternatives(session)
                await session.execute(
                    text(
                        """
                        UPDATE eval_datasets
                        SET version = CASE
                            WHEN position('benchmark-v2' IN version) > 0 THEN version
                            ELSE version || '+benchmark-v2'
                        END,
                        updated_at=now()
                        WHERE name=ANY(:names) AND split='dev'
                        """
                    ),
                    {"names": list(DEV_DATASETS)},
                )
            repaired = await _load_rows(session)
            audit = _postflight(repaired)
            audit["temporal_invisible_gold"] = await _temporal_invisible_gold(session)
            if audit["temporal_invisible_gold"]:
                raise ValueError(
                    f"temporal gold 在冻结快照不可见: {audit['temporal_invisible_gold']}"
                )
            await session.rollback()
            return {
                "applied": True,
                "snapshot": str(snapshot_path),
                "sha256": digest,
                "audit": audit,
            }
    finally:
        await close_database()


async def _load_rows(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT i.*, d.name AS dataset_name, d.split AS dataset_split,
                       validate_eval_spans(i.gold_spans) AS spans_valid,
                       validate_eval_evidence_groups(i.gold_evidence_groups)
                         AS groups_valid
                FROM eval_items i
                JOIN eval_datasets d ON d.id=i.dataset_id
                WHERE d.name=ANY(:names) AND i.origin='human'
                ORDER BY d.name, i.id
                """
            ),
            {"names": list(DEV_DATASETS)},
        )
    ).mappings().all()
    await session.rollback()
    return [dict(row) for row in rows]


def _preflight(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 70 or {row["dataset_split"] for row in rows} != {"dev"}:
        raise ValueError("repair 只允许 m1-dev-70 的 70 条 dev human")
    expected_ids = {
        RECLASSIFY,
        CROSS_BLOCK,
        *ANSWER_REWRITES,
        *QUESTION_REWRITES,
        *(item_id for item_id, *_ in ALTERNATIVES),
    }
    actual_ids = {str(row["id"]) for row in rows}
    missing = sorted(expected_ids - actual_ids)
    if missing:
        raise ValueError(f"repair 目标样本缺失: {missing}")
    if any(not row["spans_valid"] or not row["groups_valid"] for row in rows):
        raise ValueError("repair 前 benchmark 已有 stale span/group")
    by_id = {str(row["id"]): row for row in rows}
    for item_id, rewritten in ANSWER_REWRITES.items():
        row = by_id[item_id]
        reviewed = (row["constraints"].get("benchmark_review") or {}).get("version")
        raw = "\n\n".join(span["quote"] for span in row["gold_spans"])
        if reviewed == "benchmark-v2" and row["gold_answer"] != rewritten:
            raise ValueError(f"已修复 gold_answer 再次漂移: {item_id}")
        if reviewed != "benchmark-v2" and row["gold_answer"] != raw:
            raise ValueError(f"原始 raw-quote gold_answer 已漂移: {item_id}")
    for item_id, rewritten in QUESTION_REWRITES.items():
        row = by_id[item_id]
        reviewed = (row["constraints"].get("benchmark_review") or {}).get("version")
        if reviewed == "benchmark-v2" and row["question"] != rewritten:
            raise ValueError(f"已修复 question 再次漂移: {item_id}")
    if by_id[RECLASSIFY]["category"] not in {"multi_hop", "single_hop"}:
        raise ValueError("重分类目标 category 已漂移")
    if len(by_id[CROSS_BLOCK]["gold_spans"]) not in {2, 3}:
        raise ValueError("跨 block 修复目标 span 数已漂移")


async def _apply_rewrites(session: AsyncSession) -> None:
    for item_id, answer in ANSWER_REWRITES.items():
        await session.execute(
            text(
                """
                UPDATE eval_items SET gold_answer=:answer,
                    constraints = constraints || jsonb_build_object(
                        'benchmark_review', jsonb_build_object(
                            'version', 'benchmark-v2', 'action', 'rewrite_gold_answer')),
                    updated_at=now()
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": item_id, "answer": answer},
        )
    for item_id, question in QUESTION_REWRITES.items():
        await session.execute(
            text(
                """
                UPDATE eval_items SET question=:question,
                    constraints = constraints || jsonb_build_object(
                        'benchmark_review', jsonb_build_object(
                            'version', 'benchmark-v2', 'action', 'rewrite_question')),
                    updated_at=now()
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": item_id, "question": question},
        )
    await session.execute(
        text(
            """
            UPDATE eval_items SET category='single_hop',
                constraints = constraints || jsonb_build_object(
                    'benchmark_review', jsonb_build_object(
                        'version', 'benchmark-v2', 'action', 'reclassify_single_hop')),
                updated_at=now()
            WHERE id=CAST(:id AS uuid) AND category='multi_hop'
            """
        ),
        {"id": RECLASSIFY},
    )


async def _fix_temporal_snapshot(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            UPDATE eval_items i SET temporal_ctx=:snapshot,
                constraints = constraints || jsonb_build_object(
                    'benchmark_review', jsonb_build_object(
                        'version', 'benchmark-v2', 'action', 'fix_temporal_snapshot')),
                updated_at=now()
            FROM eval_datasets d
            WHERE d.id=i.dataset_id AND d.name=ANY(:names) AND d.split='dev'
              AND (i.category='temporal' OR i.temporal_ctx IS NOT NULL)
            """
        ),
        {"snapshot": TEMPORAL_SNAPSHOT, "names": list(DEV_DATASETS)},
    )


async def _split_cross_block_span(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text("SELECT gold_spans FROM eval_items WHERE id=CAST(:id AS uuid) FOR UPDATE"),
            {"id": CROSS_BLOCK},
        )
    ).scalar_one()
    spans = list(row)
    if len(spans) == 3:
        return
    if len(spans) != 2:
        raise ValueError("跨 block 修复样本的 canonical span 数已漂移")
    version_id = spans[1]["version_id"]
    blocks = (
        await session.execute(
            text(
                """
                SELECT char_start, char_end, text FROM parsed_blocks
                WHERE version_id=CAST(:version_id AS uuid) AND block_idx IN (82,83)
                ORDER BY block_idx
                """
            ),
            {"version_id": version_id},
        )
    ).mappings().all()
    if len(blocks) != 2:
        raise ValueError("跨 block 修复所需的 block 82/83 不存在")
    split_spans = [
        {
            "version_id": version_id,
            "char_start": int(block["char_start"]),
            "char_end": int(block["char_end"]),
            "quote": str(block["text"]),
            "note": "benchmark-v2: split cross-block fact",
        }
        for block in blocks
    ]
    canonical = [spans[0], *split_spans]
    groups = [
        {"fact_id": f"R{index}", "alternatives": [span]}
        for index, span in enumerate(canonical, start=1)
    ]
    await session.execute(
        text(
            """
            UPDATE eval_items SET gold_spans=CAST(:spans AS jsonb),
                gold_evidence_groups=CAST(:groups AS jsonb),
                constraints = constraints || jsonb_build_object(
                    'benchmark_review', jsonb_build_object(
                        'version', 'benchmark-v2', 'action', 'split_cross_block_span')),
                updated_at=now()
            WHERE id=CAST(:id AS uuid)
            """
        ),
        {
            "id": CROSS_BLOCK,
            "spans": json.dumps(canonical, ensure_ascii=False),
            "groups": json.dumps(groups, ensure_ascii=False),
        },
    )


async def _append_alternatives(session: AsyncSession) -> None:
    for item_id, group_index, title, needle in ALTERNATIVES:
        block = (
            (
                await session.execute(
                    text(
                        """
                        SELECT b.version_id::text AS version_id, b.char_start, b.char_end,
                               b.text
                        FROM parsed_blocks b
                        JOIN document_versions v ON v.id=b.version_id
                        JOIN documents d ON d.id=v.document_id
                        WHERE d.title=:title AND position(:needle IN b.text)>0
                          AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                        """
                    ),
                    {"title": title, "needle": needle},
                )
            )
            .mappings()
            .one()
        )
        groups = list(
            (
                await session.execute(
                    text(
                        "SELECT gold_evidence_groups FROM eval_items "
                        "WHERE id=CAST(:id AS uuid) FOR UPDATE"
                    ),
                    {"id": item_id},
                )
            ).scalar_one()
        )
        alternative = {
            "version_id": block["version_id"],
            "char_start": int(block["char_start"]),
            "char_end": int(block["char_end"]),
            "quote": str(block["text"]),
            "note": "benchmark-v2: equivalent evidence",
        }
        alternatives = groups[group_index]["alternatives"]
        key = (alternative["version_id"], alternative["char_start"], alternative["char_end"])
        existing = {
            (span["version_id"], span["char_start"], span["char_end"])
            for span in alternatives
        }
        if key not in existing:
            alternatives.append(alternative)
        await session.execute(
            text(
                """
                UPDATE eval_items SET gold_evidence_groups=CAST(:groups AS jsonb),
                    constraints = constraints || jsonb_build_object(
                        'benchmark_review', jsonb_build_object(
                            'version', 'benchmark-v2', 'action', 'add_equivalent_evidence')),
                    updated_at=now()
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": item_id, "groups": json.dumps(groups, ensure_ascii=False)},
        )


def _postflight(rows: list[dict[str, Any]]) -> dict[str, object]:
    _preflight(rows)
    categories: dict[str, int] = {}
    raw_answers: list[str] = []
    constraint_failures: list[str] = []
    alternative_count = 0
    for row in rows:
        category = str(row["category"])
        categories[category] = categories.get(category, 0) + 1
        quoted = "\n\n".join(span["quote"] for span in row["gold_spans"])
        if row["gold_answer"] and row["gold_answer"].strip() == quoted.strip():
            raw_answers.append(str(row["id"]))
        if category != "unanswerable":
            result = evaluate_constraints(str(row["gold_answer"] or ""), row["constraints"])
            if not result.passed:
                constraint_failures.append(str(row["id"]))
        alternative_count += sum(
            max(0, len(group["alternatives"]) - 1)
            for group in row["gold_evidence_groups"]
        )
    expected = {
        "single_hop": 20,
        "multi_hop": 13,
        "table": 12,
        "temporal": 6,
        "unanswerable": 13,
        "global": 6,
    }
    if categories != expected or raw_answers or constraint_failures:
        raise ValueError(
            f"postflight 失败: categories={categories}, raw={raw_answers}, "
            f"constraints={constraint_failures}"
        )
    return {
        "item_count": len(rows),
        "category_counts": categories,
        "raw_quote_gold_answers": 0,
        "gold_constraint_failures": 0,
        "equivalent_alternative_count": alternative_count,
        "spans_valid": all(bool(row["spans_valid"]) for row in rows),
        "groups_valid": all(bool(row["groups_valid"]) for row in rows),
    }


async def _temporal_invisible_gold(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT i.id::text
                FROM eval_items i
                JOIN eval_datasets d ON d.id=i.dataset_id
                CROSS JOIN LATERAL jsonb_array_elements(i.gold_spans) span
                JOIN document_versions v ON v.id=(span->>'version_id')::uuid
                WHERE d.name=ANY(:names) AND d.split='dev' AND i.category='temporal'
                  AND NOT (
                    v.activated_at <= i.temporal_ctx
                    AND (v.invalid_at IS NULL OR v.invalid_at > i.temporal_ctx)
                  )
                """
            ),
            {"names": list(DEV_DATASETS)},
        )
    ).scalars().all()
    return [str(value) for value in rows]


def _json_row(row: dict[str, Any]) -> dict[str, object]:
    keys = (
        "id",
        "dataset_id",
        "dataset_name",
        "dataset_split",
        "category",
        "question",
        "gold_answer",
        "gold_spans",
        "gold_evidence_groups",
        "gold_tools",
        "constraints",
        "temporal_ctx",
        "difficulty",
        "origin",
        "created_at",
        "updated_at",
    )
    return {key: row[key] for key in keys}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="修复 m1-dev-70 benchmark-v2")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/outputs/dev-benchmark-repair"),
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    print(
        json.dumps(
            asyncio.run(repair(output_dir=args.output_dir, apply=args.apply)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
