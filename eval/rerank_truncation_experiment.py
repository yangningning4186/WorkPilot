"""P1：tokenizer 级可见性审计与客户端字符 × 服务端 token 的 2×2 实验。

每条 dev 跨文档问题只执行一次 dense/lexical 检索，随后四个实验格复用完全相同的
两臂并集候选。实验只改变候选字符上限与 cross-encoder pair tokenizer 窗口：

- A: 1200 chars / 512 tokens（当前基线）
- B: 8000 chars / 512 tokens（只放开客户端截断）
- C: 1200 chars / 1024 tokens（只放开服务端窗口）
- D: 8000 chars / 1024 tokens（组合）

服务端返回每个 gold span 在未截断/已截断 pair encoding 中的 token 覆盖数，因而
不会再用字符位置或“中文约等于一字一 token”的近似替代真实可见性。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

import httpx
from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, dense_search
from app.retrieval.fusion import rerank_candidate_union
from app.retrieval.lexical import lexical_search
from app.retrieval.reranker import (
    build_candidate_text,
    candidate_content_offset,
    parse_cross_encoder_response,
)
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy
from sqlalchemy.ext.asyncio import AsyncSession

from eval.dense_baseline import (
    EvalItem,
    _candidate_chunks,
    _load_items,
    _retrieved_chunk,
)
from eval.mapping import GoldSpan, RetrievedChunk
from eval.metrics.diagnostics import percentile
from eval.metrics.retrieval import evaluate_retrieval
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import load_suite, validate_suite

CELLS = (
    ("A-1200c-512t", 1200, 512),
    ("B-8000c-512t", 8000, 512),
    ("C-1200c-1024t", 1200, 1024),
    ("D-8000c-1024t", 8000, 1024),
)
AUDIT_STATUSES = (
    "survives_top_k",
    "ranking_mismatch",
    "server_truncated",
    "client_truncated",
    "pool_outside",
)
METRIC_NAMES = (
    "span_recall_at_k",
    "gold_doc_recall_at_k",
    "ndcg_at_k",
    "max_doc_share_at_k",
)


@dataclass(frozen=True)
class GoldCandidateSpan:
    span_index: int
    candidate_id: str
    chunk_id: str
    title: str
    char_start: int
    char_end: int
    rrf_rank: int


@dataclass(frozen=True)
class ItemPool:
    dataset: str
    item: EvalItem
    candidates: list[DenseSearchHit]
    ideal_candidates: list[RetrievedChunk]
    gold_candidates: dict[int, list[GoldCandidateSpan]]


@dataclass(frozen=True)
class CellItemResult:
    item_id: str
    metrics: dict[str, float]
    latency_ms: float
    spans: list[dict[str, object]]


async def run_truncation_experiment(
    *,
    suite_path: Path,
    label: str,
    per_arm_k: int,
    final_top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    timeout_s: float,
    bootstrap_resamples: int,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path:
    if per_arm_k != 50:
        raise ValueError("本轮固定候选池要求 per_arm_k=50，避免混入候选深度变量")
    if final_top_k != 10:
        raise ValueError("本轮固定最终预算要求 final_top_k=10")
    if token_budget < 1 or timeout_s <= 0 or bootstrap_resamples < 1:
        raise ValueError("token_budget/timeout_s/bootstrap_resamples 必须为正数")
    if not 0 < theta <= 1 or not 0 <= alpha < 1:
        raise ValueError("theta/alpha 超出合法范围")

    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("截断实验禁止访问 test suite")
    settings = settings or Settings()
    strategy = validate_chunk_strategy("heading")
    text_mode = settings.rerank_candidate_text_mode

    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
            try:
                pools = await _build_fixed_pools(
                    session,
                    gateway,
                    suite=suite,
                    per_arm_k=per_arm_k,
                    theta=theta,
                    settings=settings,
                    strategy=strategy,
                    text_mode=text_mode,
                )
            finally:
                await gateway.aclose()

        if len(pools) != 16:
            raise ValueError(f"P1 dev 跨文档集合漂移：预期 16 条，实际 {len(pools)}")
        gold_span_count = sum(len(pool.item.gold_spans) for pool in pools)
        if gold_span_count != 33:
            raise ValueError(f"P1 dev gold span 集合漂移：预期 33 条，实际 {gold_span_count}")
        if max(len(pool.candidates) for pool in pools) > 100:
            raise ValueError("当前 reranker 服务契约最多接收 100 个候选")

        async with httpx.AsyncClient(
            base_url=settings.reranker_base_url.rstrip("/"),
            timeout=timeout_s,
            trust_env=False,
        ) as client:
            health_response = await client.get("/health")
            health_response.raise_for_status()
            health = health_response.json()
            service_window = int(health.get("max_length", 0))
            if service_window < 1024:
                raise RuntimeError(
                    "2x2 实验需要服务以 RERANKER_MAX_LENGTH>=1024 启动，"
                    f"当前 {service_window}"
                )
            by_cell: dict[str, list[CellItemResult]] = {}
            for cell_name, char_limit, token_window in CELLS:
                by_cell[cell_name] = [
                    await _run_cell_item(
                        client,
                        pool=pool,
                        char_limit=char_limit,
                        token_window=token_window,
                        final_top_k=final_top_k,
                        token_budget=token_budget,
                        theta=theta,
                        alpha=alpha,
                        model=settings.reranker_model,
                        text_mode=text_mode,
                    )
                    for pool in pools
                ]

        summaries = {
            name: _summarize_cell(results)
            for name, results in by_cell.items()
        }
        comparisons = _paired_comparisons(
            by_cell,
            resamples=bootstrap_resamples,
        )
        payload: dict[str, object] = {
            "schema_version": "rerank-truncation-2x2.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "suite_sha256": _sha256(suite_path),
            "cross_document_item_count": len(pools),
            "gold_span_count": gold_span_count,
            "candidate_pool_sha256": _pool_fingerprint(pools),
            "candidate_count_range": [
                min(len(pool.candidates) for pool in pools),
                max(len(pool.candidates) for pool in pools),
            ],
            "config": {
                "chunk_strategy": strategy,
                "per_arm_k": per_arm_k,
                "candidate_mode": "dense_lexical_union",
                "final_top_k": final_top_k,
                "token_budget": token_budget,
                "theta": theta,
                "alpha": alpha,
                "lexical_mode": settings.lexical_mode,
                "rrf_k": settings.rrf_k,
                "reranker_model": settings.reranker_model,
                "candidate_text_mode": text_mode,
                "cells": [
                    {"name": name, "client_chars": chars, "server_tokens": tokens}
                    for name, chars, tokens in CELLS
                ],
                "latency_note": "包含 tokenizer gold-span 审计开销，不等同于生产纯精排延迟",
            },
            "service_health": health,
            "summaries": summaries,
            "paired_bootstrap": comparisons,
            "decision": _decision(summaries),
            "items": {
                name: [asdict(result) for result in results]
                for name, results in by_cell.items()
            },
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"{stem}.md").write_text(_render_markdown(payload), encoding="utf-8")
        return json_path
    finally:
        await close_database()


async def _build_fixed_pools(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    suite: Any,
    per_arm_k: int,
    theta: float,
    settings: Settings,
    strategy: ChunkStrategy,
    text_mode: str,
) -> list[ItemPool]:
    pools: list[ItemPool] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in items:
            if not _is_cross_document(item):
                continue
            dense_hits = await dense_search(
                session,
                gateway,
                query=item.question,
                top_k=per_arm_k,
                strategy=strategy,
            )
            lexical_hits = await lexical_search(
                session,
                query=item.question,
                top_k=per_arm_k,
                mode=settings.lexical_mode,
                strategy=strategy,
            )
            candidates = rerank_candidate_union(
                dense_hits,
                lexical_hits,
                rrf_k=settings.rrf_k,
                strategy=strategy,
            )
            ideal_candidates = await _candidate_chunks(
                session,
                item.gold_spans,
                embedding_model=gateway.embedding_model,
                embedding_provider=gateway.embedding_provider,
                embedding_revision=gateway.embedding_revision,
                chunk_strategy=strategy,
                token_count_mode="stored",
            )
            pools.append(
                ItemPool(
                    dataset=dataset.name,
                    item=item,
                    candidates=candidates,
                    ideal_candidates=ideal_candidates,
                    gold_candidates=_map_gold_candidates(
                        item.gold_spans,
                        candidates,
                        theta=theta,
                        text_mode=text_mode,
                    ),
                )
            )
    return pools


def _map_gold_candidates(
    spans: list[GoldSpan],
    candidates: list[DenseSearchHit],
    *,
    theta: float,
    text_mode: str,
) -> dict[int, list[GoldCandidateSpan]]:
    mapped: dict[int, list[GoldCandidateSpan]] = {index: [] for index in range(len(spans))}
    for rank, hit in enumerate(candidates, start=1):
        content_offset = candidate_content_offset(hit, mode=text_mode)
        for span_index, span in enumerate(spans):
            if hit.version_id != span.version_id:
                continue
            overlap = max(
                0,
                min(hit.char_end, span.char_end) - max(hit.char_start, span.char_start),
            )
            if overlap / (span.char_end - span.char_start) < theta:
                continue
            # tokenizer 可见性必须审计完整 gold span。当前 P1 标注预期完整落在一个 heading
            # chunk 内；若这个前提漂移，不能把部分覆盖伪装成“模型看见了答案”。
            if span.char_start < hit.char_start or span.char_end > hit.char_end:
                continue
            relative_start = span.char_start - hit.char_start
            relative_end = span.char_end - hit.char_start
            if span.quote and hit.content[relative_start:relative_end] != span.quote:
                raise ValueError(
                    f"gold quote 与候选正文不一致: chunk={hit.chunk_id}, span={span_index}"
                )
            mapped[span_index].append(
                GoldCandidateSpan(
                    span_index=span_index,
                    candidate_id=f"C{rank}",
                    chunk_id=str(hit.chunk_id),
                    title=hit.title,
                    char_start=content_offset + relative_start,
                    char_end=content_offset + relative_end,
                    rrf_rank=rank,
                )
            )
    return mapped


async def _run_cell_item(
    client: httpx.AsyncClient,
    *,
    pool: ItemPool,
    char_limit: int,
    token_window: int,
    final_top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    model: str,
    text_mode: str,
) -> CellItemResult:
    documents: list[dict[str, object]] = []
    sent_lengths: dict[str, int] = {}
    for rank, hit in enumerate(pool.candidates, start=1):
        candidate_id = f"C{rank}"
        candidate_text = build_candidate_text(hit, max_chars=char_limit, mode=text_mode)
        sent_lengths[candidate_id] = len(candidate_text)
        audit_spans = [
            {
                "id": f"S{gold.span_index}",
                "char_start": gold.char_start,
                "char_end": gold.char_end,
            }
            for mappings in pool.gold_candidates.values()
            for gold in mappings
            if gold.candidate_id == candidate_id and gold.char_end <= len(candidate_text)
        ]
        documents.append(
            {"id": candidate_id, "text": candidate_text, "audit_spans": audit_spans}
        )

    started = time.perf_counter()
    response = await client.post(
        "/v1/rerank",
        json={
            "model": model,
            "query": pool.item.question,
            "documents": documents,
            "top_n": len(documents),
            "max_length": token_window,
        },
    )
    response.raise_for_status()
    elapsed_ms = (time.perf_counter() - started) * 1000
    response_payload = response.json()
    ranked, _ = parse_cross_encoder_response(
        response_payload,
        allowed_ids={f"C{rank}" for rank in range(1, len(pool.candidates) + 1)},
    )
    candidate_by_id = {
        f"C{rank}": hit for rank, hit in enumerate(pool.candidates, start=1)
    }
    reranked = [
        replace(candidate_by_id[candidate_id], rerank_score=score)
        for candidate_id, score in ranked
    ]
    rank_by_id = {
        candidate_id: rank for rank, (candidate_id, _) in enumerate(ranked, start=1)
    }
    audit_by_key = _parse_span_audits(response_payload.get("span_audits"))
    span_rows = [
        _span_result(
            span_index,
            pool.gold_candidates[span_index],
            sent_lengths=sent_lengths,
            rank_by_id=rank_by_id,
            audit_by_key=audit_by_key,
            final_top_k=final_top_k,
        )
        for span_index in range(len(pool.item.gold_spans))
    ]
    metrics = evaluate_retrieval(
        pool.item.gold_spans,
        [_retrieved_chunk(hit) for hit in reranked],
        pool.ideal_candidates,
        top_k=final_top_k,
        token_budget=token_budget,
        theta=theta,
        alpha=alpha,
    ).to_dict()
    return CellItemResult(
        item_id=str(pool.item.id),
        metrics={name: float(metrics[name]) for name in METRIC_NAMES},
        latency_ms=elapsed_ms,
        spans=span_rows,
    )


def _parse_span_audits(payload: object) -> dict[tuple[str, str], dict[str, object]]:
    if not isinstance(payload, list):
        raise ValueError("reranker 响应缺少 span_audits")
    parsed: dict[tuple[str, str], dict[str, object]] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("span_audits 元素必须是对象")
        document_id = row.get("document_id")
        span_id = row.get("span_id")
        if not isinstance(document_id, str) or not isinstance(span_id, str):
            raise ValueError("span_audits 缺少 document_id/span_id")
        key = (document_id, span_id)
        if key in parsed:
            raise ValueError(f"span audit 重复: {key}")
        parsed[key] = row
    return parsed


def _span_result(
    span_index: int,
    mappings: list[GoldCandidateSpan],
    *,
    sent_lengths: dict[str, int],
    rank_by_id: dict[str, int],
    audit_by_key: dict[tuple[str, str], dict[str, object]],
    final_top_k: int,
) -> dict[str, object]:
    details: list[dict[str, object]] = []
    for mapping in mappings:
        client_visible = mapping.char_end <= sent_lengths[mapping.candidate_id]
        audit = audit_by_key.get((mapping.candidate_id, f"S{span_index}"))
        if client_visible and audit is None:
            raise ValueError(
                f"客户端可见 span 缺少 tokenizer audit: {mapping.candidate_id}/S{span_index}"
            )
        details.append(
            {
                **asdict(mapping),
                "client_visible": client_visible,
                "total_tokens": _require_int(audit["total_tokens"]) if audit else None,
                "visible_tokens": _require_int(audit["visible_tokens"]) if audit else 0,
                "server_visible": bool(audit["fully_visible"]) if audit else False,
                "rerank_rank": rank_by_id[mapping.candidate_id],
            }
        )

    server_visible = [detail for detail in details if detail["server_visible"]]
    client_visible_details = [detail for detail in details if detail["client_visible"]]
    visible_top_k = [
        detail
        for detail in server_visible
        if _require_int(detail["rerank_rank"]) <= final_top_k
    ]
    if visible_top_k:
        status = "survives_top_k"
    elif server_visible:
        status = "ranking_mismatch"
    elif client_visible_details:
        status = "server_truncated"
    elif details:
        status = "client_truncated"
    else:
        status = "pool_outside"
    rrf_rank = min((_require_int(detail["rrf_rank"]) for detail in details), default=None)
    rerank_rank = min(
        (_require_int(detail["rerank_rank"]) for detail in details), default=None
    )
    return {
        "span_index": span_index,
        "status": status,
        "rrf_rank": rrf_rank,
        "rerank_rank": rerank_rank,
        "demoted_from_top_k": (
            status == "ranking_mismatch"
            and rrf_rank is not None
            and rrf_rank <= final_top_k
        ),
        "candidates": details,
    }


def _summarize_cell(results: list[CellItemResult]) -> dict[str, object]:
    span_rows = [span for result in results for span in result.spans]
    status_counts = Counter(str(span["status"]) for span in span_rows)
    latencies = sorted(result.latency_ms for result in results)
    return {
        "metrics": {
            name: fmean(result.metrics[name] for result in results)
            for name in METRIC_NAMES
        },
        "status_counts": {name: status_counts.get(name, 0) for name in AUDIT_STATUSES},
        "client_visible_gold_spans": sum(
            span["status"] not in {"client_truncated", "pool_outside"}
            for span in span_rows
        ),
        "server_visible_gold_spans": sum(
            span["status"] in {"survives_top_k", "ranking_mismatch"}
            for span in span_rows
        ),
        "demoted_from_top_k": sum(bool(span["demoted_from_top_k"]) for span in span_rows),
        "latency_ms": {
            "mean": fmean(latencies),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies),
        },
    }


def _paired_comparisons(
    by_cell: dict[str, list[CellItemResult]], *, resamples: int
) -> dict[str, object]:
    comparisons: dict[str, object] = {}
    # 除所有格相对 A 的总体差值外，再保留两个 factorial 条件差值：
    # D-C 隔离 1024-token 下的客户端字符预算，D-B 隔离 8000-char 下的服务端窗口。
    pairs = (
        (CELLS[0][0], CELLS[1][0]),
        (CELLS[0][0], CELLS[2][0]),
        (CELLS[0][0], CELLS[3][0]),
        (CELLS[2][0], CELLS[3][0]),
        (CELLS[1][0], CELLS[3][0]),
    )
    for baseline_name, candidate_name in pairs:
        baseline = by_cell[baseline_name]
        candidate = by_cell[candidate_name]
        if [item.item_id for item in baseline] != [item.item_id for item in candidate]:
            raise ValueError("2x2 item 轴漂移，无法做 paired bootstrap")
        metrics = {
            name: MetricSamples(
                baseline=tuple(RatioPoint(item.metrics[name], 1) for item in baseline),
                candidate=tuple(RatioPoint(item.metrics[name], 1) for item in candidate),
            )
            for name in METRIC_NAMES
        }
        comparisons[f"{candidate_name}_vs_{baseline_name}"] = {
            name: result.to_dict()
            for name, result in paired_bootstrap(
                metrics,
                resamples=resamples,
                higher_is_better={"max_doc_share_at_k": False},
            ).items()
        }
    return comparisons


def _decision(summaries: dict[str, dict[str, object]]) -> dict[str, object]:
    combined = summaries[CELLS[-1][0]]
    counts = combined["status_counts"]
    assert isinstance(counts, dict)
    server_truncated = int(counts["server_truncated"])
    ranking_mismatch = int(counts["ranking_mismatch"])
    return {
        "probe_2048": server_truncated > 0,
        "reason": (
            f"1024 token 下仍有 {server_truncated} 条客户端可见 gold span 未完整进入模型，"
            "可只对这些条目追加 2048 探针。"
            if server_truncated
            else (
                "8000 chars / 1024 tokens 已让候选池内 gold span 全部 token 可见；"
                f"剩余 {ranking_mismatch} 条属于可见但 Top-10 外的排序失配，"
                "没有证据支持升到 2048。"
            )
        ),
    }


def _render_markdown(payload: dict[str, object]) -> str:
    summaries = payload["summaries"]
    comparisons = payload["paired_bootstrap"]
    decision = payload["decision"]
    assert isinstance(summaries, dict)
    assert isinstance(comparisons, dict)
    assert isinstance(decision, dict)
    candidate_range = payload["candidate_count_range"]
    assert isinstance(candidate_range, list) and len(candidate_range) == 2
    lines = [
        f"# P1 tokenizer 可见性与 2×2 截断实验 · {payload['label']}",
        "",
        (
            f"- suite: `{payload['suite']}`；跨文档题 {payload['cross_document_item_count']}；"
            f"gold spans {payload['gold_span_count']}"
        ),
        f"- 固定候选池 SHA256: `{payload['candidate_pool_sha256']}`",
        f"- 候选规模: {candidate_range[0]}–{candidate_range[1]}；最终 Top-10 不变",
        "- 可见性：真实 pair tokenizer offset；`fully_visible` 要求 gold span 的全部 token 均保留",
        "",
        "| 格 | chars/tokens | goldDocR | spanRec | maxShare | nDCG | 客户端可见 | 模型可见 | 客户端截断 | 服务端截断 | 可见但 Top-10 外 | Top-10 存活 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    cell_lookup = {name: (chars, tokens) for name, chars, tokens in CELLS}
    for name, _, _ in CELLS:
        summary = summaries[name]
        assert isinstance(summary, dict)
        metrics = summary["metrics"]
        counts = summary["status_counts"]
        assert isinstance(metrics, dict) and isinstance(counts, dict)
        chars, tokens = cell_lookup[name]
        lines.append(
            f"| {name[0]} | {chars}/{tokens} | {metrics['gold_doc_recall_at_k']:.3f} |"
            f" {metrics['span_recall_at_k']:.3f} | {metrics['max_doc_share_at_k']:.3f} |"
            f" {metrics['ndcg_at_k']:.3f} | {summary['client_visible_gold_spans']} |"
            f" {summary['server_visible_gold_spans']} | {counts['client_truncated']} |"
            f" {counts['server_truncated']} | {counts['ranking_mismatch']} |"
            f" {counts['survives_top_k']} |"
        )
    lines.extend(
        [
            "",
            "## 配对 bootstrap",
            "",
            "| 对照 | 指标 | Δ | 95% CI | 判定 |",
            "|---|---|---:|---:|---|",
        ]
    )
    for comparison, metric_rows in comparisons.items():
        assert isinstance(metric_rows, dict)
        for metric in METRIC_NAMES:
            row = metric_rows[metric]
            assert isinstance(row, dict)
            lines.append(
                f"| `{comparison}` | `{metric}` | {_fmt(row['delta'])} |"
                f" [{_fmt(row['ci_low'])}, {_fmt(row['ci_high'])}] | `{row['verdict']}` |"
            )
    lines.extend(
        [
            "",
            "## 2048 决策",
            "",
            f"- `probe_2048={str(decision['probe_2048']).lower()}`：{decision['reason']}",
            "- 延迟包含额外 tokenizer 可见性审计，只能比较四格相对成本，不能冒充生产纯精排延迟。",
            "",
        ]
    )
    return "\n".join(lines)


def _is_cross_document(item: EvalItem) -> bool:
    return item.answerable and len({span.version_id for span in item.gold_spans}) > 1


def _pool_fingerprint(pools: list[ItemPool]) -> str:
    material = [
        {
            "item_id": str(pool.item.id),
            "candidate_ids": [str(hit.chunk_id) for hit in pool.candidates],
        }
        for pool in pools
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    ).strip("-")


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return f"{float(value):.3f}"


def _require_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"预期整数，实际 {type(value).__name__}")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1 tokenizer 可见性与 2x2 截断实验")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--per-arm-k", type=int, default=50)
    parser.add_argument("--final-top-k", type=int, default=10)
    parser.add_argument("--token-budget", type=int, default=6000)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--bootstrap-resamples", type=int, default=10000)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/outputs/rerank-truncation-2x2"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_truncation_experiment(
            suite_path=args.suite,
            label=args.label,
            per_arm_k=args.per_arm_k,
            final_top_k=args.final_top_k,
            token_budget=args.token_budget,
            theta=args.theta,
            alpha=args.alpha,
            timeout_s=args.timeout_s,
            bootstrap_resamples=args.bootstrap_resamples,
            output_dir=args.output_dir,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
