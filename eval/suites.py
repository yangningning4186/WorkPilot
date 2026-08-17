import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SuiteDataset:
    name: str
    item_count: int


@dataclass(frozen=True)
class EvalSuite:
    name: str
    description: str
    origin: str
    item_count: int
    datasets: tuple[SuiteDataset, ...]
    provenance: dict[str, dict[str, str]]
    category_counts: dict[str, int]


def load_suite(path: Path) -> EvalSuite:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    datasets = tuple(
        SuiteDataset(name=str(item["name"]), item_count=int(item["item_count"]))
        for item in payload["datasets"]
    )
    suite = EvalSuite(
        name=str(payload["name"]),
        description=str(payload["description"]),
        origin=str(payload["origin"]),
        item_count=int(payload["item_count"]),
        datasets=datasets,
        provenance={
            str(dataset): {str(key): str(value) for key, value in review.items()}
            for dataset, review in payload["provenance"].items()
        },
        category_counts={
            str(key): int(value) for key, value in payload["category_counts"].items()
        },
    )
    if (
        not suite.datasets
        or sum(item.item_count for item in suite.datasets) != suite.item_count
    ):
        raise ValueError("suite 的 dataset 条数之和与 item_count 不一致")
    if sum(suite.category_counts.values()) != suite.item_count:
        raise ValueError("suite 的 category_counts 与 item_count 不一致")
    dataset_names = {item.name for item in suite.datasets}
    if set(suite.provenance) != dataset_names:
        raise ValueError("suite 的 provenance 必须覆盖每个 dataset")
    for dataset, review in suite.provenance.items():
        if not review.get("reviewer") or not review.get("reviewed_at"):
            raise ValueError(f"suite 的人工 provenance 不完整: {dataset}")
    return suite


async def validate_suite(session: AsyncSession, suite: EvalSuite) -> dict[str, object]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT d.name, i.category, i.gold_spans, i.gold_evidence_groups,
                           validate_eval_spans(i.gold_spans) AS spans_valid,
                           validate_eval_evidence_groups(i.gold_evidence_groups)
                             AS groups_valid
                    FROM eval_items i
                    JOIN eval_datasets d ON d.id=i.dataset_id
                    WHERE d.name=ANY(:dataset_names) AND i.origin=:origin
                    """
                ),
                {
                    "dataset_names": [item.name for item in suite.datasets],
                    "origin": suite.origin,
                },
            )
        )
        .mappings()
        .all()
    )
    actual_dataset_counts = Counter(str(row["name"]) for row in rows)
    expected_dataset_counts = {item.name: item.item_count for item in suite.datasets}
    if dict(actual_dataset_counts) != expected_dataset_counts:
        raise ValueError(
            f"suite dataset 条数不匹配: expected={expected_dataset_counts}, "
            f"actual={dict(actual_dataset_counts)}"
        )
    category_counts = Counter(str(row["category"]) for row in rows)
    if dict(category_counts) != suite.category_counts:
        raise ValueError(
            f"suite category 条数不匹配: expected={suite.category_counts}, "
            f"actual={dict(category_counts)}"
        )
    stale = sum(not bool(row["spans_valid"]) for row in rows)
    invalid_groups = sum(not bool(row["groups_valid"]) for row in rows)
    missing_answerable_spans = sum(
        row["category"] != "unanswerable" and not row["gold_spans"] for row in rows
    )
    missing_answerable_groups = sum(
        row["category"] not in {"unanswerable", "agent_task"}
        and row["gold_spans"]
        and not row["gold_evidence_groups"]
        for row in rows
    )
    if stale or invalid_groups or missing_answerable_spans or missing_answerable_groups:
        raise ValueError(
            "suite gold 无效: "
            f"stale={stale}, invalid_groups={invalid_groups}, "
            f"missing_answerable_spans={missing_answerable_spans}, "
            f"missing_answerable_groups={missing_answerable_groups}"
        )
    return {
        "name": suite.name,
        "origin": suite.origin,
        "item_count": len(rows),
        "dataset_counts": dict(actual_dataset_counts),
        "category_counts": dict(category_counts),
        "stale_gold_spans": stale,
        "invalid_gold_evidence_groups": invalid_groups,
        "missing_answerable_groups": missing_answerable_groups,
        "missing_answerable_spans": missing_answerable_spans,
    }
