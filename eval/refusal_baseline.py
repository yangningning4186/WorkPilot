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
from app.rag.retrieval.pipeline import SearchPipeline, SearchPipelineRequest
from workpilot_ai.gateway import ModelGateway


@dataclass(frozen=True)
class RefusalItemResult:
    category: str
    question: str
    answerable: bool
    refused: bool
    refusal_reason: str | None
    top_score: float | None
    second_score: float | None
    score_margin: float | None
    score_margin_ratio: float | None
    score_source: str | None
    score_threshold_applied: bool
    low_margin: bool
    evidence_reason: str | None
    invalid_gate_response: bool
    latency_ms: int


async def run_refusal_baseline(
    *,
    dataset_name: str,
    origin: str,
    label: str,
    strategy: str,
    top_k: int,
    output_root: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    if strategy not in {
        "dense-only",
        "dense-lexical-rrf",
        "dense-lexical-rrf-rerank",
    }:
        raise ValueError("拒答基线不支持该检索策略")
    config: dict[str, object] = {
        # dataset 必须进 config_hash: 否则同一套参数跑不同数据集会撞出同一个 hash,
        # 台账里用 config_hash 当 run 句柄就会指错运行。
        "dataset": dataset_name,
        "strategy": strategy,
        "top_k": top_k,
        "origin": origin,
        "refusal_threshold": settings.refusal_threshold,
        "refusal_score_gate_source": settings.refusal_score_gate_source,
        "refusal_margin_threshold": settings.refusal_margin_threshold,
        "evidence_gate_max_chars": settings.evidence_gate_max_chars,
        "rerank_evidence_gate_max_chars": settings.rerank_evidence_gate_max_chars,
        "evidence_gate_max_tokens": settings.evidence_gate_max_tokens,
        "embedding_model": settings.embedding_model,
        "embedding_revision": settings.embedding_revision,
        "chat_model": settings.tier_main_model,
        "rrf_k": settings.rrf_k,
        "lexical_mode": settings.lexical_mode,
        "reranker_base_url": settings.reranker_base_url,
        "reranker_model": settings.reranker_model,
        "rerank_candidate_k_per_arm": settings.rerank_candidate_k,
        "rerank_candidate_mode": "rrf_top_k" if strategy.endswith("rerank") else None,
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    gateway = build_model_gateway(settings)
    try:
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT i.category, i.question
                            FROM eval_items i
                            JOIN eval_datasets d ON d.id=i.dataset_id
                            WHERE d.name=:dataset_name
                              AND (:origin='all' OR i.origin=:origin)
                            ORDER BY i.id
                            """
                        ),
                        {"dataset_name": dataset_name, "origin": origin},
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise ValueError("评测数据集没有匹配样本")
            results: list[RefusalItemResult] = []
            for row in rows:
                result = await _evaluate_item(
                    session,
                    gateway,
                    category=str(row["category"]),
                    question=str(row["question"]),
                    strategy=strategy,
                    top_k=top_k,
                    settings=settings,
                )
                results.append(result)
    finally:
        await gateway.aclose()
        await close_database()

    metrics = _aggregate(results)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{timestamp}-{_slug(label)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, object] = {
        "label": label,
        "dataset": dataset_name,
        "git_sha": _git_sha(),
        "config": config,
        "config_hash": config_hash,
        "metrics": metrics,
        "items": [asdict(item) for item in results],
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = run_dir / "report.md"
    report.write_text(_markdown_report(payload), encoding="utf-8")
    return report


async def _evaluate_item(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    category: str,
    question: str,
    strategy: str,
    top_k: int,
    settings: Settings,
) -> RefusalItemResult:
    started = time.monotonic()
    candidate_k = max(top_k, settings.rerank_candidate_k)
    search = await SearchPipeline(session, gateway).search(
        SearchPipelineRequest(
            query=question,
            top_k=top_k,
            candidate_k=candidate_k,
            retrieval_mode=("dense" if strategy == "dense-only" else "hybrid"),
            lexical_enabled="lexical-rrf" in strategy,
            lexical_mode=settings.lexical_mode,
            rrf_k=settings.rrf_k,
            document_cap_per_version=settings.document_cap_per_version,
            rerank_enabled=strategy.endswith("rerank"),
            reranker_base_url=settings.reranker_base_url,
            reranker_model=settings.reranker_model,
            reranker_timeout_s=settings.reranker_timeout_s,
            rerank_max_candidate_chars=settings.rerank_max_candidate_chars,
            rerank_candidate_text_mode=settings.rerank_candidate_text_mode,
        )
    )
    if (
        strategy.endswith("rerank")
        and search.rerank.candidate_count > top_k
        and not search.rerank.applied
    ):
        raise RuntimeError(search.rerank.reason)
    hits = list(search.hits)
    score_source = retrieval_score_source(
        hits,
        rerank_applied=search.rerank.applied,
        lexical_rrf_applied=search.lexical_applied,
    )
    signals = evaluate_refusal(
        hits,
        threshold=settings.refusal_threshold,
        margin_threshold=settings.refusal_margin_threshold,
        score_source=score_source,
        threshold_enabled=settings.refusal_score_gate_source == score_source,
    )
    refusal_reason = signals.refusal_reason
    evidence_reason: str | None = None
    invalid = False
    if refusal_reason is None:
        evidence = build_evidence_segments(
            hits,
            max_chars=(
                settings.rerank_evidence_gate_max_chars
                if strategy.endswith("rerank")
                else settings.evidence_gate_max_chars
            ),
        )
        if not evidence:
            refusal_reason = "no_evidence"
        else:
            assert signals.top_score is not None
            try:
                assessment = await assess_evidence_sufficiency(
                    gateway,
                    query=question,
                    evidence=evidence,
                    top_score=signals.top_score,
                    second_score=signals.second_score,
                    score_margin=signals.score_margin,
                    low_margin=signals.low_margin,
                    score_source=score_source,
                    score_threshold_applied=signals.threshold_applied,
                    max_tokens=settings.evidence_gate_max_tokens,
                )
                evidence_reason = assessment.reason
                if not assessment.sufficient:
                    refusal_reason = "model_insufficient_evidence"
            except EvidenceAssessmentError as error:
                invalid = True
                evidence_reason = str(error)
                refusal_reason = "evidence_gate_invalid"
    return RefusalItemResult(
        category=category,
        question=question,
        answerable=category != "unanswerable",
        refused=refusal_reason is not None,
        refusal_reason=refusal_reason,
        top_score=signals.top_score,
        second_score=signals.second_score,
        score_margin=signals.score_margin,
        score_margin_ratio=signals.score_margin_ratio,
        score_source=signals.score_source,
        score_threshold_applied=signals.threshold_applied,
        low_margin=signals.low_margin,
        evidence_reason=evidence_reason,
        invalid_gate_response=invalid,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _aggregate(results: list[RefusalItemResult]) -> dict[str, object]:
    true_answerable = sum(item.answerable and not item.refused for item in results)
    false_refusal = sum(item.answerable and item.refused for item in results)
    true_refusal = sum(not item.answerable and item.refused for item in results)
    false_answerable = sum(not item.answerable and not item.refused for item in results)
    answerable_f1 = _f1(true_answerable, false_answerable, false_refusal)
    refusal_f1 = _f1(true_refusal, false_refusal, false_answerable)
    by_category = {
        category: {
            "count": len(items),
            "refused": sum(item.refused for item in items),
            "refusal_rate": sum(item.refused for item in items) / len(items),
        }
        for category in sorted({item.category for item in results})
        if (items := [item for item in results if item.category == category])
    }
    return {
        "item_count": len(results),
        "true_answerable": true_answerable,
        "false_refusal": false_refusal,
        "true_refusal": true_refusal,
        "false_answerable": false_answerable,
        "accuracy": (true_answerable + true_refusal) / len(results),
        "answerable_f1": answerable_f1,
        "refusal_f1": refusal_f1,
        "macro_f1": (answerable_f1 + refusal_f1) / 2,
        "invalid_gate_responses": sum(item.invalid_gate_response for item in results),
        "reasons": dict(Counter(item.refusal_reason or "answered" for item in results)),
        "by_category": by_category,
        "latency_ms_mean": fmean(item.latency_ms for item in results),
    }


def _f1(true_positive: int, false_positive: int, false_negative: int) -> float:
    denominator = 2 * true_positive + false_positive + false_negative
    return 2 * true_positive / denominator if denominator else 0.0


def _markdown_report(payload: dict[str, object]) -> str:
    metrics = payload["metrics"]
    config = payload["config"]
    assert isinstance(metrics, dict)
    assert isinstance(config, dict)
    return "\n".join(
        [
            "# 组合拒答评测",
            "",
            f"- 数据集: `{payload['dataset']}`",
            f"- 标签: `{payload['label']}`",
            f"- 策略: `{config['strategy']}`",
            f"- 模型: `{config['chat_model']}`",
            f"- 样本: {metrics['item_count']}",
            "",
            "| accuracy | macro-F1 | answerable F1 | refusal F1 | 误答 | 误拒 |",
            "|---:|---:|---:|---:|---:|---:|",
            "| {accuracy:.4f} | {macro:.4f} | {answerable:.4f} | {refusal:.4f} | "
            "{false_answerable} | {false_refusal} |".format(
                accuracy=metrics["accuracy"],
                macro=metrics["macro_f1"],
                answerable=metrics["answerable_f1"],
                refusal=metrics["refusal_f1"],
                false_answerable=metrics["false_answerable"],
                false_refusal=metrics["false_refusal"],
            ),
            "",
            f"- 非法门控响应: {metrics['invalid_gate_responses']}",
            f"- 平均延迟: {metrics['latency_ms_mean']:.1f} ms",
            f"- 拒答原因: `{json.dumps(metrics['reasons'], ensure_ascii=False)}`",
            "",
            "> 逐样本问题与门控原因只保存在 Git 忽略的本地报告中。",
        ]
    )


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
        description="运行检索分数 + margin + 证据充分性组合拒答评测"
    )
    parser.add_argument("--dataset", default="core-dev")
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="all"
    )
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--strategy",
        choices=["dense-only", "dense-lexical-rrf", "dense-lexical-rrf-rerank"],
        default="dense-only",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/refusal-baseline")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_refusal_baseline(
            dataset_name=args.dataset,
            origin=args.origin,
            label=args.label,
            strategy=args.strategy,
            top_k=args.top_k,
            output_root=args.output_dir,
        )
    )
    print(report)


if __name__ == "__main__":
    main()
