"""E1 四套 chunk strategy 的离线、单变量检索跑批入口。"""

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.ingest.chunk_strategies import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_SEMANTIC_MIN_TOKENS,
    HARD_MAX_TOKENS,
)
from app.llm.gateway import build_model_gateway
from app.retrieval.lexical import LEXICAL_MODES
from app.retrieval.strategy import CHUNK_STRATEGIES, ChunkStrategy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eval.dense_baseline import (
    RETRIEVAL_STRATEGIES,
    _load_items,
    fingerprint_eval_items,
    run_dense_baseline,
)


class ChunkCorpusNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategyCorpusSummary:
    strategy: ChunkStrategy
    version_count: int
    chunk_count: int
    searchable_chunk_count: int
    stored_total_tokens: int
    stored_min_chunk_tokens: int
    stored_max_chunk_tokens: int
    build_signatures: tuple[str, ...]
    parameters: dict[str, object]


@dataclass(frozen=True)
class ChunkCorpusPreflight:
    dataset_id: UUID
    dataset_fingerprint: str
    item_count: int
    gold_version_ids: tuple[UUID, ...]
    active_version_ids: tuple[UUID, ...]
    embedding_model: str
    embedding_provider: str
    embedding_revision: str
    corpus_fingerprint: str
    strategies: tuple[StrategyCorpusSummary, ...]


@dataclass(frozen=True)
class ChunkStrategyBatchResult:
    manifest_path: Path
    run_ids: dict[ChunkStrategy, UUID]
    reused: dict[ChunkStrategy, bool]


