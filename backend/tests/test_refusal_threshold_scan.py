import json
from pathlib import Path

import pytest

from eval.refusal_threshold_scan import ScanRefused, build_report


def _write_report(tmp_path: Path, *, dataset: str, score_source: str) -> Path:
    path = tmp_path / f"{dataset}.json"
    path.write_text(
        json.dumps(
            {
                "dataset": dataset,
                "config": {
                    "refusal_threshold": 0.35,
                    "retrieval_score_source": score_source,
                    "refusal_threshold_applied": False,
                },
                "items": [
                    {
                        "item_id": f"{dataset}-a",
                        "category": "single_hop",
                        "answerable": True,
                        "refusal_signals": {
                            "top_score": 0.032,
                            "score_margin_ratio": 0.02,
                        },
                        "error": None,
                        "refused": False,
                    },
                    {
                        "item_id": f"{dataset}-u",
                        "category": "unanswerable",
                        "answerable": False,
                        "refusal_signals": {
                            "top_score": 0.027,
                            "score_margin_ratio": 0.01,
                        },
                        "error": None,
                        "refused": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_scan_uses_fusion_support_instead_of_legacy_cosine_range(tmp_path: Path) -> None:
    report = _write_report(tmp_path, dataset="core-dev", score_source="fusion")

    payload = build_report(
        [report],
        override_threshold=0.0,
        generation_reports=[report],
    )

    assert payload["meta"]["score_source"] == "fusion"
    assert payload["meta"]["threshold_applied"] is False
    thresholds = [row["threshold"] for row in payload["sweep"]]
    assert min(thresholds) < 0.027
    assert max(thresholds) > 0.032
    assert all(threshold < 0.1 for threshold in thresholds)


def test_scan_rejects_mixed_score_sources(tmp_path: Path) -> None:
    fusion = _write_report(tmp_path, dataset="core-dev", score_source="fusion")
    dense = _write_report(tmp_path, dataset="english-dev", score_source="dense")

    with pytest.raises(ScanRefused, match="score source"):
        build_report(
            [fusion, dense],
            override_threshold=0.0,
        )
