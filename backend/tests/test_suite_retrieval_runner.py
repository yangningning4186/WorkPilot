import json
from pathlib import Path

import pytest

from eval.suite_retrieval_runner import _combine_reports, _validate_dev_only_suite
from eval.suites import EvalSuite, SuiteDataset


def _suite(*, include_test: bool = False) -> EvalSuite:
    names = ("dev-a", "dev-b", "dev-c", "core-test") if include_test else (
        "dev-a",
        "dev-b",
        "dev-c",
        "dev-d",
    )
    counts = (20, 20, 17, 13)
    datasets = tuple(
        SuiteDataset(name=name, item_count=count) for name, count in zip(names, counts, strict=True)
    )
    return EvalSuite(
        name="m1-dev-70",
        description="fixture",
        origin="human",
        item_count=70,
        datasets=datasets,
        provenance={name: {"reviewer": "owner", "reviewed_at": "2026-08-16"} for name in names},
        category_counts={
            "single_hop": 19,
            "multi_hop": 14,
            "table": 12,
            "unanswerable": 13,
            "temporal": 6,
            "global": 6,
        },
    )


def _write_child(path: Path, *, dataset: str, count: int, start: int) -> Path:
    config = {
        "dataset": dataset,
        "dataset_fingerprint": f"fingerprint-{dataset}",
        "runner_git_sha": "abc123",
        "strategy": "dense-lexical-rrf-rerank",
        "chunk_strategy": "heading",
        "chunk_metadata": {},
        "top_k": 10,
        "diagnostic_k": 50,
        "token_budget": 4000,
        "token_count_mode": "unicode",
        "theta": 0.5,
        "alpha": 0.5,
        "origin": "human",
        "refusal_threshold": 0.35,
    }
    items = [
        {
            "item_id": f"00000000-0000-0000-0000-{index:012d}",
            "category": "unanswerable" if index % 10 == 0 else "single_hop",
            "question": f"question-{index}",
            "answerable": index % 10 != 0,
            "top_score": 0.2 if index % 10 == 0 else 0.8,
            "latency_ms": 10,
            "retrieval": None
            if index % 10 == 0
            else {
                "span_recall_at_k": 1.0,
                "budget_span_recall": 1.0,
                "ndcg_at_k": 1.0,
                "alpha_ndcg_at_k": 1.0,
                "mrr": 1.0,
                "context_precision": 1.0,
            },
            "retrieved": [],
            "span_diagnostics": [],
        }
        for index in range(start, start + count)
    ]
    path.write_text(
        json.dumps(
            {
                "run_id": f"run-{dataset}",
                "dataset": dataset,
                "label": dataset,
                "git_sha": "abc123",
                "config": config,
                "config_hash": f"hash-{dataset}",
                "metrics": {},
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_combine_reports_builds_one_70_item_suite_report(tmp_path: Path) -> None:
    suite = _suite()
    paths: list[Path] = []
    start = 1
    for dataset in suite.datasets:
        paths.append(
            _write_child(
                tmp_path / f"{dataset.name}.json",
                dataset=dataset.name,
                count=dataset.item_count,
                start=start,
            )
        )
        start += dataset.item_count

    report_path = _combine_reports(
        suite=suite,
        suite_definition_sha256="suite-sha",
        label="m1-expanded",
        chunk_strategy="heading",
        child_paths=paths,
        output_dir=tmp_path / "combined",
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["dataset"] == "m1-dev-70"
    assert report["metrics"]["item_count"] == 70
    assert len(report["items"]) == 70
    assert report["metrics"]["refusal"]["configured"]["accuracy"] == 1.0
    assert len(report["source_reports"]) == 4


def test_dev_suite_guard_rejects_test_dataset() -> None:
    with pytest.raises(ValueError, match="禁止包含 test dataset"):
        _validate_dev_only_suite(_suite(include_test=True))
