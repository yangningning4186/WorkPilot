"""P1 A-D：严格 dev16 的 rerank、池外深排与文档二跳 oracle 诊断。

本 runner 只读冻结的 m1-dev-70 dev suite，拒绝 test；每条问题只生成一次 query
embedding，并在 A-D 四项中复用同一 dense-50 / lexical-50 候选池。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID

import httpx
from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.retrieval.fusion import reciprocal_rank_fusion, rerank_candidate_union
from app.retrieval.lexical import lexical_search
from app.retrieval.reranker import build_candidate_text, parse_cross_encoder_response
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eval.dense_baseline import (
    EvalItem,
    _candidate_chunks,
    _load_items,
    _retrieved_chunk,
)
from eval.mapping import GoldSpan, RetrievedChunk, hits
from eval.metrics.retrieval import evaluate_retrieval
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import load_suite, validate_suite

METRIC_NAMES = (
    "span_recall_at_k",
    "gold_doc_recall_at_k",
    "ndcg_at_k",
    "max_doc_share_at_k",
)
TEXT_MODES = ("title_heading_content", "heading_content", "content")
BLEND_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
DEEP_K = 400

DEEP_DENSE_SQL = """
SELECT c.id AS chunk_id, c.version_id, c.char_start, c.char_end,
       row_number() OVER (ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id) AS rank
FROM chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id
WHERE c.is_searchable=true AND c.strategy='heading' AND c.embedding IS NOT NULL
  AND c.embedding_model=:embedding_model
  AND c.embedding_provider=:embedding_provider
  AND c.embedding_revision=:embedding_revision
  AND d.deleted_at IS NULL AND v.invalid_at IS NULL
ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id
LIMIT :deep_k
"""

DEEP_LEXICAL_SQL = """
WITH q AS (
  SELECT to_tsquery('simple', (
           SELECT string_agg(quote_literal(lexeme), ' | ')
           FROM unnest(to_tsvector('english', lexical_en_text(:query)))
           WHERE position(chr(39) IN lexeme)=0
             AND position(chr(92) IN lexeme)=0)) AS q_en,
         to_tsquery('simple', (
           SELECT string_agg(quote_literal(lexeme), ' | ')
           FROM unnest(to_tsvector('simple', lexical_zh_bigrams(:query)))
           WHERE position(chr(39) IN lexeme)=0
             AND position(chr(92) IN lexeme)=0)) AS q_zh
)
SELECT c.id AS chunk_id, c.version_id, c.char_start, c.char_end,
       row_number() OVER (ORDER BY
         COALESCE(ts_rank('{1,1,1,1}', c.tsv_en, q.q_en, 32),0)
       + COALESCE(ts_rank('{1,1,1,1}', c.tsv_zh, q.q_zh, 32),0) DESC,
         length(c.content), c.id) AS rank
FROM chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id
CROSS JOIN q
WHERE c.is_searchable=true AND c.strategy='heading'
  AND d.deleted_at IS NULL AND v.invalid_at IS NULL
  AND ((q.q_en IS NOT NULL AND c.tsv_en @@ q.q_en)
    OR (q.q_zh IS NOT NULL AND c.tsv_zh @@ q.q_zh))
ORDER BY rank
LIMIT :deep_k
"""

ORACLE_DOC_SQL = """
SELECT c.id AS chunk_id, d.id AS document_id, c.version_id, v.version_no,
       d.title, d.source_uri, c.content, c.content_tokens, c.char_start, c.char_end,
       COALESCE(c.heading_path, ARRAY[]::text[]) AS heading_path,
       1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
