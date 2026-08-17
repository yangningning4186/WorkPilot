"""拒答阈值 ROC 调优（W6 的 E7 遗留项）。

**离线**：只读已完成跑批的 `report.json`，不连数据库、不调模型、不重跑检索。
阈值扫描本来就只依赖每条样本的 `top_score` 与 `answerable`，重跑一遍纯属浪费 GPU，
而且会因为跑批噪声让"同一份数据扫出不同阈值"。

口径与线上严格对齐（`app/services/grounded_answer.py::evaluate_refusal`）：

- 硬门是**唯一**的一条：`top_score < threshold` → `below_threshold`。
  `top_score` 必须来自报告显式声明的最终排序器（dense / lexical / fusion / rerank），
  不再允许把不同量纲的遗留 `hit.score` 混扫。
- `score_margin`（top1 − top2）**不直接触发拒答**，它只作为信号送进证据充分性门控。
  因此 margin 阈值**无法离线调**——离线改它对最终拒答的影响是 0，
  真实影响全在 LLM 门控里。本脚本只报它的可分性，不给建议值。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.refusal_threshold_scan \\
      --report eval/outputs/dev-suite-retrieval/<batch>/heading/report.json \\
      --output-dir eval/outputs/refusal-threshold/<label>
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from eval.metrics.refusal import analyze_refusal, score_threshold

# 留出集绝不参与调参（docs/06 §1.2 的 R7 纪律）。名字里带 test 一律拒绝。
_TEST_MARKER = "test"


class ScanRefused(RuntimeError):
    """前置条件不满足，拒绝出建议值。"""


@dataclass(frozen=True)
class Observation:
    item_id: str
    category: str
    answerable: bool
    top_score: float
    margin: float | None
    # 该条在真实链路里最终有没有拒答（来自生成轨报告）。
    # 线上拒答是 **OR 组合**：分数门 OR 证据充分性门控。只扫分数门等于假设另一半不存在，
    # 而实测证据门控已经把 13/13 不可答全部拦下——不带上它扫出来的"最优阈值"是错的。
    gate_refused: bool | None = None


def load_gate_outcomes(paths: Sequence[Path]) -> dict[str, bool]:
    """从生成轨报告读逐条真实拒答结果。"""
    outcomes: dict[str, bool] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["items"]:
            if item.get("error") is not None:
                continue
            item_id = str(item["item_id"])
            refused = bool(item.get("refused"))
            if outcomes.setdefault(item_id, refused) != refused:
                raise ScanRefused(
                    f"item {item_id} 在多份生成报告里拒答结果不一致，"
                    "说明混了不同配置的跑批，组合口径无效"
                )
    return outcomes


def composite_scan(
    observations: Sequence[Observation], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    """组合门（分数门 OR 证据门控）下的拒答正确率随阈值的变化。

    这才是线上真实会发生的事。只有它能回答"调高阈值到底换来什么"。
    """
    usable = [item for item in observations if item.gate_refused is not None]
    rows: list[dict[str, Any]] = []
    for threshold in thresholds:
        correct = false_answer = false_refusal = 0
        for item in usable:
            refused = item.top_score < threshold or bool(item.gate_refused)
            if item.answerable and not refused:
                correct += 1
            elif item.answerable:
                false_refusal += 1
            elif refused:
                correct += 1
            else:
                false_answer += 1
        rows.append(
            {
                "threshold": threshold,
                "sample_count": len(usable),
                "refusal_correct": correct / len(usable) if usable else 0.0,
                "false_answer": false_answer,
                "false_refusal": false_refusal,
                "answered": sum(
                    1
                    for item in usable
                    if not (item.top_score < threshold or bool(item.gate_refused))
                ),
            }
        )
    return rows


def load_observations(paths: Sequence[Path]) -> tuple[list[Observation], dict[str, Any]]:
    observations: list[Observation] = []
    configured: set[float] = set()
    score_sources: set[str] = set()
    threshold_applied: set[bool] = set()
    datasets: set[str] = set()
    seen: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        dataset = str(payload.get("dataset", ""))
        if _TEST_MARKER in dataset.lower():
            raise ScanRefused(
                f"{path} 的 dataset 是 {dataset}；留出集不得参与阈值选择（docs/06 §1.2）"
            )
        datasets.add(dataset)
        config = payload.get("config") or {}
        configured_source = config.get("retrieval_score_source")
        if configured_source is not None:
            score_sources.add(str(configured_source))
        if config.get("refusal_threshold_applied") is not None:
            threshold_applied.add(bool(config["refusal_threshold_applied"]))
        if config.get("refusal_threshold") is not None:
            configured.add(float(config["refusal_threshold"]))
        for item in payload["items"]:
            top_score = _top_score(item)
            if item.get("error") is not None or top_score is None:
                continue
            item_id = str(item["item_id"])
            if item_id in seen:
                raise ScanRefused(f"item_id 重复，多份报告混了同一批样本: {item_id}")
            seen.add(item_id)
            observations.append(
                Observation(
                    item_id=item_id,
                    category=str(item["category"]),
                    answerable=bool(item["answerable"]),
                    top_score=top_score,
                    margin=_margin(item),
                )
            )
    if len(configured) > 1:
        raise ScanRefused(f"多份报告的 refusal_threshold 不一致: {sorted(configured)}")
    if len(score_sources) > 1:
        raise ScanRefused(f"多份报告的 score source 不一致: {sorted(score_sources)}")
    if len(threshold_applied) > 1:
        raise ScanRefused("多份报告的 refusal_threshold_applied 不一致")
    if not observations:
        raise ScanRefused("没有可用样本")
    return observations, {
        "datasets": sorted(datasets),
        "configured_threshold": next(iter(configured), None),
        "score_source": next(iter(score_sources), None),
        "threshold_applied": next(iter(threshold_applied), None),
        "reports": [str(path) for path in paths],
    }


def _margin(item: dict[str, Any]) -> float | None:
    """优先读取线上已按分数源归一的相对 margin，兼容旧检索报告。

    报告里没有直接存 `second_score`，只能从 `retrieved` 的分数集合里取。
    注意 `retrieved` 是**最终排序**（rerank 之后），不是按 score 排的，所以必须
    自己取最大与次大，不能拿前两条相减——那会算出负数。
    """

    signals = item.get("refusal_signals")
    if isinstance(signals, dict) and signals.get("score_margin_ratio") is not None:
        return float(signals["score_margin_ratio"])
    scores = sorted(
        (float(chunk["score"]) for chunk in item.get("retrieved") or [] if chunk.get("score") is not None),
        reverse=True,
    )
    return scores[0] - scores[1] if len(scores) > 1 else None


def _top_score(item: dict[str, Any]) -> float | None:
    """兼容检索报告的顶层字段与生成报告的 refusal_signals。"""

    value = item.get("top_score")
    if value is None:
        signals = item.get("refusal_signals")
        if isinstance(signals, dict):
            value = signals.get("top_score")
    return float(value) if value is not None else None


def scan(observations: Sequence[Observation], *, configured_threshold: float) -> dict[str, Any]:
    pairs = [(item.top_score, item.answerable) for item in observations]
    analysis = analyze_refusal(list(pairs), configured_threshold=configured_threshold)
    by_category = {
        category: analyze_refusal(
            [(item.top_score, item.answerable) for item in observations if item.category == category],
            configured_threshold=configured_threshold,
        )
        for category in sorted({item.category for item in observations})
    }
    return {
        "overall": analysis.to_dict(),
        "by_category": {name: value.to_dict() for name, value in by_category.items()},
        "sweep": [
            score_threshold(list(pairs), threshold).to_dict()
            for threshold in _sweep_points(observations)
        ],
        "margin_separability": _margin_separability(observations),
    }


def _sweep_points(observations: Sequence[Observation]) -> list[float]:
    """按当前分数源的真实支持集扫描，不能把 cosine 的区间硬套给 RRF。"""

    scores = sorted({item.top_score for item in observations})
    if not scores:
        return []
    epsilon = max(max(abs(score) for score in scores), 1.0) * 1e-12
    return [
        scores[0] - epsilon,
        *((left + right) / 2 for left, right in pairwise(scores)),
        scores[-1] + epsilon,
    ]


def _margin_separability(observations: Sequence[Observation]) -> dict[str, Any]:
    """margin 对可答/不可答的区分度。

    **只是诊断**：margin 不进硬门，调它不会改变离线拒答结果。
    报它是为了回答"margin 这个信号本身有没有区分度"，
    而不是为了给 `refusal_margin_threshold` 挑一个值。
    """

    usable = [item for item in observations if item.margin is not None]
    answerable = [item.margin for item in usable if item.answerable]
    unanswerable = [item.margin for item in usable if not item.answerable]
    if not answerable or not unanswerable:
        return {"auroc": None, "reason": "缺少可答或不可答样本", "sample_count": len(usable)}
    analysis = analyze_refusal(
        [(item.margin, item.answerable) for item in usable],  # type: ignore[misc]
        configured_threshold=0.03,
    )
    return {
        "auroc": analysis.auroc,
        "sample_count": len(usable),
        "note": "margin 不直接触发拒答，只作为信号进入证据充分性门控；此处仅报可分性",
    }


def _recommend(payload: dict[str, Any], *, configured_threshold: float) -> dict[str, Any]:
    overall = payload["overall"]
    best = overall["best"]
    configured = overall["configured"]
    if best is None or configured is None:
        return {"action": "keep", "reason": "样本不足以判定"}
    gain = best["macro_f1"] - configured["macro_f1"]
    # 一条样本的翻转就是 1/n；小于这个幅度的"提升"是取整噪声，不是信号（G0 的量化下限）
    quantum = 1.0 / max(overall["answerable_count"] + overall["unanswerable_count"], 1)
    if gain <= quantum:
        return {
            "action": "keep",
            "reason": (
                f"最优阈值的 macro-F1 只比现值高 {gain:.4f}，不超过一条样本的量化下限 "
                f"{quantum:.4f}；换阈值属于在噪声上调参"
            ),
            "gain": gain,
            "quantum": quantum,
        }
    return {
        "action": "change",
        "from": configured_threshold,
        "to": best["threshold"],
        "gain": gain,
        "quantum": quantum,
        "reason": f"macro-F1 {configured['macro_f1']:.4f} → {best['macro_f1']:.4f}",
    }


def build_report(
    paths: Sequence[Path],
    *,
    override_threshold: float | None,
    generation_reports: Sequence[Path] = (),
) -> dict[str, Any]:
    observations, meta = load_observations(paths)
    configured = override_threshold if override_threshold is not None else meta["configured_threshold"]
    if configured is None:
        raise ScanRefused("报告里没有 refusal_threshold，且没有显式给 --configured-threshold")
    if generation_reports:
        outcomes = load_gate_outcomes(generation_reports)
        missing = [item.item_id for item in observations if item.item_id not in outcomes]
        if missing:
            raise ScanRefused(
                f"{len(missing)} 条样本在生成报告里找不到拒答结果，组合口径会缺样本: {missing[:5]}"
            )
        observations = [
            Observation(**{**vars(item), "gate_refused": outcomes[item.item_id]})
            for item in observations
        ]
    payload = scan(observations, configured_threshold=float(configured))
    composite = (
        composite_scan(observations, [*_sweep_points(observations), float(configured)])
        if generation_reports
        else []
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "meta": {
            **meta,
            "configured_threshold": configured,
            "item_count": len(observations),
            "generation_reports": [str(path) for path in generation_reports],
        },
        **payload,
        "composite": composite,
        "recommendation": (
            _recommend_composite(composite, configured_threshold=float(configured))
            if composite
            else _recommend(payload, configured_threshold=float(configured))
        ),
    }


def _recommend_composite(
    composite: Sequence[dict[str, Any]], *, configured_threshold: float
) -> dict[str, Any]:
    """按**组合门**给建议。只扫分数门会给出方向相反的错误结论。"""
    current = min(composite, key=lambda row: abs(row["threshold"] - configured_threshold))
    best = max(composite, key=lambda row: (row["refusal_correct"], -row["threshold"]))
    quantum = 1.0 / max(int(current["sample_count"]), 1)
    gain = best["refusal_correct"] - current["refusal_correct"]
    if gain <= quantum:
        return {
            "action": "keep",
            "reason": (
                f"组合门下最优阈值的拒答正确率只比现值高 {gain:.4f}，不超过一条样本的量化下限 "
                f"{quantum:.4f}。分数门单独扫描会建议调高阈值，但证据门控已经把不可答"
                f"全部拦下，调高只增加误拒——**这是只扫分数门会得出的相反结论**"
            ),
            "gain": gain,
            "quantum": quantum,
            "current": current,
            "best_seen": best,
        }
    return {
        "action": "change",
        "from": configured_threshold,
        "to": best["threshold"],
        "gain": gain,
        "quantum": quantum,
        "current": current,
        "best_seen": best,
    }


def markdown(payload: dict[str, Any]) -> str:
    overall = payload["overall"]
    best, configured = overall["best"], overall["configured"]
    meta = payload["meta"]
    lines = [
        "# 拒答阈值扫描（E7）",
        "",
        (
            f"- 数据集：{'、'.join(meta['datasets'])}（{meta['item_count']} 条，"
            f"可答 {overall['answerable_count']} / 不可答 {overall['unanswerable_count']}）"
        ),
        f"- 现行阈值：`{meta['configured_threshold']}`",
        f"- top_score AUROC：**{_fmt(overall['auroc'])}**",
        "",
        "## 现值 vs 最优",
        "",
        "| | 阈值 | macro-F1 | 可答 F1 | 不可答 F1 | 误答 | 误拒 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in (("现行", configured), ("最优", best)):
        if row is None:
            continue
        lines.append(
            f"| {name} | {row['threshold']:.4f} | {row['macro_f1']:.4f} | "
            f"{row['answerable_f1']:.4f} | {row['unanswerable_f1']:.4f} | "
            f"{row['false_answerable']} | {row['false_refusal']} |"
        )
    lines += ["", "> 上表是**分数门单独**的口径，只用于看 top_score 这个信号本身。", ""]
    composite = payload.get("composite") or []
    if composite:
        lines += [
            "## 组合门（分数门 OR 证据充分性门控）★",
            "",
            "线上拒答是 OR 组合，这才是调阈值真正会发生的事。",
            "",
            "| 阈值 | 拒答正确率 | 误答 | 误拒 | 实际作答 |",
            "|---:|---:|---:|---:|---:|",
        ]
        shown = {round(row["threshold"], 3) for row in composite}
        for row in composite:
            if round(row["threshold"], 3) in shown:
                lines.append(
                    f"| {row['threshold']:.4f} | {row['refusal_correct']:.4f} | "
                    f"{row['false_answer']} | {row['false_refusal']} | {row['answered']} |"
                )
        lines.append("")
    recommendation = payload["recommendation"]
    lines += [
        "",
        (
            f"**结论：{'维持现值' if recommendation['action'] == 'keep' else '建议改为 ' + str(recommendation['to'])}**"
            f" —— {recommendation['reason']}"
        ),
        "",
        "## 分类别 AUROC",
        "",
        "| 类别 | 可答 | 不可答 | AUROC |",
        "|---|---:|---:|---:|",
    ]
    for name, row in payload["by_category"].items():
        lines.append(
            f"| {name} | {row['answerable_count']} | {row['unanswerable_count']} | "
            f"{_fmt(row['auroc'])} |"
        )
    margin = payload["margin_separability"]
    lines += [
        "",
        "## margin 可分性（诊断，不给建议值）",
        "",
        f"- margin AUROC：{_fmt(margin.get('auroc'))}（{margin.get('sample_count')} 条）",
        "- margin 不直接触发拒答，只作为信号进入证据充分性门控，",
        "  所以它的阈值**无法离线调**：离线改它对拒答结果的影响恒为 0。",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: object) -> str:
    return "—" if value is None else f"{float(value):.4f}"  # type: ignore[arg-type]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拒答阈值 ROC 扫描（离线）")
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--configured-threshold", type=float, default=None)
    # 强烈建议给：不给就只有分数门口径，而那个口径会给出方向相反的建议
    parser.add_argument("--generation-report", type=Path, action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    payload = build_report(
        args.report,
        override_threshold=args.configured_threshold,
        generation_reports=args.generation_report,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = markdown(payload)
    (args.output_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
