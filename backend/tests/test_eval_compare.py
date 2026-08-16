"""compare 与 paired bootstrap 的独立 fixture 测试。

这里的报告全部在测试内构造，不读 eval/outputs 下的真实跑批结果——
真实报告会随策略迭代变化，拿它当 fixture 会让测试在无关改动上红。
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from eval.compare import (
    build_comparison,
    load_report,
    main,
    markdown_report,
)
from eval.stats import INELIGIBLE, MetricSamples, RatioPoint, paired_bootstrap

RESAMPLES = 200
SEED = 7


def _points(values: list[float | None]) -> tuple[RatioPoint, ...]:
    return tuple(INELIGIBLE if value is None else RatioPoint(value, 1.0) for value in values)


def _samples(baseline: list[float | None], candidate: list[float | None]) -> MetricSamples:
    return MetricSamples(baseline=_points(baseline), candidate=_points(candidate))


def _bootstrap(metrics: dict[str, MetricSamples], **kwargs: Any) -> dict[str, Any]:
    return paired_bootstrap(metrics, seed=SEED, resamples=RESAMPLES, **kwargs)


# --------------------------------------------------------------------------- stats


def test_paired_bootstrap_is_deterministic_for_a_fixed_seed() -> None:
    # 逐样本 Δ 必须有差异, 否则任何重采样都给出同一个区间, 测不出种子的作用
    metrics = {"recall": _samples([0.2, 0.4, 0.6, 0.8], [0.9, 0.4, 0.1, 1.0])}

    first = _bootstrap(metrics)["recall"]
    second = _bootstrap(metrics)["recall"]
    other_seed = paired_bootstrap(metrics, seed=SEED + 1, resamples=RESAMPLES)["recall"]

    assert (first.ci_low, first.ci_high) == (second.ci_low, second.ci_high)
    assert (other_seed.ci_low, other_seed.ci_high) != (first.ci_low, first.ci_high)


def test_uniform_gain_is_significant_and_mixed_gain_is_not() -> None:
    uniform = _bootstrap({"m": _samples([0.2, 0.4, 0.6, 0.8], [0.4, 0.6, 0.8, 1.0])})["m"]
    mixed = _bootstrap({"m": _samples([0.2, 0.9, 0.5, 0.4], [0.9, 0.2, 0.4, 0.5])})["m"]

    assert uniform.delta == pytest.approx(0.2)
    assert uniform.ci_low is not None and uniform.ci_low > 0
    assert uniform.verdict == "improved"
    assert uniform.significant is True

    assert mixed.ci_low is not None and mixed.ci_high is not None
    assert mixed.ci_low < 0 < mixed.ci_high
    assert mixed.verdict == "inconclusive"
    assert mixed.significant is False


def test_identical_arms_produce_zero_width_interval() -> None:
    result = _bootstrap({"m": _samples([0.3, 0.7, 1.0], [0.3, 0.7, 1.0])})["m"]

    assert result.delta == 0.0
    assert (result.ci_low, result.ci_high) == (0.0, 0.0)
    # 区间退化成一个点也仍然包含 0, 不能判成显著
    assert result.verdict == "inconclusive"


def test_lower_is_better_metric_flips_the_verdict() -> None:
    metrics = {"latency": _samples([100.0, 120.0, 140.0], [80.0, 100.0, 120.0])}

    higher = _bootstrap(metrics)["latency"]
    lower = _bootstrap(metrics, higher_is_better={"latency": False})["latency"]

    assert higher.delta == pytest.approx(-20.0)
    assert higher.verdict == "regressed"
    assert lower.verdict == "improved"


def test_ratio_metric_aggregates_micro_not_macro() -> None:
    baseline = (RatioPoint(1.0, 1.0), RatioPoint(1.0, 9.0))
    candidate = (RatioPoint(1.0, 1.0), RatioPoint(5.0, 9.0))
    result = _bootstrap({"alignment": MetricSamples(baseline=baseline, candidate=candidate)})

    # micro: (1+1)/(1+9) 而不是 macro 的 (1.0 + 1/9)/2
    assert result["alignment"].baseline == pytest.approx(0.2)
    assert result["alignment"].candidate == pytest.approx(0.6)


def test_ineligible_samples_leave_the_metric_untouched() -> None:
    result = _bootstrap({"m": _samples([0.5, None, 0.5], [1.0, None, 1.0])})["m"]

    assert result.sample_size == 2
    assert result.baseline == pytest.approx(0.5)
    assert result.candidate == pytest.approx(1.0)


def test_metric_without_any_eligible_sample_is_not_applicable() -> None:
    result = _bootstrap({"m": _samples([None, None], [None, None])})["m"]

    assert result.sample_size == 0
    assert (result.baseline, result.candidate, result.delta) == (None, None, None)
    assert result.verdict == "not_applicable"
    assert result.effective_resamples == 0


def test_paired_bootstrap_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="配对样本数量"):
        MetricSamples(baseline=_points([0.1]), candidate=_points([0.1, 0.2]))
    with pytest.raises(ValueError, match="同一条 item 轴"):
        _bootstrap({"a": _samples([0.1], [0.2]), "b": _samples([0.1, 0.3], [0.2, 0.4])})
    with pytest.raises(ValueError, match="ci_level"):
        paired_bootstrap({"m": _samples([0.1], [0.2])}, resamples=10, ci_level=1.5)
    with pytest.raises(ValueError, match="resamples"):
        paired_bootstrap({"m": _samples([0.1], [0.2])}, resamples=0)


# ----------------------------------------------------------------------- fixtures


def _retrieval_item(
    item_id: str,
    category: str,
    *,
    answerable: bool = True,
    recall: float = 1.0,
    top_score: float = 0.8,
    latency_ms: int = 10,
) -> dict[str, Any]:
    retrieval = (
        {
            "span_recall_at_k": recall,
            "budget_span_recall": recall,
            "ndcg_at_k": recall,
            "alpha_ndcg_at_k": recall,
            "mrr": recall,
            "context_precision": recall / 10,
            "retrieved_tokens": 1000,
            "relevant_chunks": 1,
        }
        if answerable
        else None
    )
    return {
        "item_id": item_id,
        "category": category,
        "question": f"问题 {item_id}",
        "answerable": answerable,
        "top_score": top_score,
        "latency_ms": latency_ms,
        "retrieval": retrieval,
        "retrieved": [],
        "span_diagnostics": [],
    }


def _retrieval_report(
    items: list[dict[str, Any]],
    *,
    label: str,
    dataset: str = "core-dev",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = {
        "strategy": "dense-only",
        "origin": "human",
        "top_k": 10,
        "token_budget": 4000,
        "theta": 0.5,
        "alpha": 0.5,
        "refusal_threshold": 0.35,
        "embedding_model": "bge-m3:latest",
        **(config or {}),
    }
    return {
        "run_id": f"run-{label}",
        "dataset": dataset,
        "label": label,
        "git_sha": "0" * 40,
        "config": merged,
        "config_hash": f"hash-{sorted(merged.items())}",
        "metrics": {},
        "items": items,
    }


def _generation_item(
    item_id: str,
    category: str,
    *,
    answerable: bool = True,
    refused: bool = False,
    refusal_correct: bool = True,
    constraint_passed: bool = True,
    citation_valid: bool = True,
    aligned: int = 1,
    total: int = 1,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": category,
        "question": f"问题 {item_id}",
        "answerable": answerable,
        "answer": "答案",
        "citations": [],
        "refused": refused,
        "refusal_correct": refusal_correct,
        "citation_validity": {"valid": citation_valid, "issues": []},
        "constraint_pass": {"passed": constraint_passed, "issues": []},
        "citation_gold_alignment": {"aligned": aligned, "total": total},
        "latency_ms": 1500,
        "error": error,
    }


def _generation_report(
    items: list[dict[str, Any]],
    *,
    label: str,
    dataset: str = "core-dev",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = {
        "strategy": "dense-only-generation",
        "origin": "human",
        "top_k": 5,
        "theta": 0.5,
        "chat_model": "qwen3.6-35b-a3b",
        **(config or {}),
    }
    return {
        "run_id": f"run-{label}",
        "dataset": dataset,
        "label": label,
        "git_sha": "1" * 40,
        "config": merged,
        "config_hash": f"hash-{sorted(merged.items())}",
        "metrics": {},
        "items": items,
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _compare(
    tmp_path: Path, baseline: dict[str, Any], candidate: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    return build_comparison(
        load_report(_write(tmp_path, "baseline", baseline)),
        load_report(_write(tmp_path, "candidate", candidate)),
        seed=SEED,
        resamples=RESAMPLES,
        **kwargs,
    )


def _four_retrieval_items(recalls: list[float]) -> list[dict[str, Any]]:
    categories = ["single_hop", "single_hop", "multi_hop", "table"]
    return [
        _retrieval_item(f"item-{index}", category, recall=recall)
        for index, (category, recall) in enumerate(zip(categories, recalls, strict=True))
    ]


# ----------------------------------------------------------------------- compare


def test_compare_pairs_by_item_id_not_by_position(tmp_path: Path) -> None:
    baseline = _retrieval_report(_four_retrieval_items([0.0, 0.5, 0.5, 1.0]), label="base")
    shuffled = list(reversed(_four_retrieval_items([0.5, 1.0, 1.0, 1.0])))
    candidate = _retrieval_report(shuffled, label="cand", config={"strategy": "dense-lexical-rrf"})

    payload = _compare(tmp_path, baseline, candidate)
    per_item = {record["item_id"]: record for record in payload["items"]}

    assert [record["item_id"] for record in payload["items"]] == [
        "item-0",
        "item-1",
        "item-2",
        "item-3",
    ]
    # item-0 在 candidate 报告里排最后, 配对必须按 item_id 而不是下标
    assert per_item["item-0"]["metrics"]["span_recall_at_k"] == {
        "baseline": 0.0,
        "candidate": 0.5,
        "delta": 0.5,
    }
    assert payload["metrics"]["budget_span_recall"]["delta"] == pytest.approx(0.375)
    assert payload["metrics"]["budget_span_recall"]["verdict"] == "improved"


def test_compare_reports_category_slices_and_sample_classification(tmp_path: Path) -> None:
    baseline = _retrieval_report(_four_retrieval_items([0.5, 0.5, 1.0, 1.0]), label="base")
    candidate = _retrieval_report(
        _four_retrieval_items([1.0, 0.5, 0.0, 1.0]),
        label="cand",
        config={"strategy": "dense-rerank"},
    )

    payload = _compare(tmp_path, baseline, candidate)
    categories = payload["by_category"]
    samples = payload["samples"]

    assert sorted(categories) == ["multi_hop", "single_hop", "table"]
    assert categories["single_hop"]["item_count"] == 2
    assert categories["single_hop"]["metrics"]["budget_span_recall"]["delta"] == pytest.approx(0.25)
    # 整体持平但类别互相抵消: single_hop 涨、multi_hop 跌
    assert categories["multi_hop"]["metrics"]["budget_span_recall"]["delta"] == pytest.approx(-1.0)
    assert payload["metrics"]["budget_span_recall"]["verdict"] == "inconclusive"

    assert samples["metric"] == "budget_span_recall"
    assert samples["counts"] == {
        "improved": 1,
        "regressed": 1,
        "tied": 2,
        "not_applicable": 0,
    }
    assert [record["item_id"] for record in samples["improved"]] == ["item-0"]
    assert [record["item_id"] for record in samples["regressed"]] == ["item-2"]
    assert samples["regressed"][0]["delta"] == pytest.approx(-1.0)


def test_unanswerable_items_only_join_the_metrics_that_apply(tmp_path: Path) -> None:
    def items(recall: float, top_score: float) -> list[dict[str, Any]]:
        return [
            _retrieval_item("item-0", "single_hop", recall=recall),
            _retrieval_item("item-1", "unanswerable", answerable=False, top_score=top_score),
        ]

    payload = _compare(
        tmp_path,
        _retrieval_report(items(0.5, 0.9), label="base"),
        _retrieval_report(items(1.0, 0.1), label="cand", config={"strategy": "dense-rerank"}),
    )

    recall = payload["metrics"]["span_recall_at_k"]
    refusal = payload["metrics"]["refusal_correct_at_threshold"]

    assert recall["sample_size"] == 1
    assert recall["baseline"] == pytest.approx(0.5)
    # 不可答题没有检索指标, 但仍然参与拒答正确率: baseline 误答, candidate 正确拒答
    assert refusal["sample_size"] == 2
    assert refusal["baseline"] == pytest.approx(0.5)
    assert refusal["candidate"] == pytest.approx(1.0)
    assert payload["samples"]["counts"]["not_applicable"] == 1


def test_one_sided_eligibility_drops_both_arms_and_is_counted(tmp_path: Path) -> None:
    baseline = _generation_report(
        [
            _generation_item("item-0", "single_hop"),
            _generation_item("item-1", "single_hop", refused=True, citation_valid=False),
        ],
        label="base",
    )
    candidate = _generation_report(
        [
            _generation_item("item-0", "single_hop", citation_valid=False),
            _generation_item("item-1", "single_hop", refused=False, citation_valid=True),
        ],
        label="cand",
        config={"chat_model": "deepseek-v4-flash"},
    )

    payload = _compare(tmp_path, baseline, candidate)
    validity = payload["metrics"]["citation_validity_non_refusal"]

    # item-1 只在 candidate 侧作答, 两侧一并剔除, 避免拿不同样本比较
    assert validity["sample_size"] == 1
    assert validity["dropped_candidate_only"] == 1
    assert validity["dropped_baseline_only"] == 0
    assert validity["baseline"] == pytest.approx(1.0)
    assert validity["candidate"] == pytest.approx(0.0)


def test_generation_alignment_uses_citation_level_micro_rate(tmp_path: Path) -> None:
    baseline = _generation_report(
        [
            _generation_item("item-0", "single_hop", aligned=1, total=1),
            _generation_item("item-1", "multi_hop", aligned=1, total=9),
        ],
        label="base",
    )
    candidate = _generation_report(
        [
            _generation_item("item-0", "single_hop", aligned=1, total=1),
            _generation_item("item-1", "multi_hop", aligned=5, total=9),
        ],
        label="cand",
        config={"chat_model": "deepseek-v4-flash"},
    )

    alignment = _compare(tmp_path, baseline, candidate)["metrics"]["citation_gold_alignment"]

    assert alignment["baseline"] == pytest.approx(0.2)
    assert alignment["candidate"] == pytest.approx(0.6)


def test_errored_items_are_excluded_from_generation_metrics(tmp_path: Path) -> None:
    baseline = _generation_report(
        [
            _generation_item("item-0", "single_hop"),
            _generation_item("item-1", "single_hop", constraint_passed=False),
        ],
        label="base",
    )
    candidate = _generation_report(
        [
            _generation_item("item-0", "single_hop"),
            _generation_item("item-1", "single_hop", error="timeout"),
        ],
        label="cand",
        config={"chat_model": "deepseek-v4-flash"},
    )

    constraint = _compare(tmp_path, baseline, candidate)["metrics"]["constraint_pass"]

    assert constraint["sample_size"] == 1
    assert constraint["dropped_baseline_only"] == 1


def test_identical_configs_are_flagged_as_a_noise_floor_run(tmp_path: Path) -> None:
    baseline = _retrieval_report(_four_retrieval_items([0.5, 0.5, 1.0, 1.0]), label="run-a")
    candidate = _retrieval_report(_four_retrieval_items([0.5, 1.0, 1.0, 1.0]), label="run-b")

    payload = _compare(tmp_path, baseline, candidate)

    assert payload["compatibility"]["identical_config"] is True
    assert payload["compatibility"]["config_diff"] == {}
    assert "噪声地板" in markdown_report(payload)


def test_config_diff_lists_the_experimental_variable(tmp_path: Path) -> None:
    payload = _compare(
        tmp_path,
        _retrieval_report(_four_retrieval_items([0.5] * 4), label="base"),
        _retrieval_report(
            _four_retrieval_items([0.5] * 4),
            label="cand",
            config={"strategy": "dense-lexical-rrf", "rrf_k": 60},
        ),
    )

    diff = payload["compatibility"]["config_diff"]

    assert diff["strategy"] == {"baseline": "dense-only", "candidate": "dense-lexical-rrf"}
    assert diff["rrf_k"] == {"baseline": None, "candidate": 60}
    assert payload["compatibility"]["controlled_diff"] == []


# ------------------------------------------------------------------ 兼容性校验


def test_compare_rejects_dataset_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="数据集不一致"):
        _compare(
            tmp_path,
            _retrieval_report(_four_retrieval_items([0.5] * 4), label="base"),
            _retrieval_report(
                _four_retrieval_items([0.5] * 4), label="cand", dataset="english-dev"
            ),
        )


def test_compare_rejects_controlled_config_drift_unless_allowed(tmp_path: Path) -> None:
    baseline = _retrieval_report(_four_retrieval_items([0.5] * 4), label="base")
    candidate = _retrieval_report(
        _four_retrieval_items([1.0] * 4), label="cand", config={"top_k": 5, "theta": 0.7}
    )

    with pytest.raises(ValueError, match="受控配置项不一致"):
        _compare(tmp_path, baseline, candidate)

    payload = _compare(tmp_path, baseline, candidate, allow_config_drift=True)

    assert payload["compatibility"]["controlled_diff"] == ["theta", "top_k"]
    assert "--allow-config-drift" in markdown_report(payload) or "受控配置项" in markdown_report(
        payload
    )


def test_compare_rejects_item_set_and_annotation_drift(tmp_path: Path) -> None:
    baseline = _retrieval_report(_four_retrieval_items([0.5] * 4), label="base")
    fewer = _retrieval_report(_four_retrieval_items([0.5] * 4)[:3], label="cand")
    with pytest.raises(ValueError, match="item_id 集合不一致"):
        _compare(tmp_path, baseline, fewer)

    drifted_items = _four_retrieval_items([0.5] * 4)
    drifted_items[2]["category"] = "table"
    with pytest.raises(ValueError, match="标注已漂移"):
        _compare(tmp_path, baseline, _retrieval_report(drifted_items, label="cand"))


def test_compare_rejects_mixed_report_kinds_and_unpairable_reports(tmp_path: Path) -> None:
    retrieval = _retrieval_report(_four_retrieval_items([0.5] * 4), label="base")
    generation = _generation_report([_generation_item("item-0", "single_hop")], label="cand")
    with pytest.raises(ValueError, match="报告类型不一致"):
        _compare(tmp_path, retrieval, generation)

    without_ids = _retrieval_report(_four_retrieval_items([0.5] * 4), label="base")
    for item in without_ids["items"]:
        del item["item_id"]
    with pytest.raises(ValueError, match="缺少 item_id"):
        load_report(_write(tmp_path, "no-ids", without_ids))

    unknown = {"label": "x", "dataset": "core-dev", "config": {}, "items": [{"item_id": "a"}]}
    with pytest.raises(ValueError, match="无法识别报告类型"):
        load_report(_write(tmp_path, "unknown", unknown))


def test_compare_rejects_unknown_primary_metric(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知的 primary metric"):
        _compare(
            tmp_path,
            _retrieval_report(_four_retrieval_items([0.5] * 4), label="base"),
            _retrieval_report(_four_retrieval_items([0.5] * 4), label="cand"),
            primary_metric="f1",
        )


# --------------------------------------------------------------------- 报告输出


def test_markdown_states_the_verdict_and_the_interpretation_limits(tmp_path: Path) -> None:
    payload = _compare(
        tmp_path,
        _retrieval_report(_four_retrieval_items([0.2, 0.4, 0.6, 0.8]), label="base"),
        _retrieval_report(
            _four_retrieval_items([0.4, 0.6, 0.8, 1.0]),
            label="cand",
            config={"strategy": "dense-lexical-rrf"},
        ),
    )

    markdown = markdown_report(payload)

    assert "# 评测对照：base → cand" in markdown
    assert "budget span Recall" in markdown
    assert "显著提升" in markdown
    assert "置信区间跨 0 即无显著差异" in markdown
    assert "| `strategy` | `dense-only` | `dense-lexical-rrf` |" in markdown
    assert "### multi_hop（1 条）" in markdown


def test_cli_writes_json_and_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _write(
        tmp_path, "base", _retrieval_report(_four_retrieval_items([0.5] * 4), label="base")
    )
    candidate = _write(
        tmp_path,
        "cand",
        _retrieval_report(
            _four_retrieval_items([1.0] * 4), label="cand", config={"strategy": "dense-rerank"}
        ),
    )
    output_dir = tmp_path / "compare-run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.compare",
            str(baseline),
            str(candidate),
            "--output-dir",
            str(output_dir),
            "--resamples",
            str(RESAMPLES),
            "--seed",
            str(SEED),
        ],
    )

    main()

    payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["bootstrap"] == {
        "method": "paired percentile bootstrap",
        "seed": SEED,
        "resamples": RESAMPLES,
        "ci_level": 0.95,
    }
    assert payload["metrics"]["budget_span_recall"]["delta"] == pytest.approx(0.5)
    assert (output_dir / "report.md").read_text(encoding="utf-8").startswith("# 评测对照")

    # 已存在的输出目录不允许被覆盖, 跑批结果只增不改
    with pytest.raises(FileExistsError):
        main()


def test_cli_accepts_a_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    _write(run_dir, "report", _retrieval_report(_four_retrieval_items([0.5] * 4), label="base"))

    report = load_report(run_dir)

    assert report.payload["label"] == "base"
