import argparse
import asyncio
import csv
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.grounded_answer import (
    SYSTEM_PROMPT,
    GroundedAnswerResult,
    answer_with_settings,
)
from app.rag.retrieval.citations import CitationValidationError
from app.rag.retrieval.strategy import ChunkStrategy, validate_chunk_strategy
from app.telemetry.llm_calls import SqlLlmCallAudit
from eval.mapping import (
    GoldEvidenceGroup,
    GoldSpan,
    flatten_evidence_groups,
    parse_evidence_groups,
    singleton_evidence_groups,
)
from eval.metrics.generation import (
    CitationSource,
    CitationValidityResult,
    RuleResult,
    evaluate_citation_validity,
    evaluate_constraints,
)
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.providers.openai_compatible import ProviderResponseError

# 生成轨支持的检索链路。名字与 `eval.dense_baseline.RETRIEVAL_STRATEGIES` 对齐,
# 这样"检索轨用哪条链路，生成轨就用哪条"可以在报告里直接对账。
# `lexical-only` 不在其中: grounded_answer 没有纯词法链路, 硬凑等于换了一条实现,
# 与检索轨同名却不同源, 比不了。
GENERATION_RETRIEVAL_STRATEGIES: dict[str, dict[str, bool]] = {
    "dense-only": {"decomposition": False, "lexical_rrf": False, "rerank": False},
    "multi-query-dense": {"decomposition": True, "lexical_rrf": False, "rerank": False},
    "dense-rerank": {"decomposition": False, "lexical_rrf": False, "rerank": True},
    "dense-lexical-rrf": {"decomposition": False, "lexical_rrf": True, "rerank": False},
    "dense-lexical-rrf-rerank": {
        "decomposition": False,
        "lexical_rrf": True,
        "rerank": True,
    },
}


@dataclass(frozen=True)
class GenerationItem:
    id: UUID
    category: str
    question: str
    gold_answer: str
    gold_spans: list[GoldSpan]
    constraints: dict[str, Any]
    gold_evidence_groups: list[GoldEvidenceGroup] = field(default_factory=list)
    temporal_ctx: datetime | None = None

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"

    @property
    def evidence_groups(self) -> list[GoldEvidenceGroup]:
        return self.gold_evidence_groups or singleton_evidence_groups(self.gold_spans)


@dataclass(frozen=True)
class ItemUsage:
    """单条样本消耗的模型用量, 来自 `llm_calls` 按 trace_id 的归集。

    一条样本会触发多次调用(query embedding、证据门控、正文生成), 全部计入。
    `cost_usd` 为 None 表示价格表是 0(自部署), 报告里必须标"不可用"而不是 0.00——
    0.00 会被读成"测过, 就是不要钱", 与"没有可用价格"是两件事。
    """

    call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None


@dataclass(frozen=True)
class GenerationRunResult:
    run_id: UUID
    config_hash: str
    report_path: Path | None
    reused: bool


@dataclass(frozen=True)
class GenerationItemResult:
    item: GenerationItem
    answer: str | None
    citations: list[dict[str, object]]
    refused: bool | None
    refusal_reason: str | None
    refusal_correct: bool
    citation_validity: CitationValidityResult
    constraint_result: RuleResult
    aligned_citations: int
    latency_ms: int
    model: str | None
    provider: str | None
    chunk_strategy: ChunkStrategy | None = None
    usage: ItemUsage | None = None
    top_score: float | None = None
    second_score: float | None = None
    score_margin: float | None = None
    score_margin_ratio: float | None = None
    score_source: str | None = None
    score_threshold_applied: bool | None = None
    low_margin: bool | None = None
    evidence_sufficient: bool | None = None
    evidence_reason: str | None = None
    evidence_model: str | None = None
    evidence_provider: str | None = None
    coverage_selection_applied: bool | None = None
    coverage_requirement_count: int | None = None
    coverage_covered_requirement_count: int | None = None
    coverage_candidate_count: int | None = None
    coverage_reason: str | None = None
    rerank_applied: bool | None = None
    error: str | None = None

    @property
    def citation_count(self) -> int:
        return len(self.citations)


