import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, dense_search, multi_query_dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.services.query_decomposition import plan_retrieval_queries
from app.services.reranker import rerank_candidates
from eval.mapping import GoldSpan, RetrievedChunk
from eval.metrics.diagnostics import diagnose_spans, percentile, summarize_scores
from eval.metrics.refusal import RefusalAnalysis, analyze_refusal
from eval.metrics.retrieval import RetrievalMetrics, evaluate_retrieval


@dataclass(frozen=True)
class EvalItem:
    id: UUID
    category: str
    question: str
    gold_spans: list[GoldSpan]

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"


@dataclass(frozen=True)
class ItemResult:
    item_id: UUID
    category: str
    question: str
    answerable: bool
    top_score: float
    latency_ms: int
    retrieval: dict[str, float | int] | None
    retrieved: list[dict[str, object]]
    span_diagnostics: list[dict[str, object]]


async def run_dense_baseline(
    *,
    dataset_name: str,
    label: str,
    origin: str,
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    output_root: Path,
    strategy: str = "dense-only",
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    if not 1 <= top_k <= diagnostic_k <= 50:
        raise ValueError("必须满足 1 <= top_k <= diagnostic_k <= 50")
    supported_strategies = {
        "dense-only",
        "multi-query-dense",
        "dense-rerank",
        "dense-lexical-rrf",
        "dense-lexical-rrf-rerank",
    }
    if strategy not in supported_strategies:
        raise ValueError(f"不支持的检索策略: {strategy}")
    config: dict[str, object] = {
        "strategy": strategy,
        "top_k": top_k,
        "diagnostic_k": diagnostic_k,
        "token_budget": token_budget,
        "theta": theta,
        "alpha": alpha,
        "origin": origin,
        "refusal_threshold": settings.refusal_threshold,
        "embedding_model": settings.embedding_model,
        "embedding_provider_base_url": settings.embedding_base_url,
        "embedding_revision": settings.embedding_revision,
        "embedding_dim": settings.embedding_dim,
        "chat_model": settings.tier_main_model,
        "query_decomposition_max_subqueries": settings.query_decomposition_max_subqueries,
        "reranker_base_url": settings.reranker_base_url,
        "reranker_model": settings.reranker_model,
        "rrf_k": settings.rrf_k,
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    git_sha = _git_sha()
    async with session_factory() as session:
        dataset_id, items = await _load_items(session, dataset_name, origin=origin)
        run_id = await _create_run(
            session,
            dataset_id=dataset_id,
            label=label,
            git_sha=git_sha,
            config=config,
            config_hash=config_hash,
            settings=settings,
        )
        gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
        try:
            results = await _evaluate_items(
                session,
                gateway,
                run_id=run_id,
                items=items,
                top_k=top_k,
                diagnostic_k=diagnostic_k,
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
                strategy=strategy,
                settings=settings,
            )
            refusal = analyze_refusal(
                [(item.top_score, item.answerable) for item in results],
                configured_threshold=settings.refusal_threshold,
            )
            metrics = _aggregate(
                results,
                refusal,
                configured_threshold=settings.refusal_threshold,
            )
            await _finish_run(session, run_id, metrics)
        except Exception:
            await session.rollback()
            raise
        finally:
            await gateway.aclose()
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
        "items": [_json_item(item) for item in results],
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = run_dir / "report.md"
    report.write_text(_markdown_report(payload), encoding="utf-8")
    return report


async def _load_items(
    session: AsyncSession, dataset_name: str, *, origin: str
) -> tuple[UUID, list[EvalItem]]:
    dataset_id = (
        await session.execute(
            text("SELECT id FROM eval_datasets WHERE name=:name"),
            {"name": dataset_name},
        )
    ).scalar_one_or_none()
    if dataset_id is None:
        await session.rollback()
        raise ValueError(f"评测数据集不存在: {dataset_name}")
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, category, question, gold_spans,
                           validate_eval_spans(gold_spans) AS spans_valid
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
    await session.rollback()
    if not rows:
        raise ValueError(f"数据集 {dataset_name} 没有 origin={origin} 的样本")
    items: list[EvalItem] = []
    for row in rows:
        if row["category"] in {"global", "agent_task"}:
            raise ValueError(
                f"dense-only 基线不支持类别 {row['category']}: {row['id']}"
            )
        if not row["spans_valid"]:
            raise ValueError(f"样本包含 stale gold span: {row['id']}")
        spans = [
            GoldSpan(
                version_id=UUID(item["version_id"]),
                char_start=int(item["char_start"]),
                char_end=int(item["char_end"]),
                quote=str(item["quote"]),
            )
            for item in row["gold_spans"]
        ]
        if row["category"] != "unanswerable" and not spans:
            raise ValueError(f"可答样本缺少 gold span: {row['id']}")
        items.append(
            EvalItem(
                id=row["id"],
                category=row["category"],
                question=row["question"],
                gold_spans=spans,
            )
        )
    return dataset_id, items


async def _create_run(
    session: AsyncSession,
    *,
    dataset_id: UUID,
    label: str,
    git_sha: str,
    config: dict[str, object],
    config_hash: str,
    settings: Settings,
) -> UUID:
    run_id = uuid7()
    actual_models = {
        "query_embedding": [
            {
                "provider": "openai_compatible",
                "model": settings.embedding_model,
                "revision": settings.embedding_revision,
            }
        ]
    }
    if config.get("strategy") in {"multi-query-dense", "dense-rerank"}:
        actual_models["chat"] = [
            {
                "provider": "openai_compatible",
                "model": settings.tier_main_model,
                "revision": "unversioned",
            }
        ]
    async with session.begin():
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
    return run_id


async def _evaluate_items(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    run_id: UUID,
    items: list[EvalItem],
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    strategy: str,
    settings: Settings,
) -> list[ItemResult]:
    results: list[ItemResult] = []
    for item in items:
        started = time.monotonic()
        hits = await _retrieve_with_strategy(
            session,
            gateway,
            query=item.question,
            strategy=strategy,
            top_k=top_k,
            diagnostic_k=diagnostic_k,
            settings=settings,
        )
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        retrieved = [_retrieved_chunk(hit) for hit in hits]
        metrics: RetrievalMetrics | None = None
        span_diagnostics: list[dict[str, object]] = []
        if item.answerable:
            candidates = await _candidate_chunks(
                session,
                item.gold_spans,
                embedding_model=gateway.embedding_model,
                embedding_provider=gateway.embedding_provider,
                embedding_revision=gateway.embedding_revision,
            )
            metrics = evaluate_retrieval(
                item.gold_spans,
                retrieved,
                candidates,
                top_k=top_k,
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
            )
            span_diagnostics = [
                diagnostic.to_dict()
                for diagnostic in diagnose_spans(
                    item.gold_spans,
                    retrieved,
                    candidates,
                    top_k=top_k,
                    token_budget=token_budget,
                    theta=theta,
                )
            ]
        result = ItemResult(
            item_id=item.id,
            category=item.category,
            question=item.question,
            answerable=item.answerable,
            top_score=max((_retrieval_score(hit) for hit in hits), default=-1.0),
            latency_ms=latency_ms,
            retrieval=metrics.to_dict() if metrics else None,
            retrieved=[_serialize_hit(hit) for hit in hits[:top_k]],
            span_diagnostics=span_diagnostics,
        )
        await _store_result(session, run_id, result)
        results.append(result)
    return results


async def _retrieve_with_strategy(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    strategy: str,
    top_k: int,
    diagnostic_k: int,
    settings: Settings,
) -> list[DenseSearchHit]:
    if strategy == "dense-only":
        return await dense_search(session, gateway, query=query, top_k=diagnostic_k)
    if strategy == "multi-query-dense":
        plan = await plan_retrieval_queries(
            gateway,
            query=query,
            max_subqueries=settings.query_decomposition_max_subqueries,
            max_tokens=settings.query_decomposition_max_tokens,
        )
        return await multi_query_dense_search(
            session,
            gateway,
            queries=plan.queries,
            top_k=diagnostic_k,
            per_query_top_k=diagnostic_k,
        )
    if strategy == "dense-rerank":
        candidates = await dense_search(session, gateway, query=query, top_k=diagnostic_k)
        if len(candidates) <= top_k:
            return candidates
        result = await rerank_candidates(
            query=query,
            candidates=candidates,
            top_k=top_k,
            base_url=settings.reranker_base_url,
            model=settings.reranker_model,
            timeout_s=settings.reranker_timeout_s,
            max_candidate_chars=settings.rerank_max_candidate_chars,
        )
        if not result.applied:
            raise RuntimeError(result.reason)
        return result.hits
    if strategy == "dense-lexical-rrf":
        dense_hits = await dense_search(session, gateway, query=query, top_k=diagnostic_k)
        lexical_hits = await lexical_search(session, query=query, top_k=diagnostic_k)
        return reciprocal_rank_fusion(
            [dense_hits, lexical_hits],
            top_k=diagnostic_k,
            rrf_k=settings.rrf_k,
        )
    if strategy == "dense-lexical-rrf-rerank":
        dense_hits = await dense_search(session, gateway, query=query, top_k=diagnostic_k)
        lexical_hits = await lexical_search(session, query=query, top_k=diagnostic_k)
        candidates = reciprocal_rank_fusion(
            [dense_hits, lexical_hits],
            top_k=diagnostic_k,
            rrf_k=settings.rrf_k,
        )
        if len(candidates) <= top_k:
            return candidates
        result = await rerank_candidates(
            query=query,
            candidates=candidates,
            top_k=top_k,
            base_url=settings.reranker_base_url,
            model=settings.reranker_model,
            timeout_s=settings.reranker_timeout_s,
            max_candidate_chars=settings.rerank_max_candidate_chars,
        )
        if not result.applied:
            raise RuntimeError(result.reason)
        return result.hits
    raise AssertionError(f"未处理的检索策略: {strategy}")


async def _candidate_chunks(
    session: AsyncSession,
    spans: list[GoldSpan],
    *,
    embedding_model: str,
    embedding_provider: str,
    embedding_revision: str,
) -> list[RetrievedChunk]:
    version_ids = list({span.version_id for span in spans})
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, version_id, char_start, char_end, content_tokens
                    FROM chunks
                    WHERE version_id=ANY(:version_ids)
                      AND strategy='heading'
                      AND embedding_model=:embedding_model
                      AND embedding_provider=:embedding_provider
                      AND embedding_revision=:embedding_revision
                    """
                ),
                {
                    "version_ids": version_ids,
                    "embedding_model": embedding_model,
                    "embedding_provider": embedding_provider,
                    "embedding_revision": embedding_revision,
                },
            )
        )
        .mappings()
        .all()
    )
    return [
        RetrievedChunk(
            chunk_id=row["id"],
            version_id=row["version_id"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            content_tokens=row["content_tokens"],
            score=0.0,
        )
        for row in rows
    ]


def _retrieved_chunk(hit: DenseSearchHit) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=hit.chunk_id,
        version_id=hit.version_id,
        char_start=hit.char_start,
        char_end=hit.char_end,
        content_tokens=hit.content_tokens,
        score=hit.score,
    )


def _serialize_hit(hit: DenseSearchHit) -> dict[str, object]:
    return {
        "chunk_id": str(hit.chunk_id),
        "version_id": str(hit.version_id),
        "document_id": str(hit.document_id),
        "source_uri": hit.source_uri,
        "score": hit.score,
        "dense_score": hit.dense_score,
        "lexical_score": hit.lexical_score,
        "fusion_score": hit.fusion_score,
        "rerank_score": hit.rerank_score,
        "char_start": hit.char_start,
        "char_end": hit.char_end,
        "content_tokens": hit.content_tokens,
    }


def _retrieval_score(hit: DenseSearchHit) -> float:
    if hit.dense_score is not None:
        return hit.dense_score
    return hit.score


async def _store_result(
    session: AsyncSession, run_id: UUID, result: ItemResult
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO eval_results
                (id, run_id, item_id, retrieved, scores, latency_ms)
            VALUES
                (:id, :run_id, :item_id, CAST(:retrieved AS jsonb),
                 CAST(:scores AS jsonb), :latency_ms)
            """
        ),
        {
            "id": uuid7(),
            "run_id": run_id,
            "item_id": result.item_id,
            "retrieved": json.dumps(result.retrieved),
            "scores": json.dumps(
                {
                    "answerable": result.answerable,
                    "top_score": result.top_score,
                    "retrieval": result.retrieval,
                    "span_diagnostics": result.span_diagnostics,
                }
            ),
            "latency_ms": result.latency_ms,
        },
    )
    await session.commit()


