"""两次跑批的快照 diff + 配对显著性判定（见 docs/06-评测体系.md §4.3、§7）。

用法：

    PYTHONPATH=backend backend/.venv/bin/python -m eval.compare \\
      eval/outputs/dense-baseline/<baseline-run> \\
      eval/outputs/dense-baseline/<candidate-run> \\
      --output-dir eval/outputs/compare/<label>

只读两份 report.json，不连数据库、不调模型、不重跑检索。
配对依据是 `item_id`：两份报告必须来自同一数据集的同一批样本，
否则"提升"可能只是换了一批题。
"""

import argparse
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.stats import (
    DEFAULT_CI_LEVEL,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    INELIGIBLE,
    BootstrapResult,
    MetricSamples,
    RatioPoint,
    paired_bootstrap,
)

KIND_RETRIEVAL = "retrieval"
KIND_GENERATION = "generation"

Extractor = Callable[[dict[str, Any], dict[str, Any]], RatioPoint]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    title: str
    unit: str
    higher_is_better: bool
    extract: Extractor


def _retrieval_metric(field: str) -> Extractor:
    def extract(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
        retrieval = item.get("retrieval")
        # 不可答题没有 gold span，检索指标整体不适用
        if not isinstance(retrieval, dict) or retrieval.get(field) is None:
            return INELIGIBLE
        return RatioPoint(float(retrieval[field]), 1.0)

    return extract


def _refusal_correct_by_score(
    item: dict[str, Any], config: dict[str, Any]
) -> RatioPoint:
    threshold = config.get("refusal_threshold")
    score = item.get("top_score")
    if threshold is None or score is None:
        return INELIGIBLE
    # 各自用自己 config 里的阈值：换阈值本身就是被对照的变量
    answered = float(score) >= float(threshold)
    return RatioPoint(float(answered is bool(item["answerable"])), 1.0)


def _latency(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    latency = item.get("latency_ms")
    if latency is None or item.get("error") is not None:
        return INELIGIBLE
    return RatioPoint(float(latency), 1.0)


def _completed(item: dict[str, Any]) -> bool:
    return item.get("error") is None


def _generation_flag(
    field: str,
    subfield: str | None = None,
    *,
    non_refusal_only: bool = False,
    answerable_only: bool = False,
) -> Extractor:
    def extract(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
        if not _completed(item):
            return INELIGIBLE
        # 一侧拒答一侧作答时，配对逻辑会把两侧一起剔除，不会拿拒答样本充数
        if non_refusal_only and item.get("refused") is not False:
            return INELIGIBLE
        if answerable_only and not item.get("answerable"):
            return INELIGIBLE
        value = item[field]
        flag = value[subfield] if subfield else value
        return RatioPoint(float(bool(flag)), 1.0)

    return extract


def _citation_gold_alignment(
    item: dict[str, Any], config: dict[str, Any]
) -> RatioPoint:
    if not _completed(item) or item.get("refused") is not False:
        return INELIGIBLE
    alignment = item["citation_gold_alignment"]
    total = float(alignment["total"])
    if not total:
        return INELIGIBLE
    # micro 口径：重采样时按引用条数加权，与单跑报告的聚合方式一致
    return RatioPoint(float(alignment["aligned"]), total)


_RETRIEVAL_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "span_recall_at_k",
        "span Recall@K",
        "ratio",
        True,
        _retrieval_metric("span_recall_at_k"),
    ),
    MetricSpec(
        "budget_span_recall",
        "budget span Recall",
        "ratio",
        True,
        _retrieval_metric("budget_span_recall"),
    ),
    MetricSpec("ndcg_at_k", "nDCG@K", "ratio", True, _retrieval_metric("ndcg_at_k")),
    MetricSpec(
        "alpha_ndcg_at_k",
        "α-nDCG@K",
        "ratio",
        True,
        _retrieval_metric("alpha_ndcg_at_k"),
    ),
    MetricSpec("mrr", "MRR", "ratio", True, _retrieval_metric("mrr")),
    MetricSpec(
        "context_precision",
        "context precision",
        "ratio",
        True,
        _retrieval_metric("context_precision"),
    ),
    MetricSpec(
        "refusal_correct_at_threshold",
        "配置阈值下拒答正确率",
        "ratio",
        True,
        _refusal_correct_by_score,
    ),
    MetricSpec("latency_ms", "单题延迟均值(ms)", "ms", False, _latency),
)

_GENERATION_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "refusal_correct",
        "拒答正确率",
        "ratio",
        True,
        _generation_flag("refusal_correct"),
    ),
    MetricSpec(
        "citation_validity_non_refusal",
        "citation_validity(非拒答)",
        "ratio",
        True,
        _generation_flag("citation_validity", "valid", non_refusal_only=True),
    ),
    MetricSpec(
        "constraint_pass",
        "constraint_pass",
        "ratio",
        True,
        _generation_flag("constraint_pass", "passed"),
    ),
    MetricSpec(
        "constraint_pass_answerable",
        "constraint_pass(可答题)",
        "ratio",
        True,
        _generation_flag("constraint_pass", "passed", answerable_only=True),
    ),
    MetricSpec(
        "citation_gold_alignment",
        "引用 gold span 对齐率",
        "ratio",
        True,
        _citation_gold_alignment,
    ),
    MetricSpec("latency_ms", "单题延迟均值(ms)", "ms", False, _latency),
)

