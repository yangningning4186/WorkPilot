"""P1-L：受约束 Agentic RAG 的严格配对离线实验。

主轴复用 P1-K 的 14 条 retrieval-miss；完整 dev70 同时承担已有正确题回退与
13 条 unanswerable 安全复核。runner 明确拒绝 test suite，不修改生产开关。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from statistics import fmean
from typing import cast
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.evidence_sufficiency import (
    EvidenceAssessmentError,
    assess_evidence_sufficiency,
)
from app.rag.grounded_answer import evaluate_refusal, retrieval_score_source
from app.rag.retrieval.citations import build_evidence_segments
from app.rag.retrieval.coverage import coverage_aware_top_k
from app.rag.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.rag.retrieval.fusion import reciprocal_rank_fusion
from app.rag.retrieval.lexical import lexical_search
from eval.agentic_retrieval import (
    AgenticPlan,
    AgenticPlanError,
    RetrievalRequirement,
    build_missing_requirement,
    evidence_ledger_top_k,
    fallback_agentic_plan,
    plan_agentic_retrieval,
    rank_documents_with_hints,
    round_robin_candidates,
    section_navigation_candidates,
    select_documents_for_requirements,
)
from eval.dense_baseline import EvalItem, _load_items
from eval.metrics.diagnostics import percentile
from eval.p1_document_two_hop_experiment import _grid_candidates, _rank_documents
from eval.p1_retrieval_diagnostics import _oracle_doc_hits, _rerank_raw, _span_rank
from eval.stats import INELIGIBLE, MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import EvalSuite, load_suite, validate_suite
from workpilot_ai.gateway import ModelGateway

VARIANTS = ("rrf_top5", "p1k_coverage", "doc_m3_local_n10", "agentic_navigation")
FINAL_TOP_K = 5
CANDIDATE_K = 50

_HYDRATE_SQL = """
SELECT c.id AS chunk_id,
       COALESCE(
         (
           SELECT jsonb_agg(
             jsonb_build_object(
               'block_id', b.id,
               'block_idx', b.block_idx,
               'block_type', b.block_type,
               'text', b.text,
               'char_start', b.char_start,
               'char_end', b.char_end,
               'heading_path', COALESCE(b.heading_path, ARRAY[]::text[]),
               'locations', COALESCE(
                 (
                   SELECT jsonb_agg(
                     jsonb_build_object(
                       'page_no', l.page_no,
                       'page_width', l.page_width,
                       'page_height', l.page_height,
                       'rotation', l.rotation,
                       'coord_origin', l.coord_origin,
                       'bbox_norm', l.bbox_norm
                     ) ORDER BY l.location_idx
                   )
                   FROM parsed_block_locations l
                   WHERE l.block_id=b.id
                 ),
                 '[]'::jsonb
               )
             ) ORDER BY b.block_idx
           )
           FROM parsed_blocks b
           WHERE b.version_id=c.version_id
             AND b.block_idx BETWEEN c.block_start_idx AND c.block_end_idx
         ),
         '[]'::jsonb
       ) AS blocks
