"""P1-F：非 oracle 文档级二跳网格实验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.rag.retrieval.fusion import reciprocal_rank_fusion, rerank_candidate_union
from app.rag.retrieval.lexical import lexical_search
from eval.dense_baseline import EvalItem, _candidate_chunks, _load_items, _retrieved_chunk
from eval.mapping import RetrievedChunk
from eval.metrics.diagnostics import percentile
from eval.metrics.retrieval import evaluate_retrieval
from eval.p1_retrieval_diagnostics import (
    _oracle_doc_hits,
    _rerank_raw,
    _span_rank,
)
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import load_suite, validate_suite
from workpilot_ai.gateway import ModelGateway

GRIDS = ((3, 10), (3, 25), (5, 10), (5, 20), (10, 10))
METRIC_NAMES = (
    "span_recall_at_k",
    "gold_doc_recall_at_k",
    "ndcg_at_k",
    "max_doc_share_at_k",
)


@dataclass(frozen=True)
class ItemPool:
    dataset: str
    item: EvalItem
    embedding: list[float]
    dense: list[DenseSearchHit]
    lexical: list[DenseSearchHit]
    baseline_candidates: list[DenseSearchHit]
    union: list[DenseSearchHit]
    ideal: list[RetrievedChunk]


async def run_two_hop_experiment(
    *,
    suite_path: Path,
    label: str,
    reranker_base_url: str,
    output_dir: Path,
    timeout_s: float = 120.0,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("P1-F 禁止访问 test suite")
    suite_sha = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings)
            try:
                pools = await _build_pools(session, gateway, suite=suite, settings=settings)
                if len(pools) != 16 or sum(len(pool.item.gold_spans) for pool in pools) != 33:
                    raise ValueError("P1-F 冻结轴漂移：必须为 dev16/33")
                pool_sha = _pool_fingerprint(pools)
                checkpoint_path = output_dir / f"{_slug(label)}.checkpoint.json"
                checkpoint = _load_checkpoint(
                    checkpoint_path, suite_sha=suite_sha, pool_sha=pool_sha
                )
                completed = checkpoint.get("items")
                items: dict[str, dict[str, object]] = (
                    completed if isinstance(completed, dict) else {}
                )
                async with httpx.AsyncClient(
                    base_url=reranker_base_url.rstrip("/"),
                    timeout=timeout_s,
                    trust_env=False,
                ) as client:
                    health_response = await client.get("/health")
                    health_response.raise_for_status()
                    health = health_response.json()
                    for pool in pools:
                        key = str(pool.item.id)
                        if key in items:
                            continue
                        items[key] = await _evaluate_item(
                            session,
                            client,
                            gateway=gateway,
                            pool=pool,
                            settings=settings,
                        )
                        _write_checkpoint(
                            checkpoint_path,
                            {"items": items},
                            suite_sha=suite_sha,
                            pool_sha=pool_sha,
                        )
            finally:
                await gateway.aclose()

        ordered = [items[str(pool.item.id)] for pool in pools]
        summary = _summary(ordered)
        payload: dict[str, object] = {
            "schema_version": "p1-document-two-hop.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "suite_sha256": suite_sha,
            "candidate_pool_sha256": pool_sha,
            "config": {
                "baseline": "RRF Top-50 -> rerank",
                "candidate_text_mode": settings.rerank_candidate_text_mode,
                "candidate_chars": 1200,
                "server_tokens": 512,
                "top_k": 10,
                "grids": [{"document_top_m": m, "local_dense_top_n": n} for m, n in GRIDS],
            },
            "service_health": health,
            "summary": summary,
            "items": ordered,
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
    session: Any,
    gateway: ModelGateway,
    *,
    suite: Any,
    settings: Settings,
) -> list[ItemPool]:
    pools: list[ItemPool] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in items:
            if not item.answerable or len({span.version_id for span in item.gold_spans}) <= 1:
                continue
            embedding = (
                await gateway.embed([item.question], task_type="query_embedding")
            ).embeddings[0]
            dense = await _dense_search_by_vector(
                session, gateway, embedding=embedding, top_k=50, strategy="heading"
            )
            lexical = await lexical_search(
                session,
                query=item.question,
                top_k=50,
                mode=settings.lexical_mode,
                strategy="heading",
            )
            baseline = reciprocal_rank_fusion(
                [dense, lexical], top_k=50, rrf_k=settings.rrf_k, strategy="heading"
            )
            union = rerank_candidate_union(
                dense, lexical, rrf_k=settings.rrf_k, strategy="heading"
            )
            ideal = await _candidate_chunks(
                session,
                item.gold_spans,
                embedding_model=gateway.embedding_model,
                embedding_provider=gateway.embedding_provider,
                embedding_revision=gateway.embedding_revision,
                chunk_strategy="heading",
                token_count_mode="stored",
            )
            pools.append(
                ItemPool(dataset.name, item, embedding, dense, lexical, baseline, union, ideal)
            )
    return pools


async def _evaluate_item(
    session: Any,
    client: httpx.AsyncClient,
    *,
    gateway: ModelGateway,
    pool: ItemPool,
    settings: Settings,
) -> dict[str, object]:
    variants: dict[str, object] = {}
    baseline_started = time.perf_counter()
    baseline = await _rerank_raw(
        client,
        query=pool.item.question,
        candidates=pool.baseline_candidates,
        model=settings.reranker_model,
        char_limit=1200,
        token_window=512,
        text_mode=settings.rerank_candidate_text_mode,
    )
    variants["baseline"] = _variant_result(
        pool, baseline, latency_ms=(time.perf_counter() - baseline_started) * 1000
    )

    document_hits = _rank_documents(pool.dense, pool.lexical, rrf_k=settings.rrf_k)
    top_versions = [hit.version_id for hit in document_hits[:10]]
    local_by_version: dict[UUID, list[DenseSearchHit]] = {}
    for version_id in top_versions:
        local_by_version[version_id] = await _oracle_doc_hits(
            session,
            gateway=gateway,
            embedding=pool.embedding,
            version_id=version_id,
        )

    for document_top_m, local_top_n in GRIDS:
        name = _grid_name(document_top_m, local_top_n)
        candidates = _grid_candidates(
            top_versions,
            local_by_version,
            document_top_m=document_top_m,
            local_top_n=local_top_n,
        )
        if len(candidates) > 100:
            raise ValueError(f"P1-F {name} 候选超过 100")
        started = time.perf_counter()
        ranked = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=candidates,
            model=settings.reranker_model,
            char_limit=1200,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        variants[name] = _variant_result(
            pool, ranked, latency_ms=(time.perf_counter() - started) * 1000
        )

    target_spans = [
        index
        for index, span in enumerate(pool.item.gold_spans)
        if _span_rank(pool.union, span, theta=0.5) is None
    ]
    return {
        "item_id": str(pool.item.id),
        "dataset": pool.dataset,
        "target_pool_outside_span_indexes": target_spans,
        "document_ranking": [
            {"version_id": str(hit.version_id), "title": hit.title}
            for hit in document_hits[:10]
        ],
        "variants": variants,
    }


def _rank_documents(
    dense: list[DenseSearchHit], lexical: list[DenseSearchHit], *, rrf_k: int
) -> list[DenseSearchHit]:
    scored: dict[UUID, tuple[DenseSearchHit, float]] = {}
    for ranking in (dense, lexical):
        seen: set[UUID] = set()
        for rank, hit in enumerate(ranking, start=1):
            if hit.version_id in seen:
                continue
            seen.add(hit.version_id)
            current = scored.get(hit.version_id)
            score = 1 / (rrf_k + rank)
            scored[hit.version_id] = (
                hit if current is None else current[0],
                score if current is None else current[1] + score,
            )
    return [
        hit
        for hit, _ in sorted(
            scored.values(), key=lambda row: (-row[1], str(row[0].version_id))
        )
    ]


def _grid_candidates(
    top_versions: list[UUID],
    local_by_version: dict[UUID, list[DenseSearchHit]],
    *,
    document_top_m: int,
    local_top_n: int,
) -> list[DenseSearchHit]:
    candidates: list[DenseSearchHit] = []
    seen: set[UUID] = set()
    for version_id in top_versions[:document_top_m]:
        for hit in local_by_version[version_id][:local_top_n]:
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            candidates.append(hit)
    return candidates


def _variant_result(
    pool: ItemPool, ranked: list[DenseSearchHit], *, latency_ms: float
) -> dict[str, object]:
    metrics = evaluate_retrieval(
        pool.item.gold_spans,
        [_retrieved_chunk(hit) for hit in ranked],
        pool.ideal,
        top_k=10,
        token_budget=6000,
        theta=0.5,
        alpha=0.5,
    ).to_dict()
    return {
        "metrics": {name: float(metrics[name]) for name in METRIC_NAMES},
        "latency_ms": latency_ms,
        "candidate_count": len(ranked),
        "span_ranks": [
            _span_rank(ranked, span, theta=0.5) for span in pool.item.gold_spans
        ],
    }


def _summary(items: list[dict[str, object]]) -> dict[str, object]:
    names = ["baseline", *(_grid_name(m, n) for m, n in GRIDS)]
    by_variant = {
        name: {
            "metrics": {
                metric: fmean(_metric(item, name, metric) for item in items)
                for metric in METRIC_NAMES
            },
            "latency_ms": _latency_summary(items, name),
            "candidate_count_mean": fmean(_candidate_count(item, name) for item in items),
            "pool_outside_rescued": _rescued_target_count(items, name),
        }
        for name in names
    }
    comparisons = {
        name: _bootstrap(items, baseline="baseline", candidate=name)
        for name in names
        if name != "baseline"
    }
    return {
        "by_variant": by_variant,
        "vs_baseline": comparisons,
        "target_pool_outside_span_count": sum(
            len(_target_indexes(item)) for item in items
        ),
        "diagnostic_best": max(
            names,
            key=lambda name: (
                fmean(_metric(item, name, "span_recall_at_k") for item in items),
                fmean(_metric(item, name, "ndcg_at_k") for item in items),
            ),
        ),
        "warning": "网格在同一 dev16 上选择，仅作机制诊断，不得直接上线",
    }


def _bootstrap(
    items: list[dict[str, object]], *, baseline: str, candidate: str
) -> dict[str, object]:
    samples = {
        metric: MetricSamples(
            tuple(RatioPoint(_metric(item, baseline, metric), 1) for item in items),
            tuple(RatioPoint(_metric(item, candidate, metric), 1) for item in items),
        )
        for metric in METRIC_NAMES
    }
    return {
        name: result.to_dict()
        for name, result in paired_bootstrap(
            samples, higher_is_better={"max_doc_share_at_k": False}
        ).items()
    }


def _rescued_target_count(items: list[dict[str, object]], variant: str) -> int:
    rescued = 0
    for item in items:
        ranks = _span_ranks(item, variant)
        for index in _target_indexes(item):
            rank = ranks[index]
            if isinstance(rank, int) and rank <= 10:
                rescued += 1
    return rescued


def _item_variant(item: dict[str, object], name: str) -> dict[str, object]:
    variants = item["variants"]
    if not isinstance(variants, dict):
        raise TypeError("variants 结构非法")
    value = variants[name]
    if not isinstance(value, dict):
        raise TypeError("variant 结构非法")
    return value


def _metric(item: dict[str, object], variant: str, metric: str) -> float:
    metrics = _item_variant(item, variant)["metrics"]
    if not isinstance(metrics, dict):
        raise TypeError("metrics 结构非法")
    return _number(metrics[metric])


def _span_ranks(item: dict[str, object], variant: str) -> list[object]:
    ranks = _item_variant(item, variant)["span_ranks"]
    if not isinstance(ranks, list):
        raise TypeError("span_ranks 结构非法")
    return ranks


def _target_indexes(item: dict[str, object]) -> list[int]:
    values = item["target_pool_outside_span_indexes"]
    if not isinstance(values, list) or not all(isinstance(value, int) for value in values):
        raise TypeError("target indexes 结构非法")
    return values


def _candidate_count(item: dict[str, object], variant: str) -> float:
    return _number(_item_variant(item, variant)["candidate_count"])


def _latency_summary(items: list[dict[str, object]], variant: str) -> dict[str, float]:
    values = sorted(_number(_item_variant(item, variant)["latency_ms"]) for item in items)
    p50 = percentile(values, 0.5)
    p95 = percentile(values, 0.95)
    assert p50 is not None and p95 is not None
    return {"mean": fmean(values), "p50": p50, "p95": p95, "max": max(values)}


def _grid_name(document_top_m: int, local_top_n: int) -> str:
    return f"doc_m{document_top_m}_local_n{local_top_n}"


def _number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return float(value)


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
        raise ValueError("P1-F checkpoint 必须是对象")
    if payload.get("suite_sha256") != suite_sha or payload.get("candidate_pool_sha256") != pool_sha:
        raise ValueError("P1-F checkpoint 与 suite/candidate pool 不一致")
    return payload


def _write_checkpoint(
    path: Path,
    payload: dict[str, object],
    *,
    suite_sha: str,
    pool_sha: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **payload,
                "suite_sha256": suite_sha,
                "candidate_pool_sha256": pool_sha,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _render_markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    variants = summary["by_variant"]
    comparisons = summary["vs_baseline"]
    assert isinstance(variants, dict) and isinstance(comparisons, dict)
    lines = [
        f"# P1-F 非 oracle 文档二跳 · {payload['label']}",
        "",
        f"- suite: `{payload['suite']}`；dev16/33；生产 candidate text；1200/512；Top-10",
        f"- candidate pool SHA256: `{payload['candidate_pool_sha256']}`",
        "",
        "| 方案 | candidates | goldDocR | spanRec | maxShare | nDCG | 池外救回 | p95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    names = ["baseline", *(_grid_name(m, n) for m, n in GRIDS)]
    for name in names:
        row = variants[name]
        assert isinstance(row, dict)
        metrics = row["metrics"]
        latency = row["latency_ms"]
        assert isinstance(metrics, dict) and isinstance(latency, dict)
        lines.append(
            f"| `{name}` | {_number(row['candidate_count_mean']):.1f} |"
            f" {_number(metrics['gold_doc_recall_at_k']):.3f} |"
            f" {_number(metrics['span_recall_at_k']):.3f} |"
            f" {_number(metrics['max_doc_share_at_k']):.3f} |"
            f" {_number(metrics['ndcg_at_k']):.3f} | {row['pool_outside_rescued']} |"
            f" {_number(latency['p95']):.1f}ms |"
        )
    lines.extend(["", "## 相对 baseline 的 span recall", ""])
    for name, comparison in comparisons.items():
        assert isinstance(comparison, dict)
        recall = comparison["span_recall_at_k"]
        assert isinstance(recall, dict)
        lines.append(
            f"- `{name}` Δ={_number(recall['delta']):+.3f}，95% CI "
            f"[{_number(recall['ci_low']):+.3f}, {_number(recall['ci_high']):+.3f}]，"
            f"`{recall['verdict']}`"
        )
    lines.extend(
        [
            "",
            f"- diagnostic best: `{summary['diagnostic_best']}`",
            f"- {summary['warning']}",
            "",
        ]
    )
    return "\n".join(lines)


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    ).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-F 非 oracle 文档二跳网格")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/p1-document-two-hop")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_two_hop_experiment(
            suite_path=args.suite,
            label=args.label,
            reranker_base_url=args.reranker_base_url,
            output_dir=args.output_dir,
            timeout_s=args.timeout_s,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
