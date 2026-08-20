"""P1-I：只重跑已归因为 gate packing miss 的 evidence gate 预算对照。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

import structlog
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.evidence_sufficiency import assess_evidence_sufficiency
from app.rag.retrieval.citations import build_evidence_segments
from app.telemetry.llm_calls import SqlLlmCallAudit
from eval.evidence_gate_analysis import _load_hits, _span_coverage


async def run_experiment(
    *,
    attribution_report: Path,
    budgets: tuple[int, ...],
    output_dir: Path,
    authorization_note: str,
    settings: Settings | None = None,
) -> Path:
    if not authorization_note.strip():
        raise ValueError("必须记录开发数据发送授权")
    if not budgets or any(budget < 1 for budget in budgets):
        raise ValueError("budgets 必须是正整数")
    if len(set(budgets)) != len(budgets):
        raise ValueError("budgets 不得重复")
    settings = settings or Settings()
    source = json.loads(attribution_report.read_text(encoding="utf-8"))
    cases = [case for case in source["cases"] if case["cause"] == "gate_packing_miss"]
    if len(cases) != 5:
        raise ValueError(f"P1-I gate packing 样本必须恰好为 5 条，实际 {len(cases)}")

    retrieval_path = Path(str(source["retrieval_report"]))
    retrieval = json.loads(retrieval_path.read_text(encoding="utf-8"))
    retrieval_items = {str(item["item_id"]): item for item in retrieval["items"]}
    generation_items = _load_generation_items(source["generation_reports"])
    effective_settings = settings.model_copy(update={"llm_cache_enabled": False})
    rows: list[dict[str, Any]] = []

    async with session_factory() as session:
        gateway = build_model_gateway(
            effective_settings,
            audit_sink=SqlLlmCallAudit(session),
            mode="online",
        )
        try:
            for budget in budgets:
                for case in cases:
                    item_id = str(case["item_id"])
                    generated = generation_items[item_id]
                    retrieved = retrieval_items[item_id]
                    hits = await _load_hits(session, list(retrieved["retrieved"]))
                    evidence = build_evidence_segments(hits, max_chars=budget)
                    spans = list(generated["span_diagnostics"])
                    coverage = [_span_coverage(span, evidence) for span in spans]
                    signals = generated["refusal_signals"]
                    trace_id = f"p1-gate-budget:{budget}:{item_id}:{time.time_ns()}"
                    structlog.contextvars.bind_contextvars(trace_id=trace_id)
                    started = time.monotonic()
                    assessment = await assess_evidence_sufficiency(
                        gateway,
                        query=str(generated["question"]),
                        evidence=evidence,
                        top_score=float(signals["top_score"]),
                        second_score=_optional_float(signals.get("second_score")),
                        score_margin=_optional_float(signals.get("score_margin")),
                        low_margin=bool(signals["low_margin"]),
                        score_source=str(signals["score_source"]),  # type: ignore[arg-type]
                        score_threshold_applied=bool(signals["threshold_applied"]),
                        max_tokens=effective_settings.evidence_gate_max_tokens,
                    )
                    latency_ms = max(0, round((time.monotonic() - started) * 1000))
                    usage = await _load_usage(session, trace_id=trace_id)
                    rows.append(
                        {
                            "budget": budget,
                            "dataset": case["dataset"],
                            "item_id": item_id,
                            "question": generated["question"],
                            "minimum_full_visibility_chars": case[
                                "minimum_full_visibility_chars"
                            ],
                            "evidence_chars": sum(len(item.quote) for item in evidence),
                            "evidence_segments": len(evidence),
                            "gold_span_coverage": coverage,
                            "gold_fully_visible": all(value >= 1.0 for value in coverage),
                            "sufficient": assessment.sufficient,
                            "reason": assessment.reason,
                            "support_ids": assessment.support_ids,
                            "missing_aspects": assessment.missing_aspects,
                            "model": assessment.model,
                            "provider": assessment.provider,
                            "latency_ms": latency_ms,
                            **usage,
                        }
                    )
                    structlog.contextvars.clear_contextvars()
            await session.commit()
        finally:
            await gateway.aclose()
    await close_database()

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "attribution_report": str(attribution_report.resolve()),
        "authorization": {
            "approved": True,
            "note": authorization_note.strip(),
            "endpoint": settings.tier_main_base_url,
            "model": settings.tier_main_model,
            "data_scope": "5 P1-I gate-packing dev cases and reconstructed Top-5 evidence",
        },
        "config": {
            "budgets": list(budgets),
            "packing_mode": "sequential",
            "evidence_gate_max_tokens": settings.evidence_gate_max_tokens,
            "cache_enabled": False,
            "final_top_k": 5,
        },
        "summary": _summarize(rows, budgets=budgets),
        "items": rows,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown(payload), encoding="utf-8")
    return report_path


def _load_generation_items(paths: list[str]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for raw_path in paths:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        for item in payload["items"]:
            item_id = str(item["item_id"])
            if item_id in items:
                raise ValueError(f"generation item 重复: {item_id}")
            items[item_id] = item
    return items


async def _load_usage(session: Any, *, trace_id: str) -> dict[str, int]:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS call_count,
                           coalesce(sum(prompt_tokens), 0) AS input_tokens,
                           coalesce(sum(output_tokens), 0) AS output_tokens,
                           coalesce(sum(latency_ms), 0) AS provider_latency_ms
                    FROM llm_calls
                    WHERE trace_id=:trace_id
                    """
                ),
                {"trace_id": trace_id},
            )
        )
        .mappings()
        .one()
    )
    return {key: int(row[key]) for key in row}


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError("score signal 必须是数值")
    return float(value)


