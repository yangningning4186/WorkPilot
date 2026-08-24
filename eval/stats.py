"""逐样本 paired bootstrap 置信区间（见 docs/06-评测体系.md §4.3）。

两次跑批的指标差值不能只看点估计：检索与生成都有随机性，
"涨了 2 个点"可能完全落在噪声里。配对重采样给出 Δ 的 95% 置信区间，
**区间跨 0 就是"无显著差异"，不允许写成提升**。

配对的含义是：重采样的是 item，而不是分别对两次跑批独立重采样。
同一次重采样里两个跑批看到的是同一批 item，样本难度的波动因此被抵消掉。
"""

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from random import Random

DEFAULT_SEED = 12345
DEFAULT_RESAMPLES = 10000
DEFAULT_CI_LEVEL = 0.95


@dataclass(frozen=True)
class RatioPoint:
    """单个 item 对某指标的贡献，聚合值 = Σnumerator / Σdenominator。

    `denominator=0` 表示该 item 不参与这个指标（不可答题没有检索指标、
    拒答样本没有引用对齐率），重采样时它既不进分子也不进分母。
    这样 macro 平均（denominator 恒为 1）和 micro 比率（引用对齐 aligned/total）
    共用一套重采样逻辑，不必为两种口径各写一遍。
    """

    numerator: float
    denominator: float

    @property
    def eligible(self) -> bool:
        return self.denominator > 0

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None


INELIGIBLE = RatioPoint(0.0, 0.0)


@dataclass(frozen=True)
class MetricSamples:
    """同一条 item 轴上，两个跑批对某指标的逐样本贡献。"""

    baseline: tuple[RatioPoint, ...]
    candidate: tuple[RatioPoint, ...]

    def __post_init__(self) -> None:
        if len(self.baseline) != len(self.candidate):
            raise ValueError("配对样本数量必须一致，先按 item_id 对齐再传入")


@dataclass(frozen=True)
class BootstrapResult:
    sample_size: int
    baseline: float | None
    candidate: float | None
    delta: float | None
    ci_low: float | None
    ci_high: float | None
    ci_level: float
    seed: int
    resamples: int
    effective_resamples: int
    verdict: str

    @property
    def significant(self) -> bool:
        return self.verdict in {"improved", "regressed"}

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "ci_level": self.ci_level,
            "seed": self.seed,
            "resamples": self.resamples,
            "effective_resamples": self.effective_resamples,
            "verdict": self.verdict,
            "significant": self.significant,
        }


def paired_bootstrap(
    metrics: Mapping[str, MetricSamples],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
    higher_is_better: Mapping[str, bool] | None = None,
) -> dict[str, BootstrapResult]:
    """对一组共享 item 轴的指标同时做配对 bootstrap。

    所有指标共用同一批重采样下标，这样"span recall 涨了但 context precision 跌了"
    这类结论来自同一个重采样宇宙，指标之间的置信区间是可以并排解读的。
    """
    if not metrics:
        return {}
    if resamples < 1:
        raise ValueError("resamples 必须为正整数")
    if not 0 < ci_level < 1:
        raise ValueError("ci_level 必须位于 (0,1)")
    sizes = {len(samples.baseline) for samples in metrics.values()}
    if len(sizes) > 1:
        raise ValueError("所有指标必须共享同一条 item 轴，长度不一致无法联合重采样")
    item_count = sizes.pop()
    direction = higher_is_better or {}

    deltas: dict[str, list[float]] = {name: [] for name in metrics}
    if item_count:
        rng = Random(seed)
        for _ in range(resamples):
            weights = Counter(rng.randrange(item_count) for _ in range(item_count)).items()
            for name, samples in metrics.items():
                baseline = _ratio(samples.baseline, weights)
                candidate = _ratio(samples.candidate, weights)
                # 重采样恰好没抽到任何有效样本时跳过，不能当成 0 计入分布
                if baseline is None or candidate is None:
                    continue
                deltas[name].append(candidate - baseline)

    full_weights = [(index, 1) for index in range(item_count)]
    results: dict[str, BootstrapResult] = {}
    for name, samples in metrics.items():
        baseline = _ratio(samples.baseline, full_weights)
        candidate = _ratio(samples.candidate, full_weights)
        delta = None if baseline is None or candidate is None else candidate - baseline
        drawn = sorted(deltas[name])
        low, high = _confidence_interval(drawn, ci_level)
        results[name] = BootstrapResult(
            sample_size=sum(
                point.eligible and other.eligible
                for point, other in zip(samples.baseline, samples.candidate, strict=True)
            ),
            baseline=baseline,
            candidate=candidate,
            delta=delta,
            ci_low=low,
            ci_high=high,
            ci_level=ci_level,
            seed=seed,
            resamples=resamples,
            effective_resamples=len(drawn),
            verdict=_verdict(low, high, higher_is_better=direction.get(name, True)),
        )
    return results


def _ratio(points: tuple[RatioPoint, ...], weights: Iterable[tuple[int, int]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for index, weight in weights:
        point = points[index]
        if point.denominator:
            numerator += weight * point.numerator
            denominator += weight * point.denominator
    return numerator / denominator if denominator else None


def _confidence_interval(
    sorted_deltas: list[float], ci_level: float
) -> tuple[float | None, float | None]:
    if not sorted_deltas:
        return None, None
    tail = (1 - ci_level) / 2
    return (
        _percentile(sorted_deltas, tail),
        _percentile(sorted_deltas, 1 - tail),
    )


def _percentile(sorted_values: list[float], quantile: float) -> float:
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _verdict(ci_low: float | None, ci_high: float | None, *, higher_is_better: bool) -> str:
    if ci_low is None or ci_high is None:
        return "not_applicable"
    if ci_low > 0:
        return "improved" if higher_is_better else "regressed"
    if ci_high < 0:
        return "regressed" if higher_is_better else "improved"
    return "inconclusive"