_METRICS: dict[str, tuple[MetricSpec, ...]] = {
    KIND_RETRIEVAL: _RETRIEVAL_METRICS,
    KIND_GENERATION: _GENERATION_METRICS,
}

_PRIMARY_METRIC: dict[str, str] = {
    # budget span recall 是跨策略比较的主口径（docs/06 §2.1）
    KIND_RETRIEVAL: "budget_span_recall",
    KIND_GENERATION: "constraint_pass",
}

# 这些配置项一旦不同，两份报告算的就不是同一个指标，默认拒绝比较。
_CONTROLLED_KEYS: dict[str, tuple[str, ...]] = {
    KIND_RETRIEVAL: ("origin", "top_k", "token_budget", "theta", "alpha"),
    KIND_GENERATION: ("origin", "top_k", "theta"),
}

_VERDICT_LABELS = {
    "improved": "显著提升",
    "regressed": "显著下降",
    "inconclusive": "无显著差异",
    "not_applicable": "不适用",
}


@dataclass(frozen=True)
class LoadedReport:
    path: Path
    payload: dict[str, Any]
    kind: str

    @property
    def config(self) -> dict[str, Any]:
        config = self.payload.get("config")
        return config if isinstance(config, dict) else {}

    @property
    def items(self) -> list[dict[str, Any]]:
        items = self.payload["items"]
        if not isinstance(items, list):
            raise TypeError(f"报告 items 必须是数组: {self.path}")
        return items

    def describe(self) -> dict[str, object]:
        return {
            "label": self.payload.get("label"),
            "run_id": self.payload.get("run_id"),
            "git_sha": self.payload.get("git_sha"),
            "config_hash": self.payload.get("config_hash"),
            "item_count": len(self.items),
            "source_report": str(self.path),
        }


def load_report(path: Path) -> LoadedReport:
    resolved = path / "report.json" if path.is_dir() else path
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"报告根节点必须是对象: {resolved}")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"报告缺少逐样本 items，无法配对: {resolved}")
    missing = [index for index, item in enumerate(items) if not item.get("item_id")]
    if missing:
        raise ValueError(
            f"报告的 items 缺少 item_id，无法按样本配对（refusal_baseline 报告不支持）: {resolved}"
        )
    return LoadedReport(
        path=resolved, payload=payload, kind=_detect_kind(items[0], resolved)
    )


def _detect_kind(item: dict[str, Any], path: Path) -> str:
    if "citations" in item:
        return KIND_GENERATION
    if "retrieval" in item:
        return KIND_RETRIEVAL
    raise ValueError(
        f"无法识别报告类型，仅支持 dense_baseline 与 generation_baseline 报告: {path}"
    )


@dataclass(frozen=True)
class PairedItems:
    ids: tuple[str, ...]
    baseline: tuple[dict[str, Any], ...]
    candidate: tuple[dict[str, Any], ...]
    categories: tuple[str, ...]
    questions: tuple[str, ...]


