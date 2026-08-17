"""P1-E：生产 1200/512 下 content-only candidate text 单变量对照。"""

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

import httpx

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.services.grounded_answer import evaluate_refusal
from eval.dense_baseline import EvalItem, _candidate_chunks, _load_items, _retrieved_chunk
from eval.mapping import RetrievedChunk
from eval.metrics.diagnostics import percentile
from eval.metrics.refusal import analyze_refusal
from eval.metrics.retrieval import evaluate_retrieval
from eval.p1_retrieval_diagnostics import _rerank_raw
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import load_suite, validate_suite

MODES = ("title_heading_content", "content")
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
    candidates: list[DenseSearchHit]
    ideal: list[RetrievedChunk]


async def run_content_mode_experiment(
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
        raise ValueError("P1-E 禁止访问 test suite")
    suite_sha = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings)
            try:
                pools = await _build_pools(session, gateway, suite=suite, settings=settings)
                if len(pools) != 70:
                    raise ValueError(f"P1-E 冻结轴漂移：预期 70，实际 {len(pools)}")
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
                    if int(health.get("max_length", 0)) < 512:
                        raise RuntimeError("reranker max_length 小于 512")
                    for pool in pools:
                        key = str(pool.item.id)
                        if key in items:
                            continue
                        items[key] = await _evaluate_item(
                            client, pool=pool, settings=settings
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
        payload: dict[str, object] = {
            "schema_version": "p1-content-mode.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "suite_sha256": suite_sha,
            "candidate_pool_sha256": pool_sha,
            "config": {
                "candidate_mode": "rrf_top_50",
                "candidate_chars": 1200,
                "server_tokens": 512,
                "top_k": 10,
                "token_budget": 6000,
                "refusal_threshold": settings.refusal_threshold,
                "refusal_margin_threshold": settings.refusal_margin_threshold,
            },
            "service_health": health,
            "summary": _summary(ordered, settings=settings),
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
        _, dataset_items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in dataset_items:
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
            candidates = reciprocal_rank_fusion(
                [dense, lexical], top_k=50, rrf_k=settings.rrf_k, strategy="heading"
            )
            ideal: list[RetrievedChunk] = []
            if item.answerable:
                ideal = await _candidate_chunks(
                    session,
                    item.gold_spans,
                    embedding_model=gateway.embedding_model,
                    embedding_provider=gateway.embedding_provider,
                    embedding_revision=gateway.embedding_revision,
                    chunk_strategy="heading",
                    token_count_mode="stored",
                )
            pools.append(ItemPool(dataset.name, item, candidates, ideal))
    return pools


async def _evaluate_item(
    client: httpx.AsyncClient, *, pool: ItemPool, settings: Settings
) -> dict[str, object]:
    variants: dict[str, object] = {}
    for mode in MODES:
        started = time.perf_counter()
        ranked = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=pool.candidates,
            model=settings.reranker_model,
            char_limit=1200,
            token_window=512,
            text_mode=mode,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        top_hits = ranked[:10]
        signals = evaluate_refusal(
            top_hits,
            threshold=settings.refusal_threshold,
            margin_threshold=settings.refusal_margin_threshold,
        )
        metrics: dict[str, float] | None = None
        if pool.item.answerable:
            values = evaluate_retrieval(
                pool.item.gold_spans,
                [_retrieved_chunk(hit) for hit in ranked],
                pool.ideal,
                top_k=10,
                token_budget=6000,
                theta=0.5,
                alpha=0.5,
            ).to_dict()
            metrics = {name: float(values[name]) for name in METRIC_NAMES}
        variants[mode] = {
            "metrics": metrics,
            "latency_ms": latency_ms,
            "top_score": signals.top_score,
            "second_score": signals.second_score,
            "score_margin": signals.score_margin,
            "refused": signals.refusal_reason is not None,
            "top_chunk_ids": [str(hit.chunk_id) for hit in top_hits],
        }
    return {
        "item_id": str(pool.item.id),
        "dataset": pool.dataset,
        "category": pool.item.category,
        "answerable": pool.item.answerable,
        "cross_document": (
            pool.item.answerable
            and len({span.version_id for span in pool.item.gold_spans}) > 1
        ),
        "candidate_count": len(pool.candidates),
        "variants": variants,
    }


def _summary(
    items: list[dict[str, object]], *, settings: Settings
) -> dict[str, object]:
    answerable = [item for item in items if bool(item["answerable"])]
    cross_document = [item for item in answerable if bool(item["cross_document"])]
    return {
        "all_answerable": _metric_slice(answerable),
        "cross_document_dev16": _metric_slice(cross_document),
        "by_category": {
            category: _metric_slice(
                [item for item in answerable if item["category"] == category]
            )
            for category in sorted({str(item["category"]) for item in answerable})
        },
        "refusal": _refusal_summary(items, configured_threshold=settings.refusal_threshold),
        "latency_ms": {
            mode: _latency_summary(items, mode=mode) for mode in MODES
        },
        "decision": _decision(answerable, items, settings=settings),
    }


def _metric_slice(items: list[dict[str, object]]) -> dict[str, object]:
    if not items:
        return {"sample_size": 0}
    summaries: dict[str, dict[str, float]] = {}
    for mode in MODES:
        summaries[mode] = {
            name: fmean(_item_metric(item, mode, name) for item in items)
            for name in METRIC_NAMES
        }
    samples = {
        name: MetricSamples(
            baseline=tuple(
                RatioPoint(_item_metric(item, MODES[0], name), 1) for item in items
            ),
            candidate=tuple(
                RatioPoint(_item_metric(item, MODES[1], name), 1) for item in items
            ),
        )
        for name in METRIC_NAMES
    }
    bootstrap = paired_bootstrap(
        samples, higher_is_better={"max_doc_share_at_k": False}
    )
    return {
        "sample_size": len(items),
        "by_mode": summaries,
        "content_vs_default": {
            name: result.to_dict() for name, result in bootstrap.items()
        },
    }


def _refusal_summary(
    items: list[dict[str, object]], *, configured_threshold: float
) -> dict[str, object]:
    analyses: dict[str, object] = {}
    for mode in MODES:
        observations = [
            (_item_top_score(item, mode), bool(item["answerable"])) for item in items
        ]
        analyses[mode] = analyze_refusal(
            observations, configured_threshold=configured_threshold
        ).to_dict()
    flips = [
        str(item["item_id"])
        for item in items
        if _item_refused(item, MODES[0]) != _item_refused(item, MODES[1])
    ]
    return {"by_mode": analyses, "decision_flip_count": len(flips), "flip_item_ids": flips}


def _latency_summary(items: list[dict[str, object]], *, mode: str) -> dict[str, float]:
    values = sorted(_item_latency(item, mode) for item in items)
    p50 = percentile(values, 0.5)
    p95 = percentile(values, 0.95)
    assert p50 is not None and p95 is not None
    return {
        "mean": fmean(values),
        "p50": p50,
        "p95": p95,
        "max": max(values),
    }


def _decision(
    answerable: list[dict[str, object]],
    all_items: list[dict[str, object]],
    *,
    settings: Settings,
) -> dict[str, object]:
    metrics = _metric_slice(answerable)
    comparison = metrics["content_vs_default"]
    assert isinstance(comparison, dict)
    recall = comparison["span_recall_at_k"]
    ndcg = comparison["ndcg_at_k"]
    assert isinstance(recall, dict) and isinstance(ndcg, dict)
    refusal = _refusal_summary(all_items, configured_threshold=settings.refusal_threshold)
    by_mode = refusal["by_mode"]
    assert isinstance(by_mode, dict)
    default_refusal = by_mode[MODES[0]]
    content_refusal = by_mode[MODES[1]]
    assert isinstance(default_refusal, dict) and isinstance(content_refusal, dict)
    default_configured = default_refusal["configured"]
    content_configured = content_refusal["configured"]
    assert isinstance(default_configured, dict) and isinstance(content_configured, dict)
    passes = (
        _optional_number(recall["ci_low"]) > 0
        and _optional_number(ndcg["ci_low"]) >= 0
        and _number(content_configured["macro_f1"])
        >= _number(default_configured["macro_f1"])
    )
    return {
        "passes": passes,
        "reason": (
            "content-only 在完整 answerable dev 上显著提升 recall/nDCG，且配置阈值 refusal macro-F1 不回退"
            if passes
            else "至少一个验收条件未满足，不修改生产默认 text mode"
        ),
    }


def _item_variant(item: dict[str, object], mode: str) -> dict[str, object]:
    variants = item["variants"]
    if not isinstance(variants, dict):
        raise TypeError("item variants 必须是对象")
    value = variants[mode]
    if not isinstance(value, dict):
        raise TypeError("variant 必须是对象")
    return value


def _item_metric(item: dict[str, object], mode: str, name: str) -> float:
    metrics = _item_variant(item, mode)["metrics"]
    if not isinstance(metrics, dict):
        raise TypeError("answerable item metrics 必须是对象")
    return _number(metrics[name])


def _item_top_score(item: dict[str, object], mode: str) -> float:
    return _number(_item_variant(item, mode)["top_score"])


def _item_latency(item: dict[str, object], mode: str) -> float:
    return _number(_item_variant(item, mode)["latency_ms"])


def _item_refused(item: dict[str, object], mode: str) -> bool:
    return bool(_item_variant(item, mode)["refused"])


def _number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return float(value)


def _optional_number(value: object) -> float:
    return float("-inf") if value is None else _number(value)


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


def _load_checkpoint(
    path: Path, *, suite_sha: str, pool_sha: str
) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P1-E checkpoint 必须是对象")
    if payload.get("suite_sha256") != suite_sha or payload.get("candidate_pool_sha256") != pool_sha:
        raise ValueError("P1-E checkpoint 与 suite/candidate pool 不一致")
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
    answerable = summary["all_answerable"]
    cross = summary["cross_document_dev16"]
    refusal = summary["refusal"]
    latency = summary["latency_ms"]
    decision = summary["decision"]
    assert all(isinstance(value, dict) for value in (answerable, cross, refusal, latency, decision))
    lines = [
        f"# P1-E content-only 生产窗口对照 · {payload['label']}",
        "",
        f"- suite: `{payload['suite']}`；RRF Top-50；1200 chars / 512 tokens；Top-10",
        f"- candidate pool SHA256: `{payload['candidate_pool_sha256']}`",
        "",
        "| 切片 | 模式 | goldDocR | spanRec | maxShare | nDCG |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for slice_name, block in (("answerable dev", answerable), ("cross-doc dev16", cross)):
        modes = block["by_mode"]
        assert isinstance(modes, dict)
        for mode in MODES:
            row = modes[mode]
            assert isinstance(row, dict)
            lines.append(
                f"| {slice_name} | `{mode}` | {_number(row['gold_doc_recall_at_k']):.3f} |"
                f" {_number(row['span_recall_at_k']):.3f} |"
                f" {_number(row['max_doc_share_at_k']):.3f} | {_number(row['ndcg_at_k']):.3f} |"
            )
    comparison = answerable["content_vs_default"]
    assert isinstance(comparison, dict)
    lines.extend(["", "## 完整 answerable dev 配对统计", ""])
    for name in METRIC_NAMES:
        row = comparison[name]
        assert isinstance(row, dict)
        lines.append(
            f"- `{name}` Δ={_optional_number(row['delta']):+.3f}，95% CI "
            f"[{_optional_number(row['ci_low']):+.3f}, {_optional_number(row['ci_high']):+.3f}]，"
            f"`{row['verdict']}`"
        )
    by_refusal = refusal["by_mode"]
    assert isinstance(by_refusal, dict)
    lines.extend(["", "## 拒答与延迟", ""])
    for mode in MODES:
        analysis = by_refusal[mode]
        latency_row = latency[mode]
        assert isinstance(analysis, dict) and isinstance(latency_row, dict)
        configured = analysis["configured"]
        assert isinstance(configured, dict)
        lines.append(
            f"- `{mode}`：AUROC={_display(analysis['auroc'])}，configured macro-F1="
            f"{_number(configured['macro_f1']):.3f}，p95={_number(latency_row['p95']):.1f}ms"
        )
    lines.extend(
        [
            f"- refusal 决策翻转：{refusal['decision_flip_count']} 条",
            "",
            "## 决策",
            "",
            f"- `passes={str(decision['passes']).lower()}`：{decision['reason']}",
            "",
        ]
    )
    return "\n".join(lines)


def _display(value: object) -> str:
    return "—" if value is None else f"{_number(value):.3f}"


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    ).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-E content-only 生产窗口单变量对照")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/p1-content-mode")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_content_mode_experiment(
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