FROM chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id
WHERE __VISIBILITY__ AND c.strategy='heading'
  AND c.version_id=:version_id AND c.embedding IS NOT NULL
  AND c.embedding_model=:embedding_model
  AND c.embedding_provider=:embedding_provider
  AND c.embedding_revision=:embedding_revision
  AND d.deleted_at IS NULL
ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id
"""


@dataclass(frozen=True)
class ItemPool:
    dataset: str
    item: EvalItem
    embedding: list[float]
    dense: list[DenseSearchHit]
    lexical: list[DenseSearchHit]
    rrf50: list[DenseSearchHit]
    union: list[DenseSearchHit]
    ideal: list[RetrievedChunk]


@dataclass(frozen=True)
class VariantItem:
    item_id: str
    metrics: dict[str, float]
    hits: list[DenseSearchHit]


@dataclass(frozen=True)
class DeepRow:
    chunk_id: UUID
    version_id: UUID
    char_start: int
    char_end: int
    rank: int


async def run_p1_diagnostics(
    *,
    suite_path: Path,
    label: str,
    output_dir: Path,
    reranker_base_url: str,
    timeout_s: float,
    final_top_k: int = 10,
    theta: float = 0.5,
    alpha: float = 0.5,
    token_budget: int = 6000,
    settings: Settings | None = None,
) -> Path:
    if final_top_k != 10:
        raise ValueError("P1 诊断固定 final_top_k=10")
    settings = settings or Settings()
    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("P1 诊断禁止访问 test suite")
    strategy = validate_chunk_strategy("heading")

    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings)
            try:
                pools = await _build_pools(
                    session,
                    gateway,
                    suite=suite,
                    settings=settings,
                    strategy=strategy,
                )
                if len(pools) != 16 or sum(len(pool.item.gold_spans) for pool in pools) != 33:
                    raise ValueError("P1 冻结轴漂移：必须是 dev16/33")
                pool_sha = _pool_fingerprint(pools)
                checkpoint_path = output_dir / f"{_slug(label)}.checkpoint.json"
                checkpoint = _load_checkpoint(
                    checkpoint_path,
                    suite_sha=_sha256(suite_path),
                    pool_sha=pool_sha,
                )
                async with httpx.AsyncClient(
                    base_url=reranker_base_url.rstrip("/"),
                    timeout=timeout_s,
                    trust_env=False,
                ) as client:
                    health_response = await client.get("/health")
                    health_response.raise_for_status()
                    health = health_response.json()
                    if int(health.get("max_length", 0)) < 1024:
                        raise RuntimeError("P1-B/D 需要 max_length>=1024 的隔离 reranker")
                    cached_p1a = checkpoint.get("p1a")
                    if isinstance(cached_p1a, dict):
                        p1a = cached_p1a
                    else:
                        p1a = await _run_p1a(
                            client,
                            pools=pools,
                            settings=settings,
                            final_top_k=final_top_k,
                            token_budget=token_budget,
                            theta=theta,
                            alpha=alpha,
                        )
                        checkpoint["p1a"] = p1a
                        _write_checkpoint(
                            checkpoint_path,
                            checkpoint,
                            suite_sha=_sha256(suite_path),
                            pool_sha=pool_sha,
                        )
                    cached_p1b = checkpoint.get("p1b")
                    if isinstance(cached_p1b, dict):
                        p1b = cached_p1b
                    else:
                        p1b = await _run_p1b(
                            client,
                            pools=pools,
                            settings=settings,
                            final_top_k=final_top_k,
                            token_budget=token_budget,
                            theta=theta,
                            alpha=alpha,
                        )
                        checkpoint["p1b"] = p1b
                        _write_checkpoint(
                            checkpoint_path,
                            checkpoint,
                            suite_sha=_sha256(suite_path),
                            pool_sha=pool_sha,
                        )
                    p1c, pool_outside = await _run_p1c(
                        session,
                        gateway,
                        pools=pools,
                        theta=theta,
                    )
                    p1d = await _run_p1d(
                        session,
                        client,
                        gateway=gateway,
                        pool_outside=pool_outside,
                        p1c=p1c,
                        settings=settings,
                        theta=theta,
                    )
            finally:
                await gateway.aclose()

        payload: dict[str, object] = {
            "schema_version": "p1-retrieval-diagnostics.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "suite_sha256": _sha256(suite_path),
            "item_count": len(pools),
            "gold_span_count": sum(len(pool.item.gold_spans) for pool in pools),
            "candidate_pool_sha256": pool_sha,
            "service_health": health,
            "config": {
                "per_arm_k": 50,
                "rrf_candidate_k": 50,
                "final_top_k": final_top_k,
                "deep_k": DEEP_K,
                "token_budget": token_budget,
                "theta": theta,
                "alpha": alpha,
                "p1a_chars_tokens": [1200, 512],
                "p1b_chars_tokens": [8000, 1024],
                "oracle_doc_top_m": 10,
            },
            "p1a": p1a,
            "p1b": p1b,
            "p1c": p1c,
            "p1d": p1d,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / f"{stem}.md").write_text(_render_markdown(payload), encoding="utf-8")
        return json_path
    finally:
        await close_database()


async def _build_pools(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    suite: Any,
    settings: Settings,
    strategy: ChunkStrategy,
) -> list[ItemPool]:
    pools: list[ItemPool] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in items:
            if not _is_cross_document(item):
                continue
            embedding = (
                await gateway.embed([item.question], task_type="query_embedding")
            ).embeddings[0]
            dense = await _dense_search_by_vector(
                session,
                gateway,
                embedding=embedding,
                top_k=50,
                strategy=strategy,
            )
            lexical = await lexical_search(
                session,
                query=item.question,
                top_k=50,
                mode=settings.lexical_mode,
                strategy=strategy,
            )
            rrf50 = reciprocal_rank_fusion(
                [dense, lexical], top_k=50, rrf_k=settings.rrf_k, strategy=strategy
            )
            union = rerank_candidate_union(
                dense, lexical, rrf_k=settings.rrf_k, strategy=strategy
            )
            ideal = await _candidate_chunks(
                session,
                item.gold_spans,
                embedding_model=gateway.embedding_model,
                embedding_provider=gateway.embedding_provider,
                embedding_revision=gateway.embedding_revision,
                chunk_strategy=strategy,
                token_count_mode="stored",
            )
            pools.append(ItemPool(dataset, item, embedding, dense, lexical, rrf50, union, ideal))
    return pools


async def _run_p1a(
    client: httpx.AsyncClient,
    *,
    pools: list[ItemPool],
    settings: Settings,
    final_top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
) -> dict[str, object]:
    variants: dict[str, list[VariantItem]] = {"rrf50": [], "old": [], "union": []}
    attribution: list[dict[str, object]] = []
    for pool in pools:
        old = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=pool.rrf50,
            model=settings.reranker_model,
            char_limit=1200,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        union = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=pool.union,
            model=settings.reranker_model,
            char_limit=1200,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        for name, ranked in (("rrf50", pool.rrf50), ("old", old), ("union", union)):
            variants[name].append(
                VariantItem(
                    str(pool.item.id),
                    _metrics(
                        pool,
                        ranked,
                        final_top_k=final_top_k,
                        token_budget=token_budget,
                        theta=theta,
                        alpha=alpha,
                    ),
                    ranked,
                )
            )
        for span_index, span in enumerate(pool.item.gold_spans):
            dense_rank = _span_rank(pool.dense, span, theta=theta)
            lexical_rank = _span_rank(pool.lexical, span, theta=theta)
            rrf_rank = _span_rank(pool.rrf50, span, theta=theta)
            union_input_rank = _span_rank(pool.union, span, theta=theta)
            old_rank = _span_rank(old, span, theta=theta)
            union_rank = _span_rank(union, span, theta=theta)
            attribution.append(
                {
                    "item_id": str(pool.item.id),
                    "span_index": span_index,
                    "version_id": str(span.version_id),
                    "title": _span_title(pool.union, span) or _span_title(pool.ideal, span),
                    "dense_rank": dense_rank,
                    "lexical_rank": lexical_rank,
                    "rrf50_rank": rrf_rank,
                    "union_input_rank": union_input_rank,
                    "old_rerank_rank": old_rank,
                    "union_rerank_rank": union_rank,
                    "status": _p1a_status(
                        dense_rank=dense_rank,
                        lexical_rank=lexical_rank,
                        rrf_rank=rrf_rank,
                        union_rank=union_input_rank,
                        old_rank=old_rank,
                        final_top_k=final_top_k,
                    ),
                }
            )
    return {
        "summary": {name: _variant_summary(rows) for name, rows in variants.items()},
        "union_vs_old_bootstrap": _variant_bootstrap(variants["old"], variants["union"]),
        "status_counts": dict(Counter(str(row["status"]) for row in attribution)),
        "attribution": attribution,
    }


async def _run_p1b(
    client: httpx.AsyncClient,
    *,
    pools: list[ItemPool],
    settings: Settings,
    final_top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
) -> dict[str, object]:
    by_mode: dict[str, list[VariantItem]] = {mode: [] for mode in TEXT_MODES}
    ranks_by_mode: dict[str, dict[tuple[str, int], int | None]] = {
        mode: {} for mode in TEXT_MODES
    }
    for pool in pools:
        for mode in TEXT_MODES:
            ranked = await _rerank_raw(
                client,
                query=pool.item.question,
                candidates=pool.union,
                model=settings.reranker_model,
                char_limit=8000,
                token_window=1024,
                text_mode=mode,
            )
            by_mode[mode].append(
                VariantItem(
                    str(pool.item.id),
                    _metrics(
                        pool,
                        ranked,
                        final_top_k=final_top_k,
                        token_budget=token_budget,
                        theta=theta,
                        alpha=alpha,
                    ),
                    ranked,
                )
            )
            for span_index, span in enumerate(pool.item.gold_spans):
                ranks_by_mode[mode][(str(pool.item.id), span_index)] = _span_rank(
                    ranked, span, theta=theta
                )

    default_rows = by_mode[settings.rerank_candidate_text_mode]
    mismatches: list[dict[str, object]] = []
    for pool, row in zip(pools, default_rows, strict=True):
        for span_index, span in enumerate(pool.item.gold_spans):
            rrf_rank = _span_rank(pool.union, span, theta=theta)
            default_rank = _span_rank(row.hits, span, theta=theta)
            if rrf_rank is None or default_rank is None or default_rank <= final_top_k:
                continue
            mismatches.append(
                {
                    "item_id": str(pool.item.id),
                    "span_index": span_index,
                    "title": _span_title(pool.union, span),
                    "rrf_rank": rrf_rank,
                    "strict_demoted": rrf_rank <= final_top_k,
                    "ranks": {
                        mode: ranks_by_mode[mode][(str(pool.item.id), span_index)]
                        for mode in TEXT_MODES
                    },
                }
            )

    blend_rows: dict[str, list[VariantItem]] = {}
    for weight in BLEND_WEIGHTS:
        name = f"ce_weight_{weight:.2f}"
        blend_rows[name] = []
        for pool, default in zip(pools, default_rows, strict=True):
            blended = blend_rrf_ce_ranks(pool.union, default.hits, ce_weight=weight)
            blend_rows[name].append(
                VariantItem(
                    str(pool.item.id),
                    _metrics(
                        pool,
                        blended,
                        final_top_k=final_top_k,
                        token_budget=token_budget,
                        theta=theta,
                        alpha=alpha,
                    ),
                    blended,
                )
            )
    return {
        "representation_summary": {
            mode: _variant_summary(rows) for mode, rows in by_mode.items()
        },
        "representation_vs_default": {
            mode: _variant_bootstrap(default_rows, rows)
            for mode, rows in by_mode.items()
            if mode != settings.rerank_candidate_text_mode
        },
        "default_visible_mismatch_count": len(mismatches),
        "strict_demoted_count": sum(bool(row["strict_demoted"]) for row in mismatches),
        "mismatches": mismatches,
        "rank_blend_summary": {
            name: _variant_summary(rows) for name, rows in blend_rows.items()
        },
        "rank_blend_note": "dev 诊断权重扫描，不得直接据此选择生产权重",
    }


async def _run_p1c(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    pools: list[ItemPool],
    theta: float,
) -> tuple[dict[str, object], list[tuple[ItemPool, int]]]:
    outside: list[tuple[ItemPool, int]] = []
    for pool in pools:
        for span_index, span in enumerate(pool.item.gold_spans):
            if _span_rank(pool.union, span, theta=theta) is None:
                outside.append((pool, span_index))
    if len(outside) != 6:
        raise ValueError(f"P1-C 池外集合漂移：预期 6 spans，实际 {len(outside)}")

    by_item: dict[str, tuple[list[DeepRow], list[DeepRow]]] = {}
    for pool, _ in outside:
        key = str(pool.item.id)
        if key in by_item:
            continue
        dense = await _deep_dense(session, gateway, pool.embedding)
        lexical = await _deep_lexical(session, pool.item.question)
        by_item[key] = (dense, lexical)

    rows: list[dict[str, object]] = []
    for pool, span_index in outside:
        span = pool.item.gold_spans[span_index]
        dense, lexical = by_item[str(pool.item.id)]
        dense_chunk_rank = _deep_span_rank(dense, span, theta=theta)
        lexical_chunk_rank = _deep_span_rank(lexical, span, theta=theta)
        dense_doc_first = _first_chunk_rank(dense, span.version_id)
        lexical_doc_first = _first_chunk_rank(lexical, span.version_id)
        rows.append(
            {
                "item_id": str(pool.item.id),
                "span_index": span_index,
                "version_id": str(span.version_id),
                "title": await _version_title(session, span.version_id),
                "dense_chunk_rank": dense_chunk_rank,
                "dense_doc_first_chunk_rank": dense_doc_first,
                "dense_doc_rank": _document_rank(dense, span.version_id),
                "lexical_chunk_rank": lexical_chunk_rank,
                "lexical_doc_first_chunk_rank": lexical_doc_first,
                "lexical_doc_rank": _document_rank(lexical, span.version_id),
                "right_doc_reachable_top10": _min_optional(
                    _document_rank(dense, span.version_id),
                    _document_rank(lexical, span.version_id),
                )
                <= 10,
            }
        )
    return {
        "pool_outside_count": len(rows),
        "deep_k": DEEP_K,
        "rows": rows,
    }, outside


async def _run_p1d(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    pool_outside: list[tuple[ItemPool, int]],
    p1c: dict[str, object],
    settings: Settings,
    theta: float,
) -> dict[str, object]:
    c_rows = p1c["rows"]
    assert isinstance(c_rows, list)
    c_index = {
        (str(row["item_id"]), int(row["span_index"])): row
        for row in c_rows
        if isinstance(row, dict)
    }
    rows: list[dict[str, object]] = []
    for pool, span_index in pool_outside:
        audit = c_index[(str(pool.item.id), span_index)]
        if not bool(audit["right_doc_reachable_top10"]):
            continue
        span = pool.item.gold_spans[span_index]
        doc_hits = await _oracle_doc_hits(
            session,
            gateway=gateway,
            embedding=pool.embedding,
            version_id=span.version_id,
        )
        ce_hits = await _rerank_batched(
            client,
            query=pool.item.question,
            candidates=doc_hits,
            model=settings.reranker_model,
            char_limit=8000,
            token_window=1024,
            text_mode=settings.rerank_candidate_text_mode,
        )
        rows.append(
            {
                "item_id": str(pool.item.id),
                "span_index": span_index,
                "title": audit["title"],
                "document_chunk_count": len(doc_hits),
                "oracle_dense_local_rank": _span_rank(doc_hits, span, theta=theta),
                "oracle_cross_encoder_local_rank": _span_rank(ce_hits, span, theta=theta),
                "oracle_cross_encoder_top10": (
                    (_span_rank(ce_hits, span, theta=theta) or math.inf) <= 10
                ),
            }
        )
    return {
        "trigger": "gold document rank <=10 in dense-400 or lexical-400 (oracle only)",
        "eligible_span_count": len(rows),
        "cross_encoder_top10_rescued": sum(
            bool(row["oracle_cross_encoder_top10"]) for row in rows
        ),
        "rows": rows,
        "warning": "gold version_id is an oracle; this is a mechanism upper bound, not deployable routing",
    }


async def _rerank_raw(
    client: httpx.AsyncClient,
    *,
    query: str,
    candidates: list[DenseSearchHit],
    model: str,
    char_limit: int,
    token_window: int,
    text_mode: str,
) -> list[DenseSearchHit]:
    ids = {f"C{index}": hit for index, hit in enumerate(candidates, start=1)}
    response = await client.post(
        "/v1/rerank",
        json={
            "model": model,
            "query": query,
            "documents": [
                {
                    "id": candidate_id,
                    "text": build_candidate_text(hit, max_chars=char_limit, mode=text_mode),
                }
                for candidate_id, hit in ids.items()
            ],
            "top_n": len(ids),
            "max_length": token_window,
        },
    )
    response.raise_for_status()
    ranked, _ = parse_cross_encoder_response(response.json(), allowed_ids=set(ids))
    return [replace(ids[candidate_id], rerank_score=score) for candidate_id, score in ranked]


async def _rerank_batched(
    client: httpx.AsyncClient,
    **kwargs: Any,
) -> list[DenseSearchHit]:
    candidates = kwargs.pop("candidates")
    if not isinstance(candidates, list):
        raise TypeError("candidates 必须是 list")
    ranked: list[DenseSearchHit] = []
    for offset in range(0, len(candidates), 100):
        ranked.extend(
            await _rerank_raw(client, candidates=candidates[offset : offset + 100], **kwargs)
        )
    return sorted(
        ranked,
        key=lambda hit: (-(hit.rerank_score or 0.0), str(hit.chunk_id)),
    )


def blend_rrf_ce_ranks(
    rrf_hits: list[DenseSearchHit],
    ce_hits: list[DenseSearchHit],
    *,
    ce_weight: float,
    rank_constant: int = 60,
) -> list[DenseSearchHit]:
    if not 0 <= ce_weight <= 1:
        raise ValueError("ce_weight 必须位于 [0,1]")
    ce_ranks = {hit.chunk_id: rank for rank, hit in enumerate(ce_hits, start=1)}
    scored = [
        (
            hit,
            (1 - ce_weight) / (rank_constant + rrf_rank)
            + ce_weight / (rank_constant + ce_ranks[hit.chunk_id]),
        )
        for rrf_rank, hit in enumerate(rrf_hits, start=1)
    ]
    return [
        replace(hit, rerank_score=score)
        for hit, score in sorted(scored, key=lambda row: (-row[1], str(row[0].chunk_id)))
    ]


async def _deep_dense(
    session: AsyncSession, gateway: ModelGateway, embedding: list[float]
) -> list[DeepRow]:
    await session.execute(text("SET LOCAL hnsw.ef_search = 400"))
    rows = (
        await session.execute(
            text(DEEP_DENSE_SQL),
            {
                "embedding": _vector(embedding),
                "embedding_model": gateway.embedding_model,
                "embedding_provider": gateway.embedding_provider,
                "embedding_revision": gateway.embedding_revision,
                "deep_k": DEEP_K,
            },
        )
    ).mappings()
    return [_deep_row(row) for row in rows]


async def _deep_lexical(session: AsyncSession, query: str) -> list[DeepRow]:
    rows = (
        await session.execute(text(DEEP_LEXICAL_SQL), {"query": query, "deep_k": DEEP_K})
    ).mappings()
    return [_deep_row(row) for row in rows]


def _deep_row(row: Any) -> DeepRow:
    return DeepRow(
        chunk_id=row["chunk_id"],
        version_id=row["version_id"],
        char_start=int(row["char_start"]),
        char_end=int(row["char_end"]),
        rank=int(row["rank"]),
    )


async def _oracle_doc_hits(
    session: AsyncSession,
    *,
    gateway: ModelGateway,
    embedding: list[float],
    version_id: UUID,
    temporal_ctx: datetime | None = None,
) -> list[DenseSearchHit]:
    visibility = (
        "c.is_searchable=true AND v.invalid_at IS NULL"
        if temporal_ctx is None
        else (
            "v.activated_at IS NOT NULL "
            "AND v.activated_at <= :temporal_ctx "
            "AND (v.invalid_at IS NULL OR v.invalid_at > :temporal_ctx)"
        )
    )
    rows = (
        await session.execute(
            text(ORACLE_DOC_SQL.replace("__VISIBILITY__", visibility)),
            {
                "embedding": _vector(embedding),
                "version_id": version_id,
                "embedding_model": gateway.embedding_model,
                "embedding_revision": gateway.embedding_revision,
                "embedding_provider": gateway.embedding_provider,
                "temporal_ctx": temporal_ctx,
            },
        )
    ).mappings()
    return [
        DenseSearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            version_no=row["version_no"],
            title=row["title"],
            source_uri=row["source_uri"],
            content=row["content"],
            content_tokens=row["content_tokens"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            score=float(row["score"]),
            dense_score=float(row["score"]),
            heading_path=list(row["heading_path"]),
            blocks=[],
            strategy="heading",
        )
        for row in rows
    ]


def _metrics(
    pool: ItemPool,
    ranked: list[DenseSearchHit],
    *,
    final_top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
) -> dict[str, float]:
    values = evaluate_retrieval(
        pool.item.gold_spans,
        [_retrieved_chunk(hit) for hit in ranked],
        pool.ideal,
        top_k=final_top_k,
        token_budget=token_budget,
        theta=theta,
        alpha=alpha,
    ).to_dict()
    return {name: float(values[name]) for name in METRIC_NAMES}


def _variant_summary(rows: list[VariantItem]) -> dict[str, float]:
    return {name: fmean(row.metrics[name] for row in rows) for name in METRIC_NAMES}


def _variant_bootstrap(
    baseline: list[VariantItem], candidate: list[VariantItem]
) -> dict[str, object]:
    if [row.item_id for row in baseline] != [row.item_id for row in candidate]:
        raise ValueError("paired bootstrap item 轴不一致")
    metrics = {
        name: MetricSamples(
            tuple(RatioPoint(row.metrics[name], 1) for row in baseline),
            tuple(RatioPoint(row.metrics[name], 1) for row in candidate),
        )
        for name in METRIC_NAMES
    }
    return {
        name: result.to_dict()
        for name, result in paired_bootstrap(
            metrics,
            higher_is_better={"max_doc_share_at_k": False},
        ).items()
    }


def _span_rank(
    ranked: list[DenseSearchHit], span: GoldSpan, *, theta: float
) -> int | None:
    return next(
        (
            rank
            for rank, hit in enumerate(ranked, start=1)
            if hits(_retrieved_chunk(hit), span, theta=theta)
        ),
        None,
    )


def _deep_span_rank(rows: list[DeepRow], span: GoldSpan, *, theta: float) -> int | None:
    return next(
        (
            row.rank
            for row in rows
            if row.version_id == span.version_id
            and _overlap(row.char_start, row.char_end, span) >= theta
        ),
        None,
    )


def _first_chunk_rank(rows: list[DeepRow], version_id: UUID) -> int | None:
    return next((row.rank for row in rows if row.version_id == version_id), None)


def _document_rank(rows: list[DeepRow], version_id: UUID) -> int | None:
    seen: list[UUID] = []
    for row in rows:
        if row.version_id not in seen:
            seen.append(row.version_id)
        if row.version_id == version_id:
            return len(seen)
    return None


def _p1a_status(
    *,
    dense_rank: int | None,
    lexical_rank: int | None,
    rrf_rank: int | None,
    union_rank: int | None,
    old_rank: int | None,
    final_top_k: int,
) -> str:
    if dense_rank is None and lexical_rank is None:
        return "pool_outside"
    if rrf_rank is None and union_rank is not None:
        return "rrf_truncated"
    if rrf_rank is not None and rrf_rank <= final_top_k and (
        old_rank is None or old_rank > final_top_k
    ):
        return "rerank_strict_demoted"
    if old_rank is None or old_rank > final_top_k:
        return "rerank_not_rescued"
    return "survives_top_k"


def _span_title(ranked: list[Any], span: GoldSpan) -> str | None:
    for hit in ranked:
        if getattr(hit, "version_id", None) == span.version_id:
            title = getattr(hit, "title", None)
            if isinstance(title, str):
                return title
    return None


async def _version_title(session: AsyncSession, version_id: UUID) -> str:
    value = (
        await session.execute(
            text(
                "SELECT d.title FROM document_versions v JOIN documents d ON d.id=v.document_id "
                "WHERE v.id=:version_id"
            ),
            {"version_id": version_id},
        )
    ).scalar_one()
    return str(value)


def _overlap(start: int, end: int, span: GoldSpan) -> float:
    return max(0, min(end, span.char_end) - max(start, span.char_start)) / (
        span.char_end - span.char_start
    )


def _min_optional(left: int | None, right: int | None) -> float:
    values = [value for value in (left, right) if value is not None]
    return float(min(values)) if values else math.inf


def _vector(embedding: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in embedding) + "]"


def _is_cross_document(item: EvalItem) -> bool:
    return item.answerable and len({span.version_id for span in item.gold_spans}) > 1


def _pool_fingerprint(pools: list[ItemPool]) -> str:
    data = [
        {
            "item_id": str(pool.item.id),
            "dense": [str(hit.chunk_id) for hit in pool.dense],
            "lexical": [str(hit.chunk_id) for hit in pool.lexical],
        }
        for pool in pools
    ]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_checkpoint(
    path: Path, *, suite_sha: str, pool_sha: str
) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P1 checkpoint 必须是 JSON 对象")
    if payload.get("suite_sha256") != suite_sha or payload.get("candidate_pool_sha256") != pool_sha:
        raise ValueError("P1 checkpoint 与当前 suite/candidate pool 不一致，拒绝复用")
    return payload


def _write_checkpoint(
    path: Path,
    payload: dict[str, object],
    *,
    suite_sha: str,
    pool_sha: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    material = {
        **payload,
        "suite_sha256": suite_sha,
        "candidate_pool_sha256": pool_sha,
    }
    path.write_text(
        json.dumps(material, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _render_markdown(payload: dict[str, object]) -> str:
    p1a = payload["p1a"]
    p1b = payload["p1b"]
    p1c = payload["p1c"]
    p1d = payload["p1d"]
    assert isinstance(p1a, dict)
    assert isinstance(p1b, dict)
    assert isinstance(p1c, dict)
    assert isinstance(p1d, dict)
    lines = [
        f"# P1 A-D 检索诊断 · {payload['label']}",
        "",
        f"- suite: `{payload['suite']}`；dev items={payload['item_count']}；gold spans={payload['gold_span_count']}",
        f"- candidate pool SHA256: `{payload['candidate_pool_sha256']}`",
        "- A 固定 1200 chars/512 tokens；B/D 固定 8000 chars/1024 tokens；最终 Top-10 不变",
        "",
        "## P1-A · 旧路径 vs 两臂并集",
        "",
        "| 路径 | goldDocR | spanRec | maxShare | nDCG |",
        "|---|---:|---:|---:|---:|",
    ]
    a_summary = p1a["summary"]
    assert isinstance(a_summary, dict)
    for name in ("rrf50", "old", "union"):
        row = a_summary[name]
        assert isinstance(row, dict)
        lines.append(_metric_row(name, row))
    lines.extend(
        [
            "",
            f"状态计数：`{p1a['status_counts']}`",
            "",
            "## P1-B · cross-encoder 表示与排序",
            "",
            "| 表示 | goldDocR | spanRec | maxShare | nDCG |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    representation = p1b["representation_summary"]
    assert isinstance(representation, dict)
    for mode in TEXT_MODES:
        row = representation[mode]
        assert isinstance(row, dict)
        lines.append(_metric_row(mode, row))
    lines.extend(
        [
            "",
            f"默认表示可见排序失配：{p1b['default_visible_mismatch_count']}；严格降级：{p1b['strict_demoted_count']}。",
            "",
            "### RRF–CE rank fusion 诊断",
            "",
            "| CE 权重 | goldDocR | spanRec | maxShare | nDCG |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    blend = p1b["rank_blend_summary"]
    assert isinstance(blend, dict)
    for weight in BLEND_WEIGHTS:
        name = f"ce_weight_{weight:.2f}"
        row = blend[name]
        assert isinstance(row, dict)
        lines.append(_metric_row(f"{weight:.2f}", row))
    lines.extend(
        [
            "",
            "## P1-C · 池外 span 深排",
            "",
            "| 文档 | dense chunk/doc-first/doc-rank | lexical chunk/doc-first/doc-rank | 文档 Top-10 可达 |",
            "|---|---:|---:|---|",
        ]
    )
    c_rows = p1c["rows"]
    assert isinstance(c_rows, list)
    for row in c_rows:
        assert isinstance(row, dict)
        lines.append(
            f"| {row['title']} | {_ranks(row, 'dense')} | {_ranks(row, 'lexical')} |"
            f" {'是' if row['right_doc_reachable_top10'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "## P1-D · 文档级二跳 oracle",
            "",
            f"符合文档 Top-10 可达的池外 spans：{p1d['eligible_span_count']}；局部 CE Top-10 救回：{p1d['cross_encoder_top10_rescued']}。",
            "",
            "| 文档 | 文档 chunk 数 | 局部 dense rank | 局部 CE rank | CE Top-10 |",
            "|---|---:|---:|---:|---|",
        ]
    )
    d_rows = p1d["rows"]
    assert isinstance(d_rows, list)
    for row in d_rows:
        assert isinstance(row, dict)
        lines.append(
            f"| {row['title']} | {row['document_chunk_count']} |"
            f" {_display(row['oracle_dense_local_rank'])} |"
            f" {_display(row['oracle_cross_encoder_local_rank'])} |"
            f" {'是' if row['oracle_cross_encoder_top10'] else '否'} |"
        )
    lines.extend(["", f"> {p1d['warning']}", ""])
    return "\n".join(lines)


def _metric_row(name: str, row: dict[str, object]) -> str:
    return (
        f"| {name} | {_number(row['gold_doc_recall_at_k']):.3f} |"
        f" {_number(row['span_recall_at_k']):.3f} |"
        f" {_number(row['max_doc_share_at_k']):.3f} |"
        f" {_number(row['ndcg_at_k']):.3f} |"
    )


def _ranks(row: dict[str, object], prefix: str) -> str:
    return "/".join(
        _display(row[f"{prefix}_{suffix}"])
        for suffix in ("chunk_rank", "doc_first_chunk_rank", "doc_rank")
    )


def _display(value: object) -> str:
    return "—" if value is None else str(value)


def _number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return float(value)


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1 A-D 严格 dev 检索诊断")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/p1-retrieval-diagnostics")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_p1_diagnostics(
            suite_path=args.suite,
            label=args.label,
            output_dir=args.output_dir,
            reranker_base_url=args.reranker_base_url,
            timeout_s=args.timeout_s,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