def check_compatibility(
    baseline: LoadedReport, candidate: LoadedReport, *, allow_config_drift: bool
) -> dict[str, object]:
    if baseline.kind != candidate.kind:
        raise ValueError(
            f"报告类型不一致，无法比较: baseline={baseline.kind}, candidate={candidate.kind}"
        )
    if baseline.payload.get("dataset") != candidate.payload.get("dataset"):
        raise ValueError(
            "数据集不一致，配对比较无效: "
            f"baseline={baseline.payload.get('dataset')}, "
            f"candidate={candidate.payload.get('dataset')}"
        )
    diff = _config_diff(baseline.config, candidate.config)
    controlled = sorted(set(diff) & set(_CONTROLLED_KEYS[baseline.kind]))
    if controlled and not allow_config_drift:
        detail = ", ".join(
            f"{key}: {diff[key]['baseline']!r} → {diff[key]['candidate']!r}"
            for key in controlled
        )
        raise ValueError(
            f"受控配置项不一致，两份报告算的不是同一个指标: {detail}。"
            "确认这是有意的对照后可加 --allow-config-drift"
        )
    return {
        "dataset": baseline.payload.get("dataset"),
        "kind": baseline.kind,
        "config_diff": diff,
        "controlled_diff": controlled,
        "config_drift_allowed": allow_config_drift,
        "identical_config": baseline.payload.get("config_hash")
        == candidate.payload.get("config_hash"),
    }


def _config_diff(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        key: {"baseline": baseline.get(key), "candidate": candidate.get(key)}
        for key in sorted(set(baseline) | set(candidate))
        if baseline.get(key) != candidate.get(key)
    }


def pair_items(baseline: LoadedReport, candidate: LoadedReport) -> PairedItems:
    baseline_by_id = _index_by_item_id(baseline)
    candidate_by_id = _index_by_item_id(candidate)
    if set(baseline_by_id) != set(candidate_by_id):
        only_baseline = sorted(set(baseline_by_id) - set(candidate_by_id))
        only_candidate = sorted(set(candidate_by_id) - set(baseline_by_id))
        raise ValueError(
            "两份报告的 item_id 集合不一致，无法配对: "
            f"仅 baseline={only_baseline[:5]}, 仅 candidate={only_candidate[:5]}"
        )
    ids = tuple(sorted(baseline_by_id))
    for item_id in ids:
        left = baseline_by_id[item_id]
        right = candidate_by_id[item_id]
        for field in ("category", "answerable"):
            if left.get(field) != right.get(field):
                raise ValueError(
                    f"item {item_id} 的 {field} 在两份报告中不一致，标注已漂移，不能配对比较"
                )
    return PairedItems(
        ids=ids,
        baseline=tuple(baseline_by_id[item_id] for item_id in ids),
        candidate=tuple(candidate_by_id[item_id] for item_id in ids),
        categories=tuple(str(baseline_by_id[item_id]["category"]) for item_id in ids),
        questions=tuple(
            str(baseline_by_id[item_id].get("question", "")) for item_id in ids
        ),
    )


def _index_by_item_id(report: LoadedReport) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in report.items:
        item_id = str(item["item_id"])
        if item_id in indexed:
            raise ValueError(f"报告中出现重复 item_id: {item_id} ({report.path})")
        indexed[item_id] = item
    return indexed


@dataclass(frozen=True)
class MetricColumn:
    spec: MetricSpec
    baseline: tuple[RatioPoint, ...]
    candidate: tuple[RatioPoint, ...]
    baseline_only: int
    candidate_only: int

    def samples(self) -> MetricSamples:
        return MetricSamples(baseline=self.baseline, candidate=self.candidate)

    def subset(self, indexes: Sequence[int]) -> MetricSamples:
        return MetricSamples(
            baseline=tuple(self.baseline[index] for index in indexes),
            candidate=tuple(self.candidate[index] for index in indexes),
        )


def build_columns(
    pairs: PairedItems,
    specs: Sequence[MetricSpec],
    *,
    baseline_config: dict[str, Any],
    candidate_config: dict[str, Any],
) -> dict[str, MetricColumn]:
    """逐指标抽取配对贡献。

    只要一侧不适用该指标（例如 baseline 拒答、candidate 作答），
    两侧一并剔除——否则比的是两批不同的样本。剔除数量单独记录，
    因为"可比样本变少"本身就是需要看见的信号。
    """
    columns: dict[str, MetricColumn] = {}
    for spec in specs:
        baseline_points = [
            spec.extract(item, baseline_config) for item in pairs.baseline
        ]
        candidate_points = [
            spec.extract(item, candidate_config) for item in pairs.candidate
        ]
        baseline_only = sum(
            left.eligible and not right.eligible
            for left, right in zip(baseline_points, candidate_points, strict=True)
        )
        candidate_only = sum(
            right.eligible and not left.eligible
            for left, right in zip(baseline_points, candidate_points, strict=True)
        )
        paired = [
            (left, right)
            if left.eligible and right.eligible
            else (INELIGIBLE, INELIGIBLE)
            for left, right in zip(baseline_points, candidate_points, strict=True)
        ]
        columns[spec.name] = MetricColumn(
            spec=spec,
            baseline=tuple(left for left, _ in paired),
            candidate=tuple(right for _, right in paired),
            baseline_only=baseline_only,
            candidate_only=candidate_only,
        )
    return columns


