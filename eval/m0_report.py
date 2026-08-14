import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from eval.metrics.refusal import analyze_refusal
from eval.suites import EvalSuite, load_suite


def build_m0_report(
    *,
    suite: EvalSuite,
    retrieval_paths: list[Path],
    generation_paths: list[Path],
    review_paths: list[Path],
) -> dict[str, object]:
    retrieval_reports = [_load_json(path) for path in retrieval_paths]
    generation_reports = [_load_json(path) for path in generation_paths]
    expected_datasets = {item.name for item in suite.datasets}
    _validate_report_set(retrieval_reports, expected_datasets, kind="retrieval")
    _validate_report_set(generation_reports, expected_datasets, kind="generation")

    retrieval_items = [item for report in retrieval_reports for item in report["items"]]
    generation_items = [
        item for report in generation_reports for item in report["items"]
    ]
    _validate_items(retrieval_items, suite, label="retrieval")
    _validate_items(generation_items, suite, label="generation")
    retrieval_by_id = {str(item["item_id"]): item for item in retrieval_items}
    generation_by_id = {str(item["item_id"]): item for item in generation_items}
    if set(retrieval_by_id) != set(generation_by_id):
        raise ValueError("检索与生成报告的 item_id 集合不一致")

    answerable_retrieval = [item for item in retrieval_items if item["answerable"]]
    retrieval_metric_names = (
        "span_recall_at_k",
        "budget_span_recall",
        "ndcg_at_k",
        "alpha_ndcg_at_k",
        "mrr",
        "context_precision",
    )
    retrieval_metrics = {
        name: fmean(float(item["retrieval"][name]) for item in answerable_retrieval)
        for name in retrieval_metric_names
    }
    configured_threshold = float(retrieval_reports[0]["config"]["refusal_threshold"])
    refusal_analysis = analyze_refusal(
        [
            (float(item["top_score"]), bool(item["answerable"]))
            for item in retrieval_items
        ],
        configured_threshold=configured_threshold,
    ).to_dict()

    completed = [item for item in generation_items if item["error"] is None]
    non_refusals = [item for item in completed if item["refused"] is False]
    answerable_generation = [item for item in completed if item["answerable"]]
    unanswerable_generation = [item for item in completed if not item["answerable"]]
    citation_total = sum(len(item["citations"]) for item in non_refusals)
    aligned_total = sum(
        int(item["citation_gold_alignment"]["aligned"]) for item in non_refusals
    )
    human_review = _human_review(generation_items, review_paths)
    generation_metrics = {
        "completed_count": len(completed),
        "error_count": len(generation_items) - len(completed),
        "refusal": {
            "accuracy": _rate(
                sum(bool(item["refusal_correct"]) for item in completed), len(completed)
            ),
            "correct": sum(bool(item["refusal_correct"]) for item in completed),
            "total": len(completed),
            "answerable_answer_rate": _rate(
                sum(item["refused"] is False for item in answerable_generation),
                len(answerable_generation),
            ),
            "unanswerable_correct_refusal_rate": _rate(
                sum(item["refused"] is True for item in unanswerable_generation),
                len(unanswerable_generation),
            ),
        },
        "citation_validity": {
            "all_output_rate": _rate(
                sum(bool(item["citation_validity"]["valid"]) for item in completed),
                len(completed),
            ),
            "non_refusal_rate": _rate(
                sum(bool(item["citation_validity"]["valid"]) for item in non_refusals),
                len(non_refusals),
            ),
            "non_refusal_valid": sum(
                bool(item["citation_validity"]["valid"]) for item in non_refusals
            ),
            "non_refusal_total": len(non_refusals),
        },
        "constraint_pass": {
            "rate": _rate(
                sum(bool(item["constraint_pass"]["passed"]) for item in completed),
                len(completed),
            ),
            "passed": sum(
                bool(item["constraint_pass"]["passed"]) for item in completed
            ),
            "total": len(completed),
            "answerable_rate": _rate(
                sum(
                    bool(item["constraint_pass"]["passed"])
                    for item in answerable_generation
                ),
                len(answerable_generation),
            ),
            "answerable_passed": sum(
                bool(item["constraint_pass"]["passed"])
                for item in answerable_generation
            ),
            "answerable_total": len(answerable_generation),
            "non_refusal_rate": _rate(
                sum(bool(item["constraint_pass"]["passed"]) for item in non_refusals),
                len(non_refusals),
            ),
            "non_refusal_passed": sum(
                bool(item["constraint_pass"]["passed"]) for item in non_refusals
            ),
            "non_refusal_total": len(non_refusals),
        },
        "citation_gold_alignment": {
            "rate": _rate(aligned_total, citation_total),
            "aligned": aligned_total,
            "total": citation_total,
        },
        "citation_accuracy": human_review,
        "latency_ms": {
            "mean": fmean(float(item["latency_ms"]) for item in completed)
            if completed
            else None,
        },
    }
    git_shas = sorted(
        {str(report["git_sha"]) for report in retrieval_reports + generation_reports}
    )
    return {
        "suite": suite.name,
        "description": suite.description,
        "generated_at": datetime.now(UTC).isoformat(),
        "item_count": suite.item_count,
        "dataset_counts": {item.name: item.item_count for item in suite.datasets},
        "provenance": suite.provenance,
        "category_counts": suite.category_counts,
        "git_shas": git_shas,
        "run_ids": {
            "retrieval": [str(report["run_id"]) for report in retrieval_reports],
            "generation": [str(report["run_id"]) for report in generation_reports],
        },
        "configs": {
            "retrieval": [report["config"] for report in retrieval_reports],
            "generation": [report["config"] for report in generation_reports],
        },
        "metrics": {
            "retrieval": retrieval_metrics,
            "retrieval_refusal_score": refusal_analysis,
            "generation": generation_metrics,
        },
        "source_reports": {
            "retrieval": [str(path) for path in retrieval_paths],
            "generation": [str(path) for path in generation_paths],
            "human_review": [str(path) for path in review_paths],
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"报告根节点必须是对象: {path}")
    return payload


def _validate_report_set(
    reports: list[dict[str, Any]], expected_datasets: set[str], *, kind: str
) -> None:
    actual = [str(report.get("dataset")) for report in reports]
    if len(actual) != len(set(actual)) or set(actual) != expected_datasets:
        raise ValueError(
            f"{kind} 报告必须各覆盖 suite dataset 一次: expected={expected_datasets}, actual={actual}"
        )


def _validate_items(
    items: list[dict[str, Any]], suite: EvalSuite, *, label: str
) -> None:
    ids = [str(item["item_id"]) for item in items]
    if len(ids) != suite.item_count or len(set(ids)) != suite.item_count:
        raise ValueError(f"{label} 结果必须包含 {suite.item_count} 个唯一 item")
    categories = Counter(str(item["category"]) for item in items)
    if dict(categories) != suite.category_counts:
        raise ValueError(
            f"{label} 类别分布不匹配: expected={suite.category_counts}, actual={dict(categories)}"
        )


def _human_review(
    generation_items: list[dict[str, Any]], review_paths: list[Path]
) -> dict[str, object]:
    expected = {
        (str(item["item_id"]), str(citation["citation_id"]))
        for item in generation_items
        for citation in item["citations"]
    }
    labels: dict[tuple[str, str], bool] = {}
    invalid_rows: list[str] = []
    for path in review_paths:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                value = (row.get("supported") or "").strip().casefold()
                if not value:
                    continue
                key = (
                    (row.get("item_id") or "").strip(),
                    (row.get("citation_id") or "").strip(),
                )
                if key not in expected:
                    invalid_rows.append(f"{path}:{row_number}:unknown_citation")
                    continue
                if key in labels:
                    invalid_rows.append(f"{path}:{row_number}:duplicate_citation")
                    continue
                if value not in {"yes", "no"}:
                    invalid_rows.append(
                        f"{path}:{row_number}:supported_must_be_yes_or_no"
                    )
                    continue
                if not (row.get("reason") or "").strip():
                    invalid_rows.append(f"{path}:{row_number}:missing_reason")
                    continue
                if (
                    not (row.get("reviewer") or "").strip()
                    or not (row.get("reviewed_at") or "").strip()
                ):
                    invalid_rows.append(f"{path}:{row_number}:missing_reviewer_or_time")
                    continue
                labels[key] = value == "yes"
    if invalid_rows:
        raise ValueError("人工引用复核表无效: " + "; ".join(invalid_rows))
    reviewed = len(labels)
    supported = sum(labels.values())
    coverage = _rate(reviewed, len(expected))
    return {
        "status": "complete" if reviewed == len(expected) else "pending_human_review",
        "rate": _rate(supported, reviewed),
        "supported_citations": supported,
        "reviewed_citations": reviewed,
        "expected_citations": len(expected),
        "review_coverage": coverage,
        "definition": "人工逐引用判定：引用证据是否语义支撑对应答案论断。",
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def markdown_report(payload: dict[str, object]) -> str:
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    retrieval = metrics["retrieval"]
    score_refusal = metrics["retrieval_refusal_score"]
    generation = metrics["generation"]
    assert isinstance(retrieval, dict) and isinstance(score_refusal, dict)
    assert isinstance(generation, dict)
    refusal = generation["refusal"]
    validity = generation["citation_validity"]
    constraints = generation["constraint_pass"]
    alignment = generation["citation_gold_alignment"]
    accuracy = generation["citation_accuracy"]
    assert all(
        isinstance(value, dict)
        for value in (refusal, validity, constraints, alignment, accuracy)
    )
    configured = score_refusal.get("configured")
    best = score_refusal.get("best")
    return f"""# M0 正式基线：{payload["suite"]}

- 时间：{payload["generated_at"]}
- 样本：{payload["item_count"]} 条 human（core-dev 20 + english-dev 20）
- 类别：single_hop 14 / multi_hop 8 / table 10 / unanswerable 8
- Git：{", ".join(f"`{sha}`" for sha in payload["git_shas"])}

## 可写入实验台账的数字

| 轨道 | 指标 | 结果 |
|---|---|---:|
| 检索 | span Recall@10 | {_percent(retrieval["span_recall_at_k"])} |
| 检索 | budget Recall（4000 tokens） | {_percent(retrieval["budget_span_recall"])} |
| 检索 | nDCG@10 | {_percent(retrieval["ndcg_at_k"])} |
| 检索 | α-nDCG@10 | {_percent(retrieval["alpha_ndcg_at_k"])} |
| 检索 | MRR | {_percent(retrieval["mrr"])} |
| 检索 | context precision | {_percent(retrieval["context_precision"])} |
| 检索分数拒答 | AUROC | {_decimal(score_refusal.get("auroc"))} |
| 检索分数拒答 | 当前阈值 0.35 macro-F1 | {_decimal(configured.get("macro_f1") if isinstance(configured, dict) else None)} |
| 检索分数拒答 | dev 最优 macro-F1 | {_decimal(best.get("macro_f1") if isinstance(best, dict) else None)} |
| 端到端拒答 | 总准确率 | {_percent(refusal["accuracy"])} ({refusal["correct"]}/{refusal["total"]}) |
| 端到端拒答 | 可答题正常回答率 | {_percent(refusal["answerable_answer_rate"])} |
| 端到端拒答 | 不可答正确拒答率 | {_percent(refusal["unanswerable_correct_refusal_rate"])} |
| 引用规则 | citation_validity（非拒答） | {_percent(validity["non_refusal_rate"])} ({validity["non_refusal_valid"]}/{validity["non_refusal_total"]}) |
| 答案规则 | constraint_pass（可答题） | {_percent(constraints["answerable_rate"])} ({constraints["answerable_passed"]}/{constraints["answerable_total"]}) |
| 答案规则 | constraint_pass（实际非拒答） | {_percent(constraints["non_refusal_rate"])} ({constraints["non_refusal_passed"]}/{constraints["non_refusal_total"]}) |
| 引用代理 | gold span alignment | {_percent(alignment["rate"])} ({alignment["aligned"]}/{alignment["total"]}) |
| 人工引用 | citation_accuracy | {_human_accuracy(accuracy)} |

## 口径与结论边界

- 检索策略固定为 dense-only；Top-K=10，diagnostic-K=50，token budget=4000，span overlap θ=0.5。
- 生成策略固定为 dense-only；关闭 query decomposition、lexical RRF 与 rerank，保留线上证据充分性门控。
- `citation_validity` 只校验标签格式、引用记录、block/version/document、字符区间与 quote 原文一致。
- `gold span alignment` 是自动代理，不代表引用在语义上支撑答案。
- `citation_accuracy` 只从带 reviewer、reviewed_at、理由和 yes/no 的人工复核表计算；未满覆盖时标记 pending，不用自动结果冒充人工结论。

## Run IDs

- retrieval: {", ".join(f"`{run_id}`" for run_id in payload["run_ids"]["retrieval"])}
- generation: {", ".join(f"`{run_id}`" for run_id in payload["run_ids"]["generation"])}
"""


def _percent(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.2%}"


def _decimal(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.4f}"


def _human_accuracy(accuracy: dict[str, Any]) -> str:
    rate = accuracy.get("rate")
    reviewed = accuracy["reviewed_citations"]
    expected = accuracy["expected_citations"]
    if accuracy["status"] != "complete":
        return f"待人工复核（{reviewed}/{expected} 个引用）"
    return f"{_percent(rate)}（{accuracy['supported_citations']}/{reviewed}）"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="合并 M0 40 条正式检索、拒答与引用报告"
    )
    parser.add_argument(
        "--suite", type=Path, default=Path("eval/suites/m0-core-40.json")
    )
    parser.add_argument("--retrieval-report", type=Path, action="append", required=True)
    parser.add_argument(
        "--generation-report", type=Path, action="append", required=True
    )
    parser.add_argument("--review-csv", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = build_m0_report(
        suite=load_suite(args.suite),
        retrieval_paths=args.retrieval_report,
        generation_paths=args.generation_report,
        review_paths=args.review_csv,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    json_path = args.output_dir / "report.json"
    md_path = args.output_dir / "report.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(markdown_report(payload), encoding="utf-8")
    print(md_path)


if __name__ == "__main__":
    main()