def _aggregate(
    results: list[ItemResult],
    refusal: RefusalAnalysis,
    *,
    configured_threshold: float,
) -> dict[str, object]:
    retrieval_summary = _retrieval_summary(results)
    category_summary = {
        category: _slice_summary(
            [item for item in results if item.category == category],
            configured_threshold=configured_threshold,
        )
        for category in sorted({item.category for item in results})
    }
    refusal_summary = refusal.to_dict()
    refusal_summary["score_distributions"] = {
        "answerable": summarize_scores(
            [item.top_score for item in results if item.answerable]
        ),
        "unanswerable": summarize_scores(
            [item.top_score for item in results if not item.answerable]
        ),
    }
    status_counts = Counter(
        str(span["status"]) for item in results for span in item.span_diagnostics
    )
    missed_items = sum(
        any(span["status"] != "hit" for span in item.span_diagnostics)
        for item in results
    )
    latencies = sorted(item.latency_ms for item in results)
    return {
        "item_count": len(results),
        "answerable_count": sum(item.answerable for item in results),
        "unanswerable_count": sum(not item.answerable for item in results),
        "retrieval": retrieval_summary,
        "by_category": category_summary,
        "diagnostics": {
            "gold_span_count": sum(len(item.span_diagnostics) for item in results),
            "missed_item_count": missed_items,
            "status_counts": dict(sorted(status_counts.items())),
        },
        "refusal": refusal_summary,
        "latency_ms": {
            "mean": fmean(latencies) if latencies else None,
            "p95": percentile(latencies, 0.95),
        },
    }