def build_comparison(
    baseline: LoadedReport,
    candidate: LoadedReport,
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    top_n: int = 10,
    primary_metric: str | None = None,
    allow_config_drift: bool = False,
) -> dict[str, object]:
    compatibility = check_compatibility(
        baseline, candidate, allow_config_drift=allow_config_drift
    )
    pairs = pair_items(baseline, candidate)
    specs = _METRICS[baseline.kind]
    primary = primary_metric or _PRIMARY_METRIC[baseline.kind]
    if primary not in {spec.name for spec in specs}:
        raise ValueError(
            f"未知的 primary metric: {primary}，可选: {sorted(spec.name for spec in specs)}"
        )
    columns = build_columns(
        pairs,
        specs,
        baseline_config=baseline.config,
        candidate_config=candidate.config,
    )
    direction = {spec.name: spec.higher_is_better for spec in specs}
    overall = paired_bootstrap(
        {name: column.samples() for name, column in columns.items()},
        seed=seed,
        resamples=resamples,
        ci_level=ci_level,
        higher_is_better=direction,
    )
    by_category = {
        category: {
            "item_count": len(indexes),
            "metrics": _metrics_payload(
                columns,
                paired_bootstrap(
                    {name: column.subset(indexes) for name, column in columns.items()},
                    seed=seed,
                    resamples=resamples,
                    ci_level=ci_level,
                    higher_is_better=direction,
                ),
            ),
        }
        for category, indexes in _category_indexes(pairs).items()
    }
    samples = _classify_samples(pairs, columns[primary], top_n=top_n)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "kind": baseline.kind,
        "dataset": baseline.payload.get("dataset"),
        "item_count": len(pairs.ids),
        "baseline": baseline.describe(),
        "candidate": candidate.describe(),
        "compatibility": compatibility,
        "bootstrap": {
            "method": "paired percentile bootstrap",
            "seed": seed,
            "resamples": resamples,
            "ci_level": ci_level,
        },
        "primary_metric": primary,
        "metrics": _metrics_payload(columns, overall),
        "by_category": by_category,
        "samples": samples,
        "items": _item_payload(pairs, columns),
    }


