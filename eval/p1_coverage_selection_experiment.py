"""P1-K：真实子问题分解 + coverage-aware Top-5 对照。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import build_model_gateway
from app.retrieval.coverage import (
    CoverageSelectionResult,
    coverage_aware_top_k,
    hybrid_rankings_for_queries,
)
from app.retrieval.dense import DenseSearchHit, dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.retrieval.query_decomposition import (
    QueryDecompositionError,
    QueryPlan,
    fallback_query_plan,
    plan_retrieval_queries,
)

from eval.dense_baseline import EvalItem, _load_items
from eval.p1_retrieval_diagnostics import _span_rank
from eval.suites import load_suite, validate_suite


async def run_experiment(
    *,
    suite_path: Path,
    attribution_report: Path,
    output_dir: Path,
    authorization_note: str,
    replay_plans: Path | None = None,
    settings: Settings | None = None,
) -> Path:
    if not authorization_note.strip():
        raise ValueError("必须记录真实查询分解的数据发送授权")
    settings = (settings or Settings()).model_copy(update={"llm_cache_enabled": False})
    suite = load_suite(suite_path)
    attribution = json.loads(attribution_report.read_text(encoding="utf-8"))
    wanted = {
        str(case["item_id"])
        for case in attribution["cases"]
        if case["cause"] == "retrieval_miss"
        and case["category"] in {"multi_hop", "global"}
    }
    if len(wanted) != 14:
        raise ValueError(f"P1-K 轴必须为 14 条，实际 {len(wanted)}")
    frozen_plans = _load_replay_plans(replay_plans) if replay_plans else {}

    rows: list[dict[str, Any]] = []
    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings, mode="online")
            try:
                for dataset in suite.datasets:
                    _, items = await _load_items(session, dataset.name, origin=suite.origin)
                    for item in items:
                        if str(item.id) not in wanted:
                            continue
                        started = time.monotonic()
                        baseline = await _baseline(
                            session, gateway, item=item, settings=settings
                        )
                        plan = frozen_plans.get(str(item.id))
                        if plan is None:
                            try:
                                plan = await plan_retrieval_queries(
                                    gateway,
                                    query=item.question,
                                    max_subqueries=settings.query_decomposition_max_subqueries,
                                    max_tokens=settings.query_decomposition_max_tokens,
                                )
                            except QueryDecompositionError as error:
                                plan = fallback_query_plan(
                                    item.question,
                                    reason=f"查询分解响应无效: {error}",
                                )
                        if plan.decomposed:
                            query_rankings = await hybrid_rankings_for_queries(
                                session,
                                gateway,
                                queries=plan.queries,
                                candidate_k=50,
                                lexical_enabled=True,
                                lexical_mode=settings.lexical_mode,
                                rrf_k=settings.rrf_k,
                                strategy="heading",
                            )
                            selection = coverage_aware_top_k(
                                query_rankings.rankings[0],
                                query_rankings.rankings[1:],
                                top_k=5,
                                rank_cutoff=settings.coverage_rank_cutoff,
                                rrf_k=settings.rrf_k,
                            )
                            selection = replace(
                                selection,
                                lexical_candidate_count=(
                                    query_rankings.lexical_candidate_count
                                ),
                            )
                            requirement_span_ranks = [
                                [
                                    _span_rank(ranking, span, theta=0.5)
                                    for span in item.gold_spans
                                ]
                                for ranking in query_rankings.rankings[1:]
                            ]
                        else:
                            selection = CoverageSelectionResult(
                                hits=baseline[:5],
                                applied=False,
                                requirement_count=0,
                                covered_requirement_count=0,
                                candidate_count=len(baseline),
                                lexical_candidate_count=50,
                                reason="规划器判定无需分解，回退原 RRF Top-5",
                            )
                            requirement_span_ranks = []
                        rows.append(
                            _row(
                                dataset=dataset.name,
                                item=item,
                                baseline=baseline,
                                plan=plan,
                                selection=selection,
                                requirement_span_ranks=requirement_span_ranks,
                                latency_ms=round((time.monotonic() - started) * 1000),
                            )
                        )
            finally:
                await gateway.aclose()
        if len(rows) != 14:
            raise ValueError(f"P1-K 实际完成 {len(rows)} 条，不等于 14")

        payload = {
            "schema_version": "p1-coverage-selection.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "suite": suite.name,
            "attribution_report": str(attribution_report.resolve()),
            "authorization_note": authorization_note.strip(),
            "config": {
                "candidate_k": 50,
                "final_top_k": 5,
                "coverage_rank_cutoff": settings.coverage_rank_cutoff,
                "max_subqueries": settings.query_decomposition_max_subqueries,
                "lexical_mode": settings.lexical_mode,
                "rrf_k": settings.rrf_k,
                "planner_model": settings.tier_main_model,
                "cache_enabled": False,
                "replay_plans": str(replay_plans.resolve()) if replay_plans else None,
            },
            "summary": _summary(rows),
            "items": rows,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-P1-K-real-coverage-top5"
        report = output_dir / f"{stem}.json"
        report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / f"{stem}.md").write_text(_markdown(payload), encoding="utf-8")
        return report
    finally:
        await close_database()


async def _baseline(
    session: Any,
    gateway: Any,
    *,
    item: EvalItem,
    settings: Settings,
) -> list[DenseSearchHit]:
    dense = await dense_search(
        session, gateway, query=item.question, top_k=50, strategy="heading"
    )
    lexical = await lexical_search(
        session,
        query=item.question,
        top_k=50,
        mode=settings.lexical_mode,
        strategy="heading",
    )
    return reciprocal_rank_fusion(
        [dense, lexical], top_k=50, rrf_k=settings.rrf_k, strategy="heading"
    )


def _row(
    *,
    dataset: str,
    item: EvalItem,
    baseline: list[DenseSearchHit],
    plan: QueryPlan,
    selection: CoverageSelectionResult,
    requirement_span_ranks: list[list[int | None]],
    latency_ms: int,
) -> dict[str, object]:
    baseline_ranks = [_span_rank(baseline, span, theta=0.5) for span in item.gold_spans]
    candidate_ranks = [
        _span_rank(selection.hits, span, theta=0.5) for span in item.gold_spans
    ]
    return {
        "dataset": dataset,
        "item_id": str(item.id),
        "category": item.category,
        "question": item.question,
        "decomposed": plan.decomposed,
        "plan_reason": plan.reason,
        "queries": plan.queries,
        "planner_model": plan.model,
        "planner_provider": plan.provider,
        "coverage_applied": selection.applied,
        "requirement_count": selection.requirement_count,
        "covered_requirement_count": selection.covered_requirement_count,
        "candidate_count": selection.candidate_count,
        "selection_reason": selection.reason,
        "baseline_span_ranks": baseline_ranks,
        "candidate_span_ranks": candidate_ranks,
        "requirement_span_ranks": requirement_span_ranks,
        "baseline_complete": _complete(baseline_ranks, top_k=5),
        "candidate_complete": _complete(candidate_ranks, top_k=5),
        "latency_ms": latency_ms,
    }


def _complete(ranks: list[int | None], *, top_k: int) -> bool:
    return all(rank is not None and rank <= top_k for rank in ranks)


def _load_replay_plans(path: Path) -> dict[str, QueryPlan]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item["item_id"]): QueryPlan(
            queries=[str(query) for query in item["queries"]],
            decomposed=bool(item["decomposed"]),
            reason=str(item["plan_reason"]),
            model=str(item["planner_model"]) if item.get("planner_model") else None,
            provider=(
                str(item["planner_provider"]) if item.get("planner_provider") else None
            ),
        )
        for item in payload["items"]
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, object]:
    baseline = sum(bool(row["baseline_complete"]) for row in rows)
    candidate = sum(bool(row["candidate_complete"]) for row in rows)
    return {
        "item_count": len(rows),
        "decomposed_count": sum(bool(row["decomposed"]) for row in rows),
        "coverage_applied_count": sum(bool(row["coverage_applied"]) for row in rows),
        "baseline_complete_count": baseline,
        "candidate_complete_count": candidate,
        "rescued_count": sum(
            not bool(row["baseline_complete"]) and bool(row["candidate_complete"])
            for row in rows
        ),
        "regressed_count": sum(
            bool(row["baseline_complete"]) and not bool(row["candidate_complete"])
            for row in rows
        ),
        "latency_ms_mean": fmean(int(row["latency_ms"]) for row in rows),
    }


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# P1-K · 真实子问题 coverage-aware Top-5",
        "",
        f"- 规划器实际分解：{summary['decomposed_count']}/{summary['item_count']}",
        f"- baseline 完整：{summary['baseline_complete_count']}/{summary['item_count']}",
        f"- candidate 完整：{summary['candidate_complete_count']}/{summary['item_count']}",
        f"- 救回：{summary['rescued_count']}；回退：{summary['regressed_count']}",
        f"- mean latency：{summary['latency_ms_mean']:.1f} ms",
        "",
        "| category | decomposed | baseline | candidate | question |",
        "|---|---|---|---|---|",
    ]
    for row in payload["items"]:
        question = str(row["question"]).replace("|", "\\|")
        lines.append(
            f"| {row['category']} | {row['decomposed']} | {row['baseline_complete']} | "
            f"{row['candidate_complete']} | {question} |"
        )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-K 真实 coverage selector")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--attribution-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--authorization-note", required=True)
    parser.add_argument("--replay-plans", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_experiment(
            suite_path=args.suite,
            attribution_report=args.attribution_report,
            output_dir=args.output_dir,
            authorization_note=args.authorization_note,
            replay_plans=args.replay_plans,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
