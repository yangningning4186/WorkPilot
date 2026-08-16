"""重建 generation run 的 evidence-gate 输入并归因 answerable 误拒。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import close_database, session_factory
from app.retrieval.citations import EvidenceSegment, build_evidence_segments
from app.retrieval.dense import DenseSearchHit


async def analyze_false_refusals(
    *,
    retrieval_report: Path,
    generation_reports: list[Path],
    output_dir: Path,
    gate_max_chars: int = 6000,
    gate_max_segment_chars: int = 1200,
    answer_max_chars: int = 12000,
    packing_mode: str = "round_robin",
    expected_false_refusals: int | None = 21,
) -> Path:
    if packing_mode not in {"round_robin", "sequential"}:
        raise ValueError("packing_mode 必须是 round_robin/sequential")
    retrieval = json.loads(retrieval_report.read_text(encoding="utf-8"))
    retrieval_items = {str(item["item_id"]): item for item in retrieval["items"]}
    generation_items: list[dict[str, Any]] = []
    for report_path in generation_reports:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        generation_items.extend(
            {**item, "dataset": payload["dataset"]} for item in payload["items"]
        )
    false_refusals = [
        item
        for item in generation_items
        if item.get("answerable") is True and item.get("refused") is True
    ]
    if (
        expected_false_refusals is not None
        and len(false_refusals) != expected_false_refusals
    ):
        raise ValueError(
            f"本轮必须恰好分析 {expected_false_refusals} 条 answerable 误拒，"
            f"实际 {len(false_refusals)}"
        )
    missing = [
        str(item["item_id"])
        for item in false_refusals
        if str(item["item_id"]) not in retrieval_items
    ]
    if missing:
        raise ValueError(f"检索报告缺少 generation 误拒样本: {missing}")

    cases: list[dict[str, Any]] = []
    async with session_factory() as session:
        for generated in false_refusals:
            item_id = str(generated["item_id"])
            retrieved = retrieval_items[item_id]
            hits = await _load_hits(session, list(retrieved["retrieved"]))
            round_robin_gate_evidence = build_evidence_segments(
                hits,
                max_chars=gate_max_chars,
                max_segment_chars=gate_max_segment_chars,
            )
            sequential_gate_evidence = build_evidence_segments(
                hits, max_chars=gate_max_chars
            )
            gate_evidence = (
                sequential_gate_evidence
                if packing_mode == "sequential"
                else round_robin_gate_evidence
            )
            answer_evidence = build_evidence_segments(hits, max_chars=answer_max_chars)
            spans = list(generated["span_diagnostics"])
            retrieval_statuses = {
                int(span["span_index"]): str(span["status"])
                for span in retrieved["span_diagnostics"]
            }
            gate_coverage = [_span_coverage(span, gate_evidence) for span in spans]
            sequential_gate_coverage = [
                _span_coverage(span, sequential_gate_evidence) for span in spans
            ]
            answer_coverage = [_span_coverage(span, answer_evidence) for span in spans]
            cause = _classify(
                refusal_reason=str(generated.get("refusal_reason") or ""),
                retrieval_statuses=retrieval_statuses,
                gate_coverage=gate_coverage,
                answer_coverage=answer_coverage,
            )
            cases.append(
                {
                    "dataset": generated["dataset"],
                    "item_id": item_id,
                    "category": generated["category"],
                    "question": generated["question"],
                    "gold_answer": generated["gold_answer"],
                    "refusal_reason": generated["refusal_reason"],
                    "cause": cause,
                    "retrieval": retrieved["retrieval"],
                    "retrieval_span_statuses": retrieval_statuses,
                    "gate_span_coverage": gate_coverage,
                    "sequential_gate_span_coverage": sequential_gate_coverage,
                    "answer_span_coverage": answer_coverage,
                    "gate_evidence_chars": sum(len(item.quote) for item in gate_evidence),
                    "gate_evidence_segments": len(gate_evidence),
                    "gate_evidence": [_serialize_segment(item) for item in gate_evidence],
                }
            )
    await close_database()

    cause_counts = Counter(str(case["cause"]) for case in cases)
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for case in cases:
        category_counts[str(case["category"])][str(case["cause"])] += 1
    payload = {
        "schema_version": 1,
        "retrieval_report": str(retrieval_report.resolve()),
        "generation_reports": [str(path.resolve()) for path in generation_reports],
        "config": {
            "gate_max_chars": gate_max_chars,
            "gate_max_segment_chars": gate_max_segment_chars,
            "answer_max_chars": answer_max_chars,
            "packing_mode": packing_mode,
            "coverage_rule": "each gold span must be fully visible in reconstructed evidence",
        },
        "false_refusal_count": len(cases),
        "cause_counts": dict(cause_counts),
        "category_cause_counts": {
            category: dict(counts) for category, counts in sorted(category_counts.items())
        },
        "cases": cases,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown_report(payload), encoding="utf-8")
    return report_path


async def _load_hits(
    session: AsyncSession, retrieved: list[dict[str, Any]]
) -> list[DenseSearchHit]:
    chunk_ids = [UUID(str(item["chunk_id"])) for item in retrieved]
    chunk_rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT c.id AS chunk_id, c.version_id, v.document_id, v.version_no,
                           d.title, d.source_uri, c.content, c.content_tokens,
                           c.char_start, c.char_end, c.heading_path, c.strategy
                    FROM chunks c
                    JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=v.document_id
                    WHERE c.id=ANY(:chunk_ids)
                    """
                ),
                {"chunk_ids": chunk_ids},
            )
        )
        .mappings()
        .all()
    )
    block_rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT c.id AS chunk_id, b.id AS block_id, b.block_idx, b.block_type,
                           b.text, b.char_start, b.char_end, b.heading_path
                    FROM chunks c
                    JOIN parsed_blocks b
                      ON b.version_id=c.version_id
                     AND b.block_idx BETWEEN c.block_start_idx AND c.block_end_idx
                    WHERE c.id=ANY(:chunk_ids)
                    ORDER BY c.id, b.block_idx
                    """
                ),
                {"chunk_ids": chunk_ids},
            )
        )
        .mappings()
        .all()
    )
    row_by_id = {str(row["chunk_id"]): row for row in chunk_rows}
    blocks_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in block_rows:
        blocks_by_id[str(row["chunk_id"])].append(
            {
                "block_id": str(row["block_id"]),
                "block_idx": int(row["block_idx"]),
                "block_type": str(row["block_type"]),
                "text": str(row["text"]),
                "char_start": int(row["char_start"]),
                "char_end": int(row["char_end"]),
                "heading_path": list(row["heading_path"] or []),
                "locations": [],
            }
        )
    hits: list[DenseSearchHit] = []
    for item in retrieved:
        chunk_id = str(item["chunk_id"])
        row = row_by_id.get(chunk_id)
        if row is None:
            raise ValueError(f"检索报告中的 chunk 已不存在: {chunk_id}")
        hits.append(
            DenseSearchHit(
                chunk_id=UUID(chunk_id),
                document_id=row["document_id"],
                version_id=row["version_id"],
                version_no=int(row["version_no"]),
                title=str(row["title"]),
                source_uri=str(row["source_uri"]),
                content=str(row["content"]),
                content_tokens=int(row["content_tokens"]),
                char_start=int(row["char_start"]),
                char_end=int(row["char_end"]),
                score=float(item["score"]),
                dense_score=_optional_float(item.get("dense_score")),
                lexical_score=_optional_float(item.get("lexical_score")),
                fusion_score=_optional_float(item.get("fusion_score")),
                rerank_score=_optional_float(item.get("rerank_score")),
                heading_path=list(row["heading_path"] or []),
                blocks=blocks_by_id[chunk_id],
                strategy=str(row["strategy"]),
            )
        )
    return hits


def _classify(
    *,
    refusal_reason: str,
    retrieval_statuses: dict[int, str],
    gate_coverage: list[float],
    answer_coverage: list[float],
) -> str:
    if refusal_reason == "evidence_gate_invalid":
        return "gate_response_invalid"
    if any(status != "hit" for status in retrieval_statuses.values()):
        return "retrieval_miss"
    if any(coverage < 1.0 for coverage in gate_coverage):
        if any(coverage < 1.0 for coverage in answer_coverage):
            return "evidence_budget_miss"
        return "gate_packing_miss"
    return "gate_model_false_negative"


def _span_coverage(span: dict[str, Any], evidence: list[EvidenceSegment]) -> float:
    start, end = int(span["char_start"]), int(span["char_end"])
    version_id = UUID(str(span["version_id"]))
    covered: list[tuple[int, int]] = []
    for segment in evidence:
        if segment.version_id != version_id:
            continue
        overlap_start = max(start, segment.char_start)
        overlap_end = min(end, segment.char_end)
        if overlap_end > overlap_start:
            covered.append((overlap_start, overlap_end))
    if not covered:
        return 0.0
    covered.sort()
    merged: list[tuple[int, int]] = []
    for interval in covered:
        if not merged or interval[0] > merged[-1][1]:
            merged.append(interval)
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], interval[1]))
    return sum(right - left for left, right in merged) / (end - start)


def _serialize_segment(segment: EvidenceSegment) -> dict[str, Any]:
    return {
        "citation_id": segment.citation_id,
        "version_id": str(segment.version_id),
        "block_id": str(segment.block_id),
        "title": segment.title,
        "heading_path": segment.heading_path,
        "char_start": segment.char_start,
        "char_end": segment.char_end,
        "quote": segment.quote,
    }


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Evidence gate 误拒归因",
        "",
        f"- answerable 误拒：{payload['false_refusal_count']}",
        f"- 根因计数：`{json.dumps(payload['cause_counts'], ensure_ascii=False)}`",
        "",
        "| dataset | category | cause | question |",
        "|---|---|---|---|",
    ]
    for case in payload["cases"]:
        question = str(case["question"]).replace("|", "\\|")
        lines.append(
            f"| {case['dataset']} | {case['category']} | {case['cause']} | {question} |"
        )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归因 70-dev generation 的 evidence-gate 误拒")
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--generation-report", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--packing-mode",
        choices=["round_robin", "sequential"],
        default="round_robin",
    )
    parser.add_argument("--expected-false-refusals", type=int, default=21)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        analyze_false_refusals(
            retrieval_report=args.retrieval_report,
            generation_reports=args.generation_report,
            output_dir=args.output_dir,
            packing_mode=args.packing_mode,
            expected_false_refusals=args.expected_false_refusals,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