def _retrieval_summary(results: list[ItemResult]) -> dict[str, float | None]:
    retrievals = [item.retrieval for item in results if item.retrieval is not None]
    metric_names = (
        "span_recall_at_k",
        "budget_span_recall",
        "ndcg_at_k",
        "alpha_ndcg_at_k",
        "mrr",
        "context_precision",
    )
    return {
        name: fmean(float(item[name]) for item in retrievals) if retrievals else None
        for name in metric_names
    }


def _slice_summary(
    results: list[ItemResult], *, configured_threshold: float
) -> dict[str, object]:
    refused = [item for item in results if item.top_score < configured_threshold]
    return {
        "item_count": len(results),
        "answerable_count": sum(item.answerable for item in results),
        "unanswerable_count": sum(not item.answerable for item in results),
        "retrieval": _retrieval_summary(results),
        "scores": summarize_scores([item.top_score for item in results]),
        "configured_refusal": {
            "threshold": configured_threshold,
            "refused_count": len(refused),
            "refusal_rate": len(refused) / len(results) if results else 0.0,
            "false_refusal": sum(item.answerable for item in refused),
            "false_answerable": sum(
                not item.answerable
                for item in results
                if item.top_score >= configured_threshold
            ),
        },
    }


async def _finish_run(
    session: AsyncSession, run_id: UUID, metrics: dict[str, object]
) -> None:
    async with session.begin():
        await session.execute(
            text(
                """
                UPDATE eval_runs
                SET metrics=CAST(:metrics AS jsonb), finished_at=now()
                WHERE id=:id
                """
            ),
            {"id": run_id, "metrics": json.dumps(metrics, ensure_ascii=False)},
        )


