from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from eval.report_metrics import KIND_GENERATION, load_report
from eval.suite_generation_runner import (
    _combine_reports,
    _validate_dev_only_suite,
    run_suite_generation,
)
from eval.suites import EvalSuite, SuiteDataset


def test_suite_generation_requires_explicit_model_send_authorization(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="必须显式授权"):
        asyncio.run(
            run_suite_generation(
                suite_path=Path("../eval/suites/m1-dev-70.json"),
                retrieval_manifest_path=tmp_path / "not-read.json",
                label="blocked-without-authorization",
                authorization_note="",
                allow_model_send=False,
                output_root=tmp_path,
            )
        )


def test_suite_generation_rejects_test_dataset() -> None:
    suite = EvalSuite(
        name="invalid-dev-suite",
        description="must fail before model send",
        origin="human",
        item_count=70,
        datasets=(SuiteDataset(name="core-test", item_count=70),),
        provenance={
            "core-test": {"reviewer": "owner", "reviewed_at": "2026-08-16"}
        },
        category_counts={
            "single_hop": 19,
            "multi_hop": 14,
            "table": 12,
            "unanswerable": 13,
            "temporal": 6,
            "global": 6,
        },
    )

    with pytest.raises(ValueError, match="禁止包含 test dataset"):
        _validate_dev_only_suite(suite)


# --------------------------------------------------------------- 四份报告并成一份


def _suite() -> EvalSuite:
    return EvalSuite(
        name="mini-dev",
        description="两个 dataset，各 1 条",
        origin="human",
        item_count=2,
        datasets=(
            SuiteDataset(name="core-dev", item_count=1),
            SuiteDataset(name="english-dev", item_count=1),
        ),
        provenance={},
        category_counts={"single_hop": 2},
    )


def _child(
    dataset: str,
    item_id: str,
    *,
    passed: bool = True,
    extra_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "dataset": dataset,
        "dataset_fingerprint": f"df-{dataset}",
        "annotation_fingerprint": f"af-{dataset}",
        "chunk_strategy": "heading",
        "chunk_metadata": {
            "suite_definition_sha256": "s" * 64,
            "retrieval_run_id": f"rr-{dataset}",
        },
        "origin": "human",
        "top_k": 10,
        "theta": 0.5,
    }
    config.update(extra_config or {})
    return {
        "run_id": f"run-{dataset}",
        "dataset": dataset,
        "label": f"lbl-{dataset}",
        "git_sha": "1" * 40,
        "config": config,
        "config_hash": f"hash-{dataset}",
        "metrics": {},
        "items": [
            {
                "item_id": item_id,
                "category": "single_hop",
                "answerable": True,
                "question": "问题",
                "gold_answer": "标准答案",
                "answer": "模型答案",
                "citations": [{"citation_id": "S1"}],
                "refused": False,
                "refusal_correct": True,
                "citation_validity": {"valid": True, "citation_count": 1},
                "constraint_pass": {"passed": passed},
                "citation_gold_alignment": {"aligned": 1 if passed else 0, "total": 1},
                "latency_ms": 1500,
                "total_tokens": 1000,
                "cost_usd": None,
                "error": None,
            }
        ],
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _combine(tmp_path: Path, children: list[dict[str, Any]], out: str = "out") -> Path:
    paths = [
        _write(tmp_path, f"child-{index}", child)
        for index, child in enumerate(children)
    ]
    return _combine_reports(
        suite=_suite(),
        suite_definition_sha256="s" * 64,
        label="lbl",
        chunk_strategy="heading",
        child_paths=paths,
        output_dir=tmp_path / out,
    )


def test_combined_report_is_loadable_as_one_generation_run(tmp_path: Path) -> None:
    """门禁要判的是整套 70 条；分 dataset 的四份报告过不了门禁。"""
    report = _combine(tmp_path, [_child("core-dev", "a"), _child("english-dev", "b")])

    loaded = load_report(report)
    assert loaded.kind == KIND_GENERATION
    assert loaded.payload["dataset"] == "mini-dev"
    assert [item["item_id"] for item in loaded.payload["items"]] == ["a", "b"]


def test_combined_metrics_match_the_gate_definition(tmp_path: Path) -> None:
    """聚合值必须用门禁那套 MetricSpec 算，否则会出现"报告没退门禁说退了"。"""
    report = _combine(
        tmp_path,
        [_child("core-dev", "a"), _child("english-dev", "b", passed=False)],
    )

    metrics = json.loads(report.read_text(encoding="utf-8"))["metrics"]
    assert metrics["constraint_pass"] == {"value": 0.5, "eligible_items": 2}
    assert metrics["citation_gold_alignment"] == {"value": 0.5, "eligible_items": 2}
    # 自部署价格表为 0，金额不可用时报 None 而不是 0.00
    assert metrics["cost_usd"]["value"] is None


def test_per_dataset_identity_is_merged_not_dropped(tmp_path: Path) -> None:
    """标注指纹逐 dataset 不同，但它是 constraint_pass 的判据身份，不能丢。"""
    report = _combine(tmp_path, [_child("core-dev", "a"), _child("english-dev", "b")])
    config = json.loads(report.read_text(encoding="utf-8"))["config"]

    assert config["chunk_metadata"]["annotation_fingerprints"] == {
        "core-dev": "af-core-dev",
        "english-dev": "af-english-dev",
    }
    assert config["chunk_metadata"]["retrieval_run_ids"] == {
        "core-dev": "rr-core-dev",
        "english-dev": "rr-english-dev",
    }
    # 合成指纹随任一 dataset 的标注变化而变
    other = _combine(
        tmp_path,
        [
            _child("core-dev", "a", extra_config={"annotation_fingerprint": "af-改过"}),
            _child("english-dev", "b"),
        ],
        out="other",
    )
    assert (
        json.loads(other.read_text(encoding="utf-8"))["config"]["annotation_fingerprint"]
        != config["annotation_fingerprint"]
    )


def test_real_config_drift_still_fails(tmp_path: Path) -> None:
    """放行逐 dataset 字段不等于放行实验配置漂移。"""
    with pytest.raises(ValueError, match="配置漂移"):
        _combine(
            tmp_path,
            [_child("core-dev", "a"), _child("english-dev", "b", extra_config={"top_k": 5})],
        )


def test_dataset_order_drift_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="顺序/集合漂移"):
        _combine(tmp_path, [_child("english-dev", "b"), _child("core-dev", "a")])


def test_duplicate_item_ids_fail(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="数量或唯一性"):
        _combine(tmp_path, [_child("core-dev", "a"), _child("english-dev", "a")])
