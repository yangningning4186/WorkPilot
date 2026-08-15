"""多策略配对矩阵：以一个基线策略为参照，比较 N 个策略的跑批结果。

E1 四分块策略对照就是这个工具的目标用法（docs/06 §2.1）：同一批 gold span、
同一套检索配置，只有分块方式不同，比较 span recall / nDCG / context precision /
冗余率 / 拒答 / 端到端质量 / 延迟 / 成本，并给出相对基线的配对 bootstrap 置信区间。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.strategy_matrix \\
      --manifest eval/outputs/chunk-strategies/<batch>/manifest.json \\
      --baseline heading \\
      --output-dir eval/outputs/strategy-matrix/<label>

只读已完成跑批导出的 report.json，不连数据库、不调模型、不重跑检索。

**fail-closed**：数据集不一致、item 缺失或重复、gold span 指纹不一致（混解析版本
或重标过）、跑批内有失败样本、受控检索配置不一致——任何一条不满足都直接拒绝出报告，
绝不降级成"能算多少算多少"。零分母指标标记为不可用，永远不写成 0。
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.report_metrics import (
    KIND_GENERATION,
    KIND_RETRIEVAL,
    METRICS,
    PRIMARY_METRIC,
    VERDICT_LABELS,
    LoadedReport,
    MetricSpec,
    as_dict,
    as_list,
    config_cell,
    config_diff,
    format_delta,
    format_value,
    gold_span_fingerprint,
    load_report,
    short_hash,
    truncate_question,
)
from eval.stats import (
    DEFAULT_CI_LEVEL,
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    INELIGIBLE,
    MetricSamples,
    RatioPoint,
    paired_bootstrap,
)

DEFAULT_DIVERGENCE = 0.5
CHUNK_MANIFEST_STRATEGIES = ("fixed", "heading", "recursive", "semantic")
CHUNK_MANIFEST_VARY_KEYS = ("chunk_strategy", "chunk_metadata")


class ValidationError(ValueError):
    """配对前置条件不满足。抛出即中止，不产出任何数字。"""


@dataclass(frozen=True)
class StrategyRun:
    name: str
    retrieval: LoadedReport
    generation: LoadedReport | None = None

    def report(self, kind: str) -> LoadedReport | None:
        return self.retrieval if kind == KIND_RETRIEVAL else self.generation


@dataclass(frozen=True)
class PairedMatrix:
    """所有策略对齐到同一条 item 轴之后的视图。"""

    ids: tuple[str, ...]
    categories: tuple[str, ...]
    questions: tuple[str, ...]
    answerable: tuple[bool, ...]
    items: dict[str, tuple[dict[str, Any], ...]]

    def category_indexes(self) -> dict[str, list[int]]:
        indexes: dict[str, list[int]] = {}
        for index, category in enumerate(self.categories):
            indexes.setdefault(category, []).append(index)
        return dict(sorted(indexes.items()))


# ----------------------------------------------------------------- fail-closed


def validate_runs(
    runs: Sequence[StrategyRun], *, baseline: str, vary_keys: Sequence[str]
) -> dict[str, object]:
    """所有前置校验集中在这里，任一条不满足即抛 ValidationError。"""
    names = [run.name for run in runs]
    if len(runs) < 2:
        raise ValidationError("至少需要两个策略才能对照")
    if len(set(names)) != len(names):
        raise ValidationError(f"策略名重复: {names}")
    if baseline not in names:
        raise ValidationError(f"基线策略 {baseline!r} 不在参与对照的策略里: {names}")

    has_generation = [run.generation is not None for run in runs]
    if any(has_generation) and not all(has_generation):
        missing = [run.name for run in runs if run.generation is None]
        raise ValidationError(
            f"生成轨报告必须覆盖全部策略，否则端到端指标比较的是不同子集；缺失: {missing}"
        )
    checks: list[dict[str, object]] = []
    checks.append(_check_kinds(runs))
    checks.append(_check_dataset(runs))
    checks.append(_check_failed_runs(runs))
    config = _check_retrieval_config(runs, baseline=baseline, vary_keys=vary_keys)
    checks.append(config["check"])  # type: ignore[arg-type]
    generation_diff: dict[str, dict[str, Any]] = {}
    if all(has_generation):
        generation = _check_generation_config(runs, baseline=baseline, vary_keys=vary_keys)
        checks.append(generation["check"])  # type: ignore[arg-type]
        generation_diff = generation["diff"]  # type: ignore[assignment]
        checks.append(_check_generation_chunk_strategy(runs))
    return {
        "checks": checks,
        "config_diff": config["diff"],
        "generation_config_diff": generation_diff,
        "vary_keys": list(vary_keys),
        "generation_included": all(has_generation),
    }


def _check_kinds(runs: Sequence[StrategyRun]) -> dict[str, object]:
    for run in runs:
        if run.retrieval.kind != KIND_RETRIEVAL:
            raise ValidationError(
                f"策略 {run.name} 的 --run 必须是检索跑批报告，实际是 {run.retrieval.kind}"
            )
        if run.generation is not None and run.generation.kind != KIND_GENERATION:
            raise ValidationError(
                f"策略 {run.name} 的 --generation 必须是生成跑批报告，"
                f"实际是 {run.generation.kind}"
            )
    return {"name": "报告类型正确", "passed": True, "detail": "检索轨 + 可选生成轨"}


def _check_dataset(runs: Sequence[StrategyRun]) -> dict[str, object]:
    datasets = {
        run.name: str(report.payload.get("dataset"))
        for run in runs
        for report in (run.retrieval, run.generation)
        if report is not None
    }
    unique = set(datasets.values())
    if len(unique) != 1:
        raise ValidationError(f"数据集不一致，跨数据集比较无效: {datasets}")
    return {"name": "数据集一致", "passed": True, "detail": unique.pop()}


def _check_failed_runs(runs: Sequence[StrategyRun]) -> dict[str, object]:
    for run in runs:
        for report in (run.retrieval, run.generation):
            if report is None:
                continue
            metrics = report.payload.get("metrics")
            errors = (
                int(metrics.get("error_count") or 0) if isinstance(metrics, dict) else 0
            )
            failed = [
                str(item["item_id"])
                for item in report.items
                if item.get("error") is not None
            ]
            if errors or failed:
                raise ValidationError(
                    f"策略 {run.name} 的跑批含失败样本，结果不完整: "
                    f"error_count={errors}, failed={failed[:5]}（{report.path}）"
                )
            missing = [
                str(item["item_id"])
                for item in report.items
                if report.kind == KIND_RETRIEVAL
                and item.get("answerable")
                and not isinstance(item.get("retrieval"), dict)
            ]
            if missing:
                raise ValidationError(
                    f"策略 {run.name} 有可答样本没有检索指标，跑批未完成或 gold 失效: "
                    f"{missing[:5]}（{report.path}）"
                )
    return {
        "name": "无失败样本",
        "passed": True,
        "detail": "全部 run 无 error、无缺失指标",
    }


def _check_retrieval_config(
    runs: Sequence[StrategyRun], *, baseline: str, vary_keys: Sequence[str]
) -> dict[str, object]:
    base = next(run for run in runs if run.name == baseline)
    diffs: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.name == baseline:
            continue
        diff = config_diff(
            base.retrieval.config, run.retrieval.config, ignore=vary_keys
        )
        if diff:
            detail = ", ".join(
                f"{key}: {value['baseline']!r} → {value['candidate']!r}"
                for key, value in diff.items()
            )
            raise ValidationError(
                f"策略 {run.name} 与基线 {baseline} 的检索配置不一致，"
                f"这不是单变量对照: {detail}。"
                "确属被对照的变量请用 --vary-key 显式声明"
            )
        varied = config_diff(base.retrieval.config, run.retrieval.config)
        diffs[run.name] = varied
    identical = all(not diff for diff in diffs.values())
    return {
        "check": {
            "name": "检索配置一致（仅 --vary-key 允许变动）",
            "passed": True,
            "detail": "所有 run 的检索配置完全相同（噪声地板口径）"
            if identical
            else "仅声明的变量键不同",
        },
        "diff": diffs,
    }


def _check_generation_config(
    runs: Sequence[StrategyRun], *, baseline: str, vary_keys: Sequence[str]
) -> dict[str, object]:
    """生成轨也必须是单变量对照。

    模型、prompt 指纹、token budget、拒答阈值任一不同, 端到端差异就不再只来自分块;
    检索轨查过一遍不代表生成轨也一致——两轨的 config 是分别写的。
    """
    base = next(run for run in runs if run.name == baseline)
    assert base.generation is not None
    diffs: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.name == baseline or run.generation is None:
            continue
        diff = config_diff(
            base.generation.config, run.generation.config, ignore=vary_keys
        )
        if diff:
            detail = ", ".join(
                f"{key}: {value['baseline']!r} → {value['candidate']!r}"
                for key, value in diff.items()
            )
            raise ValidationError(
                f"策略 {run.name} 与基线 {baseline} 的生成配置不一致，"
                f"端到端差异无法归因到分块: {detail}。"
                "确属被对照的变量请用 --vary-key 显式声明"
            )
        diffs[run.name] = config_diff(base.generation.config, run.generation.config)
    return {
        "check": {
            "name": "生成配置一致（模型 / prompt / token budget / 阈值）",
            "passed": True,
            "detail": "仅声明的变量键不同",
        },
        "diff": diffs,
    }


def _check_generation_chunk_strategy(runs: Sequence[StrategyRun]) -> dict[str, object]:
    """生成 run 的 config.chunk_strategy 必须与它挂的策略名一致。

    否则 manifest 里的策略名只是标签, 可能把 semantic 的报告挂在 fixed 名下, 结论整体翻转。

    只在"四个策略名恰好就是四套分块策略"时强制要求报告声明 chunk_strategy——
    矩阵本身是通用的 N 策略工具, 拿它比 prompt 或阈值时策略名与分块无关。
    其余情况下只要报告声明了, 就仍然核对是否自相矛盾。
    """
    is_chunk_matrix = {run.name for run in runs} == set(CHUNK_MANIFEST_STRATEGIES)
    mismatched: list[str] = []
    unlabeled: list[str] = []
    for run in runs:
        if run.generation is None:
            continue
        actual = run.generation.config.get("chunk_strategy")
        if actual is None:
            unlabeled.append(run.name)
        elif str(actual) != run.name and str(actual) in CHUNK_MANIFEST_STRATEGIES:
            mismatched.append(f"{run.name} 的报告实际跑在 {actual} 上")
    if mismatched:
        raise ValidationError(
            "生成报告的 chunk_strategy 与策略名对不上，报告挂错了位置: "
            + "; ".join(mismatched)
        )
    if is_chunk_matrix and unlabeled:
        raise ValidationError(
            "四分块策略对照要求生成报告记录 chunk_strategy，否则无法证明它跑在哪套分块上: "
            f"{unlabeled}。请用当前版本的 runner 重跑"
        )
    if unlabeled:
        return {
            "name": "生成报告的 chunk_strategy 与策略名一致",
            "passed": True,
            "detail": f"非分块对照，{len(unlabeled)} 个 run 未声明 chunk_strategy",
        }
    return {
        "name": "生成报告的 chunk_strategy 与策略名一致",
        "passed": True,
        "detail": "逐个 run 核对 config.chunk_strategy",
    }


def pair_matrix(
    runs: Sequence[StrategyRun], *, kind: str
) -> tuple[PairedMatrix, list[dict[str, object]]]:
    """按 item_id 严格配对，并核对类别、可答性与 gold span 指纹。"""
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        report = run.report(kind)
        if report is None:
            raise ValidationError(f"策略 {run.name} 缺少 {kind} 报告")
        by_id: dict[str, dict[str, Any]] = {}
        for item in report.items:
            item_id = str(item["item_id"])
            if item_id in by_id:
                raise ValidationError(
                    f"策略 {run.name} 的报告中出现重复 item_id: {item_id}（{report.path}）"
                )
            by_id[item_id] = item
        indexed[run.name] = by_id

    reference = runs[0].name
    expected = set(indexed[reference])
    for name, by_id in indexed.items():
        if set(by_id) != expected:
            missing = sorted(expected - set(by_id))
            extra = sorted(set(by_id) - expected)
            raise ValidationError(
                f"策略 {name} 与 {reference} 的 item 集合不一致，无法严格配对: "
                f"缺失={missing[:5]}, 多出={extra[:5]}"
            )
    ids = tuple(sorted(expected))
    for item_id in ids:
        base_item = indexed[reference][item_id]
        for name, by_id in indexed.items():
            item = by_id[item_id]
            for field in ("category", "answerable"):
                if item.get(field) != base_item.get(field):
                    raise ValidationError(
                        f"item {item_id} 的 {field} 在策略 {name} 与 {reference} 之间不一致，"
                        "标注已漂移，配对无效"
                    )
            # 两轨的报告都带 span_diagnostics，所以指纹核对对生成轨同样生效；
            # 旧报告没有这个字段时两侧同为空元组，不会误报。
            if gold_span_fingerprint(item) != gold_span_fingerprint(base_item):
                raise ValidationError(
                    f"item {item_id} 的 gold span 指纹在策略 {name} 与 {reference} 之间不一致："
                    "混了解析版本或重新标注过，跨策略比较无效"
                )
    matrix = PairedMatrix(
        ids=ids,
        categories=tuple(str(indexed[reference][i]["category"]) for i in ids),
        questions=tuple(str(indexed[reference][i].get("question", "")) for i in ids),
        answerable=tuple(bool(indexed[reference][i].get("answerable")) for i in ids),
        items={
            name: tuple(by_id[item_id] for item_id in ids)
            for name, by_id in indexed.items()
        },
    )
    checks: list[dict[str, object]] = [
        {"name": f"{kind} item 集合一致", "passed": True, "detail": f"{len(ids)} 条"},
        {"name": f"{kind} 无重复 item_id", "passed": True, "detail": "唯一"},
        {"name": f"{kind} 类别与可答性一致", "passed": True, "detail": "逐条核对"},
        {
            "name": f"{kind} gold span 指纹一致",
            "passed": True,
            "detail": "version + 字符区间 + quote 逐条核对",
        },
    ]
    return matrix, checks


# --------------------------------------------------------------------- 指标计算


@dataclass(frozen=True)
class MetricColumns:
    spec: MetricSpec
    points: dict[str, tuple[RatioPoint, ...]]
    own_ineligible: dict[str, int]
    common_eligible: int

    def subset(
        self, baseline: str, strategy: str, indexes: Sequence[int]
    ) -> MetricSamples:
        return MetricSamples(
            baseline=tuple(self.points[baseline][index] for index in indexes),
            candidate=tuple(self.points[strategy][index] for index in indexes),
        )

    def value(
        self, strategy: str, indexes: Sequence[int] | None = None
    ) -> float | None:
        points = self.points[strategy]
        selected = points if indexes is None else [points[index] for index in indexes]
        numerator = sum(p.numerator for p in selected if p.denominator)
        denominator = sum(p.denominator for p in selected if p.denominator)
        return numerator / denominator if denominator else None


def build_metric_columns(
    matrix: PairedMatrix,
    specs: Sequence[MetricSpec],
    configs: dict[str, dict[str, Any]],
) -> dict[str, MetricColumns]:
    """逐指标抽取各策略的贡献，并取所有策略的**公共可比样本**。

    N 路对照必须用交集：只要有一个策略在某条样本上不适用该指标，
    这条样本就从该指标的所有策略里一起剔除，否则矩阵里的列不同源，
    横向比较没有意义。剔除数量按策略单独记录。
    """
    strategies = list(matrix.items)
    columns: dict[str, MetricColumns] = {}
    for spec in specs:
        raw = {
            name: [spec.extract(item, configs[name]) for item in matrix.items[name]]
            for name in strategies
        }
        mask = [
            all(raw[name][index].eligible for name in strategies)
            for index in range(len(matrix.ids))
        ]
        columns[spec.name] = MetricColumns(
            spec=spec,
            points={
                name: tuple(
                    point if keep else INELIGIBLE
                    for point, keep in zip(raw[name], mask, strict=True)
                )
                for name in strategies
            },
            own_ineligible={
                name: sum(not point.eligible for point in raw[name])
                for name in strategies
            },
            common_eligible=sum(mask),
        )
    return columns


def _win_loss_tie(
    column: MetricColumns, baseline: str, strategy: str, indexes: Sequence[int]
) -> dict[str, int]:
    wins = losses = ties = 0
    for index in indexes:
        base = column.points[baseline][index].value
        other = column.points[strategy][index].value
        if base is None or other is None:
            continue
        gain = other - base if column.spec.higher_is_better else base - other
        if abs(gain) <= 1e-12:
            ties += 1
        elif gain > 0:
            wins += 1
        else:
            losses += 1
    return {"wins": wins, "losses": losses, "ties": ties}


def _metric_payload(
    column: MetricColumns,
    *,
    baseline: str,
    strategies: Sequence[str],
    indexes: Sequence[int],
    seed: int,
    resamples: int,
    ci_level: float,
) -> dict[str, object]:
    others = [name for name in strategies if name != baseline]
    # 公共可比样本在当前切片内的数量；为 0 时只报"不可用"，绝不落成 0 值
    eligible = sum(column.points[baseline][index].eligible for index in indexes)
    if eligible == 0:
        reason = (
            "所有跑批都没有记录该字段"
            if all(
                count == len(column.points[name])
                for name, count in column.own_ineligible.items()
            )
            else "配对后没有公共可比样本"
        )
        return {
            "title": column.spec.title,
            "unit": column.spec.unit,
            "higher_is_better": column.spec.higher_is_better,
            "note": column.spec.note,
            "status": "unavailable",
            "reason": reason,
            "sample_size": 0,
            "by_strategy": {name: None for name in strategies},
            "vs_baseline": {},
        }
    # 所有策略共用同一批重采样下标，列与列之间的区间可以并排解读
    results = paired_bootstrap(
        {name: column.subset(baseline, name, indexes) for name in others},
        seed=seed,
        resamples=resamples,
        ci_level=ci_level,
        higher_is_better={name: column.spec.higher_is_better for name in others},
    )
    vs_baseline: dict[str, object] = {}
    for name in others:
        result = results[name]
        entry = result.to_dict()
        entry["crosses_zero"] = (
            None
            if result.ci_low is None or result.ci_high is None
            else result.ci_low <= 0 <= result.ci_high
        )
        entry.update(_win_loss_tie(column, baseline, name, indexes))
        vs_baseline[name] = entry
    sample_size = next(iter(results.values())).sample_size if results else 0
    return {
        "title": column.spec.title,
        "unit": column.spec.unit,
        "higher_is_better": column.spec.higher_is_better,
        "note": column.spec.note,
        "status": "ok",
        "sample_size": sample_size,
        "own_ineligible": dict(column.own_ineligible),
        "by_strategy": {name: column.value(name, indexes) for name in strategies},
        "vs_baseline": vs_baseline,
    }


def _outliers(
    matrix: PairedMatrix,
    column: MetricColumns,
    *,
    strategies: Sequence[str],
    top_n: int,
    divergence: float,
) -> dict[str, object]:
    """异常样本：策略间分歧最大的、全体未命中的、全体满分的。"""
    divergent: list[dict[str, Any]] = []
    universal_zero: list[dict[str, Any]] = []
    universal_max: list[dict[str, Any]] = []
    for index, item_id in enumerate(matrix.ids):
        values = {name: column.points[name][index].value for name in strategies}
        if any(value is None for value in values.values()):
            continue
        numbers = [float(value) for value in values.values() if value is not None]
        spread = max(numbers) - min(numbers)
        record: dict[str, Any] = {
            "item_id": item_id,
            "category": matrix.categories[index],
            "question": matrix.questions[index],
            "values": values,
            "spread": spread,
        }
        if spread >= divergence:
            divergent.append(record)
        if all(abs(number) <= 1e-12 for number in numbers):
            universal_zero.append(record)
        elif column.spec.unit == "ratio" and all(
            abs(number - 1.0) <= 1e-12 for number in numbers
        ):
            universal_max.append(record)
    divergent.sort(
        key=lambda record: (-float(record["spread"]), str(record["item_id"]))
    )
    return {
        "metric": column.spec.name,
        "divergence_threshold": divergence,
        "counts": {
            "divergent": len(divergent),
            "universal_zero": len(universal_zero),
            "universal_max": len(universal_max),
        },
        "divergent": divergent[:top_n],
        "universal_zero": universal_zero,
        "universal_max": universal_max,
    }


def build_matrix_report(
    runs: Sequence[StrategyRun],
    *,
    baseline: str,
    vary_keys: Sequence[str] = (),
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    top_n: int = 10,
    primary_metric: str | None = None,
    divergence: float = DEFAULT_DIVERGENCE,
) -> dict[str, object]:
    validation = validate_runs(runs, baseline=baseline, vary_keys=vary_keys)
    strategies = [run.name for run in runs]
    checks = as_list(validation["checks"])

    retrieval_matrix, retrieval_checks = pair_matrix(runs, kind=KIND_RETRIEVAL)
    checks.extend(retrieval_checks)
    tracks: dict[str, object] = {}
    primary = primary_metric or PRIMARY_METRIC[KIND_RETRIEVAL]
    if primary not in {spec.name for spec in METRICS[KIND_RETRIEVAL]}:
        raise ValidationError(
            f"未知的 primary metric: {primary}，可选: "
            f"{sorted(spec.name for spec in METRICS[KIND_RETRIEVAL])}"
        )
    retrieval_columns = build_metric_columns(
        retrieval_matrix,
        METRICS[KIND_RETRIEVAL],
        {run.name: run.retrieval.config for run in runs},
    )
    if retrieval_columns[primary].common_eligible == 0:
        raise ValidationError(
            f"主指标 {primary} 没有任何公共可比样本，对照无意义（可能全是不可答题或 gold 缺失）"
        )
    all_indexes = list(range(len(retrieval_matrix.ids)))
    tracks[KIND_RETRIEVAL] = {
        name: _metric_payload(
            column,
            baseline=baseline,
            strategies=strategies,
            indexes=all_indexes,
            seed=seed,
            resamples=resamples,
            ci_level=ci_level,
        )
        for name, column in retrieval_columns.items()
    }
    by_category = {
        category: {
            "item_count": len(indexes),
            "metrics": {
                primary: _metric_payload(
                    retrieval_columns[primary],
                    baseline=baseline,
                    strategies=strategies,
                    indexes=indexes,
                    seed=seed,
                    resamples=resamples,
                    ci_level=ci_level,
                )
            },
        }
        for category, indexes in retrieval_matrix.category_indexes().items()
    }
    outliers = _outliers(
        retrieval_matrix,
        retrieval_columns[primary],
        strategies=strategies,
        top_n=top_n,
        divergence=divergence,
    )

    if validation["generation_included"]:
        generation_matrix, generation_checks = pair_matrix(runs, kind=KIND_GENERATION)
        checks.extend(generation_checks)
        if generation_matrix.ids != retrieval_matrix.ids:
            raise ValidationError("检索轨与生成轨的 item 集合不一致，端到端比较无效")
        generation_columns = build_metric_columns(
            generation_matrix,
            METRICS[KIND_GENERATION],
            {run.name: run.generation.config for run in runs if run.generation},
        )
        tracks[KIND_GENERATION] = {
            name: _metric_payload(
                column,
                baseline=baseline,
                strategies=strategies,
                indexes=list(range(len(generation_matrix.ids))),
                seed=seed,
                resamples=resamples,
                ci_level=ci_level,
            )
            for name, column in generation_columns.items()
        }

    unavailable = {
        f"{track}.{name}": entry.get("reason")
        for track, metrics in tracks.items()
        for name, entry in as_dict(metrics).items()
        if as_dict(entry)["status"] == "unavailable"
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": runs[0].retrieval.payload.get("dataset"),
        "baseline_strategy": baseline,
        "strategies": strategies,
        "item_count": len(retrieval_matrix.ids),
        "answerable_count": sum(retrieval_matrix.answerable),
        "unanswerable_count": sum(not flag for flag in retrieval_matrix.answerable),
        "category_counts": {
            category: len(indexes)
            for category, indexes in retrieval_matrix.category_indexes().items()
        },
        "runs": {
            run.name: {
                "retrieval": run.retrieval.describe(),
                "generation": run.generation.describe() if run.generation else None,
            }
            for run in runs
        },
        "validation": {
            "checks": checks,
            "config_diff": validation["config_diff"],
            "generation_config_diff": validation["generation_config_diff"],
            "vary_keys": validation["vary_keys"],
        },
        "bootstrap": {
            "method": "paired percentile bootstrap",
            "seed": seed,
            "resamples": resamples,
            "ci_level": ci_level,
        },
        "primary_metric": primary,
        "metrics": tracks,
        "by_category": by_category,
        "outliers": outliers,
        "unavailable_metrics": unavailable,
    }


# ----------------------------------------------------------------------- 报告


def markdown_report(payload: dict[str, object]) -> str:
    strategies = [str(name) for name in as_list(payload["strategies"])]
    baseline = str(payload["baseline_strategy"])
    bootstrap = as_dict(payload["bootstrap"])
    lines = [
        f"# 策略对照矩阵：{payload['dataset']} · {len(strategies)} 策略",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- 配对样本：{payload['item_count']} 条"
        f"（可答 {payload['answerable_count']}，不可答 {payload['unanswerable_count']}）",
        f"- 类别分布：{_counts(as_dict(payload['category_counts']))}",
        f"- 基线策略：`{baseline}`",
        f"- 显著性：配对百分位 bootstrap，resamples={bootstrap['resamples']}，"
        f"seed={bootstrap['seed']}，CI={float(bootstrap['ci_level']):.0%}",
        "",
        "## 参与对照的跑批",
        "",
        "| 策略 | 检索 run | 生成 run | git | config_hash |",
        "|---|---|---|---|---|",
    ]
    runs = as_dict(payload["runs"])
    for name in strategies:
        entry = as_dict(runs[name])
        retrieval = as_dict(entry["retrieval"])
        generation = entry["generation"]
        mark = "（基线）" if name == baseline else ""
        lines.append(
            f"| `{name}`{mark} | `{retrieval['label']}` | "
            f"{'`' + str(as_dict(generation)['label']) + '`' if generation else '—'} | "
            f"`{short_hash(retrieval['git_sha'])}` | "
            f"`{short_hash(retrieval['config_hash'])}` |"
        )
    lines.extend(["", "## fail-closed 校验", "", "| 检查项 | 结果 |", "|---|---|"])
    for check in list(as_dict(payload["validation"])["checks"]):
        entry = as_dict(check)
        lines.append(f"| {entry['name']} | ✅ {entry['detail']} |")
    lines.extend(_variable_section(as_dict(payload["validation"])))

    for track, title in ((KIND_RETRIEVAL, "检索轨"), (KIND_GENERATION, "端到端生成轨")):
        metrics = as_dict(payload["metrics"]).get(track)
        if metrics is None:
            continue
        lines.extend(["", f"## {title}指标矩阵", ""])
        lines.extend(_absolute_table(as_dict(metrics), strategies, baseline))
        lines.extend(["", f"### {title}相对基线的差值与 95% 置信区间", ""])
        lines.extend(_delta_tables(as_dict(metrics), strategies, baseline))

    lines.extend(_category_section(payload, strategies, baseline))
    lines.extend(_outlier_section(payload, strategies))
    lines.extend(_unavailable_section(payload))
    lines.extend(
        [
            "",
            "## 口径与结论边界",
            "",
            "- **置信区间跨 0 即无显著差异，不得写成提升或回退**；"
            "「跨零」列直接给出这一判定，胜/负/平是逐样本方向，不能替代显著性。",
            "- 每个指标取所有策略的**公共可比样本**：只要一个策略在该样本上不适用，"
            "所有策略一起剔除，因此不同指标的样本量不同，且可能小于总样本数。",
            "- 比率类指标的 Δ 与置信区间是百分点差，不是相对涨幅。",
            "- 冗余率与检索上下文 token 是**检索侧成本**口径；"
            "生成侧 token 与金额需要跑批导出逐条用量，未记录时标记为不可用，不写成 0。",
            "- 端到端质量只含规则轨指标；语义正确性需要校准过的 Judge，M0 尚未具备。",
            "- 小切片置信区间必然很宽，类别结论只能当方向性线索。",
            "- bootstrap 只度量抽样噪声，不能校正数据集偏差、标注错误或对 dev 集的过拟合。",
        ]
    )
    return "\n".join(lines) + "\n"


def _counts(counts: dict[str, Any]) -> str:
    return " / ".join(f"{key} {value}" for key, value in counts.items())


def _variable_section(validation: dict[str, Any]) -> list[str]:
    vary_keys = validation.get("vary_keys") or []
    diff = validation.get("config_diff") or {}
    lines = ["", "### 被对照的变量", ""]
    if not vary_keys:
        lines.append("未声明 `--vary-key`：各策略的检索配置完全相同，")
        lines.append("差异只来自跑批之外的因素（分块产物本身）。")
        return lines
    lines.append(f"声明允许变动的配置键：{', '.join(f'`{key}`' for key in vary_keys)}")
    for title, entries in (
        ("检索轨", diff),
        ("生成轨", validation.get("generation_config_diff") or {}),
    ):
        if not entries:
            continue
        lines.extend(["", f"**{title}**", "", "| 策略 | 变动的配置 |", "|---|---|"])
        for name, entry in entries.items():
            detail = (
                "、".join(
                    f"`{key}`: {config_cell(value['baseline'])} → {config_cell(value['candidate'])}"
                    for key, value in entry.items()
                )
                or "无"
            )
            lines.append(f"| `{name}` | {detail} |")
    return lines


def _absolute_table(
    metrics: dict[str, Any], strategies: Sequence[str], baseline: str
) -> list[str]:
    header = " | ".join(
        f"{name}（基线）" if name == baseline else name for name in strategies
    )
    lines = [
        f"| 指标 | 有效样本 | {header} |",
        "|---|---:|" + "---:|" * len(strategies),
    ]
    for entry in metrics.values():
        if entry["status"] != "ok":
            cells = " | ".join("不可用" for _ in strategies)
            lines.append(f"| {entry['title']} | 0 | {cells} |")
            continue
        cells = " | ".join(
            format_value(entry["by_strategy"][name], entry["unit"])
            for name in strategies
        )
        lines.append(f"| {entry['title']} | {entry['sample_size']} | {cells} |")
    return lines


def _delta_tables(
    metrics: dict[str, Any], strategies: Sequence[str], baseline: str
) -> list[str]:
    lines: list[str] = []
    others = [name for name in strategies if name != baseline]
    for entry in metrics.values():
        if entry["status"] != "ok":
            continue
        lines.append(
            f"**{entry['title']}**（基线 {baseline}"
            f" = {format_value(entry['by_strategy'][baseline], entry['unit'])}）"
        )
        lines.append("")
        lines.append("| 策略 | Δ | 95% CI | 跨零 | 判定 | 胜 | 负 | 平 |")
        lines.append("|---|---:|:---:|:---:|:---:|---:|---:|---:|")
        for name in others:
            delta = as_dict(entry["vs_baseline"][name])
            crosses = delta["crosses_zero"]
            lines.append(
                f"| `{name}` | {format_delta(delta['delta'], entry['unit'])} | "
                f"[{format_delta(delta['ci_low'], entry['unit'])}, "
                f"{format_delta(delta['ci_high'], entry['unit'])}] | "
                f"{'是' if crosses else '否' if crosses is not None else 'N/A'} | "
                f"{VERDICT_LABELS[str(delta['verdict'])]} | "
                f"{delta['wins']} | {delta['losses']} | {delta['ties']} |"
            )
        lines.append("")
    return lines


def _category_section(
    payload: dict[str, object], strategies: Sequence[str], baseline: str
) -> list[str]:
    primary = str(payload["primary_metric"])
    by_category = as_dict(payload["by_category"])
    header = " | ".join(
        f"{name}（基线）" if name == baseline else name for name in strategies
    )
    lines = [
        "",
        f"## 分类别切片（主指标：{primary}）",
        "",
        f"| 类别 | 样本 | 有效 | {header} |",
        "|---|---:|---:|" + "---:|" * len(strategies),
    ]
    for category, entry in by_category.items():
        slice_entry = as_dict(entry)
        metric = as_dict(as_dict(slice_entry["metrics"])[primary])
        if metric["status"] != "ok":
            cells = " | ".join("不可用" for _ in strategies)
            lines.append(f"| {category} | {slice_entry['item_count']} | 0 | {cells} |")
            continue
        cells = " | ".join(
            format_value(metric["by_strategy"][name], metric["unit"])
            for name in strategies
        )
        lines.append(
            f"| {category} | {slice_entry['item_count']} | {metric['sample_size']} | {cells} |"
        )
    return lines


def _outlier_section(
    payload: dict[str, object], strategies: Sequence[str]
) -> list[str]:
    outliers = as_dict(payload["outliers"])
    counts = as_dict(outliers["counts"])
    lines = [
        "",
        f"## 异常样本（主指标：{outliers['metric']}）",
        "",
        f"- 策略间分歧 ≥ {float(outliers['divergence_threshold']):.0%}：{counts['divergent']} 条"
        f"｜所有策略均未命中：{counts['universal_zero']} 条"
        f"｜所有策略满分（无分辨力）：{counts['universal_max']} 条",
        "",
    ]
    for key, heading in (
        ("divergent", "策略间分歧最大"),
        ("universal_zero", "所有策略都未命中（应归因语料或标注，不是策略差异）"),
    ):
        rows = as_list(outliers[key])
        lines.extend([f"### {heading}", ""])
        if not rows:
            lines.extend(["无。", ""])
            continue
        lines.append("| item_id | 类别 | 问题 | " + " | ".join(strategies) + " |")
        lines.append("|---|---|---|" + "---:|" * len(strategies))
        for row in rows:
            record = as_dict(row)
            values = as_dict(record["values"])
            cells = " | ".join(
                format_value(values[name], "ratio") for name in strategies
            )
            lines.append(
                f"| `{record['item_id']}` | {record['category']} | "
                f"{truncate_question(record['question'])} | {cells} |"
            )
        lines.append("")
    return lines


def _unavailable_section(payload: dict[str, object]) -> list[str]:
    unavailable = as_dict(payload["unavailable_metrics"])
    if not unavailable:
        return []
    lines = [
        "",
        "## 不可用指标（标记为不可用，不是 0）",
        "",
        "| 指标 | 原因 |",
        "|---|---|",
    ]
    lines.extend(f"| `{name}` | {reason} |" for name, reason in unavailable.items())
    return lines


# ------------------------------------------------------------------------- CLI


def _parse_assignment(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name.strip() or not path.strip():
        raise argparse.ArgumentTypeError(
            f"格式必须是 名称=路径，实际收到: {value!r}（例如 fixed-window=eval/outputs/...）"
        )
    return name.strip(), Path(path.strip())


def load_chunk_strategy_manifest(path: Path, *, track: str = "retrieval") -> list[tuple[str, Path]]:
    """把 runner manifest 转成矩阵的四条报告输入。

    检索轨与生成轨的 manifest 结构相同(`runs.<策略>.report`), 所以共用一个读取器;
    `track` 只用于把错误信息说清楚是哪一轨。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("runs"), dict):
        raise ValidationError(f"{track} manifest 缺少 runs 对象: {path}")
    entries = payload["runs"]
    actual = set(entries)
    expected = set(CHUNK_MANIFEST_STRATEGIES)
    if actual != expected:
        raise ValidationError(
            f"{track} manifest 必须恰好包含四套策略: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )

    runs: list[tuple[str, Path]] = []
    for strategy in CHUNK_MANIFEST_STRATEGIES:
        entry = entries[strategy]
        if not isinstance(entry, dict) or not entry.get("run_id"):
            raise ValidationError(f"{track} manifest 策略 {strategy} 缺少 run_id")
        report_value = entry.get("report")
        if not isinstance(report_value, str) or not report_value.strip():
            raise ValidationError(
                f"{track} manifest 策略 {strategy} 没有 report.json; "
                "复用旧 run 时请重新执行 runner --no-reuse 以导出可比较报告"
            )
        report = Path(report_value)
        if report.suffix == ".md":
            report = report.with_name("report.json")
        if not report.exists() and not report.is_absolute():
            report = path.parent / report
        if not report.is_file():
            raise ValidationError(f"{track} manifest 策略 {strategy} 的报告不存在: {report}")
        runs.append((strategy, report))
    return runs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="多策略配对矩阵：严格配对 + 相对基线的 bootstrap 置信区间"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--run",
        type=_parse_assignment,
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="策略名=检索跑批 report.json（或其目录），至少两个",
    )
    source.add_argument(
        "--manifest",
        type=Path,
        help="chunk_strategy_runner 生成的 manifest.json; 自动载入四策略报告",
    )
    parser.add_argument(
        "--generation",
        type=_parse_assignment,
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="策略名=生成跑批报告；给了就必须覆盖全部策略",
    )
    parser.add_argument(
        "--generation-manifest",
        type=Path,
        default=None,
        help="generation_strategy_runner 生成的 manifest.json; 自动载入四策略生成报告",
    )
    parser.add_argument("--baseline", default=None, help="基线策略名，默认第一个 --run")
    parser.add_argument(
        "--vary-key",
        action="append",
        default=[],
        help="声明允许在各策略间不同的 config 键，例如 chunk_strategy",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--ci-level", type=float, default=DEFAULT_CI_LEVEL)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--primary-metric", default=None)
    parser.add_argument("--divergence", type=float, default=DEFAULT_DIVERGENCE)
    return parser.parse_args()