def _json_item(item: ItemResult) -> dict[str, object]:
    payload = asdict(item)
    payload["item_id"] = str(item.item_id)
    return payload


def _markdown_report(payload: dict[str, object]) -> str:
    metrics = payload["metrics"]
    config = payload["config"]
    items = payload["items"]
    assert isinstance(metrics, dict)
    assert isinstance(config, dict)
    assert isinstance(items, list)
    retrieval = metrics["retrieval"]
    refusal = metrics["refusal"]
    latency = metrics["latency_ms"]
    by_category = metrics["by_category"]
    diagnostics = metrics["diagnostics"]
    assert isinstance(retrieval, dict)
    assert isinstance(refusal, dict)
    assert isinstance(latency, dict)
    assert isinstance(by_category, dict)
    assert isinstance(diagnostics, dict)
    best = refusal.get("best")
    configured = refusal.get("configured")
    lines = [
        "# 检索策略评测",
        "",
        f"- 数据集: `{payload['dataset']}`",
        f"- 标签: `{payload['label']}`",
        f"- Git: `{payload['git_sha']}`",
        f"- Config hash: `{payload['config_hash']}`",
        f"- 策略: `{config['strategy']}`",
        f"- 样本: {metrics['item_count']}（可答 {metrics['answerable_count']} / 不可答 {metrics['unanswerable_count']}）",
        f"- 排名口径: Top {config['top_k']}；漏召回诊断深度: Top {config['diagnostic_k']}；token budget: {config['token_budget']}",
        "",
        "## 检索",
        "",
        "| span recall@k | budget recall | nDCG@k | α-nDCG@k | MRR | context precision |",
        "|---:|---:|---:|---:|---:|---:|",
        "| {span} | {budget} | {ndcg} | {alpha} | {mrr} | {precision} |".format(
            span=_fmt(retrieval.get("span_recall_at_k")),
            budget=_fmt(retrieval.get("budget_span_recall")),
            ndcg=_fmt(retrieval.get("ndcg_at_k")),
            alpha=_fmt(retrieval.get("alpha_ndcg_at_k")),
            mrr=_fmt(retrieval.get("mrr")),
            precision=_fmt(retrieval.get("context_precision")),
        ),
        "",
        "## 分类别",
        "",
        "| category | n | span recall | budget recall | nDCG | MRR | score median | 当前阈值拒答 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        *_category_markdown_rows(by_category),
        "",
        "## Gold span 漏召回诊断",
        "",
        f"- Gold spans: {diagnostics['gold_span_count']}；涉及漏召回/预算截断的样本: {diagnostics['missed_item_count']}。",
        f"- 状态计数: `{json.dumps(diagnostics['status_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        *_missed_span_markdown(items),
        "",
        "## 拒答",
        "",
        f"- AUROC: {_fmt(refusal.get('auroc'))}",
        f"- 当前阈值: {_threshold_line(configured)}",
        f"- dev 最优阈值: {_threshold_line(best)}",
        "",
        *_score_distribution_markdown(refusal),
        "",
        "## 延迟",
        "",
        f"- mean: {_fmt(latency.get('mean'), digits=1)} ms",
        f"- p95: {_fmt(latency.get('p95'), digits=1)} ms",
        "",
        _report_caveat(config, metrics),
        "",
    ]
    return "\n".join(lines)