FROM chunks c
WHERE c.id=ANY(:chunk_ids)
"""


@dataclass(frozen=True)
class QueryPool:
    query: str
    embedding: list[float]
    dense: list[DenseSearchHit]
    lexical: list[DenseSearchHit]
    fused: list[DenseSearchHit]


@dataclass(frozen=True)
class GateDecision:
    sufficient: bool
    reason: str
    missing_aspects: list[str]
    support_ids: list[str]
    model: str | None
    provider: str | None
    invalid: bool
    latency_ms: float


@dataclass(frozen=True)
class AgenticCandidate:
    hits: list[DenseSearchHit]
    requirement_rankings: list[list[DenseSearchHit]]
    candidates: list[DenseSearchHit]
    selected_documents: list[DenseSearchHit]
    reranker_calls: int
    local_searches: int
    latency_ms: float


async def run_experiment(
    *,
    suite_path: Path,
    attribution_report: Path,
    label: str,
    authorization_note: str,
    reranker_base_url: str,
    output_dir: Path,
    timeout_s: float = 120.0,
    gate_max_chars: int = 6000,
    settings: Settings | None = None,
) -> Path:
    if not authorization_note.strip():
        raise ValueError("必须记录问题文本与证据发送给规划/门控模型的数据授权")
    if not 500 <= gate_max_chars <= 20000:
        raise ValueError("gate_max_chars 必须位于 500 到 20000")
    settings = (settings or Settings()).model_copy(update={"llm_cache_enabled": False})
    suite = load_suite(suite_path)
    _validate_frozen_suite(suite)
    wanted = _load_target_axis(attribution_report)
    input_sha = _input_fingerprint(
        suite_path=suite_path,
        attribution_report=attribution_report,
        gate_max_chars=gate_max_chars,
        settings=settings,
    )
    checkpoint_path = output_dir / f"{_slug(label)}.checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path, input_sha=input_sha)
    completed = checkpoint.get("items")
    completed_items: dict[str, dict[str, object]] = (
        completed if isinstance(completed, dict) else {}
    )

    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            items = await _load_suite_items(session, suite)
            _validate_axes(items, wanted)
            gateway = build_model_gateway(settings, mode="online")
            try:
                async with httpx.AsyncClient(
                    base_url=reranker_base_url.rstrip("/"),
                    timeout=timeout_s,
                    trust_env=False,
                ) as client:
                    health = await _require_reranker(client)
                    for dataset, item in items:
                        item_id = str(item.id)
                        if item_id in completed_items:
                            continue
                        row, transport_retries = await _with_transport_retries(
                            partial(
                                _evaluate_item,
                                session,
                                client,
                                gateway=gateway,
                                dataset=dataset,
                                item=item,
                                target=item_id in wanted,
                                settings=settings,
                                gate_max_chars=gate_max_chars,
                            )
                        )
                        row["transport_retry_count"] = transport_retries
                        completed_items[item_id] = row
                        _write_checkpoint(
                            checkpoint_path,
                            {"items": completed_items},
                            input_sha=input_sha,
                        )
            finally:
                await gateway.aclose()

        ordered = [completed_items[str(item.id)] for _, item in items]
        payload: dict[str, object] = {
            "schema_version": "p1-agentic-rag.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "input_sha256": input_sha,
            "attribution_report": str(attribution_report.resolve()),
            "authorization_note": authorization_note.strip(),
            "service_health": health,
            "config": {
                "primary_axis": "P1-K retrieval_miss multi_hop/global 14",
                "regression_axis": "all answerable dev57",
                "safety_axis": "all unanswerable dev13",
                "candidate_k": CANDIDATE_K,
                "final_top_k": FINAL_TOP_K,
                "document_top_m": 3,
                "local_top_n": 10,
                "section_seed_k": 2,
                "neighbor_radius": 1,
                "max_agent_candidates": 80,
                "max_refinement_loops": 1,
                "gate_max_chars": gate_max_chars,
                "candidate_chars": settings.rerank_max_candidate_chars,
                "reranker_tokens": 512,
                "lexical_mode": settings.lexical_mode,
                "rrf_k": settings.rrf_k,
                "cache_enabled": False,
            },
            "summary": summarize_items(ordered),
            "items": ordered,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"{stem}.md").write_text(_markdown(payload), encoding="utf-8")
        return json_path
    finally:
        await close_database()


async def _evaluate_item(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    dataset: str,
    item: EvalItem,
    target: bool,
    settings: Settings,
    gate_max_chars: int,
) -> dict[str, object]:
    original_started = time.perf_counter()
    original = (await _build_query_pools(
        session,
        gateway,
        queries=[item.question],
        settings=settings,
        temporal_ctx=item.temporal_ctx,
    ))[0]
    original_ms = _elapsed_ms(original_started)

    plan_started = time.perf_counter()
    try:
        plan = await plan_agentic_retrieval(
            gateway,
            query=item.question,
            max_requirements=settings.query_decomposition_max_subqueries,
            max_tokens=max(500, settings.query_decomposition_max_tokens),
        )
    except (AgenticPlanError, ValueError) as error:
        plan = fallback_agentic_plan(
            item.question,
            reason=f"规划降级: {type(error).__name__}: {error}",
        )
    plan_ms = _elapsed_ms(plan_started)

    requirements_started = time.perf_counter()
    requirement_pools = await _build_query_pools(
        session,
        gateway,
        queries=[requirement.retrieval_query for requirement in plan.requirements],
        settings=settings,
        temporal_ctx=item.temporal_ctx,
    )
    requirements_ms = _elapsed_ms(requirements_started)

    baseline = await _evaluate_baseline(
        session,
        gateway,
        item=item,
        hits=original.fused[:FINAL_TOP_K],
        retrieval_ms=original_ms,
        gate_max_chars=gate_max_chars,
    )
    coverage = await _evaluate_coverage(
        session,
        gateway,
        item=item,
        original=original,
        plan=plan,
        requirement_pools=requirement_pools,
        shared_ms=original_ms + plan_ms + requirements_ms,
        settings=settings,
        gate_max_chars=gate_max_chars,
    )
    document = await _evaluate_document_two_hop(
        session,
        client,
        gateway=gateway,
        item=item,
        original=original,
        shared_ms=original_ms,
        settings=settings,
        gate_max_chars=gate_max_chars,
    )
    agentic = await _evaluate_agentic(
        session,
        client,
        gateway=gateway,
        item=item,
        original=original,
        plan=plan,
        requirement_pools=requirement_pools,
        shared_ms=original_ms + plan_ms + requirements_ms,
        settings=settings,
        gate_max_chars=gate_max_chars,
    )
    return {
        "dataset": dataset,
        "item_id": str(item.id),
        "category": item.category,
        "answerable": item.answerable,
        "target_axis": target,
        "question": item.question,
        "plan": _plan_dict(plan),
        "shared_latency_ms": {
            "original_retrieval": original_ms,
            "planning": plan_ms,
            "requirement_retrieval": requirements_ms,
        },
        "variants": {
            "rrf_top5": baseline,
            "p1k_coverage": coverage,
            "doc_m3_local_n10": document,
            "agentic_navigation": agentic,
        },
    }


async def _with_transport_retries(
    operation: Callable[[], Awaitable[dict[str, object]]],
    *,
    attempts: int = 3,
) -> tuple[dict[str, object], int]:
    if attempts < 1:
        raise ValueError("attempts 必须为正数")
    for attempt in range(attempts):
        try:
            return await operation(), attempt
        except httpx.TransportError:
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("transport retry 未返回结果")  # pragma: no cover


async def _evaluate_baseline(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    item: EvalItem,
    hits: list[DenseSearchHit],
    retrieval_ms: float,
    gate_max_chars: int,
) -> dict[str, object]:
    hydrated = await _hydrate_hits(session, hits)
    gate = await _assess_gate(
        gateway,
        query=item.question,
        hits=hydrated,
        max_chars=gate_max_chars,
    )
    return _variant_row(
        item,
        hydrated,
        gate=gate,
        latency_ms=retrieval_ms + gate.latency_ms,
        logical_model_calls=2,
        reranker_calls=0,
        refinement_applied=False,
        selected_documents=hydrated,
    )


async def _evaluate_coverage(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    item: EvalItem,
    original: QueryPool,
    plan: AgenticPlan,
    requirement_pools: list[QueryPool],
    shared_ms: float,
    settings: Settings,
    gate_max_chars: int,
) -> dict[str, object]:
    if plan.decomposed:
        selection = coverage_aware_top_k(
            original.fused,
            [pool.fused for pool in requirement_pools],
            top_k=FINAL_TOP_K,
            rank_cutoff=settings.coverage_rank_cutoff,
            rrf_k=settings.rrf_k,
        )
        hits = selection.hits
    else:
        hits = original.fused[:FINAL_TOP_K]
    hydrated = await _hydrate_hits(session, hits)
    gate = await _assess_gate(
        gateway,
        query=item.question,
        hits=hydrated,
        max_chars=gate_max_chars,
    )
    return _variant_row(
        item,
        hydrated,
        gate=gate,
        latency_ms=shared_ms + gate.latency_ms,
        logical_model_calls=3 + bool(requirement_pools),
        reranker_calls=0,
        refinement_applied=False,
        selected_documents=hydrated,
    )


async def _evaluate_document_two_hop(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    item: EvalItem,
    original: QueryPool,
    shared_ms: float,
    settings: Settings,
    gate_max_chars: int,
) -> dict[str, object]:
    started = time.perf_counter()
    documents = _rank_documents(original.dense, original.lexical, rrf_k=settings.rrf_k)
    local_by_version: dict[UUID, list[DenseSearchHit]] = {}
    for document in documents[:3]:
        local_by_version[document.version_id] = await _oracle_doc_hits(
            session,
            gateway=gateway,
            embedding=original.embedding,
            version_id=document.version_id,
            temporal_ctx=item.temporal_ctx,
        )
    candidates = _grid_candidates(
        [document.version_id for document in documents[:3]],
        local_by_version,
        document_top_m=3,
        local_top_n=10,
    )
    ranked = await _rerank_if_needed(
        client,
        query=item.question,
        candidates=candidates,
        settings=settings,
    )
    hydrated = await _hydrate_hits(session, ranked[:FINAL_TOP_K])
    variant_ms = _elapsed_ms(started)
    gate = await _assess_gate(
        gateway,
        query=item.question,
        hits=hydrated,
        max_chars=gate_max_chars,
    )
    return _variant_row(
        item,
        hydrated,
        gate=gate,
        latency_ms=shared_ms + variant_ms + gate.latency_ms,
        logical_model_calls=2,
        reranker_calls=1 if candidates else 0,
        refinement_applied=False,
        selected_documents=documents[:3],
    )


async def _evaluate_agentic(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    item: EvalItem,
    original: QueryPool,
    plan: AgenticPlan,
    requirement_pools: list[QueryPool],
    shared_ms: float,
    settings: Settings,
    gate_max_chars: int,
) -> dict[str, object]:
    if not plan.decomposed or not requirement_pools:
        return await _agentic_noop(
            session,
            gateway,
            item=item,
            hits=original.fused[:FINAL_TOP_K],
            shared_ms=shared_ms,
            gate_max_chars=gate_max_chars,
        )

    candidate = await _agentic_candidates(
        session,
        client,
        gateway=gateway,
        original=original,
        requirements=list(plan.requirements),
        requirement_pools=requirement_pools,
        settings=settings,
        temporal_ctx=item.temporal_ctx,
    )
    hydrated = await _hydrate_hits(session, candidate.hits)
    gate = await _assess_gate(
        gateway,
        query=item.question,
        hits=hydrated,
        max_chars=gate_max_chars,
    )
    total_ms = shared_ms + candidate.latency_ms + gate.latency_ms
    reranker_calls = candidate.reranker_calls
    logical_model_calls = 4
    final_candidate = candidate
    initial_gate = gate
    refinement_applied = False

    if not gate.sufficient and gate.missing_aspects:
        refinement_applied = True
        refinement = await _refine_missing_once(
            session,
            client,
            gateway=gateway,
            item=item,
            original=original,
            plan=plan,
            previous=candidate,
            missing_aspects=gate.missing_aspects,
            settings=settings,
        )
        final_candidate = refinement
        hydrated = await _hydrate_hits(session, refinement.hits)
        gate = await _assess_gate(
            gateway,
            query=item.question,
            hits=hydrated,
            max_chars=gate_max_chars,
        )
        total_ms += refinement.latency_ms + gate.latency_ms
        reranker_calls += refinement.reranker_calls
        logical_model_calls += 2

    row = _variant_row(
        item,
        hydrated,
        gate=gate,
        latency_ms=total_ms,
        logical_model_calls=logical_model_calls,
        reranker_calls=reranker_calls,
        refinement_applied=refinement_applied,
        selected_documents=final_candidate.selected_documents,
    )
    row["initial_gate_sufficient"] = initial_gate.sufficient
    row["initial_missing_aspects"] = initial_gate.missing_aspects
    row["local_searches"] = final_candidate.local_searches
    row["candidate_count"] = len(final_candidate.candidates)
    return row


async def _agentic_noop(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    item: EvalItem,
    hits: list[DenseSearchHit],
    shared_ms: float,
    gate_max_chars: int,
) -> dict[str, object]:
    hydrated = await _hydrate_hits(session, hits)
    gate = await _assess_gate(
        gateway,
        query=item.question,
        hits=hydrated,
        max_chars=gate_max_chars,
    )
    row = _variant_row(
        item,
        hydrated,
        gate=gate,
        latency_ms=shared_ms + gate.latency_ms,
        logical_model_calls=3,
        reranker_calls=0,
        refinement_applied=False,
        selected_documents=hydrated,
    )
    row["initial_gate_sufficient"] = gate.sufficient
    row["initial_missing_aspects"] = gate.missing_aspects
    row["local_searches"] = 0
    row["candidate_count"] = len(hydrated)
    return row


async def _agentic_candidates(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    original: QueryPool,
    requirements: list[RetrievalRequirement],
    requirement_pools: list[QueryPool],
    settings: Settings,
    temporal_ctx: datetime | None = None,
) -> AgenticCandidate:
    started = time.perf_counter()
    original_documents = _rank_documents(
        original.dense, original.lexical, rrf_k=settings.rrf_k
    )
    requirement_documents = [
        rank_documents_with_hints(
            _rank_documents(pool.dense, pool.lexical, rrf_k=settings.rrf_k),
            requirement,
        )
        for requirement, pool in zip(requirements, requirement_pools, strict=True)
    ]
    selected_documents = select_documents_for_requirements(
        requirement_documents,
        original_documents,
        max_documents=3,
    )
    selected_versions = {document.version_id for document in selected_documents}
    candidate_rankings: list[list[DenseSearchHit]] = []
    local_searches = 0
    for _requirement, pool, documents in zip(
        requirements, requirement_pools, requirement_documents, strict=True
    ):
        routed = [doc for doc in documents if doc.version_id in selected_versions][:2]
        local_rankings: list[list[DenseSearchHit]] = []
        for document in routed:
            local = await _oracle_doc_hits(
                session,
                gateway=gateway,
                embedding=pool.embedding,
                version_id=document.version_id,
                temporal_ctx=temporal_ctx,
            )
            local_searches += 1
            local_rankings.append(
                section_navigation_candidates(
                    local,
                    top_n=10,
                    section_seed_k=2,
                    neighbor_radius=1,
                )
            )
        candidate_rankings.append(round_robin_candidates(local_rankings, max_candidates=20))

    candidates = round_robin_candidates(candidate_rankings, max_candidates=80)
    requirement_reranked: list[list[DenseSearchHit]] = []
    reranker_calls = 0
    for requirement, ranking in zip(requirements, candidate_rankings, strict=True):
        if not ranking:
            requirement_reranked.append([])
            continue
        requirement_reranked.append(
            await _rerank_raw(
                client,
                query=requirement.retrieval_query,
                candidates=ranking,
                model=settings.reranker_model,
                char_limit=settings.rerank_max_candidate_chars,
                token_window=512,
                text_mode=settings.rerank_candidate_text_mode,
            )
        )
        reranker_calls += 1
    fallback = await _rerank_if_needed(
        client,
        query=original.query,
        candidates=candidates,
        settings=settings,
    )
    reranker_calls += bool(candidates)
    hits = evidence_ledger_top_k(
        requirement_reranked,
        fallback,
        top_k=FINAL_TOP_K,
    )
    return AgenticCandidate(
        hits=hits,
        requirement_rankings=requirement_reranked,
        candidates=candidates,
        selected_documents=selected_documents,
        reranker_calls=reranker_calls,
        local_searches=local_searches,
        latency_ms=_elapsed_ms(started),
    )


async def _refine_missing_once(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    item: EvalItem,
    original: QueryPool,
    plan: AgenticPlan,
    previous: AgenticCandidate,
    missing_aspects: list[str],
    settings: Settings,
) -> AgenticCandidate:
    started = time.perf_counter()
    entities = tuple(
        dict.fromkeys(entity for req in plan.requirements for entity in req.entities)
    )
    document_hints = tuple(
        dict.fromkeys(hint for req in plan.requirements for hint in req.document_hints)
    )
    requirement = build_missing_requirement(
        item.question,
        missing_aspects,
        entities=entities,
        document_hints=document_hints,
    )
    pool = (await _build_query_pools(
        session,
        gateway,
        queries=[requirement.retrieval_query],
        settings=settings,
        temporal_ctx=item.temporal_ctx,
    ))[0]
    documents = rank_documents_with_hints(
        _rank_documents(pool.dense, pool.lexical, rrf_k=settings.rrf_k),
        requirement,
    )
    selected_documents = select_documents_for_requirements(
        [documents], previous.selected_documents, max_documents=3
    )
    local_rankings: list[list[DenseSearchHit]] = []
    for document in selected_documents[:2]:
        local = await _oracle_doc_hits(
            session,
            gateway=gateway,
            embedding=pool.embedding,
            version_id=document.version_id,
            temporal_ctx=item.temporal_ctx,
        )
        local_rankings.append(section_navigation_candidates(local, top_n=10))
    candidates = round_robin_candidates(local_rankings, max_candidates=30)
    missing_ranking = await _rerank_if_needed(
        client,
        query=requirement.retrieval_query,
        candidates=candidates,
        settings=settings,
    )
    union = round_robin_candidates(
        [previous.candidates, candidates], max_candidates=80
    )
    fallback = await _rerank_if_needed(
        client,
        query=item.question,
        candidates=union,
        settings=settings,
    )
    requirement_rankings = [*previous.requirement_rankings, missing_ranking]
    hits = evidence_ledger_top_k(requirement_rankings, fallback, top_k=FINAL_TOP_K)
    all_documents = select_documents_for_requirements(
        [[*previous.selected_documents, *selected_documents]],
        [],
        max_documents=6,
    )
    return AgenticCandidate(
        hits=hits,
        requirement_rankings=requirement_rankings,
        candidates=union,
        selected_documents=all_documents,
        reranker_calls=(1 if candidates else 0) + (1 if union else 0),
        local_searches=previous.local_searches + len(local_rankings),
        latency_ms=_elapsed_ms(started),
    )


async def _build_query_pools(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    queries: list[str],
    settings: Settings,
    temporal_ctx: datetime | None = None,
) -> list[QueryPool]:
    if not queries:
        return []
    embedding_result = await gateway.embed(
        queries,
        task_type="query_embedding" if len(queries) == 1 else "multi_query_embedding",
    )
    pools: list[QueryPool] = []
    for query, embedding in zip(queries, embedding_result.embeddings, strict=True):
        dense = await _dense_search_by_vector(
            session,
            gateway,
            embedding=embedding,
            top_k=CANDIDATE_K,
            strategy="heading",
            temporal_ctx=temporal_ctx,
        )
        lexical = await lexical_search(
            session,
            query=query,
            top_k=CANDIDATE_K,
            mode=settings.lexical_mode,
            strategy="heading",
            temporal_ctx=temporal_ctx,
        )
        fused = reciprocal_rank_fusion(
            [dense, lexical],
            top_k=CANDIDATE_K,
            rrf_k=settings.rrf_k,
            strategy="heading",
        )
        pools.append(QueryPool(query, embedding, dense, lexical, fused))
    return pools


async def _rerank_if_needed(
    client: httpx.AsyncClient,
    *,
    query: str,
    candidates: list[DenseSearchHit],
    settings: Settings,
) -> list[DenseSearchHit]:
    if not candidates:
        return []
    return await _rerank_raw(
        client,
        query=query,
        candidates=candidates,
        model=settings.reranker_model,
        char_limit=settings.rerank_max_candidate_chars,
        token_window=512,
        text_mode=settings.rerank_candidate_text_mode,
    )


async def _hydrate_hits(
    session: AsyncSession, hits: list[DenseSearchHit]
) -> list[DenseSearchHit]:
    if not hits:
        return []
    rows = (
        await session.execute(
            text(_HYDRATE_SQL),
            {"chunk_ids": [hit.chunk_id for hit in hits]},
        )
    ).mappings()
    blocks_by_id = {row["chunk_id"]: list(row["blocks"]) for row in rows}
    missing = [str(hit.chunk_id) for hit in hits if hit.chunk_id not in blocks_by_id]
    if missing:
        raise ValueError(f"候选 hydrate 缺失 chunk: {', '.join(missing)}")
    from dataclasses import replace

    return [replace(hit, blocks=blocks_by_id[hit.chunk_id]) for hit in hits]


async def _assess_gate(
    gateway: ModelGateway,
    *,
    query: str,
    hits: list[DenseSearchHit],
    max_chars: int,
) -> GateDecision:
    started = time.perf_counter()
    if not hits:
        return GateDecision(
            False,
            "没有候选证据",
            ["全部问题事实"],
            [],
            None,
            None,
            False,
            _elapsed_ms(started),
        )
    evidence = build_evidence_segments(hits, max_chars=max_chars)
    source = retrieval_score_source(
        hits,
        rerank_applied=all(hit.rerank_score is not None for hit in hits),
        lexical_rrf_applied=all(hit.fusion_score is not None for hit in hits),
    )
    signals = evaluate_refusal(
        hits,
        threshold=0.0,
        margin_threshold=0.03,
        score_source=source,
        threshold_enabled=False,
    )
    try:
        assessment = await assess_evidence_sufficiency(
            gateway,
            query=query,
            evidence=evidence,
            top_score=signals.top_score or 0.0,
            second_score=signals.second_score,
            score_margin=signals.score_margin,
            low_margin=signals.low_margin,
            score_source=source,
            score_threshold_applied=False,
            max_tokens=300,
        )
    except EvidenceAssessmentError as error:
        return GateDecision(
            False,
            f"gate_invalid: {error}",
            [],
            [],
            None,
            None,
            True,
            _elapsed_ms(started),
        )
    return GateDecision(
        assessment.sufficient,
        assessment.reason,
        assessment.missing_aspects,
        assessment.support_ids,
        assessment.model,
        assessment.provider,
        False,
        _elapsed_ms(started),
    )


def _variant_row(
    item: EvalItem,
    hits: list[DenseSearchHit],
    *,
    gate: GateDecision,
    latency_ms: float,
    logical_model_calls: int,
    reranker_calls: int,
    refinement_applied: bool,
    selected_documents: list[DenseSearchHit],
) -> dict[str, object]:
    span_ranks = [
        _span_rank(hits, span, theta=0.5) for span in item.gold_spans
    ]
    group_ranks = [
        min(
            (
                rank
                for span in group.alternatives
                if (rank := _span_rank(hits, span, theta=0.5)) is not None
            ),
            default=None,
        )
        for group in item.evidence_groups
    ]
    complete = (
        all(rank is not None and rank <= FINAL_TOP_K for rank in group_ranks)
        if item.answerable
        else None
    )
    return {
        "selected_chunk_ids": [str(hit.chunk_id) for hit in hits],
        "selected_document_version_ids": list(
            dict.fromkeys(str(hit.version_id) for hit in selected_documents)
        ),
        "span_ranks": span_ranks,
        "evidence_group_ranks": group_ranks,
        "complete_evidence": complete,
        "gate_sufficient": gate.sufficient,
        "gate_reason": gate.reason,
        "gate_missing_aspects": gate.missing_aspects,
        "gate_support_ids": gate.support_ids,
        "gate_model": gate.model,
        "gate_provider": gate.provider,
        "gate_invalid": gate.invalid,
        "latency_ms": round(latency_ms, 2),
        "logical_model_calls": logical_model_calls,
        "reranker_calls": reranker_calls,
        "refinement_applied": refinement_applied,
    }


def summarize_items(items: list[dict[str, object]]) -> dict[str, object]:
    if len(items) != 70:
        raise ValueError(f"P1-L summary 必须是 70 条，实际 {len(items)}")
    answerable = [item for item in items if bool(item["answerable"])]
    unanswerable = [item for item in items if not bool(item["answerable"])]
    targets = [item for item in items if bool(item["target_axis"])]
    if (len(answerable), len(unanswerable), len(targets)) != (57, 13, 14):
        raise ValueError("P1-L summary 轴漂移：必须为 answerable57/unanswerable13/target14")
    baseline_name = "rrf_top5"
    by_variant: dict[str, object] = {}
    for name in VARIANTS:
        target_complete = sum(_flag(item, name, "complete_evidence") for item in targets)
        answerable_complete = sum(
            _flag(item, name, "complete_evidence") for item in answerable
        )
        gate_answerable = sum(_flag(item, name, "gate_sufficient") for item in answerable)
        safe_refusals = sum(
            not _flag(item, name, "gate_sufficient") for item in unanswerable
        )
        latencies = [_number(_variant(item, name)["latency_ms"]) for item in items]
        by_variant[name] = {
            "target_complete": target_complete,
            "target_rescued_vs_baseline": sum(
                not _flag(item, baseline_name, "complete_evidence")
                and _flag(item, name, "complete_evidence")
                for item in targets
            ),
            "answerable_complete": answerable_complete,
            "complete_regressions_vs_baseline": sum(
                _flag(item, baseline_name, "complete_evidence")
                and not _flag(item, name, "complete_evidence")
                for item in answerable
            ),
            "answerable_gate_pass": gate_answerable,
            "gate_regressions_vs_baseline": sum(
                _flag(item, baseline_name, "gate_sufficient")
                and not _flag(item, name, "gate_sufficient")
                for item in answerable
            ),
            "unanswerable_refused": safe_refusals,
            "unanswerable_false_answer": 13 - safe_refusals,
            "gate_invalid": sum(_flag(item, name, "gate_invalid") for item in items),
            "refinement_applied": sum(
                _flag(item, name, "refinement_applied") for item in items
            ),
            "logical_model_calls_mean": fmean(
                _number(_variant(item, name)["logical_model_calls"]) for item in items
            ),
            "reranker_calls_mean": fmean(
                _number(_variant(item, name)["reranker_calls"]) for item in items
            ),
            "latency_ms": {
                "mean": fmean(latencies),
                "p50": _percentile(latencies, 0.5),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies),
            },
        }
    return {
        "item_count": len(items),
        "target_count": len(targets),
        "answerable_count": len(answerable),
        "unanswerable_count": len(unanswerable),
        "transport_retry_count": sum(
            int(_number(item.get("transport_retry_count", 0))) for item in items
        ),
        "by_variant": by_variant,
        "vs_baseline": {
            name: _paired_comparison(items, candidate=name)
            for name in VARIANTS
            if name != baseline_name
        },
        "call_count_note": (
            "logical_model_calls 统计业务阶段调用；网关内部 schema repair/升档重试另由 llm_calls 审计"
        ),
    }


def _paired_comparison(
    items: list[dict[str, object]], *, candidate: str
) -> dict[str, object]:
    baseline = "rrf_top5"

    def points(metric: str, variant: str) -> tuple[RatioPoint, ...]:
        values: list[RatioPoint] = []
        for item in items:
            eligible, value = _paired_value(item, variant=variant, metric=metric)
            values.append(RatioPoint(value, 1.0) if eligible else INELIGIBLE)
        return tuple(values)

    names = (
        "target_complete",
        "answerable_complete",
        "answerable_gate_pass",
        "unanswerable_refused",
        "latency_ms",
    )
    metrics = {
        name: MetricSamples(points(name, baseline), points(name, candidate))
        for name in names
    }
    return {
        name: result.to_dict()
        for name, result in paired_bootstrap(
            metrics,
            higher_is_better={"latency_ms": False},
        ).items()
    }


def _paired_value(
    item: dict[str, object], *, variant: str, metric: str
) -> tuple[bool, float]:
    if metric == "target_complete":
        return bool(item["target_axis"]), float(_flag(item, variant, "complete_evidence"))
    if metric == "answerable_complete":
        return bool(item["answerable"]), float(_flag(item, variant, "complete_evidence"))
    if metric == "answerable_gate_pass":
        return bool(item["answerable"]), float(_flag(item, variant, "gate_sufficient"))
    if metric == "unanswerable_refused":
        return (
            not bool(item["answerable"]),
            float(not _flag(item, variant, "gate_sufficient")),
        )
    if metric == "latency_ms":
        return True, _number(_variant(item, variant)["latency_ms"])
    raise ValueError(f"未知配对指标: {metric}")


def _flag(item: dict[str, object], variant: str, key: str) -> bool:
    return bool(_variant(item, variant).get(key))


def _variant(item: dict[str, object], name: str) -> dict[str, object]:
    variants = item.get("variants")
    if not isinstance(variants, dict) or not isinstance(variants.get(name), dict):
        raise TypeError(f"item 缺少 variant: {name}")
    return cast("dict[str, object]", variants[name])


async def _load_suite_items(
    session: AsyncSession, suite: EvalSuite
) -> list[tuple[str, EvalItem]]:
    result: list[tuple[str, EvalItem]] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        result.extend((dataset.name, item) for item in items)
    return result


def _validate_frozen_suite(suite: EvalSuite) -> None:
    if "test" in suite.name.casefold():
        raise ValueError("P1-L 禁止访问 test suite")
    if suite.name != "m1-dev-70" or suite.item_count != 70:
        raise ValueError("P1-L 只允许冻结的 m1-dev-70")
    if suite.category_counts.get("unanswerable") != 13:
        raise ValueError("P1-L 安全轴必须有 13 条 unanswerable")


def _load_target_axis(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("attribution report 缺少 cases")
    wanted = {
        str(case["item_id"])
        for case in cases
        if isinstance(case, dict)
        and case.get("cause") == "retrieval_miss"
        and case.get("category") in {"multi_hop", "global"}
    }
    if len(wanted) != 14:
        raise ValueError(f"P1-L 目标轴必须为 P1-K 14 条，实际 {len(wanted)}")
    return wanted


def _validate_axes(items: list[tuple[str, EvalItem]], wanted: set[str]) -> None:
    if len(items) != 70 or len({item.id for _, item in items}) != 70:
        raise ValueError("P1-L dev70 item 轴漂移")
    answerable = sum(item.answerable for _, item in items)
    unanswerable = len(items) - answerable
    present = {str(item.id) for _, item in items}
    if (answerable, unanswerable) != (57, 13) or not wanted <= present:
        raise ValueError("P1-L 冻结轴必须为 answerable57/unanswerable13，且包含 target14")


async def _require_reranker(client: httpx.AsyncClient) -> dict[str, object]:
    response = await client.get("/health")
    response.raise_for_status()
    health = response.json()
    if int(health.get("max_length", 0)) < 512:
        raise RuntimeError("P1-L 需要 reranker max_length>=512")
    return dict(health)


def _plan_dict(plan: AgenticPlan) -> dict[str, object]:
    return {
        "decomposed": plan.decomposed,
        "reason": plan.reason,
        "model": plan.model,
        "provider": plan.provider,
        "requirements": [
            {
                "id": requirement.id,
                "query": requirement.query,
                "entities": list(requirement.entities),
                "document_hints": list(requirement.document_hints),
            }
            for requirement in plan.requirements
        ],
    }


def _input_fingerprint(
    *,
    suite_path: Path,
    attribution_report: Path,
    gate_max_chars: int,
    settings: Settings,
) -> str:
    payload = {
        "suite_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        "attribution_sha256": hashlib.sha256(attribution_report.read_bytes()).hexdigest(),
        "gate_max_chars": gate_max_chars,
        "lexical_mode": settings.lexical_mode,
        "rrf_k": settings.rrf_k,
        "reranker_model": settings.reranker_model,
        "rerank_candidate_text_mode": settings.rerank_candidate_text_mode,
        "rerank_max_candidate_chars": settings.rerank_max_candidate_chars,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_checkpoint(path: Path, *, input_sha: str) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("input_sha256") != input_sha:
        raise ValueError("P1-L checkpoint 与当前冻结输入或配置不一致")
    return payload


def _write_checkpoint(
    path: Path, payload: dict[str, object], *, input_sha: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {**payload, "input_sha256": input_sha},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    variants = summary["by_variant"]
    assert isinstance(variants, dict)
    lines = [
        f"# P1-L · 受约束 Agentic RAG · {payload['label']}",
        "",
        "- 主轴：P1-K 14 条；回退轴：answerable dev57；安全轴：unanswerable dev13。",
        "- 只读离线实验；最多一次缺失子事实补查；不访问 test，不修改生产开关。",
        "",
        "| variant | target complete | rescued | complete regressions | gate pass | gate regressions | safe refusal | false answer | calls | CE calls | mean | p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in VARIANTS:
        row = variants[name]
        assert isinstance(row, dict)
        latency = row["latency_ms"]
        assert isinstance(latency, dict)
        lines.append(
            f"| `{name}` | {row['target_complete']}/14 |"
            f" {row['target_rescued_vs_baseline']} |"
            f" {row['complete_regressions_vs_baseline']} |"
            f" {row['answerable_gate_pass']}/57 |"
            f" {row['gate_regressions_vs_baseline']} |"
            f" {row['unanswerable_refused']}/13 |"
            f" {row['unanswerable_false_answer']} |"
            f" {_number(row['logical_model_calls_mean']):.2f} |"
            f" {_number(row['reranker_calls_mean']):.2f} |"
            f" {_number(latency['mean']):.1f}ms |"
            f" {_number(latency['p95']):.1f}ms |"
        )
    comparisons = summary["vs_baseline"]
    assert isinstance(comparisons, dict)
    lines.extend(["", "## Paired bootstrap vs `rrf_top5`", ""])
    for name, comparison in comparisons.items():
        assert isinstance(comparison, dict)
        target = comparison["target_complete"]
        safety = comparison["unanswerable_refused"]
        latency = comparison["latency_ms"]
        assert isinstance(target, dict) and isinstance(safety, dict)
        assert isinstance(latency, dict)
        lines.append(
            f"- `{name}`：target Δ={_number(target['delta']):+.3f} "
            f"[{_number(target['ci_low']):+.3f}, {_number(target['ci_high']):+.3f}]；"
            f"safety Δ={_number(safety['delta']):+.3f}；"
            f"latency Δ={_number(latency['delta']):+.1f}ms。"
        )
    lines.extend(["", f"- {summary['call_count_note']}", ""])
    return "\n".join(lines)


def _number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return float(value)


def _percentile(values: list[float], q: float) -> float:
    value = percentile(sorted(values), q)
    if value is None:
        raise ValueError("percentile 输入不能为空")
    return float(value)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value
    ).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-L 受约束 Agentic RAG 严格配对实验")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--attribution-report", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--authorization-note", required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--gate-max-chars", type=int, default=6000)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/outputs/p1-agentic-rag"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_experiment(
            suite_path=args.suite,
            attribution_report=args.attribution_report,
            label=args.label,
            authorization_note=args.authorization_note,
            reranker_base_url=args.reranker_base_url,
            output_dir=args.output_dir,
            timeout_s=args.timeout_s,
            gate_max_chars=args.gate_max_chars,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
