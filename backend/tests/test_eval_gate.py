"""夜间门禁的 fixture 测试（eval/gate.py，见 docs/06-评测体系.md §4.2）。

报告全部在测试内构造，不读 eval/outputs 下的真实跑批——真实报告会随策略迭代变化，
拿它当 fixture 会让测试在无关改动上红。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.gate import (
    COST_MAX_INCREASE,
    GateRefused,
    build_snapshot,
    evaluate,
    main,
    render_markdown,
)
from eval.report_metrics import load_report

# --------------------------------------------------------------------- fixtures


def _retrieval_item(
    item_id: str,
    *,
    category: str = "single_hop",
    answerable: bool = True,
    recall: float = 1.0,
    tokens: int = 1000,
    quote: str = "原文片段",
    error: str | None = None,
) -> dict[str, Any]:
    retrieval = (
        {
            "span_recall_at_k": recall,
            "budget_span_recall": recall,
            "ndcg_at_k": recall,
            "alpha_ndcg_at_k": recall,
            "mrr": recall,
            "context_precision": recall / 10,
            "retrieved_tokens": tokens,
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
        "top_score": 0.8 if answerable else 0.1,
        "latency_ms": 10,
        "retrieval": retrieval,
        "retrieved": [
            {
                "chunk_id": f"chunk-{item_id}",
                "version_id": "version-1",
                "document_id": "doc-1",
                "source_uri": "私人论文.md",
                "char_start": 0,
                "char_end": 100,
                "content_tokens": tokens,
                "chunk_strategy": "heading",
            }
        ],
        "span_diagnostics": [
            {
                "version_id": "version-1",
                "char_start": 0,
                "char_end": 10,
                "quote": quote,
            }
        ],
        "error": error,
    }


def _retrieval_report(
    items: list[dict[str, Any]],
    *,
    label: str = "run",
    dataset: str = "core-dev",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = {
        "strategy": "dense-lexical-rrf",
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
        "config_hash": f"hash-{label}",
        "metrics": {},
        "items": items,
    }


def _generation_item(
    item_id: str,
    *,
    passed: bool = True,
    total_tokens: int = 1000,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "category": "single_hop",
        "question": f"问题 {item_id}",
        "gold_answer": "标准答案原文",
        "answer": "模型答案原文",
        "answerable": True,
        "citations": [{"citation_id": "S1", "title": "私人论文标题"}],
        "refused": False,
        "refusal_correct": True,
        "citation_validity": {"valid": True, "citation_count": 1, "issues": []},
        "constraint_pass": {"passed": passed, "issues": []},
        "citation_gold_alignment": {"aligned": 1 if passed else 0, "total": 1},
        "latency_ms": 1500,
        "total_tokens": total_tokens,
        "cost_usd": None,
        "span_diagnostics": [{"version_id": "v1", "char_start": 0, "char_end": 5, "quote": "原文"}],
        "error": None,
    }


def _generation_report(items: list[dict[str, Any]], *, label: str = "gen") -> dict[str, Any]:
    return {
        "run_id": f"run-{label}",
        "dataset": "core-dev",
        "label": label,
        "git_sha": "1" * 40,
        "config": {"origin": "human", "top_k": 5, "theta": 0.5},
        "config_hash": f"hash-{label}",
        "metrics": {},
        "items": items,
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _snapshot_file(tmp_path: Path, report: dict[str, Any], name: str = "baseline") -> Path:
    snapshot = build_snapshot(load_report(_write(tmp_path, f"{name}-src", report)))
    return _write(tmp_path, name, snapshot)


def _evaluate(tmp_path: Path, baseline: dict[str, Any], candidate: dict[str, Any]) -> Any:
    return evaluate(
        load_report(_snapshot_file(tmp_path, baseline)),
        load_report(_write(tmp_path, "candidate", candidate)),
    )


# ------------------------------------------------------------------ 快照与约束 7


def test_snapshot_carries_no_source_text(tmp_path: Path) -> None:
    """快照要提交进 git，绝不能带原文或私人文件名（CLAUDE.md 约束 7）。"""
    report = _generation_report([_generation_item("a"), _generation_item("b")])
    snapshot = build_snapshot(load_report(_write(tmp_path, "gen", report)))

    blob = json.dumps(snapshot, ensure_ascii=False)
    for leaked in ("模型答案原文", "标准答案原文", "私人论文标题", "问题 a", "原文"):
        assert leaked not in blob


def test_retrieval_snapshot_drops_document_identity(tmp_path: Path) -> None:
    report = _retrieval_report([_retrieval_item("a")])
    snapshot = build_snapshot(load_report(_write(tmp_path, "ret", report)))

    blob = json.dumps(snapshot, ensure_ascii=False)
    assert "私人论文.md" not in blob
    assert "doc-1" not in blob
    # 但检索侧成本代理要能继续算，所以 token 数与字符区间保留
    assert snapshot["items"][0]["retrieved"][0]["content_tokens"] == 1000


def test_snapshot_refuses_a_run_with_failed_items(tmp_path: Path) -> None:
    report = _retrieval_report([_retrieval_item("a"), _retrieval_item("b", error="超时")])

    with pytest.raises(GateRefused, match="含失败样本"):
        build_snapshot(load_report(_write(tmp_path, "ret", report)))


def test_snapshot_is_loadable_and_keeps_its_kind(tmp_path: Path) -> None:
    """快照裁过字段还必须能被 load_report 当成同类报告读回来，否则门禁根本跑不起来。"""
    for report, kind in (
        (_retrieval_report([_retrieval_item("a")]), "retrieval"),
        (_generation_report([_generation_item("a")]), "generation"),
    ):
        loaded = load_report(_snapshot_file(tmp_path, report, name=f"snap-{kind}"))
        assert loaded.kind == kind


# ----------------------------------------------------------------------- 判定


def test_identical_run_passes(tmp_path: Path) -> None:
    items = [_retrieval_item("a"), _retrieval_item("b", recall=0.5)]
    outcome = _evaluate(tmp_path, _retrieval_report(items), _retrieval_report(items))

    assert outcome.passed
    assert not outcome.violations


def test_aggregate_regression_is_blocked(tmp_path: Path) -> None:
    baseline = _retrieval_report([_retrieval_item("a"), _retrieval_item("b")])
    candidate = _retrieval_report([_retrieval_item("a"), _retrieval_item("b", recall=0.0)])

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert not outcome.passed
    regressed = {item.metric for item in outcome.violations if item.rule == "no_regression"}
    assert "span_recall_at_k" in regressed


def test_per_sample_churn_alone_does_not_block(tmp_path: Path) -> None:
    """一条掉一条涨、聚合上升时必须放行。

    阻断条件刻意定在**聚合**而不是逐样本：最初写成"任何一条样本回退即阻断"，
    拿真实的 E1 semantic 一试就把净收益的改动也拦下来了。
    天天误报的门禁最后会被习惯性忽略（docs/06 §4.3），比没有门禁更糟。
    """
    baseline = _retrieval_report(
        [_retrieval_item("a", recall=1.0), _retrieval_item("b", recall=0.0)]
    )
    candidate = _retrieval_report(
        [_retrieval_item("a", recall=0.5), _retrieval_item("b", recall=1.0)]
    )

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert outcome.passed
    check = next(item for item in outcome.checks if item["metric"] == "span_recall_at_k")
    # 放行，但逐样本的胜负必须照样报出来给人看
    assert check["improved_samples"] == 1
    assert check["regressed_samples"] == 1


def test_cost_increase_beyond_the_limit_is_blocked(tmp_path: Path) -> None:
    baseline = _retrieval_report([_retrieval_item("a", tokens=1000)])
    over = int(1000 * (1 + COST_MAX_INCREASE) + 100)
    candidate = _retrieval_report([_retrieval_item("a", tokens=over)])

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert not outcome.passed
    assert any(item.rule == "cost_increase" for item in outcome.violations)


def test_cost_within_the_limit_passes(tmp_path: Path) -> None:
    baseline = _retrieval_report([_retrieval_item("a", tokens=1000)])
    candidate = _retrieval_report([_retrieval_item("a", tokens=1100)])

    assert _evaluate(tmp_path, baseline, candidate).passed


def test_missing_cost_metric_is_blocked_not_skipped(tmp_path: Path) -> None:
    """成本算不出来就不能放行——否则"用堆 token 换指标"这条根本没人挡。"""
    baseline = _retrieval_report([_retrieval_item("a")])
    candidate = _retrieval_report([_retrieval_item("a")])
    for item in candidate["items"]:
        item["retrieved"] = []

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert not outcome.passed
    assert any(item.rule == "cost_unavailable" for item in outcome.violations)


def test_metric_without_comparable_samples_is_blocked(tmp_path: Path) -> None:
    """没有可配对样本时门禁会静默失效，必须当成不合格而不是"这项跳过"。"""
    baseline = _retrieval_report([_retrieval_item("a", answerable=False)])
    candidate = _retrieval_report([_retrieval_item("a", answerable=False)])

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert not outcome.passed
    assert any(item.rule == "no_comparable_samples" for item in outcome.violations)


def test_generation_track_is_gated_too(tmp_path: Path) -> None:
    baseline = _generation_report([_generation_item("a"), _generation_item("b")])
    candidate = _generation_report([_generation_item("a"), _generation_item("b", passed=False)])

    outcome = _evaluate(tmp_path, baseline, candidate)

    assert not outcome.passed
    assert {item.metric for item in outcome.violations} & {
        "constraint_pass",
        "citation_gold_alignment",
    }


# ------------------------------------------------------------------- fail-closed


def test_gold_span_drift_refuses_judgement(tmp_path: Path) -> None:
    """重标过就不是同一场比较；快照存的是 quote 的哈希，改一个字就该拒判。"""
    baseline = _retrieval_report([_retrieval_item("a", quote="原始 quote")])
    candidate = _retrieval_report([_retrieval_item("a", quote="改过的 quote")])

    with pytest.raises(GateRefused, match="标注已漂移"):
        _evaluate(tmp_path, baseline, candidate)


def test_candidate_with_failed_items_refuses_judgement(tmp_path: Path) -> None:
    baseline = _retrieval_report([_retrieval_item("a"), _retrieval_item("b")])
    candidate = _retrieval_report([_retrieval_item("a"), _retrieval_item("b", error="集群超时")])

    with pytest.raises(GateRefused, match="含失败样本"):
        _evaluate(tmp_path, baseline, candidate)


def test_controlled_config_drift_is_not_silently_allowed(tmp_path: Path) -> None:
    """受控配置变了就不是同一个指标；门禁不提供 --allow-config-drift 这种后门。"""
    baseline = _retrieval_report([_retrieval_item("a")])
    candidate = _retrieval_report([_retrieval_item("a")], config={"top_k": 5})

    with pytest.raises(GateRefused, match="受控配置项不一致"):
        _evaluate(tmp_path, baseline, candidate)


def test_dataset_mismatch_is_rejected(tmp_path: Path) -> None:
    baseline = _retrieval_report([_retrieval_item("a")])
    candidate = _retrieval_report([_retrieval_item("a")], dataset="english-dev")

    with pytest.raises(GateRefused, match="数据集不一致"):
        _evaluate(tmp_path, baseline, candidate)


def test_item_id_mismatch_is_rejected(tmp_path: Path) -> None:
    """配不上对就没有"比较"可言，同样属于拒判。"""
    baseline = _retrieval_report([_retrieval_item("a")])
    candidate = _retrieval_report([_retrieval_item("b")])

    with pytest.raises(GateRefused, match="item_id"):
        _evaluate(tmp_path, baseline, candidate)


# ------------------------------------------------------------------------- CLI


def test_markdown_lists_what_is_not_gated(tmp_path: Path) -> None:
    """未启用的指标必须写进报告，免得以后有人以为门禁已经覆盖了语义正确性。"""
    items = [_retrieval_item("a")]
    report = render_markdown(
        _evaluate(tmp_path, _retrieval_report(items), _retrieval_report(items))
    )

    assert "answer_correctness" in report
    assert "latency_ms" in report
    assert "75.8%" in report


def test_cli_exit_codes_separate_failure_from_refusal(tmp_path: Path) -> None:
    items = [_retrieval_item("a"), _retrieval_item("b")]
    baseline = _snapshot_file(tmp_path, _retrieval_report(items))
    passing = _write(tmp_path, "pass", _retrieval_report(items))
    failing = _write(
        tmp_path,
        "fail",
        _retrieval_report([_retrieval_item("a"), _retrieval_item("b", recall=0.0)]),
    )
    broken = _write(
        tmp_path,
        "broken",
        _retrieval_report([_retrieval_item("a"), _retrieval_item("b", error="超时")]),
    )
    # 下面两条走的是比较层的 ValueError，历史上会漏成 traceback + 退出码 1，
    # 也就是把"跑批配错了"混报成"质量回退"
    wrong_dataset = _write(tmp_path, "dataset", _retrieval_report(items, dataset="english-dev"))
    drifted_config = _write(tmp_path, "config", _retrieval_report(items, config={"top_k": 5}))

    def _check(report: Path) -> int:
        return main(["check", str(report), "--against", "working", "--baseline", str(baseline)])

    assert _check(passing) == 0
    assert _check(failing) == 1
    # 拒判与判为不合格是两件事，退出码必须分开
    assert _check(broken) == 2
    assert _check(wrong_dataset) == 2
    assert _check(drifted_config) == 2


def test_cli_snapshot_writes_a_committable_file(tmp_path: Path) -> None:
    source = _write(tmp_path, "src", _retrieval_report([_retrieval_item("a")]))
    output = tmp_path / "snapshots" / "baseline.json"

    assert main(["snapshot", str(source), "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["snapshot_version"] == 1
    assert "私人论文.md" not in output.read_text(encoding="utf-8")