def _category_markdown_rows(by_category: dict[object, object]) -> list[str]:
    rows: list[str] = []
    for category, raw_summary in by_category.items():
        assert isinstance(raw_summary, dict)
        retrieval = raw_summary["retrieval"]
        scores = raw_summary["scores"]
        refusal = raw_summary["configured_refusal"]
        assert isinstance(retrieval, dict)
        assert isinstance(scores, dict)
        assert isinstance(refusal, dict)
        rows.append(
            "| {category} | {count} | {recall} | {budget} | {ndcg} | {mrr} | "
            "{median} | {refused}/{count} |".format(
                category=category,
                count=raw_summary["item_count"],
                recall=_fmt(retrieval.get("span_recall_at_k")),
                budget=_fmt(retrieval.get("budget_span_recall")),
                ndcg=_fmt(retrieval.get("ndcg_at_k")),
                mrr=_fmt(retrieval.get("mrr")),
                median=_fmt(scores.get("median")),
                refused=refusal["refused_count"],
            )
        )
    return rows


def _missed_span_markdown(items: list[object]) -> list[str]:
    rows = [
        "| category | question | span | status | first hit rank | best overlap | quote |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    missed = 0
    for raw_item in items:
        assert isinstance(raw_item, dict)
        raw_spans = raw_item["span_diagnostics"]
        assert isinstance(raw_spans, list)
        for raw_span in raw_spans:
            assert isinstance(raw_span, dict)
            if raw_span["status"] == "hit":
                continue
            missed += 1
            rank = raw_span["first_hit_rank"]
            rows.append(
                "| {category} | {question} | {span} | {status} | {rank} | {overlap} | {quote} |".format(
                    category=raw_item["category"],
                    question=_markdown_cell(str(raw_item["question"]), limit=80),
                    span=int(raw_span["span_index"]) + 1,
                    status=raw_span["status"],
                    rank=rank if rank is not None else "-",
                    overlap=_fmt(raw_span["best_retrieved_overlap"]),
                    quote=_markdown_cell(str(raw_span["quote"]), limit=100),
                )
            )
    return rows if missed else ["Top-K 与 token budget 均覆盖全部 gold span。"]


def _score_distribution_markdown(refusal: dict[object, object]) -> list[str]:
    distributions = refusal["score_distributions"]
    assert isinstance(distributions, dict)
    answerable = distributions["answerable"]
    unanswerable = distributions["unanswerable"]
    assert isinstance(answerable, dict)
    assert isinstance(unanswerable, dict)
    lines = [
        "### Top score 分布",
        "",
        "| label | n | min | p25 | median | p75 | p95 | max | mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        _distribution_row("answerable", answerable),
        _distribution_row("unanswerable", unanswerable),
        "",
        "| score bin | answerable | unanswerable |",
        "|---|---:|---:|",
    ]
    answerable_histogram = answerable["histogram"]
    unanswerable_histogram = unanswerable["histogram"]
    assert isinstance(answerable_histogram, dict)
    assert isinstance(unanswerable_histogram, dict)
    for label in sorted(set(answerable_histogram) | set(unanswerable_histogram)):
        lines.append(
            f"| {label} | {answerable_histogram.get(label, 0)} | "
            f"{unanswerable_histogram.get(label, 0)} |"
        )
    return lines


def _distribution_row(label: str, distribution: dict[object, object]) -> str:
    return (
        "| {label} | {count} | {min} | {p25} | {median} | {p75} | {p95} | "
        "{max} | {mean} |"
    ).format(
        label=label,
        count=distribution["count"],
        min=_fmt(distribution["min"]),
        p25=_fmt(distribution["p25"]),
        median=_fmt(distribution["median"]),
        p75=_fmt(distribution["p75"]),
        p95=_fmt(distribution["p95"]),
        max=_fmt(distribution["max"]),
        mean=_fmt(distribution["mean"]),
    )


def _markdown_cell(value: str, *, limit: int) -> str:
    compact = " ".join(value.split()).replace("|", "\\|")
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _report_caveat(config: dict[object, object], metrics: dict[object, object]) -> str:
    if config.get("origin") != "human":
        return "> 非 human 数据只用于工程 smoke，不能作为正式质量结论。"
    unanswerable_count = metrics["unanswerable_count"]
    assert isinstance(unanswerable_count, int)
    if unanswerable_count < 10:
        return (
            "> 当前不可答样本少于 10 条；阈值分析仅作方向判断，不能直接固化为线上阈值。"
        )
    return "> 本报告来自人工确认 gold span；阈值仍应在独立 test 集复核后上线。"


def _threshold_line(value: object) -> str:
    if not isinstance(value, dict):
        return "样本类别不足，无法计算"
    return (
        f"{float(value['threshold']):.4f}，macro-F1={float(value['macro_f1']):.4f}，"
        f"误答={value['false_answerable']}，误拒={value['false_refusal']}"
    )


def _fmt(value: object, *, digits: int = 4) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "-"


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 gold span 检索策略评测")
    parser.add_argument("--dataset", default="core-dev")
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="human"
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--diagnostic-k",
        type=int,
        default=50,
        help="为漏召回归因保留的最大排名深度，必须不小于 top-k",
    )
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument(
        "--strategy",
        choices=[
            "dense-only",
            "multi-query-dense",
            "dense-rerank",
            "dense-lexical-rrf",
            "dense-lexical-rrf-rerank",
        ],
        default="dense-only",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/dense-baseline")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_dense_baseline(
            dataset_name=args.dataset,
            label=args.label,
            origin=args.origin,
            top_k=args.top_k,
            diagnostic_k=args.diagnostic_k,
            token_budget=args.token_budget,
            theta=args.theta,
            alpha=args.alpha,
            output_root=args.output_dir,
            strategy=args.strategy,
        )
    )
    print(report)


if __name__ == "__main__":
    main()