def _summarize(rows: list[dict[str, Any]], *, budgets: tuple[int, ...]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for budget in budgets:
        selected = [row for row in rows if row["budget"] == budget]
        latencies = sorted(int(row["latency_ms"]) for row in selected)
        summary[str(budget)] = {
            "item_count": len(selected),
            "fully_visible": sum(bool(row["gold_fully_visible"]) for row in selected),
            "sufficient": sum(bool(row["sufficient"]) for row in selected),
            "decision_counts": dict(Counter(str(row["sufficient"]) for row in selected)),
            "input_tokens_mean": fmean(int(row["input_tokens"]) for row in selected),
            "output_tokens_mean": fmean(int(row["output_tokens"]) for row in selected),
            "latency_ms_mean": fmean(latencies),
            "latency_ms_max": max(latencies),
        }
    return summary


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# P1-I gate-only 预算对照",
        "",
        "| budget | gold 全可见 | gate sufficient | mean input tokens | mean latency | max latency |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for budget, summary in payload["summary"].items():
        lines.append(
            f"| {budget} | {summary['fully_visible']}/{summary['item_count']} | "
            f"{summary['sufficient']}/{summary['item_count']} | "
            f"{summary['input_tokens_mean']:.1f} | {summary['latency_ms_mean']:.1f} ms | "
            f"{summary['latency_ms_max']} ms |"
        )
    lines += ["", "## 逐条", "", "| budget | sufficient | visible | latency | question |", "|---:|---|---|---:|---|"]
    for item in payload["items"]:
        question = str(item["question"]).replace("|", "\\|")
        lines.append(
            f"| {item['budget']} | {item['sufficient']} | {item['gold_fully_visible']} | "
            f"{item['latency_ms']} ms | {question} |"
        )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-I gate-only 证据预算对照")
    parser.add_argument("--attribution-report", type=Path, required=True)
    parser.add_argument("--budget", type=int, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--authorization-note", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_experiment(
            attribution_report=args.attribution_report,
            budgets=tuple(args.budget),
            output_dir=args.output_dir,
            authorization_note=args.authorization_note,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