async def run_generation_baseline(
    *,
    dataset_name: str,
    label: str,
    origin: str,
    top_k: int,
    theta: float,
    output_root: Path,
    retrieval_strategy: str = "dense-only",
    chunk_strategy: ChunkStrategy = "heading",
    rerank_candidate_text_mode: str | None = None,
    lexical_mode: str | None = None,
    expected_dataset_fingerprint: str | None = None,
    expected_annotation_fingerprint: str | None = None,
    chunk_metadata: dict[str, object] | None = None,
    reuse_completed: bool = False,
    settings: Settings | None = None,
) -> GenerationRunResult:
    settings = settings or Settings()
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 0 到 1")
    if retrieval_strategy not in GENERATION_RETRIEVAL_STRATEGIES:
        raise ValueError(
            f"生成轨不支持的检索策略: {retrieval_strategy}, "
            f"可选 {sorted(GENERATION_RETRIEVAL_STRATEGIES)}"
        )
    chunk_strategy = validate_chunk_strategy(chunk_strategy)
    flags = GENERATION_RETRIEVAL_STRATEGIES[retrieval_strategy]
    score_source = (
        "rerank"
        if flags["rerank"]
        else "fusion"
        if flags["lexical_rrf"]
        else "dense"
    )
    text_mode = rerank_candidate_text_mode or settings.rerank_candidate_text_mode
    lex_mode = lexical_mode or settings.lexical_mode
    git_sha = _git_sha()
    async with session_factory() as session:
        dataset_id, items = await _load_items(session, dataset_name, origin=origin)
        dataset_fingerprint = _fingerprint_dataset(items)
        annotation_fingerprint = _fingerprint_annotations(items)
        # 检索轨与生成轨必须跑在同一批 gold span 上; 指纹对不上说明标注在两轨之间被改过,
        # 端到端结论就不是同一个数据集的结论。宁可中止, 不做"大致相同"的比较。
        for name, expected, actual in (
            ("gold span", expected_dataset_fingerprint, dataset_fingerprint),
            ("gold answer/constraints", expected_annotation_fingerprint, annotation_fingerprint),
        ):
            if expected is not None and expected != actual:
                await session.rollback()
                raise ValueError(
                    f"评测数据在四策略跑批期间发生变化({name}): "
                    f"expected={expected}, actual={actual}"
                )
        # 先建一个只用来读模型身份的网关: config_hash 依赖身份, 而带 run_id 的
        # 审计网关又依赖 run_id, 两者互为前置, 只能拆成两步。
        identity_gateway = build_model_gateway(settings)
        try:
            gateway_identity = {
                "embedding_model": identity_gateway.embedding_model,
                "embedding_provider": identity_gateway.embedding_provider,
                "embedding_revision": identity_gateway.embedding_revision,
                "embedding_dim": identity_gateway.embedding_dimensions,
                "chat_model": identity_gateway.chat_model,
                "chat_provider": identity_gateway.chat_provider,
            }
        finally:
            await identity_gateway.aclose()
        config: dict[str, object] = {
            "dataset": dataset_name,
            # 数据集内容与标注都进 config_hash: 同名数据集改过之后不得误复用旧 run。
            "dataset_fingerprint": dataset_fingerprint,
            "annotation_fingerprint": annotation_fingerprint,
            "runner_git_sha": git_sha,
            "origin": origin,
            "track": "generation",
            "strategy": retrieval_strategy,
            "chunk_strategy": chunk_strategy,
            "chunk_metadata": chunk_metadata or {},
            "top_k": top_k,
            "theta": theta,
            # Prompt 是生成轨最容易被悄悄改掉的变量, 指纹进 config_hash:
            # 改了 prompt 就必须重跑, 不能复用旧 run 冒充同一条件下的对照。
            "prompt_fingerprint": prompt_fingerprint(),
            "refusal_score_gate_source": settings.refusal_score_gate_source,
            "retrieval_score_source": score_source,
            "refusal_threshold_applied": settings.refusal_score_gate_source == score_source,
            "refusal_threshold": settings.refusal_threshold,
            "refusal_margin_threshold": settings.refusal_margin_threshold,
            "query_decomposition_enabled": flags["decomposition"],
            "rerank_enabled": flags["rerank"],
            "lexical_rrf_enabled": flags["lexical_rrf"],
            **gateway_identity,
            "embedding_provider_base_url": settings.embedding_base_url,
            "chat_provider_base_url": settings.tier_main_base_url,
            "evidence_gate_max_chars": settings.evidence_gate_max_chars,
            "rerank_evidence_gate_max_chars": settings.rerank_evidence_gate_max_chars,
            "evidence_gate_max_tokens": settings.evidence_gate_max_tokens,
            "query_decomposition_max_subqueries": settings.query_decomposition_max_subqueries,
            "coverage_selection_enabled": settings.coverage_selection_enabled,
            "coverage_rank_cutoff": settings.coverage_rank_cutoff,
            "rerank_candidate_k": settings.rerank_candidate_k,
            "reranker_base_url": settings.reranker_base_url,
            "reranker_model": settings.reranker_model,
            "rerank_candidate_text_mode": text_mode,
            "lexical_mode": lex_mode,
            "rrf_k": settings.rrf_k,
            "document_cap_per_version": settings.document_cap_per_version,
            "answer_max_evidence_chars": settings.answer_max_evidence_chars,
            "answer_max_tokens": settings.answer_max_tokens,
        }
        config_hash = hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if reuse_completed:
            existing_run_id = await _find_completed_run(
                session, dataset_id=dataset_id, config_hash=config_hash
            )
            if existing_run_id is not None:
                await session.close()
                await close_database()
                return GenerationRunResult(
                    run_id=existing_run_id,
                    config_hash=config_hash,
                    report_path=None,
                    reused=True,
                )
        run_id = await _create_run(
            session,
            dataset_id=dataset_id,
            label=label,
            git_sha=git_sha,
            config=config,
            config_hash=config_hash,
            identity=gateway_identity,
        )
        # 用量归集按 eval_run_id + trace_id 过滤, 网关必须知道自己在哪个跑批里。
        # 注意是 eval_run_id 不是 run_id: 后者的外键指向 agent_runs, 塞评测 run 会违反外键。
        # mode="evaluation" 是硬要求(docs/07 §7.4): 关掉档位 fallback 与精确缓存。
        # 允许 fallback 会让 eval_runs.config 记着 heavy、实际因超时切到别的模型作答;
        # 允许缓存则会让重复跑批直接命中上一次结果, 三次重复退化成一次。
        run_gateway = build_model_gateway(
            settings,
            audit_sink=SqlLlmCallAudit(session),
            eval_run_id=run_id,
            mode="evaluation",
        )
        results: list[GenerationItemResult] = []
        try:
            for item in items:
                result = await _evaluate_item(
                    session,
                    run_gateway,
                    item=item,
                    run_id=run_id,
                    top_k=top_k,
                    theta=theta,
                    chunk_strategy=chunk_strategy,
                    flags=flags,
                    text_mode=text_mode,
                    lex_mode=lex_mode,
                    settings=settings,
                )
                await _store_result(session, run_id, result)
                results.append(result)
            metrics = _aggregate(results)
            await _finish_run(session, run_id, metrics)
        except Exception:
            await session.rollback()
            raise
        finally:
            await run_gateway.aclose()
    await close_database()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{timestamp}-{_slug(label)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, object] = {
        "run_id": str(run_id),
        "dataset": dataset_name,
        "label": label,
        "git_sha": git_sha,
        "config": config,
        "config_hash": config_hash,
        "metrics": metrics,
        "items": [_json_item(result) for result in results],
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = run_dir / "report.md"
    report_path.write_text(_markdown_report(payload), encoding="utf-8")
    _write_review_csv(run_dir / "citation-review.csv", results)
    return GenerationRunResult(
        run_id=run_id,
        config_hash=config_hash,
        report_path=report_path,
        reused=False,
    )


def prompt_fingerprint() -> str:
    """生成轨实际使用的 system prompt 的指纹。"""
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def _fingerprint_dataset(items: list[GenerationItem]) -> str:
    """与 `eval.dense_baseline.fingerprint_eval_items` 同口径, 便于跨两轨核对。

    只覆盖检索轨也看得见的字段(id / category / question / gold_spans),
    所以生成轨可以直接拿检索 manifest 里的指纹做前置校验。
    """
    payload = [
        {
            "id": str(item.id),
            "category": item.category,
            "question": item.question,
            "gold_spans": [
                {
                    "version_id": str(span.version_id),
                    "char_start": span.char_start,
                    "char_end": span.char_end,
                    "quote": span.quote,
                }
                for span in item.gold_spans
            ],
            "gold_evidence_groups": [
                {
                    "fact_id": group.fact_id,
                    "alternatives": [
                        {
                            "version_id": str(span.version_id),
                            "char_start": span.char_start,
                            "char_end": span.char_end,
                            "quote": span.quote,
                        }
                        for span in group.alternatives
                    ],
                }
                for group in item.evidence_groups
            ],
            "temporal_ctx": item.temporal_ctx.isoformat() if item.temporal_ctx else None,
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fingerprint_annotations(items: list[GenerationItem]) -> str:
    """生成轨独有的标注: gold answer 与 constraints。

    `constraint_pass` 直接由 constraints 决定, 改了约束却复用旧 run 会读出假结论,
    所以它必须独立进 config_hash, 不能藏在 gold span 指纹里。
    """
    payload = [
        {
            "id": str(item.id),
            "gold_answer": item.gold_answer,
            "constraints": item.constraints,
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _find_completed_run(
    session: AsyncSession, *, dataset_id: UUID, config_hash: str
) -> UUID | None:
    run_id = (
        await session.execute(
            text(
                """
                SELECT id FROM eval_runs
                WHERE dataset_id=:dataset_id
                  AND config_hash=:config_hash
                  AND finished_at IS NOT NULL
                  AND metrics IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"dataset_id": dataset_id, "config_hash": config_hash},
        )
    ).scalar_one_or_none()
    await session.rollback()
    return run_id


async def _load_items(
    session: AsyncSession, dataset_name: str, *, origin: str
) -> tuple[UUID, list[GenerationItem]]:
    dataset_id = (
        await session.execute(
            text("SELECT id FROM eval_datasets WHERE name=:name"),
            {"name": dataset_name},
        )
    ).scalar_one_or_none()
    if dataset_id is None:
        raise ValueError(f"评测数据集不存在: {dataset_name}")
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, category, question, gold_answer, gold_spans,
                           gold_evidence_groups, constraints, temporal_ctx,
                           validate_eval_spans(gold_spans) AS spans_valid,
                           validate_eval_evidence_groups(gold_evidence_groups)
                             AS groups_valid
                    FROM eval_items
                    WHERE dataset_id=:dataset_id
                      AND (:origin='all' OR origin=:origin)
                    ORDER BY id
                    """
                ),
                {"dataset_id": dataset_id, "origin": origin},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise ValueError(f"数据集 {dataset_name} 没有 origin={origin} 的样本")
    items: list[GenerationItem] = []
    for row in rows:
        if not row["spans_valid"]:
            raise ValueError(f"样本包含 stale gold span: {row['id']}")
        spans = [
            GoldSpan(
                version_id=UUID(span["version_id"]),
                char_start=int(span["char_start"]),
                char_end=int(span["char_end"]),
                quote=str(span["quote"]),
            )
            for span in row["gold_spans"]
        ]
        if not row["groups_valid"]:
            raise ValueError(f"样本包含无效 gold evidence group: {row['id']}")
        groups = parse_evidence_groups(
            row["gold_evidence_groups"], fallback_spans=spans
        )
        if row["category"] != "unanswerable" and not spans:
            raise ValueError(f"可答样本缺少 gold span: {row['id']}")
        items.append(
            GenerationItem(
                id=row["id"],
                category=str(row["category"]),
                question=str(row["question"]),
                gold_answer=str(row["gold_answer"] or ""),
                gold_spans=spans,
                constraints=dict(row["constraints"] or {}),
                gold_evidence_groups=groups,
                temporal_ctx=row["temporal_ctx"],
            )
        )
    return dataset_id, items


async def _evaluate_item(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    item: GenerationItem,
    run_id: UUID,
    top_k: int,
    theta: float,
    chunk_strategy: ChunkStrategy,
    flags: dict[str, bool],
    text_mode: str,
    lex_mode: str,
    settings: Settings,
) -> GenerationItemResult:
    started = time.monotonic()
    # 每条样本一个 trace_id, 网关审计据此把这条样本触发的所有调用(embedding、
    # 证据门控、正文生成)归到一起, 逐条 token 与成本才有出处(约束 4 的日志口径)。
    trace_id = f"{run_id}:{item.id}"
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    try:
        effective_settings = settings.model_copy(
            update={
                "query_decomposition_enabled": flags["decomposition"],
                "rerank_enabled": flags["rerank"],
                "lexical_rrf_enabled": flags["lexical_rrf"],
                "rerank_candidate_text_mode": text_mode,
                "lexical_mode": lex_mode,
            }
        )
        generated: GroundedAnswerResult = await answer_with_settings(
            session,
            gateway,
            query=item.question,
            top_k=top_k,
            settings=effective_settings,
            chunk_strategy=chunk_strategy,
            temporal_ctx=item.temporal_ctx,
        )
    # 模型侧失败按条记账, 不是整批丢弃: 20 条里第 3 条超上下文就丢掉整次跑批,
    # 等于把"这个档位在哪些题上做不到"这个最有信息量的结果也一起丢了。
    # 只收模型与引用校验的失败; 代码 bug 与数据库错误仍然上抛, 它们不是实验结果。
    except (CitationValidationError, ProviderResponseError, httpx.HTTPError) as error:
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        invalid = CitationValidityResult(
            valid=False,
            citation_count=0,
            format_valid=False,
            references_match=False,
            objects_exist=True,
            quotes_match=True,
            issues=(f"generation_validation_error:{error}",),
        )
        return GenerationItemResult(
            item=item,
            answer=None,
            citations=[],
            refused=None,
            refusal_reason=None,
            refusal_correct=False,
            citation_validity=invalid,
            constraint_result=RuleResult(False, ("answer_unavailable",)),
            aligned_citations=0,
            latency_ms=latency_ms,
            model=None,
            provider=None,
            chunk_strategy=chunk_strategy,
            usage=await _load_item_usage(session, run_id=run_id, trace_id=trace_id),
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        structlog.contextvars.unbind_contextvars("trace_id")

    if generated.chunk_strategy != chunk_strategy:  # pragma: no cover - 防御性
        raise RuntimeError(
            "生成链路返回的 chunk strategy 与请求不一致: "
            f"expected={chunk_strategy}, actual={generated.chunk_strategy}"
        )
    usage = await _load_item_usage(session, run_id=run_id, trace_id=trace_id)
    sources = await _load_citation_sources(session, generated)
    citation_validity = evaluate_citation_validity(
        answer=generated.answer,
        citations=generated.citations,
        sources=sources,
        refused=generated.refused,
    )
    constraint_result = evaluate_constraints(generated.answer, item.constraints)
    aligned_citations = sum(
        _citation_aligned(
            citation,
            flatten_evidence_groups(item.evidence_groups),
            theta=theta,
        )
        for citation in generated.citations
    )
    return GenerationItemResult(
        item=item,
        answer=generated.answer,
        citations=[_serialize_citation(citation) for citation in generated.citations],
        refused=generated.refused,
        refusal_reason=generated.refusal_reason,
        refusal_correct=generated.refused == (not item.answerable),
        citation_validity=citation_validity,
        constraint_result=constraint_result,
        aligned_citations=aligned_citations,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        model=generated.model,
        provider=generated.provider,
        chunk_strategy=generated.chunk_strategy,
        usage=usage,
        top_score=generated.top_score,
        second_score=generated.second_score,
        score_margin=generated.score_margin,
        score_margin_ratio=generated.score_margin_ratio,
        score_source=generated.score_source,
        score_threshold_applied=generated.score_threshold_applied,
        low_margin=generated.low_margin,
        evidence_sufficient=generated.evidence_sufficient,
        evidence_reason=generated.evidence_reason,
        evidence_model=generated.evidence_model,
        evidence_provider=generated.evidence_provider,
        coverage_selection_applied=generated.coverage_selection_applied,
        coverage_requirement_count=generated.coverage_requirement_count,
        coverage_covered_requirement_count=generated.coverage_covered_requirement_count,
        coverage_candidate_count=generated.coverage_candidate_count,
        coverage_reason=generated.coverage_reason,
        rerank_applied=generated.rerank_applied,
    )


async def _load_item_usage(
    session: AsyncSession, *, run_id: UUID, trace_id: str
) -> ItemUsage:
    """把这条样本触发的所有模型调用归集成一条用量记录。

    审计行与业务写入同一个 session, 本条样本尚未提交时也能读到自己的调用。
    价格表为 0(自部署)时 `cost_usd` 归零, 这里显式退回 None——报告宁可写"不可用",
    也不能写 0.00 让人读成"测过成本"。
    """
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS call_count,
                           coalesce(sum(prompt_tokens), 0) AS input_tokens,
                           coalesce(sum(output_tokens), 0) AS output_tokens,
                           sum(cost_usd) AS cost_usd,
                           count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced
                    FROM llm_calls
                    WHERE eval_run_id=:run_id AND trace_id=:trace_id
                    """
                ),
                {"run_id": run_id, "trace_id": trace_id},
            )
        )
        .mappings()
        .one()
    )
    input_tokens = int(row["input_tokens"])
    output_tokens = int(row["output_tokens"])
    cost = row["cost_usd"]
    priced = cost is not None and int(row["unpriced"]) == 0 and float(cost) > 0
    return ItemUsage(
        call_count=int(row["call_count"]),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cost_usd=float(cost) if priced else None,
    )


async def _load_citation_sources(
    session: AsyncSession, generated: GroundedAnswerResult
) -> dict[UUID, CitationSource]:
    block_ids = [citation.block_id for citation in generated.citations]
    if not block_ids:
        return {}
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT pb.id AS block_id, pb.version_id, dv.document_id,
                           pb.char_start, pb.char_end, dv.full_text
                    FROM parsed_blocks pb
                    JOIN document_versions dv ON dv.id=pb.version_id
                    WHERE pb.id=ANY(:block_ids)
                    """
                ),
                {"block_ids": block_ids},
            )
        )
        .mappings()
        .all()
    )
    return {
        row["block_id"]: CitationSource(
            block_id=row["block_id"],
            version_id=row["version_id"],
            document_id=row["document_id"],
            block_char_start=int(row["char_start"]),
            block_char_end=int(row["char_end"]),
            full_text=str(row["full_text"]),
        )
        for row in rows
    }


def _citation_aligned(citation: Any, spans: list[GoldSpan], *, theta: float) -> bool:
    for span in spans:
        if citation.version_id != span.version_id:
            continue
        overlap = max(
            0,
            min(citation.char_end, span.char_end)
            - max(citation.char_start, span.char_start),
        )
        if overlap / (span.char_end - span.char_start) >= theta:
            return True
    return False


async def _create_run(
    session: AsyncSession,
    *,
    dataset_id: UUID,
    label: str,
    git_sha: str,
    config: dict[str, object],
    config_hash: str,
    identity: dict[str, object],
) -> UUID:
    run_id = uuid7()
    actual_models = {
        "query_embedding": [
            {
                "provider": identity["embedding_provider"],
                "model": identity["embedding_model"],
                "revision": identity["embedding_revision"],
            }
        ],
        "chat": [
            {
                "provider": identity["chat_provider"],
                "model": identity["chat_model"],
                "revision": "unversioned",
            }
        ],
    }
    await session.execute(
        text(
            """
            INSERT INTO eval_runs
                (id, dataset_id, label, git_sha, config, config_hash,
                 fallback_enabled, actual_models)
            VALUES
                (:id, :dataset_id, :label, :git_sha, CAST(:config AS jsonb),
                 :config_hash, false, CAST(:actual_models AS jsonb))
            """
        ),
        {
            "id": run_id,
            "dataset_id": dataset_id,
            "label": label,
            "git_sha": git_sha,
            "config": json.dumps(config, ensure_ascii=False),
            "config_hash": config_hash,
            "actual_models": json.dumps(actual_models),
        },
    )
    await session.commit()
    return run_id


async def _store_result(
    session: AsyncSession, run_id: UUID, result: GenerationItemResult
) -> None:
    scores = {
        "answerable": result.item.answerable,
        "refusal_correct": result.refusal_correct,
        "refusal_signals": {
            "top_score": result.top_score,
            "second_score": result.second_score,
            "score_margin": result.score_margin,
            "score_margin_ratio": result.score_margin_ratio,
            "score_source": result.score_source,
            "threshold_applied": result.score_threshold_applied,
            "low_margin": result.low_margin,
        },
        "evidence_gate": {
            "sufficient": result.evidence_sufficient,
            "reason": result.evidence_reason,
            "model": result.evidence_model,
            "provider": result.evidence_provider,
            "rerank_applied": result.rerank_applied,
        },
        "coverage_selection": {
            "applied": result.coverage_selection_applied,
            "requirement_count": result.coverage_requirement_count,
            "covered_requirement_count": result.coverage_covered_requirement_count,
            "candidate_count": result.coverage_candidate_count,
            "reason": result.coverage_reason,
        },
        "citation_validity": result.citation_validity.to_dict(),
        "constraint_pass": result.constraint_result.to_dict(),
        "citation_gold_alignment": {
            "aligned": result.aligned_citations,
            "total": result.citation_count,
        },
        "chunk_strategy": result.chunk_strategy,
        "usage": _usage_json(result.usage),
        "error": result.error,
    }
    await session.execute(
        text(
            """
            INSERT INTO eval_results
                (id, run_id, item_id, answer, retrieved, scores, latency_ms)
            VALUES
                (:id, :run_id, :item_id, :answer, CAST(:retrieved AS jsonb),
                 CAST(:scores AS jsonb), :latency_ms)
            """
        ),
        {
            "id": uuid7(),
            "run_id": run_id,
            "item_id": result.item.id,
            "answer": result.answer,
            "retrieved": json.dumps(result.citations, ensure_ascii=False),
            "scores": json.dumps(scores, ensure_ascii=False),
            "latency_ms": result.latency_ms,
        },
    )
    await session.commit()


async def _finish_run(
    session: AsyncSession, run_id: UUID, metrics: dict[str, object]
) -> None:
    await session.execute(
        text(
            """
            UPDATE eval_runs
            SET metrics=CAST(:metrics AS jsonb), finished_at=now()
            WHERE id=:run_id
            """
        ),
        {"run_id": run_id, "metrics": json.dumps(metrics, ensure_ascii=False)},
    )
    await session.commit()


def _aggregate(results: list[GenerationItemResult]) -> dict[str, object]:
    completed = [result for result in results if result.error is None]
    non_refusals = [result for result in completed if result.refused is False]
    answerable = [result for result in completed if result.item.answerable]
    unanswerable = [result for result in completed if not result.item.answerable]
    citation_total = sum(result.citation_count for result in non_refusals)
    aligned_total = sum(result.aligned_citations for result in non_refusals)
    latencies = [result.latency_ms for result in completed]
    return {
        "item_count": len(results),
        "completed_count": len(completed),
        "error_count": len(results) - len(completed),
        "category_counts": dict(Counter(result.item.category for result in results)),
        "refusal": {
            "accuracy": _rate(
                sum(result.refusal_correct for result in completed), len(completed)
            ),
            "correct": sum(result.refusal_correct for result in completed),
            "total": len(completed),
            "answerable_answer_rate": _rate(
                sum(result.refused is False for result in answerable), len(answerable)
            ),
            "unanswerable_correct_refusal_rate": _rate(
                sum(result.refused is True for result in unanswerable),
                len(unanswerable),
            ),
        },
        "citation_validity": {
            "all_output_rate": _rate(
                sum(result.citation_validity.valid for result in completed),
                len(completed),
            ),
            "all_output_valid": sum(
                result.citation_validity.valid for result in completed
            ),
            "all_output_total": len(completed),
            "non_refusal_rate": _rate(
                sum(result.citation_validity.valid for result in non_refusals),
                len(non_refusals),
            ),
            "non_refusal_valid": sum(
                result.citation_validity.valid for result in non_refusals
            ),
            "non_refusal_total": len(non_refusals),
        },
        "constraint_pass": {
            "rate": _rate(
                sum(result.constraint_result.passed for result in completed),
                len(completed),
            ),
            "passed": sum(result.constraint_result.passed for result in completed),
            "total": len(completed),
        },
        "citation_gold_alignment": {
            "rate": _rate(aligned_total, citation_total),
            "aligned": aligned_total,
            "total": citation_total,
            "note": "自动代理指标：引用 quote 覆盖 gold span 达到 theta；不等于语义引用准确率。",
        },
        "citation_accuracy": {
            "status": "pending_human_review",
            "reviewed_citations": 0,
            "supported_citations": 0,
            "rate": None,
        },
        "latency_ms": {
            "mean": fmean(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "usage": _aggregate_usage(completed),
        "actual_models": sorted(
            {
                f"{result.provider}/{result.model}"
                for result in completed
                if result.model
            }
        ),
    }


def _aggregate_usage(completed: list[GenerationItemResult]) -> dict[str, object]:
    """端到端 token 与成本。价格表为 0 时成本整体标不可用, 不写 0。"""
    usages = [result.usage for result in completed if result.usage is not None]
    if not usages:
        return {
            "status": "unavailable",
            "reason": "跑批未记录逐条用量",
            "measured_items": 0,
        }
    costs = [usage.cost_usd for usage in usages if usage.cost_usd is not None]
    total_tokens = sum(usage.total_tokens for usage in usages)
    return {
        "status": "ok",
        "measured_items": len(usages),
        "call_count": sum(usage.call_count for usage in usages),
        "input_tokens": sum(usage.input_tokens for usage in usages),
        "output_tokens": sum(usage.output_tokens for usage in usages),
        "total_tokens": total_tokens,
        "mean_total_tokens": total_tokens / len(usages),
        "cost_usd": sum(costs) if len(costs) == len(usages) else None,
        "cost_status": "ok" if len(costs) == len(usages) else "unavailable",
        "cost_reason": None
        if len(costs) == len(usages)
        else "当前模型价格表为 0(自部署), 没有可报告的金额; token 用量才是成本口径",
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _serialize_citation(citation: Any) -> dict[str, object]:
    return {
        "citation_id": citation.citation_id,
        "block_id": str(citation.block_id),
        "version_id": str(citation.version_id),
        "document_id": str(citation.document_id),
        "title": citation.title,
        "source_uri": citation.source_uri,
        "quote": citation.quote,
        "char_start": citation.char_start,
        "char_end": citation.char_end,
    }


def _usage_json(usage: ItemUsage | None) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "call_count": usage.call_count,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cost_usd": usage.cost_usd,
    }


def _json_item(result: GenerationItemResult) -> dict[str, object]:
    usage = result.usage
    return {
        "item_id": str(result.item.id),
        "category": result.item.category,
        "question": result.item.question,
        "gold_answer": result.item.gold_answer,
        "answerable": result.item.answerable,
        "answer": result.answer,
        "citations": result.citations,
        "refused": result.refused,
        "refusal_reason": result.refusal_reason,
        "refusal_correct": result.refusal_correct,
        "refusal_signals": {
            "top_score": result.top_score,
            "second_score": result.second_score,
            "score_margin": result.score_margin,
            "score_margin_ratio": result.score_margin_ratio,
            "score_source": result.score_source,
            "threshold_applied": result.score_threshold_applied,
            "low_margin": result.low_margin,
        },
        "evidence_gate": {
            "sufficient": result.evidence_sufficient,
            "reason": result.evidence_reason,
            "model": result.evidence_model,
            "provider": result.evidence_provider,
            "rerank_applied": result.rerank_applied,
        },
        "coverage_selection": {
            "applied": result.coverage_selection_applied,
            "requirement_count": result.coverage_requirement_count,
            "covered_requirement_count": result.coverage_covered_requirement_count,
            "candidate_count": result.coverage_candidate_count,
            "reason": result.coverage_reason,
        },
        "citation_validity": result.citation_validity.to_dict(),
        "constraint_pass": result.constraint_result.to_dict(),
        "citation_gold_alignment": {
            "aligned": result.aligned_citations,
            "total": result.citation_count,
        },
        "latency_ms": result.latency_ms,
        "model": result.model,
        "provider": result.provider,
        "chunk_strategy": result.chunk_strategy,
        "usage": _usage_json(usage),
        # 矩阵的成本指标读顶层 total_tokens / cost_usd; 缺失即标"不可用"而不是 0。
        "total_tokens": usage.total_tokens if usage else None,
        "cost_usd": usage.cost_usd if usage else None,
        # gold span 指纹的原料。检索轨报告里叫同一个名字, 两轨才能核对是不是同一批标注。
        "span_diagnostics": [
            {
                "version_id": str(span.version_id),
                "char_start": span.char_start,
                "char_end": span.char_end,
                "quote": span.quote,
            }
            for span in result.item.gold_spans
        ],
        "error": result.error,
    }


def _write_review_csv(path: Path, results: list[GenerationItemResult]) -> None:
    fieldnames = [
        "item_id",
        "category",
        "question",
        "answer",
        "citation_id",
        "citation_quote",
        "supported",
        "reason",
        "reviewer",
        "reviewed_at",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            for citation in result.citations:
                writer.writerow(
                    {
                        "item_id": str(result.item.id),
                        "category": result.item.category,
                        "question": result.item.question,
                        "answer": result.answer or "",
                        "citation_id": citation["citation_id"],
                        "citation_quote": citation["quote"],
                        "supported": "",
                        "reason": "",
                        "reviewer": "",
                        "reviewed_at": "",
                    }
                )


def _markdown_report(payload: dict[str, object]) -> str:
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    refusal = metrics["refusal"]
    validity = metrics["citation_validity"]
    constraints = metrics["constraint_pass"]
    alignment = metrics["citation_gold_alignment"]
    latency = metrics["latency_ms"]
    config = payload["config"]
    assert isinstance(refusal, dict) and isinstance(validity, dict)
    assert isinstance(constraints, dict) and isinstance(alignment, dict)
    assert isinstance(latency, dict) and isinstance(config, dict)
    usage = metrics.get("usage")
    assert isinstance(usage, dict)
    return f"""# {payload["label"]}

- dataset: `{payload["dataset"]}`
- run_id: `{payload["run_id"]}`
- git_sha: `{payload["git_sha"]}`
- config_hash: `{payload["config_hash"]}`
- chunk_strategy: `{config["chunk_strategy"]}` ｜ 检索链路: `{config["strategy"]}`
- prompt_fingerprint: `{str(config["prompt_fingerprint"])[:12]}` ｜ \
answer_max_tokens: {config["answer_max_tokens"]}
- completed: {metrics["completed_count"]}/{metrics["item_count"]}

## 指标

| 指标 | 结果 |
|---|---:|
| 拒答准确率 | {_percent(refusal["accuracy"])} ({refusal["correct"]}/{refusal["total"]}) |
| 不可答正确拒答率 | {_percent(refusal["unanswerable_correct_refusal_rate"])} |
| citation_validity（非拒答） | {_percent(validity["non_refusal_rate"])} ({validity["non_refusal_valid"]}/{validity["non_refusal_total"]}) |
| constraint_pass | {_percent(constraints["rate"])} ({constraints["passed"]}/{constraints["total"]}) |
| citation_gold_alignment（自动代理） | {_percent(alignment["rate"])} ({alignment["aligned"]}/{alignment["total"]}) |
| citation_accuracy（语义支撑） | 待人工复核 |
| 端到端延迟均值(ms) | {_number(latency["mean"])} |
| 端到端 token 均值 | {_number(usage.get("mean_total_tokens"))} |
| 端到端成本(USD) | {_cost(usage)} |

`citation_validity` 只证明标签格式、数据库对象与 quote 区间有效；
`citation_gold_alignment` 只证明引用覆盖人工 gold span。两者都不能冒充语义引用准确率。
人工复核工作表见同目录 `citation-review.csv`。

延迟含检索、证据门控与生成全过程；token 覆盖本条样本触发的所有模型调用。
"""


def _number(value: object) -> str:
    if not isinstance(value, int | float):
        return "N/A"
    return f"{float(value):.1f}"


def _cost(usage: dict[str, Any]) -> str:
    value = usage.get("cost_usd")
    if usage.get("cost_status") != "ok" or not isinstance(value, int | float):
        return f"不可用（{usage.get('cost_reason') or usage.get('reason')}）"
    return f"{float(value):.6f}"


def _percent(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2%}"


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行 M0 dense-only 生成与引用规则基线"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="human"
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument(
        "--retrieval-strategy",
        choices=sorted(GENERATION_RETRIEVAL_STRATEGIES),
        default="dense-only",
    )
    parser.add_argument(
        "--chunk-strategy",
        choices=["fixed", "heading", "recursive", "semantic"],
        default="heading",
    )
    parser.add_argument(
        "--rerank-candidate-text-mode",
        choices=["title_heading_content", "heading_content", "content"],
        default=None,
    )
    parser.add_argument("--lexical-mode", default=None)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/generation-baseline")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        run_generation_baseline(
            dataset_name=args.dataset,
            label=args.label,
            origin=args.origin,
            top_k=args.top_k,
            theta=args.theta,
            output_root=args.output_dir,
            retrieval_strategy=args.retrieval_strategy,
            chunk_strategy=args.chunk_strategy,
            rerank_candidate_text_mode=args.rerank_candidate_text_mode,
            lexical_mode=args.lexical_mode,
            reuse_completed=args.reuse,
        )
    )
    print(
        json.dumps(
            {
                "run_id": str(result.run_id),
                "config_hash": result.config_hash,
                "report": str(result.report_path) if result.report_path else None,
                "reused": result.reused,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
