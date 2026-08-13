import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from uuid import UUID

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, dense_search
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from eval.mapping import GoldSpan, RetrievedChunk
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


async def run_dense_baseline(
    *,
    dataset_name: str,
    label: str,
    origin: str,
    top_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    output_root: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    config: dict[str, object] = {
        "strategy": "dense-only",
        "top_k": top_k,
        "token_budget": token_budget,
        "theta": theta,
        "alpha": alpha,
        "origin": origin,
        "refusal_threshold": settings.refusal_threshold,
        "embedding_model": settings.embedding_model,
        "embedding_provider_base_url": settings.embedding_base_url,
        "embedding_revision": settings.embedding_revision,
        "embedding_dim": settings.embedding_dim,
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
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
            )
            refusal = analyze_refusal(
                [(item.top_score, item.answerable) for item in results],
                configured_threshold=settings.refusal_threshold,
            )
            metrics = _aggregate(results, refusal)
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
    token_budget: int,
    theta: float,
    alpha: float,
) -> list[ItemResult]:
    results: list[ItemResult] = []
    for item in items:
        started = time.monotonic()
        hits = await dense_search(session, gateway, query=item.question, top_k=top_k)
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        retrieved = [_retrieved_chunk(hit) for hit in hits]
        metrics: RetrievalMetrics | None = None
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
        result = ItemResult(
            item_id=item.id,
            category=item.category,
            question=item.question,
            answerable=item.answerable,
            top_score=hits[0].score if hits else -1.0,
            latency_ms=latency_ms,
            retrieval=metrics.to_dict() if metrics else None,
            retrieved=[_serialize_hit(hit) for hit in hits],
        )
        await _store_result(session, run_id, result)
        results.append(result)
    return results


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
        "char_start": hit.char_start,
        "char_end": hit.char_end,
        "content_tokens": hit.content_tokens,
    }


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
                }
            ),
            "latency_ms": result.latency_ms,
        },
    )
    await session.commit()


def _aggregate(
    results: list[ItemResult], refusal: RefusalAnalysis
) -> dict[str, object]:
    retrievals = [item.retrieval for item in results if item.retrieval is not None]
    metric_names = (
        "span_recall_at_k",
        "budget_span_recall",
        "ndcg_at_k",
        "alpha_ndcg_at_k",
        "mrr",
        "context_precision",
    )
    retrieval_summary = {
        name: fmean(float(item[name]) for item in retrievals) if retrievals else None
        for name in metric_names
    }
    latencies = sorted(item.latency_ms for item in results)
    return {
        "item_count": len(results),
        "answerable_count": sum(item.answerable for item in results),
        "unanswerable_count": sum(not item.answerable for item in results),
        "retrieval": retrieval_summary,
        "refusal": refusal.to_dict(),
        "latency_ms": {
            "mean": fmean(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
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
    assert isinstance(metrics, dict)
    retrieval = metrics["retrieval"]
    refusal = metrics["refusal"]
    latency = metrics["latency_ms"]
    assert isinstance(retrieval, dict)
    assert isinstance(refusal, dict)
    assert isinstance(latency, dict)
    best = refusal.get("best")
    configured = refusal.get("configured")
    lines = [
        "# Dense-only 评测基线",
        "",
        f"- 数据集: `{payload['dataset']}`",
        f"- 标签: `{payload['label']}`",
        f"- Git: `{payload['git_sha']}`",
        f"- 样本: {metrics['item_count']}（可答 {metrics['answerable_count']} / 不可答 {metrics['unanswerable_count']}）",
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
        "## 拒答",
        "",
        f"- AUROC: {_fmt(refusal.get('auroc'))}",
        f"- 当前阈值: {_threshold_line(configured)}",
        f"- dev 最优阈值: {_threshold_line(best)}",
        "",
        "## 延迟",
        "",
        f"- mean: {_fmt(latency.get('mean'), digits=1)} ms",
        f"- p95: {_fmt(latency.get('p95'), digits=1)} ms",
        "",
        "> 只有包含人工确认 gold span 的数据集才能作为正式质量结论；synthetic title smoke 仅验证工程链路。",
        "",
    ]
    return "\n".join(lines)


def _threshold_line(value: object) -> str:
    if not isinstance(value, dict):
        return "样本类别不足，无法计算"
    return (
        f"{float(value['threshold']):.4f}，macro-F1={float(value['macro_f1']):.4f}，"
        f"误答={value['false_answerable']}，误拒={value['false_refusal']}"
    )


def _fmt(value: object, *, digits: int = 4) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "-"


def _percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    position = min(len(values) - 1, max(0.0, (len(values) - 1) * quantile))
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


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
    parser = argparse.ArgumentParser(description="运行 dense-only gold span 评测")
    parser.add_argument("--dataset", default="core-dev")
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="human"
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--alpha", type=float, default=0.5)
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
            token_budget=args.token_budget,
            theta=args.theta,
            alpha=args.alpha,
            output_root=args.output_dir,
        )
    )
    print(report)


if __name__ == "__main__":
    main()
