"""多策略配对矩阵的独立 fixture 测试。

报告全部在测试内构造：真实跑批会随策略迭代变化，拿它当 fixture 会让测试
在无关改动上红，也测不到 fail-closed 分支（真实报告本来就是合法的）。
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from eval.report_metrics import RETRIEVAL_METRICS, load_report
from eval.strategy_matrix import (
    ValidationError,
    build_matrix_report,
    build_runs,
    main,
    markdown_report,
)

RESAMPLES = 200
SEED = 11
VERSION = "0199ffff-0000-7000-8000-00000000000a"


def _chunk(start: int, end: int, *, tokens: int = 100, version: str = VERSION) -> dict[str, Any]:
    return {
        "chunk_id": f"chunk-{version[-1]}-{start}-{end}",
        "version_id": version,
        "document_id": "doc-1",
        "char_start": start,
        "char_end": end,
        "content_tokens": tokens,
        "score": 0.5,
    }


def _span(start: int = 100, end: int = 200, *, version: str = VERSION) -> dict[str, Any]:
    return {
        "span_index": 0,
        "version_id": version,
        "char_start": start,
        "char_end": end,
        "quote": "gold",
        "status": "hit",
    }


def _retrieval_item(
    item_id: str,
    category: str = "single_hop",
    *,
    answerable: bool = True,
    recall: float = 1.0,
    top_score: float = 0.8,
    latency_ms: int = 10,
    chunks: list[dict[str, Any]] | None = None,
    spans: list[dict[str, Any]] | None = None,
    retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scores = retrieval or {
        "span_recall_at_k": recall,
        "budget_span_recall": recall,
        "ndcg_at_k": recall,
        "alpha_ndcg_at_k": recall,
        "mrr": recall,
        "context_precision": recall / 10,
        "retrieved_tokens": 1000,
        "relevant_chunks": 1,
    }
    return {
        "item_id": item_id,
        "category": category,
        "question": f"问题 {item_id}",
        "answerable": answerable,
        "top_score": top_score,
        "latency_ms": latency_ms,
        "retrieval": scores if answerable else None,
        "retrieved": chunks if chunks is not None else [_chunk(0, 500)],
        "span_diagnostics": (spans if spans is not None else [_span()]) if answerable else [],
    }


def _retrieval_report(
    items: list[dict[str, Any]],
    *,
    label: str,
    dataset: str = "core-dev",
    config: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = {
        "strategy": "dense-only",
        "origin": "human",
        "top_k": 10,
        "token_budget": 4000,
        "theta": 0.5,
        "alpha": 0.5,
        "refusal_threshold": 0.35,
        **(config or {}),
    }
    return {
        "run_id": f"run-{label}",
        "dataset": dataset,
        "label": label,
        "git_sha": "0" * 40,
        "config": merged,
        "config_hash": f"hash-{sorted(merged.items())}",
        "metrics": metrics or {},
        "items": items,
    }


def _generation_item(
    item_id: str,
    category: str = "single_hop",
    *,
    answerable: bool = True,
    refused: bool = False,
    constraint_passed: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": category,
        "question": f"问题 {item_id}",
        "answerable": answerable,
        "answer": "答案",
        "citations": [],
        "refused": refused,
        "refusal_correct": True,
        "citation_validity": {"valid": True},
        "constraint_pass": {"passed": constraint_passed},
        "citation_gold_alignment": {"aligned": 1, "total": 1},
        "latency_ms": 1500,
        "error": None,
        **(extra or {}),
    }


def _generation_report(
    items: list[dict[str, Any]], *, label: str, dataset: str = "core-dev"
) -> dict[str, Any]:
    config = {"strategy": "dense-only-generation", "origin": "human", "top_k": 5, "theta": 0.5}
    return {
        "run_id": f"gen-{label}",
        "dataset": dataset,
        "label": label,
        "git_sha": "1" * 40,
        "config": config,
        "config_hash": f"gen-hash-{label}",
        "metrics": {"error_count": 0},
        "items": items,
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _runs(
    tmp_path: Path,
    reports: dict[str, dict[str, Any]],
    generations: dict[str, dict[str, Any]] | None = None,
) -> list[Any]:
    run_paths = [(name, _write(tmp_path, name, payload)) for name, payload in reports.items()]
    generation_paths = [
        (name, _write(tmp_path, f"gen-{name}", payload))
        for name, payload in (generations or {}).items()
    ]
    return build_runs(run_paths, generation_paths)


def _matrix(
    tmp_path: Path,
    reports: dict[str, dict[str, Any]],
    *,
    generations: dict[str, dict[str, Any]] | None = None,
    baseline: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    runs = _runs(tmp_path, reports, generations)
    return build_matrix_report(
        runs,
        baseline=baseline or runs[0].name,
        seed=SEED,
        resamples=RESAMPLES,
        **kwargs,
    )


_CATEGORIES = ("single_hop", "single_hop", "multi_hop", "table")


def _strategy_reports(recalls: dict[str, list[float]]) -> dict[str, dict[str, Any]]:
    """每个策略一份检索报告，item 数由 recall 列表长度决定。"""
    return {
        name: _retrieval_report(
            [
                _retrieval_item(
                    f"item-{index}",
                    _CATEGORIES[index % len(_CATEGORIES)],
                    recall=recall,
                )
                for index, recall in enumerate(values)
            ],
            label=name,
            config={"chunk_strategy": name},
        )
        for name, values in recalls.items()
    }


VARY = {"vary_keys": ["chunk_strategy"]}


# ------------------------------------------------------------------ 指标与统计


def test_matrix_reports_absolute_values_deltas_and_win_loss_tie(tmp_path: Path) -> None:
    payload = _matrix(
        tmp_path,
        _strategy_reports(
            {
                "fixed": [0.0, 0.5, 1.0, 1.0],
                "heading": [1.0, 0.5, 1.0, 1.0],
                "semantic": [0.0, 0.0, 1.0, 1.0],
            }
        ),
        **VARY,
    )
    metric = payload["metrics"]["retrieval"]["budget_span_recall"]

    assert payload["baseline_strategy"] == "fixed"
    assert metric["sample_size"] == 4
    assert metric["by_strategy"] == {
        "fixed": pytest.approx(0.625),
        "heading": pytest.approx(0.875),
        "semantic": pytest.approx(0.5),
    }
    heading = metric["vs_baseline"]["heading"]
    semantic = metric["vs_baseline"]["semantic"]
    assert heading["delta"] == pytest.approx(0.25)
    assert (heading["wins"], heading["losses"], heading["ties"]) == (1, 0, 3)
    assert (semantic["wins"], semantic["losses"], semantic["ties"]) == (0, 1, 3)
    assert heading["crosses_zero"] is (heading["ci_low"] <= 0 <= heading["ci_high"])


def test_all_strategies_share_one_resampling_universe(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [0.2, 0.4, 0.6, 0.8], "b": [0.4, 0.6, 0.8, 1.0]})

    first = _matrix(tmp_path, reports, **VARY)["metrics"]["retrieval"]["mrr"]
    second = _matrix(tmp_path, reports, **VARY)["metrics"]["retrieval"]["mrr"]
    other_seed = build_matrix_report(
        _runs(tmp_path, reports), baseline="a", seed=SEED + 1, resamples=RESAMPLES, **VARY
    )["metrics"]["retrieval"]["mrr"]

    assert first["vs_baseline"]["b"]["ci_low"] == second["vs_baseline"]["b"]["ci_low"]
    assert first["vs_baseline"]["b"]["ci_high"] == second["vs_baseline"]["b"]["ci_high"]
    # 逐样本 Δ 恒定时任何种子都给同一个区间, 这里用有差异的样本确认种子确实起作用
    assert other_seed["vs_baseline"]["b"]["delta"] == first["vs_baseline"]["b"]["delta"]


def test_identical_strategies_are_all_tie_and_inconclusive(tmp_path: Path) -> None:
    payload = _matrix(
        tmp_path,
        _strategy_reports({"a": [0.5, 1.0, 0.0, 0.5], "b": [0.5, 1.0, 0.0, 0.5]}),
        **VARY,
    )
    metric = payload["metrics"]["retrieval"]["ndcg_at_k"]["vs_baseline"]["b"]

    assert metric["delta"] == 0.0
    assert (metric["ci_low"], metric["ci_high"]) == (0.0, 0.0)
    assert metric["crosses_zero"] is True
    assert metric["verdict"] == "inconclusive"
    assert (metric["wins"], metric["losses"], metric["ties"]) == (0, 0, 4)


def test_common_eligible_sample_is_the_intersection(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0, 1.0], "b": [1.0, 1.0]})
    # b 在 item-1 上没有检索指标: 该样本从两个策略里一起剔除
    reports["b"]["items"][1]["retrieval"] = None
    reports["b"]["items"][1]["answerable"] = False
    reports["a"]["items"][1]["answerable"] = False
    reports["a"]["items"][1]["retrieval"] = None

    payload = _matrix(tmp_path, reports, **VARY)
    recall = payload["metrics"]["retrieval"]["span_recall_at_k"]
    tokens = payload["metrics"]["retrieval"]["retrieved_tokens"]

    assert recall["sample_size"] == 1
    assert recall["own_ineligible"] == {"a": 1, "b": 1}
    # 不可答题没有检索指标, 但仍然计入成本与延迟
    assert tokens["sample_size"] == 2


def test_one_sided_eligibility_removes_the_item_from_every_strategy(
    tmp_path: Path,
) -> None:
    reports = _strategy_reports({"a": [1.0, 1.0], "b": [1.0, 1.0], "c": [1.0, 1.0]})
    reports["c"]["items"][0]["retrieved"] = []

    payload = _matrix(tmp_path, reports, **VARY)
    tokens = payload["metrics"]["retrieval"]["retrieved_tokens"]

    assert tokens["sample_size"] == 1
    assert tokens["own_ineligible"] == {"a": 0, "b": 0, "c": 1}


def test_context_redundancy_counts_overlap_within_a_version(tmp_path: Path) -> None:
    other_version = "0199ffff-0000-7000-8000-00000000000b"
    reports = _strategy_reports({"no-overlap": [1.0], "overlap": [1.0]})
    reports["no-overlap"]["items"][0]["retrieved"] = [
        _chunk(0, 100),
        _chunk(100, 200),
        _chunk(0, 100, version=other_version),
    ]
    # 同 version 内 50 字符重叠; 跨 version 的相同区间不算重叠
    reports["overlap"]["items"][0]["retrieved"] = [_chunk(0, 100), _chunk(50, 150)]

    payload = _matrix(tmp_path, reports, **VARY)
    redundancy = payload["metrics"]["retrieval"]["context_redundancy"]

    assert redundancy["by_strategy"]["no-overlap"] == pytest.approx(0.0)
    assert redundancy["by_strategy"]["overlap"] == pytest.approx(50 / 200)
    assert redundancy["higher_is_better"] is False


def test_retrieved_tokens_respects_top_k(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    chunks = [_chunk(index * 10, index * 10 + 10, tokens=50) for index in range(5)]
    reports["a"]["items"][0]["retrieved"] = chunks
    reports["b"]["items"][0]["retrieved"] = chunks
    reports["a"]["config"]["top_k"] = 2
    reports["b"]["config"]["top_k"] = 2

    tokens = _matrix(tmp_path, reports, **VARY)["metrics"]["retrieval"]["retrieved_tokens"]

    assert tokens["by_strategy"] == {"a": pytest.approx(100.0), "b": pytest.approx(100.0)}


def test_missing_cost_fields_are_unavailable_not_zero(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    generations = {
        name: _generation_report([_generation_item("item-0")], label=name) for name in reports
    }

    payload = _matrix(tmp_path, reports, generations=generations, **VARY)
    cost = payload["metrics"]["generation"]["cost_usd"]

    assert cost["status"] == "unavailable"
    assert cost["by_strategy"] == {"a": None, "b": None}
    assert cost["vs_baseline"] == {}
    assert "没有记录" in str(cost["reason"])
    assert "generation.cost_usd" in payload["unavailable_metrics"]


def test_cost_metric_is_computed_when_the_run_exports_it(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    generations = {
        "a": _generation_report([_generation_item("item-0", extra={"cost_usd": 0.002})], label="a"),
        "b": _generation_report([_generation_item("item-0", extra={"cost_usd": 0.001})], label="b"),
    }

    cost = _matrix(tmp_path, reports, generations=generations, **VARY)["metrics"]["generation"][
        "cost_usd"
    ]

    assert cost["status"] == "ok"
    assert cost["by_strategy"]["b"] == pytest.approx(0.001)
    # 成本越低越好: 下降应判成提升
    assert cost["vs_baseline"]["b"]["delta"] == pytest.approx(-0.001)
    assert cost["vs_baseline"]["b"]["verdict"] == "improved"


def test_end_to_end_quality_metrics_come_from_generation_reports(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0, 1.0], "b": [1.0, 1.0]})
    generations = {
        "a": _generation_report(
            [
                _generation_item("item-0"),
                _generation_item("item-1", constraint_passed=False),
            ],
            label="a",
        ),
        "b": _generation_report(
            [_generation_item("item-0"), _generation_item("item-1")], label="b"
        ),
    }

    constraint = _matrix(tmp_path, reports, generations=generations, **VARY)["metrics"][
        "generation"
    ]["constraint_pass"]

    assert constraint["by_strategy"] == {"a": pytest.approx(0.5), "b": pytest.approx(1.0)}
    assert constraint["vs_baseline"]["b"]["wins"] == 1


def test_category_slices_and_outliers(tmp_path: Path) -> None:
    payload = _matrix(
        tmp_path,
        _strategy_reports(
            {
                "a": [1.0, 1.0, 0.0, 1.0],
                "b": [0.0, 1.0, 0.0, 1.0],
            }
        ),
        **VARY,
    )
    categories = payload["by_category"]
    outliers = payload["outliers"]

    assert categories["single_hop"]["item_count"] == 2
    assert categories["multi_hop"]["metrics"]["budget_span_recall"]["by_strategy"] == {
        "a": 0.0,
        "b": 0.0,
    }
    assert outliers["counts"] == {"divergent": 1, "universal_zero": 1, "universal_max": 2}
    assert outliers["divergent"][0]["item_id"] == "item-0"
    assert outliers["divergent"][0]["spread"] == pytest.approx(1.0)
    assert outliers["universal_zero"][0]["item_id"] == "item-2"


def test_unanswerable_only_slice_is_reported_as_unavailable(tmp_path: Path) -> None:
    reports = {
        name: _retrieval_report(
            [
                _retrieval_item("item-0", "single_hop"),
                _retrieval_item("item-1", "unanswerable", answerable=False),
            ],
            label=name,
            config={"chunk_strategy": name},
        )
        for name in ("a", "b")
    }

    payload = _matrix(tmp_path, reports, **VARY)
    slice_metric = payload["by_category"]["unanswerable"]["metrics"]["budget_span_recall"]

    assert slice_metric["status"] == "unavailable"
    assert slice_metric["by_strategy"] == {"a": None, "b": None}


def test_single_item_dataset_still_produces_a_degenerate_interval(tmp_path: Path) -> None:
    payload = _matrix(tmp_path, _strategy_reports({"a": [0.0], "b": [1.0]}), **VARY)
    metric = payload["metrics"]["retrieval"]["budget_span_recall"]["vs_baseline"]["b"]

    assert metric["sample_size"] == 1
    assert (metric["ci_low"], metric["ci_high"]) == (1.0, 1.0)
    assert metric["crosses_zero"] is False
    assert metric["verdict"] == "improved"


# --------------------------------------------------------------------- 错误输入


def test_rejects_dataset_mismatch(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    reports["b"]["dataset"] = "english-dev"
    with pytest.raises(ValidationError, match="数据集不一致"):
        _matrix(tmp_path, reports, **VARY)


def test_rejects_missing_and_duplicate_items(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0, 1.0], "b": [1.0, 1.0]})
    reports["b"]["items"] = reports["b"]["items"][:1]
    with pytest.raises(ValidationError, match="item 集合不一致"):
        _matrix(tmp_path, reports, **VARY)

    duplicated = _strategy_reports({"a": [1.0, 1.0], "b": [1.0, 1.0]})
    duplicated["b"]["items"][1]["item_id"] = "item-0"
    with pytest.raises(ValidationError, match="重复 item_id"):
        _matrix(tmp_path, duplicated, **VARY)


def test_rejects_gold_span_drift_and_mixed_parse_versions(tmp_path: Path) -> None:
    drifted = _strategy_reports({"a": [1.0], "b": [1.0]})
    drifted["b"]["items"][0]["span_diagnostics"] = [_span(120, 220)]
    with pytest.raises(ValidationError, match="gold span 指纹"):
        _matrix(tmp_path, drifted, **VARY)

    mixed = _strategy_reports({"a": [1.0], "b": [1.0]})
    mixed["b"]["items"][0]["span_diagnostics"] = [
        _span(version="0199ffff-0000-7000-8000-00000000000c")
    ]
    with pytest.raises(ValidationError, match="混了解析版本"):
        _matrix(tmp_path, mixed, **VARY)


def test_rejects_annotation_drift_in_category_or_answerability(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    reports["b"]["items"][0]["category"] = "table"
    with pytest.raises(ValidationError, match="标注已漂移"):
        _matrix(tmp_path, reports, **VARY)


def test_rejects_failed_runs(tmp_path: Path) -> None:
    errored = _strategy_reports({"a": [1.0], "b": [1.0]})
    errored["b"]["items"][0]["error"] = "timeout"
    with pytest.raises(ValidationError, match="含失败样本"):
        _matrix(tmp_path, errored, **VARY)

    counted = _strategy_reports({"a": [1.0], "b": [1.0]})
    counted["b"]["metrics"] = {"error_count": 2}
    with pytest.raises(ValidationError, match="含失败样本"):
        _matrix(tmp_path, counted, **VARY)

    incomplete = _strategy_reports({"a": [1.0], "b": [1.0]})
    incomplete["b"]["items"][0]["retrieval"] = None
    with pytest.raises(ValidationError, match="没有检索指标"):
        _matrix(tmp_path, incomplete, **VARY)


def test_rejects_mixed_retrieval_config_unless_declared(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    reports["b"]["config"]["top_k"] = 5

    with pytest.raises(ValidationError, match="检索配置不一致"):
        _matrix(tmp_path, reports, **VARY)

    payload = _matrix(tmp_path, reports, vary_keys=["chunk_strategy", "top_k"])
    assert payload["validation"]["config_diff"]["b"]["top_k"] == {
        "baseline": 10,
        "candidate": 5,
    }


def test_rejects_partial_generation_coverage(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    generations = {"a": _generation_report([_generation_item("item-0")], label="a")}
    with pytest.raises(ValidationError, match="必须覆盖全部策略"):
        _matrix(tmp_path, reports, generations=generations, **VARY)


def test_rejects_bad_strategy_arguments(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})

    with pytest.raises(ValidationError, match="至少需要两个策略"):
        build_matrix_report(_runs(tmp_path, {"a": reports["a"]}), baseline="a", resamples=RESAMPLES)
    with pytest.raises(ValidationError, match="不在参与对照的策略里"):
        _matrix(tmp_path, reports, baseline="missing", **VARY)
    with pytest.raises(ValidationError, match="未知的 primary metric"):
        _matrix(tmp_path, reports, primary_metric="f1", **VARY)


def test_rejects_wrong_report_kind(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    generation_as_run = _generation_report([_generation_item("item-0")], label="wrong")
    with pytest.raises(ValidationError, match="必须是检索跑批报告"):
        _matrix(tmp_path, {"a": reports["a"], "b": generation_as_run}, **VARY)

    with pytest.raises(ValidationError, match="必须是生成跑批报告"):
        _matrix(
            tmp_path,
            reports,
            generations={name: reports[name] for name in reports},
            **VARY,
        )


def test_rejects_a_primary_metric_without_any_comparable_sample(tmp_path: Path) -> None:
    reports = {
        name: _retrieval_report(
            [_retrieval_item("item-0", "unanswerable", answerable=False)],
            label=name,
            config={"chunk_strategy": name},
        )
        for name in ("a", "b")
    }
    with pytest.raises(ValidationError, match="没有任何公共可比样本"):
        _matrix(tmp_path, reports, **VARY)


def test_build_runs_validates_generation_names(tmp_path: Path) -> None:
    reports = _strategy_reports({"a": [1.0], "b": [1.0]})
    paths = [(name, _write(tmp_path, name, payload)) for name, payload in reports.items()]
    with pytest.raises(ValidationError, match="不在 --run 中"):
        build_runs(paths, [("c", paths[0][1])])
    with pytest.raises(ValidationError, match="重复的策略名"):
        build_runs(paths, [("a", paths[0][1]), ("a", paths[0][1])])


# ----------------------------------------------------------------------- 报告


def test_markdown_carries_sample_size_baseline_verdict_and_outliers(
    tmp_path: Path,
) -> None:
    payload = _matrix(
        tmp_path,
        _strategy_reports({"fixed": [0.0, 0.0, 0.0, 0.0], "heading": [1.0, 1.0, 1.0, 1.0]}),
        **VARY,
    )

    markdown = markdown_report(payload)

    assert "# 策略对照矩阵：core-dev · 2 策略" in markdown  # noqa: RUF001
    assert "配对样本：4 条（可答 4，不可答 0）" in markdown  # noqa: RUF001
    assert "| `fixed`（基线）" in markdown  # noqa: RUF001
    assert "| 检查项 | 结果 |" in markdown
    assert "gold span 指纹一致" in markdown
    assert "| 策略 | Δ | 95% CI | 跨零 | 判定 | 胜 | 负 | 平 |" in markdown
    assert "显著提升" in markdown
    assert "置信区间跨 0 即无显著差异" in markdown
    assert "所有策略都未命中" in markdown


def test_cli_writes_json_and_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _strategy_reports({"fixed": [0.5, 1.0], "heading": [1.0, 1.0]})
    paths = {name: _write(tmp_path, name, payload) for name, payload in reports.items()}
    output_dir = tmp_path / "matrix-run"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.strategy_matrix",
            "--run",
            f"fixed={paths['fixed']}",
            "--run",
            f"heading={paths['heading']}",
            "--baseline",
            "fixed",
            "--vary-key",
            "chunk_strategy",
            "--resamples",
            str(RESAMPLES),
            "--seed",
            str(SEED),
            "--output-dir",
            str(output_dir),
        ],
    )

    main()

    payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["strategies"] == ["fixed", "heading"]
    assert payload["bootstrap"]["seed"] == SEED
    assert (output_dir / "report.md").read_text(encoding="utf-8").startswith("# 策略对照矩阵")

    # 输出目录不可覆盖: 跑批结果只增不改
    with pytest.raises(FileExistsError):
        main()


def test_chunk_runner_manifest_flows_into_four_strategy_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = _strategy_reports(
        {
            "fixed": [0.5, 1.0],
            "heading": [1.0, 1.0],
            "recursive": [0.5, 0.5],
            "semantic": [1.0, 0.5],
        }
    )
    manifest_runs = {}
    for name, report in reports.items():
        report["config"]["chunk_metadata"] = {
            "corpus_fingerprint": "same-corpus",
            "summary": {"strategy": name},
        }
        report_path = _write(tmp_path, name, report)
        manifest_runs[name] = {
            "run_id": f"run-{name}",
            "reused": False,
            "report": str(report_path),
        }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"runs": manifest_runs}, ensure_ascii=False), encoding="utf-8"
    )
    output_dir = tmp_path / "manifest-matrix"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.strategy_matrix",
            "--manifest",
            str(manifest_path),
            "--resamples",
            str(RESAMPLES),
            "--seed",
            str(SEED),
            "--output-dir",
            str(output_dir),
        ],
    )

    main()

    payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["strategies"] == ["fixed", "heading", "recursive", "semantic"]
    assert payload["baseline_strategy"] == "heading"
    assert payload["validation"]["vary_keys"] == [
        "chunk_strategy",
        "chunk_metadata",
    ]


def test_cli_rejects_malformed_run_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval.strategy_matrix", "--run", "no-equals-sign", "--output-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit):
        main()


def test_run_directory_is_accepted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    _write(run_dir, "report", _retrieval_report([_retrieval_item("item-0")], label="a"))

    assert load_report(run_dir).payload["label"] == "a"


def test_every_declared_metric_is_extractable(tmp_path: Path) -> None:
    """新增指标忘记接进矩阵会在这里被抓到。"""
    payload = _matrix(tmp_path, _strategy_reports({"a": [1.0], "b": [1.0]}), **VARY)

    assert set(payload["metrics"]["retrieval"]) == {spec.name for spec in RETRIEVAL_METRICS}


# ------------------------------------------------- E1 生成轨接入矩阵的 fail-closed


def _chunk_generation_reports(
    *, constraint_passed: dict[str, bool] | None = None
) -> dict[str, dict[str, Any]]:
    """四套分块策略的生成报告, 除 chunk_strategy 外配置逐字相同。"""
    passed = constraint_passed or {}
    reports: dict[str, dict[str, Any]] = {}
    for name in ("fixed", "heading", "recursive", "semantic"):
        report = _generation_report(
            [
                _generation_item(
                    "item-0",
                    constraint_passed=passed.get(name, True),
                    extra={"span_diagnostics": [_span()], "total_tokens": 900},
                ),
                _generation_item(
                    "item-1", extra={"span_diagnostics": [_span()], "total_tokens": 900}
                ),
            ],
            label=name,
        )
        report["config"].update(
            {
                "chunk_strategy": name,
                "chunk_metadata": {"corpus_fingerprint": "same-corpus"},
                "prompt_fingerprint": "p" * 64,
                "chat_model": "qwen",
                "answer_max_tokens": 1200,
            }
        )
        reports[name] = report
    return reports


def test_generation_manifest_flows_into_the_four_strategy_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _strategy_reports(
        {
            "fixed": [0.5, 1.0],
            "heading": [1.0, 1.0],
            "recursive": [0.5, 0.5],
            "semantic": [1.0, 0.5],
        }
    )
    generation = _chunk_generation_reports(constraint_passed={"fixed": False})

    def _manifest(name: str, reports: dict[str, dict[str, Any]]) -> Path:
        runs = {}
        for strategy, report in reports.items():
            report["config"].setdefault("chunk_metadata", {"corpus_fingerprint": "same-corpus"})
            path = _write(tmp_path, f"{name}-{strategy}", report)
            runs[strategy] = {"run_id": f"{name}-{strategy}", "report": str(path)}
        manifest_path = tmp_path / f"{name}-manifest.json"
        manifest_path.write_text(json.dumps({"runs": runs}, ensure_ascii=False), encoding="utf-8")
        return manifest_path

    output_dir = tmp_path / "e2e-matrix"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.strategy_matrix",
            "--manifest",
            str(_manifest("retrieval", retrieval)),
            "--generation-manifest",
            str(_manifest("generation", generation)),
            "--resamples",
            str(RESAMPLES),
            "--seed",
            str(SEED),
            "--output-dir",
            str(output_dir),
        ],
    )

    main()

    payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    assert set(metrics) == {"retrieval", "generation"}
    # 端到端指标确实来自生成报告, 而不是被检索轨顶替
    assert metrics["generation"]["constraint_pass"]["by_strategy"]["fixed"] == 0.5
    assert metrics["generation"]["constraint_pass"]["by_strategy"]["heading"] == 1.0
    # 逐条 token 已导出 ⇒ 成本代理指标可用, 不再是 "unavailable"
    assert metrics["generation"]["total_tokens"]["status"] == "ok"
    names = {check["name"] for check in payload["validation"]["checks"]}
    assert any(name.startswith("生成配置一致") for name in names)
    assert "生成报告的 chunk_strategy 与策略名一致" in names
    assert "generation gold span 指纹一致" in names


def test_matrix_rejects_generation_reports_that_drifted_apart(tmp_path: Path) -> None:
    retrieval = _strategy_reports({name: [1.0, 1.0] for name in ("fixed", "heading")})

    mutations: list[dict[str, Any]] = [
        {"answer_max_tokens": 4096},
        {"prompt_fingerprint": "q" * 64},
        {"chat_model": "另一个模型"},
    ]
    for index, mutation in enumerate(mutations):
        generation = _chunk_generation_reports()
        pair = {"fixed": generation["fixed"], "heading": generation["heading"]}
        pair["fixed"]["config"].update(mutation)
        workdir = tmp_path / f"drift-{index}"
        workdir.mkdir()
        with pytest.raises(ValidationError, match="生成配置不一致"):
            _matrix(workdir, retrieval, generations=pair, **VARY)


def test_matrix_rejects_a_generation_report_hung_on_the_wrong_strategy(
    tmp_path: Path,
) -> None:
    retrieval = _strategy_reports({name: [1.0, 1.0] for name in ("fixed", "heading")})
    generation = _chunk_generation_reports()
    pair = {"fixed": generation["fixed"], "heading": generation["heading"]}
    # semantic 的报告被挂在 fixed 名下: 不拦下来, 四策略结论会整体错位
    pair["fixed"]["config"]["chunk_strategy"] = "semantic"

    with pytest.raises(ValidationError, match="报告挂错了位置"):
        _matrix(tmp_path, retrieval, generations=pair, **VARY)


def test_matrix_rejects_generation_reports_with_drifted_gold_spans(tmp_path: Path) -> None:
    retrieval = _strategy_reports({name: [1.0, 1.0] for name in ("fixed", "heading")})
    generation = _chunk_generation_reports()
    pair = {"fixed": generation["fixed"], "heading": generation["heading"]}
    # 重标过: 同一条 item 的 gold 区间变了, 两轨比较的已经不是同一批标注
    pair["fixed"]["items"][0]["span_diagnostics"] = [_span(300, 400)]

    with pytest.raises(ValidationError, match="gold span 指纹"):
        _matrix(tmp_path, retrieval, generations=pair, **VARY)


def test_four_chunk_strategy_matrix_demands_labelled_generation_reports(
    tmp_path: Path,
) -> None:
    """四个策略名恰好是四套分块时, 生成报告必须自证跑在哪套 chunk 上。"""
    retrieval = _strategy_reports(
        {"fixed": [1.0], "heading": [1.0], "recursive": [1.0], "semantic": [1.0]}
    )
    generation = _chunk_generation_reports()
    for report in generation.values():
        report["config"].pop("chunk_strategy")

    with pytest.raises(ValidationError, match="要求生成报告记录 chunk_strategy"):
        _matrix(tmp_path, retrieval, generations=generation, **VARY)