def _metrics_payload(
    columns: dict[str, MetricColumn], results: dict[str, BootstrapResult]
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for name, result in results.items():
        column = columns[name]
        entry = result.to_dict()
        entry["title"] = column.spec.title
        entry["unit"] = column.spec.unit
        entry["higher_is_better"] = column.spec.higher_is_better
        entry["dropped_baseline_only"] = column.baseline_only
        entry["dropped_candidate_only"] = column.candidate_only
        payload[name] = entry
    return payload


def _category_indexes(pairs: PairedItems) -> dict[str, list[int]]:
    indexes: dict[str, list[int]] = {}
    for index, category in enumerate(pairs.categories):
        indexes.setdefault(category, []).append(index)
    return dict(sorted(indexes.items()))


def _sample_sort_key(record: dict[str, Any]) -> tuple[float, str]:
    return -abs(float(record["delta"] or 0.0)), str(record["item_id"])


def _classify_samples(
    pairs: PairedItems, column: MetricColumn, *, top_n: int
) -> dict[str, object]:
    improved: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    tied: list[dict[str, Any]] = []
    not_applicable: list[dict[str, Any]] = []
    for index, item_id in enumerate(pairs.ids):
        baseline_value = column.baseline[index].value
        candidate_value = column.candidate[index].value
        record: dict[str, Any] = {
            "item_id": item_id,
            "category": pairs.categories[index],
            "question": pairs.questions[index],
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": None,
        }
        if baseline_value is None or candidate_value is None:
            not_applicable.append(record)
            continue
        delta = candidate_value - baseline_value
        record["delta"] = delta
        gain = delta if column.spec.higher_is_better else -delta
        if math.isclose(gain, 0.0, abs_tol=1e-12):
            tied.append(record)
        elif gain > 0:
            improved.append(record)
        else:
            regressed.append(record)
    # 变化幅度降序，同幅度按 item_id 排，保证同一份报告每次生成顺序一致
    improved.sort(key=_sample_sort_key)
    regressed.sort(key=_sample_sort_key)
    return {
        "metric": column.spec.name,
        "counts": {
            "improved": len(improved),
            "regressed": len(regressed),
            "tied": len(tied),
            "not_applicable": len(not_applicable),
        },
        "improved": improved[:top_n],
        "regressed": regressed[:top_n],
        "tied": tied,
        "not_applicable": not_applicable,
    }


def _item_payload(
    pairs: PairedItems, columns: dict[str, MetricColumn]
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for index, item_id in enumerate(pairs.ids):
        metrics: dict[str, object] = {}
        for name, column in columns.items():
            baseline_value = column.baseline[index].value
            candidate_value = column.candidate[index].value
            metrics[name] = {
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": None
                if baseline_value is None or candidate_value is None
                else candidate_value - baseline_value,
            }
        payload.append(
            {
                "item_id": item_id,
                "category": pairs.categories[index],
                "question": pairs.questions[index],
                "metrics": metrics,
            }
        )
    return payload


def markdown_report(payload: dict[str, object]) -> str:
    baseline = _as_dict(payload["baseline"])
    candidate = _as_dict(payload["candidate"])
    compatibility = _as_dict(payload["compatibility"])
    bootstrap = _as_dict(payload["bootstrap"])
    metrics = _as_dict(payload["metrics"])
    by_category = _as_dict(payload["by_category"])
    samples = _as_dict(payload["samples"])
    primary = str(payload["primary_metric"])
    lines = [
        f"# 评测对照：{baseline['label']} → {candidate['label']}",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- 数据集：`{payload['dataset']}`｜报告类型：`{payload['kind']}`"
        f"｜配对样本：{payload['item_count']} 条",
        f"- baseline：`{baseline['label']}` run=`{baseline['run_id']}` "
        f"git=`{baseline['git_sha']}` config=`{_short(baseline['config_hash'])}`",
        f"- candidate：`{candidate['label']}` run=`{candidate['run_id']}` "
        f"git=`{candidate['git_sha']}` config=`{_short(candidate['config_hash'])}`",
        f"- 显著性：配对百分位 bootstrap，resamples={bootstrap['resamples']}，"
        f"seed={bootstrap['seed']}，CI={float(bootstrap['ci_level']):.0%}",
        "",
        "## 配置差异",
        "",
    ]
    lines.extend(_config_section(compatibility))
    lines.extend(["", "## 总体指标", "", *_metric_table(metrics)])
    lines.extend(["", "## 分类别切片", ""])
    if by_category:
        for category, entry in by_category.items():
            slice_entry = _as_dict(entry)
            lines.append(f"### {category}（{slice_entry['item_count']} 条）")
            lines.append("")
            lines.extend(_metric_table(_as_dict(slice_entry["metrics"])))
            lines.append("")
    else:
        lines.extend(["无类别信息。", ""])
    lines.extend(_sample_section(samples, primary, metrics))
    lines.extend(
        [
            "## 口径与结论边界",
            "",
            "- **置信区间跨 0 即无显著差异，不得写成提升或回退**；"
            "显著性来自配对重采样，不是点估计的大小。",
            "- 比率类指标的 Δ 与置信区间是百分点差，不是相对涨幅。",
            "- 配对口径：任一跑批中不适用该指标的样本两侧一并剔除，"
            "因此这里的绝对值可能与单次跑批报告里的聚合值不同；剔除数量见"
            "「仅一侧适用」列。",
            "- 小切片（如 4 条的类别）置信区间必然很宽，这是样本量的真实反映，"
            "不是计算错误；类别结论只能当方向性线索。",
            "- 延迟只在同机同负载下可比：跨机器、跨时间的跑批，延迟差值不构成结论。",
            "- bootstrap 只度量抽样噪声，不能校正数据集偏差、标注错误或对 dev 集的过拟合。",
            "- 同 config_hash 的两次跑批做对照，得到的是噪声地板估计，不是策略收益。",
        ]
    )
    return "\n".join(lines) + "\n"


def _config_section(compatibility: dict[str, Any]) -> list[str]:
    diff = compatibility.get("config_diff") or {}
    if not diff:
        return [
            "两次跑批配置完全一致（config_hash 相同）。"
            "本次对照测的是跑批噪声地板，不能解读为策略收益。"
        ]
    lines = ["| 配置项 | baseline | candidate |", "|---|---|---|"]
    lines.extend(
        f"| `{key}` | {_cell(value['baseline'])} | {_cell(value['candidate'])} |"
        for key, value in diff.items()
    )
    controlled = compatibility.get("controlled_diff") or []
    if controlled:
        lines.extend(
            [
                "",
                f"⚠️ 受控配置项 {', '.join(f'`{key}`' for key in controlled)} 发生变化"
                "（已通过 --allow-config-drift 放行）：两侧指标的定义不完全相同，"
                "解读时必须一并说明。",
            ]
        )
    return lines


def _metric_table(metrics: dict[str, Any]) -> list[str]:
    lines = [
        "| 指标 | baseline | candidate | Δ | 95% CI | 判定 | 配对样本 | 仅一侧适用 |",
        "|---|---:|---:|---:|:---:|:---:|---:|---:|",
    ]
    for entry in metrics.values():
        unit = entry["unit"]
        dropped = int(entry["dropped_baseline_only"]) + int(
            entry["dropped_candidate_only"]
        )
        lines.append(
            f"| {entry['title']} | {_value(entry['baseline'], unit)} | "
            f"{_value(entry['candidate'], unit)} | {_delta(entry['delta'], unit)} | "
            f"[{_delta(entry['ci_low'], unit)}, {_delta(entry['ci_high'], unit)}] | "
            f"{_VERDICT_LABELS[str(entry['verdict'])]} | {entry['sample_size']} | {dropped} |"
        )
    return lines


def _sample_section(
    samples: dict[str, Any], primary: str, metrics: dict[str, Any]
) -> list[str]:
    counts = samples["counts"]
    spec = _as_dict(metrics[primary])
    title = spec["title"]
    unit = str(spec["unit"])
    lines = [
        f"## 逐样本变化（主指标：{title}）",
        "",
        f"- 变好 {counts['improved']} 条｜变差 {counts['regressed']} 条｜"
        f"持平 {counts['tied']} 条｜不适用 {counts['not_applicable']} 条",
        "",
    ]
    for key, heading in (("improved", "变好样本"), ("regressed", "变差样本")):
        rows = samples[key]
        lines.append(f"### {heading}")
        lines.append("")
        if not rows:
            lines.extend(["无。", ""])
            continue
        lines.append("| item_id | 类别 | 问题 | baseline | candidate | Δ |")
        lines.append("|---|---|---|---:|---:|---:|")
        lines.extend(
            f"| `{row['item_id']}` | {row['category']} | {_question(row['question'])} | "
            f"{_value(row['baseline'], unit)} | {_value(row['candidate'], unit)} | "
            f"{_delta(row['delta'], unit)} |"
            for row in rows
        )
        lines.append("")
    return lines


def _as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("报告节点必须是对象")
    return value


def _short(value: Any) -> str:
    return "N/A" if value is None else str(value)[:12]


def _cell(value: Any) -> str:
    return "—" if value is None else f"`{value}`"


def _question(value: Any, limit: int = 32) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _value(value: Any, unit: str) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    return f"{number:.1f}" if unit == "ms" else f"{number:.2%}"


def _delta(value: Any, unit: str) -> str:
    if value is None:
        return "N/A"
    # 浮点求和残差会渲染成 "-0.0"，先按展示精度归零，避免读成"下降"
    number = round(float(value), 1 if unit == "ms" else 4) or 0.0
    return f"{number:+.1f}" if unit == "ms" else f"{number:+.2%}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="配对比较两次评测跑批，输出快照 diff 与 bootstrap 置信区间"
    )
    parser.add_argument(
        "baseline", type=Path, help="baseline 的 report.json 或其所在目录"
    )
    parser.add_argument(
        "candidate", type=Path, help="candidate 的 report.json 或其所在目录"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--ci-level", type=float, default=DEFAULT_CI_LEVEL)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--primary-metric", default=None)
    parser.add_argument(
        "--allow-config-drift",
        action="store_true",
        help="允许受控配置项不同；只在明确知道两侧指标定义差异时使用",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = build_comparison(
        load_report(args.baseline),
        load_report(args.candidate),
        seed=args.seed,
        resamples=args.resamples,
        ci_level=args.ci_level,
        top_n=args.top_n,
        primary_metric=args.primary_metric,
        allow_config_drift=args.allow_config_drift,
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