async def preflight_chunk_corpus(
    session: AsyncSession,
    *,
    dataset_name: str,
    origin: str,
    embedding_model: str,
    embedding_provider: str,
    embedding_revision: str,
) -> ChunkCorpusPreflight:
    dataset_id, items = await _load_items(session, dataset_name, origin=origin)
    dataset_fingerprint = fingerprint_eval_items(items)
    gold_version_ids = tuple(
        sorted(
            {span.version_id for item in items for span in item.gold_spans},
            key=str,
        )
    )
    version_rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT v.id, d.source_uri,
                           v.embedding_model, v.embedding_provider, v.embedding_revision,
                           v.parse_meta->>'ingest_signature' AS heading_build_signature
                    FROM document_versions v
                    JOIN documents d ON d.id=v.document_id
                    WHERE v.activated_at IS NOT NULL
                      AND v.invalid_at IS NULL
                      AND d.deleted_at IS NULL
                    ORDER BY d.source_uri, v.id
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    if not version_rows:
        await session.rollback()
        raise ChunkCorpusNotReadyError("当前没有已激活、可参与 E1 的 document version")
    active_version_ids = tuple(row["id"] for row in version_rows)
    missing_gold_versions = sorted(set(gold_version_ids) - set(active_version_ids), key=str)
    if missing_gold_versions:
        await session.rollback()
        raise ChunkCorpusNotReadyError(
            "gold span 引用的 version 未激活: "
            + ", ".join(str(value) for value in missing_gold_versions)
        )
    expected_identity = (embedding_model, embedding_provider, embedding_revision)
    wrong_versions = [
        str(row["id"])
        for row in version_rows
        if (
            row["embedding_model"],
            row["embedding_provider"],
            row["embedding_revision"],
        )
        != expected_identity
    ]
    if wrong_versions:
        await session.rollback()
        raise ChunkCorpusNotReadyError(
            "document version embedding 身份与本次跑批不一致: " + ", ".join(wrong_versions)
        )

    aggregate_rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT version_id, strategy,
                           count(*) AS chunk_count,
                           count(DISTINCT chunk_index) AS distinct_indexes,
                           min(chunk_index) AS first_index,
                           max(chunk_index) AS last_index,
                           count(*) FILTER (WHERE is_searchable) AS searchable_count,
                           count(*) FILTER (WHERE embedding IS NULL) AS missing_embeddings,
                           count(*) FILTER (
                               WHERE ROW(embedding_model, embedding_provider, embedding_revision)
                                 IS DISTINCT FROM ROW(
                                   :embedding_model, :embedding_provider, :embedding_revision
                                 )
                           ) AS mismatched_embeddings,
                           sum(content_tokens) AS total_tokens,
                           min(content_tokens) AS min_tokens,
                           max(content_tokens) AS max_tokens,
                           array_remove(array_agg(DISTINCT build_signature), NULL)
                             AS build_signatures
                    FROM chunks
                    WHERE version_id=ANY(:version_ids)
                      AND strategy=ANY(:strategies)
                    GROUP BY version_id, strategy
                    ORDER BY version_id, strategy
                    """
                ),
                {
                    "version_ids": list(active_version_ids),
                    "strategies": list(CHUNK_STRATEGIES),
                    "embedding_model": embedding_model,
                    "embedding_provider": embedding_provider,
                    "embedding_revision": embedding_revision,
                },
            )
        )
        .mappings()
        .all()
    )
    by_key = {(row["version_id"], row["strategy"]): row for row in aggregate_rows}
    versions_by_id = {row["id"]: row for row in version_rows}
    failures: list[str] = []
    for version_id in active_version_ids:
        for strategy in CHUNK_STRATEGIES:
            row = by_key.get((version_id, strategy))
            if row is None:
                failures.append(f"{version_id}/{strategy}: missing")
                continue
            count = int(row["chunk_count"])
            if (
                int(row["distinct_indexes"]) != count
                or row["first_index"] != 0
                or row["last_index"] != count - 1
            ):
                failures.append(f"{version_id}/{strategy}: chunk_index 不连续")
            if int(row["searchable_count"]) != count:
                failures.append(f"{version_id}/{strategy}: 存在不可搜索 chunk")
            if int(row["missing_embeddings"]) > 0:
                failures.append(f"{version_id}/{strategy}: 缺少 embedding")
            if int(row["mismatched_embeddings"]) > 0:
                failures.append(f"{version_id}/{strategy}: embedding 身份不一致")
            signatures = tuple(sorted(str(value) for value in row["build_signatures"] or []))
            if strategy != "heading" and len(signatures) != 1:
                failures.append(f"{version_id}/{strategy}: build_signature 缺失或不唯一")
            if (
                strategy == "heading"
                and versions_by_id[version_id]["heading_build_signature"] is None
            ):
                failures.append(f"{version_id}/heading: ingest_signature 缺失")
    await session.rollback()
    if failures:
        preview = "; ".join(failures[:20])
        suffix = f"; 另有 {len(failures) - 20} 项" if len(failures) > 20 else ""
        raise ChunkCorpusNotReadyError(f"四策略 corpus 前置检查失败: {preview}{suffix}")

    heading_signatures = tuple(
        sorted(
            {
                str(row["heading_build_signature"])
                for row in version_rows
                if row["heading_build_signature"] is not None
            }
        )
    )
    summaries: list[StrategyCorpusSummary] = []
    for strategy in CHUNK_STRATEGIES:
        rows = [row for row in aggregate_rows if row["strategy"] == strategy]
        build_signatures = (
            heading_signatures
            if strategy == "heading"
            else tuple(
                sorted(
                    {
                        str(signature)
                        for row in rows
                        for signature in row["build_signatures"] or []
                    }
                )
            )
        )
        summaries.append(
            StrategyCorpusSummary(
                strategy=strategy,
                version_count=len(rows),
                chunk_count=sum(int(row["chunk_count"]) for row in rows),
                searchable_chunk_count=sum(int(row["searchable_count"]) for row in rows),
                stored_total_tokens=sum(int(row["total_tokens"]) for row in rows),
                stored_min_chunk_tokens=min(int(row["min_tokens"]) for row in rows),
                stored_max_chunk_tokens=max(int(row["max_tokens"]) for row in rows),
                build_signatures=build_signatures,
                parameters=_chunk_parameters(strategy),
            )
        )
    corpus_payload = {
        "active_version_ids": [str(value) for value in active_version_ids],
        "strategies": [_summary_json(summary) for summary in summaries],
        "embedding_identity": list(expected_identity),
    }
    corpus_fingerprint = hashlib.sha256(
        json.dumps(corpus_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ChunkCorpusPreflight(
        dataset_id=dataset_id,
        dataset_fingerprint=dataset_fingerprint,
        item_count=len(items),
        gold_version_ids=gold_version_ids,
        active_version_ids=active_version_ids,
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        embedding_revision=embedding_revision,
        corpus_fingerprint=corpus_fingerprint,
        strategies=tuple(summaries),
    )


async def run_chunk_strategy_batch(
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
    retrieval_strategy: str = "dense-lexical-rrf-rerank",
    rerank_candidate_text_mode: str | None = None,
    lexical_mode: str | None = None,
    reuse_completed: bool = True,
    settings: Settings | None = None,
) -> ChunkStrategyBatchResult:
    settings = settings or Settings()
    identity_gateway = build_model_gateway(settings)
    try:
        identity = (
            identity_gateway.embedding_model,
            identity_gateway.embedding_provider,
            identity_gateway.embedding_revision,
        )
    finally:
        await identity_gateway.aclose()
    async with session_factory() as session:
        preflight = await preflight_chunk_corpus(
            session,
            dataset_name=dataset_name,
            origin=origin,
            embedding_model=identity[0],
            embedding_provider=identity[1],
            embedding_revision=identity[2],
        )

    run_ids: dict[ChunkStrategy, UUID] = {}
    reused: dict[ChunkStrategy, bool] = {}
    reports: dict[ChunkStrategy, str | None] = {}
    summaries = {summary.strategy: summary for summary in preflight.strategies}
    for chunk_strategy in CHUNK_STRATEGIES:
        summary = summaries[chunk_strategy]
        result = await run_dense_baseline(
            dataset_name=dataset_name,
            label=f"{label}-{chunk_strategy}",
            origin=origin,
            top_k=top_k,
            diagnostic_k=diagnostic_k,
            token_budget=token_budget,
            theta=theta,
            alpha=alpha,
            output_root=output_root / "runs",
            strategy=retrieval_strategy,
            rerank_candidate_text_mode=rerank_candidate_text_mode,
            lexical_mode=lexical_mode,
            chunk_strategy=chunk_strategy,
            expected_dataset_fingerprint=preflight.dataset_fingerprint,
            chunk_metadata={
                "corpus_fingerprint": preflight.corpus_fingerprint,
                "summary": _summary_json(summary),
            },
            token_count_mode="unicode",
            reuse_completed=reuse_completed,
            settings=settings,
        )
        run_ids[chunk_strategy] = result.run_id
        reused[chunk_strategy] = result.reused
        reports[chunk_strategy] = str(result.report_path) if result.report_path else None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    manifest_dir = output_root / f"{timestamp}-{_slug(label)}"
    manifest_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = manifest_dir / "manifest.json"
    manifest = {
        "dataset": dataset_name,
        "label": label,
        "origin": origin,
        "retrieval_strategy": retrieval_strategy,
        "top_k": top_k,
        "diagnostic_k": diagnostic_k,
        "token_budget": token_budget,
        "token_count_mode": "unicode",
        "theta": theta,
        "alpha": alpha,
        "dataset_fingerprint": preflight.dataset_fingerprint,
        "corpus_fingerprint": preflight.corpus_fingerprint,
        "embedding_identity": {
            "model": preflight.embedding_model,
            "provider": preflight.embedding_provider,
            "revision": preflight.embedding_revision,
        },
        "preflight": {
            "item_count": preflight.item_count,
            "gold_version_ids": [str(value) for value in preflight.gold_version_ids],
            "active_version_ids": [str(value) for value in preflight.active_version_ids],
            "strategies": [_summary_json(value) for value in preflight.strategies],
        },
        "runs": {
            strategy: {
                "run_id": str(run_ids[strategy]),
                "reused": reused[strategy],
                "report": reports[strategy],
            }
            for strategy in CHUNK_STRATEGIES
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    await close_database()
    return ChunkStrategyBatchResult(
        manifest_path=manifest_path,
        run_ids=run_ids,
        reused=reused,
    )


def _chunk_parameters(strategy: ChunkStrategy) -> dict[str, object]:
    common: dict[str, object] = {
        "offset_unit": "unicode_code_point",
        "table_policy": "atomic_until_hard_max",
        "hard_max_tokens": HARD_MAX_TOKENS,
    }
    if strategy == "fixed":
        return {
            **common,
            "algorithm": "unicode-fixed:1",
            "max_tokens": DEFAULT_MAX_TOKENS,
            "overlap_tokens": DEFAULT_OVERLAP_TOKENS,
        }
    if strategy == "recursive":
        return {
            **common,
            "algorithm": "paragraph-line-sentence:1",
            "max_tokens": DEFAULT_MAX_TOKENS,
            "separators": ["paragraph", "line", "sentence", "hard_token"],
        }
    if strategy == "semantic":
        return {
            **common,
            "algorithm": "adjacent-cosine-mad:1",
            "max_tokens": DEFAULT_MAX_TOKENS,
            "min_tokens": DEFAULT_SEMANTIC_MIN_TOKENS,
            "breakpoint": "cosine_distance_median_mad",
        }
    return {
        "algorithm": "heading:1",
        "offset_unit": "unicode_code_point",
        "configuration_source": "document_versions.parse_meta.ingest_signature",
    }


def _summary_json(summary: StrategyCorpusSummary) -> dict[str, object]:
    payload = asdict(summary)
    payload["build_signatures"] = list(summary.build_signatures)
    return payload


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查四套 chunk corpus 后，以固定配置依次创建四个 E1 eval run"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="human"
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--diagnostic-k", type=int, default=50)
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument(
        "--retrieval-strategy",
        choices=list(RETRIEVAL_STRATEGIES),
        default="dense-lexical-rrf-rerank",
    )
    parser.add_argument(
        "--rerank-candidate-text-mode",
        choices=["title_heading_content", "heading_content", "content"],
        default=None,
    )
    parser.add_argument("--lexical-mode", choices=list(LEXICAL_MODES), default=None)
    parser.add_argument("--no-reuse", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/outputs/chunk-strategies"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        run_chunk_strategy_batch(
            dataset_name=args.dataset,
            label=args.label,
            origin=args.origin,
            top_k=args.top_k,
            diagnostic_k=args.diagnostic_k,
            token_budget=args.token_budget,
            theta=args.theta,
            alpha=args.alpha,
            output_root=args.output_dir,
            retrieval_strategy=args.retrieval_strategy,
            rerank_candidate_text_mode=args.rerank_candidate_text_mode,
            lexical_mode=args.lexical_mode,
            reuse_completed=not args.no_reuse,
        )
    )
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "run_ids": {key: str(value) for key, value in result.run_ids.items()},
                "reused": result.reused,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
