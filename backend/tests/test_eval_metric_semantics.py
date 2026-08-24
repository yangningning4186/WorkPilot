"""三个指标口径的回归用例。

对应 2026-08-24 的 baseline 复核结论（docs/06）：
1. 拒答阈值没有绑定排序器量纲与标定来源 —— 0.35 套在 RRF 分数上把 57 条可答题全判成拒答；
2. `citation_validity` 只查格式，按构造恒为 1.0，被当成质量门禁；
3. `constraint_pass` 的分母含拒答样本，多拒答即可刷分。

这三条都属于"指标照常输出、数字却没有意义"的无声失败，只能靠用例挡。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.kb_retrieval_runner import (
    load_refusal_calibration,
    resolve_actual_score_source,
    validate_refusal_threshold,
)
from eval.refusal_calibration import build_calibration
from eval.report_metrics import KIND_GENERATION, KIND_RETRIEVAL, METRICS

# RRF 双路等权、rrf_k=60 时 top_score 的实测区间，取自
# eval/outputs/kb-retrieval/20260823T105804Z-e3-nr2-adaptive-top10-candidate-v1
_RRF_SCORES = (0.016666666666666666, 0.03278688524590164, 0.03333333333333333)


def _extract(kind: str, name: str, item: dict[str, Any], config: dict[str, Any]) -> Any:
    for spec in METRICS[kind]:
        if spec.name == name:
            return spec.extract(item, config)
    raise AssertionError(f"指标不存在: {name}")


def _generation_item(
    *,
    answerable: bool = True,
    refused: bool = False,
    constraint_passed: bool = True,
    aligned: int = 1,
    total: int = 1,
) -> dict[str, Any]:
    return {
        "item_id": "item-0",
        "answerable": answerable,
        "refused": refused,
        "refusal_correct": True,
        "citation_validity": {"valid": True},
        "constraint_pass": {"passed": constraint_passed},
        "citation_gold_alignment": {"aligned": aligned, "total": total},
        "error": None,
    }


# --------------------------------------------------------------- 拒答阈值的量纲


def test_threshold_outside_the_scorer_scale_is_refused_before_the_report_is_written() -> None:
    # 0.35 是给归一化打分器定的；RRF 分数上界约 2/rrf_k，套上去等于全部拒答
    with pytest.raises(ValueError, match="量纲"):
        validate_refusal_threshold(
            threshold=0.35,
            source="dev_calibrated",
            score_source="rrf",
            observed_scores=_RRF_SCORES,
        )


def test_threshold_calibrated_on_the_eval_set_itself_is_refused() -> None:
    with pytest.raises(ValueError, match="拟合"):
        validate_refusal_threshold(
            threshold=0.032,
            source="eval_best",
            score_source="rrf",
            observed_scores=_RRF_SCORES,
        )


def test_threshold_without_a_declared_source_is_refused() -> None:
    with pytest.raises(ValueError, match="refusal-threshold-source"):
        validate_refusal_threshold(
            threshold=0.032,
            source=None,
            score_source="rrf",
            observed_scores=_RRF_SCORES,
        )


def test_threshold_inside_the_observed_range_passes() -> None:
    validate_refusal_threshold(
        threshold=0.032,
        source="dev_calibrated",
        score_source="rrf",
        observed_scores=_RRF_SCORES,
    )


def test_actual_score_source_rejects_mixed_scales_and_reranker_fallback() -> None:
    fusion = {"error": None, "retrieval_score_source": "fusion"}
    rerank = {"error": None, "retrieval_score_source": "rerank"}

    with pytest.raises(ValueError, match="混用了多个"):
        resolve_actual_score_source([fusion, rerank], rerank_required=False)
    with pytest.raises(ValueError, match="reranker fallback"):
        resolve_actual_score_source([fusion], rerank_required=True)


def _calibration_report(path: Path) -> Path:
    payload = {
        "kind": "retrieval",
        "dataset": "kb-calibration-v1",
        "git_sha": "f" * 40,
        "config_hash": "c" * 64,
        "config": {"retrieval_score_source": "fusion"},
        "suite": {
            "sha256": "a" * 64,
            "origin": "synthetic",
            "review_status": "approved",
            "reviewer": "suite-owner",
            "reviewed_at": "2026-08-24T09:00:00+08:00",
        },
        "reproducibility": {"git_dirty": False},
        "metrics": {
            "refusal": {
                "best": {"threshold": 0.025},
                "answerable_count": 1,
                "unanswerable_count": 1,
            }
        },
        "items": [
            {
                "item_id": "answerable",
                "answerable": True,
                "top_score": 0.03,
                "retrieval_score_source": "fusion",
                "error": None,
            },
            {
                "item_id": "unanswerable",
                "answerable": False,
                "top_score": 0.02,
                "retrieval_score_source": "fusion",
                "error": None,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_independent_calibration_artifact_is_signed_and_bound_to_score_source(
    tmp_path: Path,
) -> None:
    report = _calibration_report(tmp_path / "report.json")
    payload = build_calibration(
        report,
        reviewer="threshold-owner",
        reviewed_at="2026-08-24T10:00:00+08:00",
    )
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    loaded = load_refusal_calibration(
        calibration_path,
        evaluation_dataset_sha256="b" * 64,
    )
    assert loaded.threshold == 0.025
    assert loaded.score_source == "fusion"

    with pytest.raises(ValueError, match="自身上标定"):
        load_refusal_calibration(
            calibration_path,
            evaluation_dataset_sha256="a" * 64,
        )

    tampered = json.loads(calibration_path.read_text())
    tampered["threshold"] = 0.5
    calibration_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="完整性"):
        load_refusal_calibration(
            calibration_path,
            evaluation_dataset_sha256="b" * 64,
        )


def test_refusal_metric_is_ineligible_when_the_threshold_has_no_declared_source() -> None:
    item = {"top_score": 0.033, "answerable": True, "error": None}
    point = _extract(
        KIND_RETRIEVAL,
        "refusal_correct_at_threshold",
        item,
        {"refusal_threshold": 0.35},
    )
    # 没有来源的历史报告不参与比较，而不是带着"全拒答"的参照点进门禁
    assert point.denominator == 0


def test_refusal_metric_accepts_an_independent_calibration_source() -> None:
    item = {"top_score": 0.033, "answerable": True, "error": None}
    point = _extract(
        KIND_RETRIEVAL,
        "refusal_correct_at_threshold",
        item,
        {
            "refusal_threshold": 0.032,
            "refusal_threshold_source": "independent_calibration",
        },
    )
    assert (point.numerator, point.denominator) == (1.0, 1.0)


# ------------------------------------------------------------------- 引文支撑率


def test_citation_support_counts_a_refused_answerable_item_as_zero() -> None:
    point = _extract(
        KIND_GENERATION,
        "citation_support_answerable",
        _generation_item(refused=True, aligned=0, total=0),
        {},
    )
    # 关键：分母仍然是 1。拒答不能让样本悄悄退出分母
    assert (point.numerator, point.denominator) == (0.0, 1.0)


def test_citation_support_counts_an_answer_with_no_citation_as_zero() -> None:
    point = _extract(
        KIND_GENERATION,
        "citation_support_answerable",
        _generation_item(aligned=0, total=0),
        {},
    )
    assert (point.numerator, point.denominator) == (0.0, 1.0)


def test_refusing_more_cannot_raise_the_citation_support_rate() -> None:
    answered = [_generation_item(aligned=1, total=1), _generation_item(aligned=0, total=1)]
    # 把那条答不好的改成拒答——旧口径下它会退出分母，把比率从 0.5 抬到 1.0
    gamed = [_generation_item(aligned=1, total=1), _generation_item(refused=True, total=0)]

    def rate(items: list[dict[str, Any]]) -> float:
        points = [
            _extract(KIND_GENERATION, "citation_support_answerable", item, {}) for item in items
        ]
        return sum(p.numerator for p in points) / sum(p.denominator for p in points)

    assert rate(answered) == pytest.approx(0.5)
    assert rate(gamed) <= rate(answered)


# ------------------------------------------------------------------ 约束通过率


def test_constraint_pass_answerable_counts_a_refusal_as_a_failure() -> None:
    point = _extract(
        KIND_GENERATION,
        "constraint_pass_answerable",
        _generation_item(refused=True, constraint_passed=True),
        {},
    )
    assert (point.numerator, point.denominator) == (0.0, 1.0)


def test_refusing_more_cannot_raise_constraint_pass_answerable() -> None:
    answered = [
        _generation_item(constraint_passed=True),
        _generation_item(constraint_passed=False),
    ]
    gamed = [
        _generation_item(constraint_passed=True),
        _generation_item(refused=True, constraint_passed=True),
    ]

    def rate(items: list[dict[str, Any]]) -> float:
        points = [
            _extract(KIND_GENERATION, "constraint_pass_answerable", item, {}) for item in items
        ]
        return sum(p.numerator for p in points) / sum(p.denominator for p in points)

    assert rate(gamed) <= rate(answered)


def test_unanswerable_items_stay_out_of_the_answerable_metrics() -> None:
    for name in ("constraint_pass_answerable", "citation_support_answerable"):
        point = _extract(KIND_GENERATION, name, _generation_item(answerable=False), {})
        assert point.denominator == 0, name
