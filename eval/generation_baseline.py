import argparse
import asyncio
import csv
import hashlib
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.retrieval.citations import CitationValidationError
from app.services.grounded_answer import GroundedAnswerResult, answer_with_citations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from eval.mapping import GoldSpan
from eval.metrics.generation import (
    CitationSource,
    CitationValidityResult,
    RuleResult,
    evaluate_citation_validity,
    evaluate_constraints,
)


@dataclass(frozen=True)
class GenerationItem:
    id: UUID
    category: str
    question: str
    gold_answer: str
    gold_spans: list[GoldSpan]
    constraints: dict[str, Any]

    @property
    def answerable(self) -> bool:
        return self.category != "unanswerable"


@dataclass(frozen=True)
class GenerationItemResult:
    item: GenerationItem
    answer: str | None
    citations: list[dict[str, object]]
    refused: bool | None
    refusal_reason: str | None
    refusal_correct: bool
    citation_validity: CitationValidityResult
    constraint_result: RuleResult
    aligned_citations: int
    latency_ms: int
    model: str | None
    provider: str | None
    error: str | None = None

    @property
    def citation_count(self) -> int:
        return len(self.citations)


async def run_generation_baseline(
    *,
    dataset_name: str,
    label: str,
    origin: str,
    top_k: int,
    theta: float,
    output_root: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 0 到 1")
    config: dict[str, object] = {
        "dataset": dataset_name,
        "origin": origin,
        "strategy": "dense-only-generation",
        "top_k": top_k,
        "theta": theta,
        "refusal_threshold": settings.refusal_threshold,
        "refusal_margin_threshold": settings.refusal_margin_threshold,
        "query_decomposition_enabled": False,
        "rerank_enabled": False,
        "lexical_rrf_enabled": False,
        "embedding_model": settings.embedding_model,
        "embedding_provider_base_url": settings.embedding_base_url,
        "embedding_revision": settings.embedding_revision,
        "chat_model": settings.tier_main_model,
        "chat_provider_base_url": settings.tier_main_base_url,
        "evidence_gate_max_chars": settings.evidence_gate_max_chars,
        "answer_max_evidence_chars": settings.answer_max_evidence_chars,
        "answer_max_tokens": settings.answer_max_tokens,
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
        results: list[GenerationItemResult] = []
        try:
            for item in items:
                result = await _evaluate_item(
                    session,
                    gateway,
                    item=item,
                    top_k=top_k,
                    theta=theta,
                    settings=settings,
                )
                await _store_result(session, run_id, result)
                results.append(result)
            metrics = _aggregate(results)
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
        "items": [_json_item(result) for result in results],
    }
    (run_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = run_dir / "report.md"
    report_path.write_text(_markdown_report(payload), encoding="utf-8")
    _write_review_csv(run_dir / "citation-review.csv", results)
    return report_path


async def _load_items(
    session: AsyncSession, dataset_name: str, *, origin: str
) -> tuple[UUID, list[GenerationItem]]:
    dataset_id = (
        await session.execute(
            text("SELECT id FROM eval_datasets WHERE name=:name"),
            {"name": dataset_name},
        )
    ).scalar_one_or_none()
    if dataset_id is None:
        raise ValueError(f"评测数据集不存在: {dataset_name}")
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, category, question, gold_answer, gold_spans, constraints,
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
    if not rows:
        raise ValueError(f"数据集 {dataset_name} 没有 origin={origin} 的样本")
    items: list[GenerationItem] = []
    for row in rows:
        if not row["spans_valid"]:
            raise ValueError(f"样本包含 stale gold span: {row['id']}")
        spans = [
            GoldSpan(
                version_id=UUID(span["version_id"]),
                char_start=int(span["char_start"]),
                char_end=int(span["char_end"]),
                quote=str(span["quote"]),
            )
            for span in row["gold_spans"]
        ]
        if row["category"] != "unanswerable" and not spans:
            raise ValueError(f"可答样本缺少 gold span: {row['id']}")
        items.append(
            GenerationItem(
                id=row["id"],
                category=str(row["category"]),
                question=str(row["question"]),
                gold_answer=str(row["gold_answer"] or ""),
                gold_spans=spans,
                constraints=dict(row["constraints"] or {}),
            )
        )
    return dataset_id, items


async def _evaluate_item(
    session: AsyncSession,
    gateway: Any,
    *,
    item: GenerationItem,
    top_k: int,
    theta: float,
    settings: Settings,
) -> GenerationItemResult:
    started = time.monotonic()
    try:
        generated: GroundedAnswerResult = await answer_with_citations(
            session,
            gateway,
            query=item.question,
            top_k=top_k,
            refusal_threshold=settings.refusal_threshold,
            refusal_margin_threshold=settings.refusal_margin_threshold,
            evidence_gate_max_chars=settings.evidence_gate_max_chars,
            rerank_evidence_gate_max_chars=settings.rerank_evidence_gate_max_chars,
            evidence_gate_max_segment_chars=settings.evidence_gate_max_segment_chars,
            evidence_gate_max_tokens=settings.evidence_gate_max_tokens,
            query_decomposition_enabled=False,
            rerank_enabled=False,
            lexical_rrf_enabled=False,
            max_evidence_chars=settings.answer_max_evidence_chars,
            max_tokens=settings.answer_max_tokens,
        )
    except CitationValidationError as error:
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        invalid = CitationValidityResult(
            valid=False,
            citation_count=0,
            format_valid=False,
            references_match=False,
            objects_exist=True,
            quotes_match=True,
            issues=(f"generation_validation_error:{error}",),
        )
        return GenerationItemResult(
            item=item,
            answer=None,
            citations=[],
            refused=None,
            refusal_reason=None,
            refusal_correct=False,
            citation_validity=invalid,
            constraint_result=RuleResult(False, ("answer_unavailable",)),
            aligned_citations=0,
            latency_ms=latency_ms,
            model=None,
            provider=None,
            error=f"{type(error).__name__}: {error}",
        )

    sources = await _load_citation_sources(session, generated)
    citation_validity = evaluate_citation_validity(
        answer=generated.answer,
        citations=generated.citations,
        sources=sources,
        refused=generated.refused,
    )
    constraint_result = evaluate_constraints(generated.answer, item.constraints)
    aligned_citations = sum(
        _citation_aligned(citation, item.gold_spans, theta=theta)
        for citation in generated.citations
    )
    return GenerationItemResult(
        item=item,
        answer=generated.answer,
        citations=[_serialize_citation(citation) for citation in generated.citations],
        refused=generated.refused,
        refusal_reason=generated.refusal_reason,
        refusal_correct=generated.refused == (not item.answerable),
        citation_validity=citation_validity,
        constraint_result=constraint_result,
        aligned_citations=aligned_citations,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        model=generated.model,
        provider=generated.provider,
    )


async def _load_citation_sources(
    session: AsyncSession, generated: GroundedAnswerResult
) -> dict[UUID, CitationSource]:
    block_ids = [citation.block_id for citation in generated.citations]
    if not block_ids:
        return {}
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT pb.id AS block_id, pb.version_id, dv.document_id,
                           pb.char_start, pb.char_end, dv.full_text
                    FROM parsed_blocks pb
                    JOIN document_versions dv ON dv.id=pb.version_id
                    WHERE pb.id=ANY(:block_ids)
                    """
                ),
                {"block_ids": block_ids},
            )
        )
        .mappings()
        .all()
    )
    return {
        row["block_id"]: CitationSource(
            block_id=row["block_id"],
            version_id=row["version_id"],
            document_id=row["document_id"],
            block_char_start=int(row["char_start"]),
            block_char_end=int(row["char_end"]),
            full_text=str(row["full_text"]),
        )
        for row in rows
    }


def _citation_aligned(citation: Any, spans: list[GoldSpan], *, theta: float) -> bool:
    for span in spans:
        if citation.version_id != span.version_id:
            continue
        overlap = max(
            0,
            min(citation.char_end, span.char_end)
            - max(citation.char_start, span.char_start),
        )
        if overlap / (span.char_end - span.char_start) >= theta:
            return True
    return False


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
        ],
        "chat": [
            {
                "provider": "openai_compatible",
                "model": settings.tier_main_model,
                "revision": "unversioned",
            }
        ],
    }
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
    await session.commit()
    return run_id


async def _store_result(
    session: AsyncSession, run_id: UUID, result: GenerationItemResult
) -> None:
    scores = {
        "answerable": result.item.answerable,
        "refusal_correct": result.refusal_correct,
        "citation_validity": result.citation_validity.to_dict(),
        "constraint_pass": result.constraint_result.to_dict(),
        "citation_gold_alignment": {
            "aligned": result.aligned_citations,
            "total": result.citation_count,
        },
        "error": result.error,
    }
    await session.execute(
        text(
            """
            INSERT INTO eval_results
                (id, run_id, item_id, answer, retrieved, scores, latency_ms)
            VALUES
                (:id, :run_id, :item_id, :answer, CAST(:retrieved AS jsonb),
                 CAST(:scores AS jsonb), :latency_ms)
            """
        ),
        {
            "id": uuid7(),
            "run_id": run_id,
            "item_id": result.item.id,
            "answer": result.answer,
            "retrieved": json.dumps(result.citations, ensure_ascii=False),
            "scores": json.dumps(scores, ensure_ascii=False),
            "latency_ms": result.latency_ms,
        },
    )
    await session.commit()


async def _finish_run(
    session: AsyncSession, run_id: UUID, metrics: dict[str, object]
) -> None:
    await session.execute(
        text(
            """
            UPDATE eval_runs
            SET metrics=CAST(:metrics AS jsonb), finished_at=now()
            WHERE id=:run_id
            """
        ),
        {"run_id": run_id, "metrics": json.dumps(metrics, ensure_ascii=False)},
    )
    await session.commit()


def _aggregate(results: list[GenerationItemResult]) -> dict[str, object]:
    completed = [result for result in results if result.error is None]
    non_refusals = [result for result in completed if result.refused is False]
    answerable = [result for result in completed if result.item.answerable]
    unanswerable = [result for result in completed if not result.item.answerable]
    citation_total = sum(result.citation_count for result in non_refusals)
    aligned_total = sum(result.aligned_citations for result in non_refusals)
    latencies = [result.latency_ms for result in completed]
    return {
        "item_count": len(results),
        "completed_count": len(completed),
        "error_count": len(results) - len(completed),
        "category_counts": dict(Counter(result.item.category for result in results)),
        "refusal": {
            "accuracy": _rate(
                sum(result.refusal_correct for result in completed), len(completed)
            ),
            "correct": sum(result.refusal_correct for result in completed),
            "total": len(completed),
            "answerable_answer_rate": _rate(
                sum(result.refused is False for result in answerable), len(answerable)
            ),
            "unanswerable_correct_refusal_rate": _rate(
                sum(result.refused is True for result in unanswerable),
                len(unanswerable),
            ),
        },
        "citation_validity": {
            "all_output_rate": _rate(
                sum(result.citation_validity.valid for result in completed),
                len(completed),
            ),
            "all_output_valid": sum(
                result.citation_validity.valid for result in completed
            ),
            "all_output_total": len(completed),
            "non_refusal_rate": _rate(
                sum(result.citation_validity.valid for result in non_refusals),
                len(non_refusals),
            ),
            "non_refusal_valid": sum(
                result.citation_validity.valid for result in non_refusals
            ),
            "non_refusal_total": len(non_refusals),
        },
        "constraint_pass": {
            "rate": _rate(
                sum(result.constraint_result.passed for result in completed),
                len(completed),
            ),
            "passed": sum(result.constraint_result.passed for result in completed),
            "total": len(completed),
        },
        "citation_gold_alignment": {
            "rate": _rate(aligned_total, citation_total),
            "aligned": aligned_total,
            "total": citation_total,
            "note": "自动代理指标：引用 quote 覆盖 gold span 达到 theta；不等于语义引用准确率。",
        },
        "citation_accuracy": {
            "status": "pending_human_review",
            "reviewed_citations": 0,
            "supported_citations": 0,
            "rate": None,
        },
        "latency_ms": {
            "mean": fmean(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "actual_models": sorted(
            {
                f"{result.provider}/{result.model}"
                for result in completed
                if result.model
            }
        ),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _serialize_citation(citation: Any) -> dict[str, object]:
    return {
        "citation_id": citation.citation_id,
        "block_id": str(citation.block_id),
        "version_id": str(citation.version_id),
        "document_id": str(citation.document_id),
        "title": citation.title,
        "source_uri": citation.source_uri,
        "quote": citation.quote,
        "char_start": citation.char_start,
        "char_end": citation.char_end,
    }


def _json_item(result: GenerationItemResult) -> dict[str, object]:
    return {
        "item_id": str(result.item.id),
        "category": result.item.category,
        "question": result.item.question,
        "gold_answer": result.item.gold_answer,
        "answerable": result.item.answerable,
        "answer": result.answer,
        "citations": result.citations,
        "refused": result.refused,
        "refusal_reason": result.refusal_reason,
        "refusal_correct": result.refusal_correct,
        "citation_validity": result.citation_validity.to_dict(),
        "constraint_pass": result.constraint_result.to_dict(),
        "citation_gold_alignment": {
            "aligned": result.aligned_citations,
            "total": result.citation_count,
        },
        "latency_ms": result.latency_ms,
        "model": result.model,
        "provider": result.provider,
        "error": result.error,
    }


def _write_review_csv(path: Path, results: list[GenerationItemResult]) -> None:
    fieldnames = [
        "item_id",
        "category",
        "question",
        "answer",
        "citation_id",
        "citation_quote",
        "supported",
        "reason",
        "reviewer",
        "reviewed_at",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            for citation in result.citations:
                writer.writerow(
                    {
                        "item_id": str(result.item.id),
                        "category": result.item.category,
                        "question": result.item.question,
                        "answer": result.answer or "",
                        "citation_id": citation["citation_id"],
                        "citation_quote": citation["quote"],
                        "supported": "",
                        "reason": "",
                        "reviewer": "",
                        "reviewed_at": "",
                    }
                )


def _markdown_report(payload: dict[str, object]) -> str:
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    refusal = metrics["refusal"]
    validity = metrics["citation_validity"]
    constraints = metrics["constraint_pass"]
    alignment = metrics["citation_gold_alignment"]
    assert isinstance(refusal, dict) and isinstance(validity, dict)
    assert isinstance(constraints, dict) and isinstance(alignment, dict)
    return f"""# {payload["label"]}

- dataset: `{payload["dataset"]}`
- run_id: `{payload["run_id"]}`
- git_sha: `{payload["git_sha"]}`
- config_hash: `{payload["config_hash"]}`
- completed: {metrics["completed_count"]}/{metrics["item_count"]}

## 指标

| 指标 | 结果 |
|---|---:|
| 拒答准确率 | {_percent(refusal["accuracy"])} ({refusal["correct"]}/{refusal["total"]}) |
| 不可答正确拒答率 | {_percent(refusal["unanswerable_correct_refusal_rate"])} |
| citation_validity（非拒答） | {_percent(validity["non_refusal_rate"])} ({validity["non_refusal_valid"]}/{validity["non_refusal_total"]}) |
| constraint_pass | {_percent(constraints["rate"])} ({constraints["passed"]}/{constraints["total"]}) |
| citation_gold_alignment（自动代理） | {_percent(alignment["rate"])} ({alignment["aligned"]}/{alignment["total"]}) |
| citation_accuracy（语义支撑） | 待人工复核 |

`citation_validity` 只证明标签格式、数据库对象与 quote 区间有效；
`citation_gold_alignment` 只证明引用覆盖人工 gold span。两者都不能冒充语义引用准确率。
人工复核工作表见同目录 `citation-review.csv`。
"""


def _percent(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2%}"


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
        description="运行 M0 dense-only 生成与引用规则基线"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--origin", choices=["human", "synthetic", "badcase", "all"], default="human"
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/generation-baseline")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_generation_baseline(
            dataset_name=args.dataset,
            label=args.label,
            origin=args.origin,
            top_k=args.top_k,
            theta=args.theta,
            output_root=args.output_dir,
        )
    )
    print(report)


if __name__ == "__main__":
    main()
