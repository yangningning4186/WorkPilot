import argparse
import asyncio
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.db import close_database, session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CitationReview:
    item_id: UUID
    citation_id: str
    supported: bool
    reason: str
    reviewer: str
    reviewed_at: str


def load_reviews(path: Path) -> dict[tuple[UUID, str], CitationReview]:
    reviews: dict[tuple[UUID, str], CitationReview] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            value = (row.get("supported") or "").strip().casefold()
            if value not in {"yes", "no"}:
                raise ValueError(f"{path}:{row_number}: supported 必须是 yes/no")
            review = CitationReview(
                item_id=UUID((row.get("item_id") or "").strip()),
                citation_id=(row.get("citation_id") or "").strip(),
                supported=value == "yes",
                reason=(row.get("reason") or "").strip(),
                reviewer=(row.get("reviewer") or "").strip(),
                reviewed_at=(row.get("reviewed_at") or "").strip(),
            )
            if not all(
                (review.citation_id, review.reason, review.reviewer, review.reviewed_at)
            ):
                raise ValueError(f"{path}:{row_number}: 人工复核字段不完整")
            key = (review.item_id, review.citation_id)
            if key in reviews:
                raise ValueError(f"{path}:{row_number}: 重复引用 {key}")
            reviews[key] = review
    return reviews


async def import_reviews(
    *,
    report_paths: list[Path],
    review_path: Path,
    apply: bool,
) -> dict[str, object]:
    reviews = load_reviews(review_path)
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in report_paths]
    expected = {
        (UUID(str(item["item_id"])), str(citation["citation_id"]))
        for report in reports
        for item in report["items"]
        for citation in item["citations"]
    }
    if set(reviews) != expected:
        missing = sorted(
            f"{item_id}/{label}" for item_id, label in expected - set(reviews)
        )
        extra = sorted(
            f"{item_id}/{label}" for item_id, label in set(reviews) - expected
        )
        raise ValueError(f"复核引用集合不一致: missing={missing}, extra={extra}")

    by_item: dict[UUID, list[CitationReview]] = defaultdict(list)
    for review in reviews.values():
        by_item[review.item_id].append(review)

    run_summaries: list[dict[str, object]] = []
    async with session_factory() as session:
        for report in reports:
            summary = await _import_run(
                session,
                report=report,
                by_item=by_item,
                apply=apply,
            )
            run_summaries.append(summary)
        if apply:
            await session.commit()
        else:
            await session.rollback()
    await close_database()
    supported = sum(review.supported for review in reviews.values())
    return {
        "status": "applied" if apply else "validated",
        "reviewed_citations": len(reviews),
        "supported_citations": supported,
        "citation_accuracy": supported / len(reviews) if reviews else None,
        "runs": run_summaries,
    }


async def _import_run(
    session: AsyncSession,
    *,
    report: dict[str, Any],
    by_item: dict[UUID, list[CitationReview]],
    apply: bool,
) -> dict[str, object]:
    run_id = UUID(str(report["run_id"]))
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT item_id, retrieved
                    FROM eval_results
                    WHERE run_id=:run_id
                    ORDER BY item_id
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    expected_items = {UUID(str(item["item_id"])) for item in report["items"]}
    if {row["item_id"] for row in rows} != expected_items:
        raise ValueError(f"run {run_id} 的数据库 item 集合与报告不一致")

    run_reviews: list[CitationReview] = []
    for row in rows:
        item_id = row["item_id"]
        item_reviews = sorted(
            by_item.get(item_id, []), key=lambda review: review.citation_id
        )
        stored_labels = {str(citation["citation_id"]) for citation in row["retrieved"]}
        review_labels = {review.citation_id for review in item_reviews}
        if stored_labels != review_labels:
            raise ValueError(
                f"run {run_id} item {item_id} 引用集合不一致: "
                f"stored={stored_labels}, review={review_labels}"
            )
        run_reviews.extend(item_reviews)
        payload = _item_payload(item_reviews)
        if apply:
            await session.execute(
                text(
                    """
                    UPDATE eval_results
                    SET human_label=COALESCE(human_label, '{}'::jsonb)
                        || CAST(:payload AS jsonb)
                    WHERE run_id=:run_id AND item_id=:item_id
                    """
                ),
                {
                    "run_id": run_id,
                    "item_id": item_id,
                    "payload": json.dumps(payload, ensure_ascii=False),
                },
            )

    supported = sum(review.supported for review in run_reviews)
    accuracy = supported / len(run_reviews) if run_reviews else None
    aggregate = {
        "status": "complete",
        "reviewed_citations": len(run_reviews),
        "supported_citations": supported,
        "rate": accuracy,
    }
    if apply:
        await session.execute(
            text(
                """
                UPDATE eval_runs
                SET metrics=jsonb_set(
                    COALESCE(metrics, '{}'::jsonb),
                    '{citation_accuracy}', CAST(:payload AS jsonb), true
                )
                WHERE id=:run_id
                """
            ),
            {"run_id": run_id, "payload": json.dumps(aggregate)},
        )
    return {"run_id": str(run_id), **aggregate}


def _item_payload(reviews: list[CitationReview]) -> dict[str, object]:
    if not reviews:
        return {
            "citation_accuracy": {
                "status": "not_applicable",
                "supported_citations": 0,
                "reviewed_citations": 0,
                "rate": None,
                "reviews": [],
            }
        }
    supported = sum(review.supported for review in reviews)
    serialized = []
    for review in reviews:
        item = asdict(review)
        item["item_id"] = str(review.item_id)
        serialized.append(item)
    return {
        "citation_accuracy": {
            "status": "complete",
            "supported_citations": supported,
            "reviewed_citations": len(reviews),
            "rate": supported / len(reviews),
            "reviews": serialized,
        }
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验并写回 M0 人工逐引用复核")
    parser.add_argument(
        "--generation-report", type=Path, action="append", required=True
    )
    parser.add_argument("--review-csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="通过全量校验后写入数据库")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        import_reviews(
            report_paths=args.generation_report,
            review_path=args.review_csv,
            apply=args.apply,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