def build_runs(
    runs: Sequence[tuple[str, Path]], generations: Sequence[tuple[str, Path]]
) -> list[StrategyRun]:
    generation_paths = dict(generations)
    if len(generation_paths) != len(generations):
        raise ValidationError("--generation 出现重复的策略名")
    names = [name for name, _ in runs]
    unknown = sorted(set(generation_paths) - set(names))
    if unknown:
        raise ValidationError(f"--generation 里的策略名不在 --run 中: {unknown}")
    return [
        StrategyRun(
            name=name,
            retrieval=load_report(path),
            generation=load_report(generation_paths[name])
            if name in generation_paths
            else None,
        )
        for name, path in runs
    ]


def main() -> None:
    args = _parse_args()
    run_inputs = (
        load_chunk_strategy_manifest(args.manifest) if args.manifest else args.run
    )
    if args.generation and args.generation_manifest:
        raise ValidationError("--generation 与 --generation-manifest 只能给一个")
    generation_inputs = (
        load_chunk_strategy_manifest(args.generation_manifest, track="generation")
        if args.generation_manifest
        else args.generation
    )
    vary_keys = list(args.vary_key)
    if args.manifest or args.generation_manifest:
        vary_keys = list(dict.fromkeys([*vary_keys, *CHUNK_MANIFEST_VARY_KEYS]))
    runs = build_runs(run_inputs, generation_inputs)
    payload = build_matrix_report(
        runs,
        baseline=args.baseline or ("heading" if args.manifest else runs[0].name),
        vary_keys=vary_keys,
        seed=args.seed,
        resamples=args.resamples,
        ci_level=args.ci_level,
        top_n=args.top_n,
        primary_metric=args.primary_metric,
        divergence=args.divergence,
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
