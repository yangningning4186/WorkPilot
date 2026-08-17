"""P1-J：20 条 Top-5 证据不完整的三路恢复机制实验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from eval.dense_baseline import EvalItem, _load_items, _retrieved_chunk
from eval.mapping import GoldSpan, hits
from eval.p1_document_two_hop_experiment import _rank_documents
from eval.p1_retrieval_diagnostics import (
    _oracle_doc_hits,
    _rerank_batched,
    _rerank_raw,
    _span_rank,
)
from eval.suites import load_suite, validate_suite


@dataclass(frozen=True)
class RecoveryPool:
    dataset: str
    item: EvalItem
    cause: str
    embedding: list[float]
    dense: list[DenseSearchHit]
    lexical: list[DenseSearchHit]
    rrf50: list[DenseSearchHit]


async def run_experiment(
    *,
    suite_path: Path,
    attribution_report: Path,
    reranker_base_url: str,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("P1-J 禁止访问 test suite")
    attribution = json.loads(attribution_report.read_text(encoding="utf-8"))
    wanted = {
        str(case["item_id"]): str(case["cause"])
        for case in attribution["cases"]
        if case["cause"] == "retrieval_miss"
    }
    if len(wanted) != 20:
        raise ValueError(f"P1-J 轴必须恰好为 20 条 retrieval_miss，实际 {len(wanted)}")

    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings)
            try:
                pools = await _build_pools(
                    session,
                    gateway,
                    suite=suite,
                    wanted=wanted,
                    settings=settings,
                )
                pool_sha = _pool_fingerprint(pools)
                pool_in = [pool for pool in pools if _all_spans_reachable(pool.rrf50, pool.item)]
                structural = [
                    pool for pool in pools if not _all_spans_reachable(pool.rrf50, pool.item)
                ]
                coverage_axis = [
                    pool for pool in pools if pool.item.category in {"multi_hop", "global"}
                ]
                if (len(pool_in), len(structural), len(coverage_axis)) != (15, 5, 14):
                    raise ValueError(
                        "P1-J 分组漂移：expected pool_in=15/structural=5/coverage=14, "
                        f"actual={len(pool_in)}/{len(structural)}/{len(coverage_axis)}"
                    )

                async with httpx.AsyncClient(
                    base_url=reranker_base_url.rstrip("/"),
                    timeout=180.0,
                    trust_env=False,
                ) as client:
                    health_response = await client.get("/health")
                    health_response.raise_for_status()
                    health = health_response.json()
                    if int(health.get("max_length", 0)) < 512:
                        raise RuntimeError("P1-J 需要 reranker max_length>=512")
                    step1 = await _run_rerank_replay(
                        client, pools=pool_in, settings=settings
                    )
                    step2 = _run_coverage_oracle(coverage_axis)
                    step3 = await _run_document_scan(
                        session,
                        client,
                        gateway=gateway,
                        pools=structural,
                        settings=settings,
                    )
            finally:
                await gateway.aclose()

        payload: dict[str, object] = {
            "schema_version": "p1-top5-recovery.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "suite": suite.name,
            "attribution_report": str(attribution_report.resolve()),
            "candidate_pool_sha256": pool_sha,
            "config": {
                "dense_k": 50,
                "lexical_k": 50,
                "rrf_candidate_k": 50,
                "rrf_k": settings.rrf_k,
                "final_top_k": 5,
                "reranker_model": settings.reranker_model,
                "candidate_text_mode": settings.rerank_candidate_text_mode,
                "candidate_chars": settings.rerank_max_candidate_chars,
                "reranker_tokens": 512,
                "document_top_m": 3,
            },
            "service_health": health,
            "summary": {
                "step1": _step_summary(step1, key="rerank_complete"),
                "step2": _step_summary(step2, key="oracle_complete"),
                "step3": _step_summary(step3, key="document_ce_complete"),
            },
            "step1_rerank": step1,
            "step2_coverage_oracle": step2,
            "step3_document_scan": step3,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-P1-J-top5-recovery"
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / f"{stem}.md").write_text(_markdown(payload), encoding="utf-8")
        return json_path
    finally:
        await close_database()


async def _build_pools(
    session: Any,
    gateway: ModelGateway,
    *,
    suite: Any,
    wanted: dict[str, str],
    settings: Settings,
) -> list[RecoveryPool]:
    pools: list[RecoveryPool] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in items:
            item_id = str(item.id)
            if item_id not in wanted:
                continue
            embedding = (
                await gateway.embed([item.question], task_type="query_embedding")
            ).embeddings[0]
            dense = await _dense_search_by_vector(
                session,
                gateway,
                embedding=embedding,
                top_k=50,
                strategy="heading",
            )
            lexical = await lexical_search(
                session,
                query=item.question,
                top_k=50,
                mode=settings.lexical_mode,
                strategy="heading",
            )
            rrf50 = reciprocal_rank_fusion(
                [dense, lexical],
                top_k=50,
                rrf_k=settings.rrf_k,
                strategy="heading",
            )
            pools.append(
                RecoveryPool(
                    dataset=dataset.name,
                    item=item,
                    cause=wanted[item_id],
                    embedding=embedding,
                    dense=dense,
                    lexical=lexical,
                    rrf50=rrf50,
                )
            )
    if len(pools) != len(wanted):
        raise ValueError(f"P1-J 缺少样本：expected={len(wanted)}, actual={len(pools)}")
    return pools


async def _run_rerank_replay(
    client: httpx.AsyncClient,
    *,
    pools: list[RecoveryPool],
    settings: Settings,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pool in pools:
        started = time.perf_counter()
        ranked = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=pool.rrf50,
            model=settings.reranker_model,
            char_limit=settings.rerank_max_candidate_chars,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        rows.append(
            {
                "dataset": pool.dataset,
                "item_id": str(pool.item.id),
                "category": pool.item.category,
                "question": pool.item.question,
                "rrf_span_ranks": _span_ranks(pool.rrf50, pool.item.gold_spans),
                "rerank_span_ranks": _span_ranks(ranked, pool.item.gold_spans),
                "rerank_complete": _all_spans_top_k(ranked, pool.item, top_k=5),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    return rows


def _run_coverage_oracle(pools: list[RecoveryPool]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pool in pools:
        selected = _gold_coverage_top_k(pool.rrf50, pool.item.gold_spans, top_k=5)
        rows.append(
            {
                "dataset": pool.dataset,
                "item_id": str(pool.item.id),
                "category": pool.item.category,
                "question": pool.item.question,
                "rrf_span_ranks": _span_ranks(pool.rrf50, pool.item.gold_spans),
                "selected_chunk_ids": [str(hit.chunk_id) for hit in selected],
                "oracle_span_ranks": _span_ranks(selected, pool.item.gold_spans),
                "oracle_complete": _all_spans_top_k(selected, pool.item, top_k=5),
            }
        )
    return rows


async def _run_document_scan(
    session: Any,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    pools: list[RecoveryPool],
    settings: Settings,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pool in pools:
        document_hits = _rank_documents(pool.dense, pool.lexical, rrf_k=settings.rrf_k)
        top_versions = [hit.version_id for hit in document_hits[:3]]
        candidates: list[DenseSearchHit] = []
        seen: set[UUID] = set()
        for version_id in top_versions:
            for hit in await _oracle_doc_hits(
                session,
                gateway=gateway,
                embedding=pool.embedding,
                version_id=version_id,
            ):
                if hit.chunk_id not in seen:
                    seen.add(hit.chunk_id)
                    candidates.append(hit)
        started = time.perf_counter()
        ranked = await _rerank_batched(
            client,
            query=pool.item.question,
            candidates=candidates,
            model=settings.reranker_model,
            char_limit=settings.rerank_max_candidate_chars,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        gold_versions = {span.version_id for span in pool.item.gold_spans}
        rows.append(
            {
                "dataset": pool.dataset,
                "item_id": str(pool.item.id),
                "category": pool.item.category,
                "question": pool.item.question,
                "top_document_version_ids": [str(value) for value in top_versions],
                "gold_documents_in_top3": len(gold_versions.intersection(top_versions)),
                "gold_document_count": len(gold_versions),
                "candidate_count": len(candidates),
                "document_ce_span_ranks": _span_ranks(ranked, pool.item.gold_spans),
                "document_ce_complete": _all_spans_top_k(ranked, pool.item, top_k=5),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    return rows


def _gold_coverage_top_k(
    candidates: list[DenseSearchHit], spans: list[GoldSpan], *, top_k: int
) -> list[DenseSearchHit]:
    uncovered = set(range(len(spans)))
    selected: list[DenseSearchHit] = []
    remaining = list(candidates)
    while uncovered and len(selected) < top_k:
        best = max(
            remaining,
            key=lambda hit: (
                len(uncovered.intersection(_covered_span_indexes(hit, spans))),
                -candidates.index(hit),
            ),
        )
        gain = uncovered.intersection(_covered_span_indexes(best, spans))
        if not gain:
            break
        selected.append(best)
        remaining.remove(best)
        uncovered.difference_update(gain)
    selected_ids = {hit.chunk_id for hit in selected}
    selected.extend(
        hit for hit in candidates if hit.chunk_id not in selected_ids
    )
    return selected[:top_k]


def _covered_span_indexes(hit: DenseSearchHit, spans: list[GoldSpan]) -> set[int]:
    chunk = _retrieved_chunk(hit)
    return {index for index, span in enumerate(spans) if hits(chunk, span, theta=0.5)}


def _span_ranks(ranked: list[DenseSearchHit], spans: list[GoldSpan]) -> list[int | None]:
    return [_span_rank(ranked, span, theta=0.5) for span in spans]


def _all_spans_reachable(ranked: list[DenseSearchHit], item: EvalItem) -> bool:
    return all(rank is not None for rank in _span_ranks(ranked, item.gold_spans))


def _all_spans_top_k(ranked: list[DenseSearchHit], item: EvalItem, *, top_k: int) -> bool:
    ranks = _span_ranks(ranked, item.gold_spans)
    return all(rank is not None and rank <= top_k for rank in ranks)


def _pool_fingerprint(pools: list[RecoveryPool]) -> str:
    payload = [
        {
            "item_id": str(pool.item.id),
            "dense": [str(hit.chunk_id) for hit in pool.dense],
            "lexical": [str(hit.chunk_id) for hit in pool.lexical],
            "rrf50": [str(hit.chunk_id) for hit in pool.rrf50],
        }
        for pool in pools
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _step_summary(rows: list[dict[str, object]], *, key: str) -> dict[str, object]:
    return {
        "item_count": len(rows),
        "complete_count": sum(bool(row[key]) for row in rows),
        "complete_rate": sum(bool(row[key]) for row in rows) / len(rows) if rows else 0.0,
    }


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# P1-J · Top-5 证据恢复机制",
        "",
        "| step | axis | complete | rate |",
        "|---|---:|---:|---:|",
    ]
    for name in ("step1", "step2", "step3"):
        row = summary[name]
        assert isinstance(row, dict)
        lines.append(
            f"| {name} | {row['item_count']} | {row['complete_count']} | "
            f"{float(row['complete_rate']):.1%} |"
        )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-J Top-5 证据恢复机制")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--attribution-report", type=Path, required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_experiment(
            suite_path=args.suite,
            attribution_report=args.attribution_report,
            reranker_base_url=args.reranker_base_url,
            output_dir=args.output_dir,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
