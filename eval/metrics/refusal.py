from dataclasses import asdict, dataclass
from itertools import pairwise


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    macro_f1: float
    answerable_f1: float
    unanswerable_f1: float
    accuracy: float
    true_answerable: int
    false_answerable: int
    true_refusal: int
    false_refusal: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class RefusalAnalysis:
    auroc: float | None
    best: ThresholdMetrics | None
    configured: ThresholdMetrics | None
    answerable_count: int
    unanswerable_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "auroc": self.auroc,
            "best": self.best.to_dict() if self.best else None,
            "configured": self.configured.to_dict() if self.configured else None,
            "answerable_count": self.answerable_count,
            "unanswerable_count": self.unanswerable_count,
        }


def analyze_refusal(
    observations: list[tuple[float, bool]], *, configured_threshold: float
) -> RefusalAnalysis:
    answerable = [score for score, label in observations if label]
    unanswerable = [score for score, label in observations if not label]
    if not observations:
        return RefusalAnalysis(None, None, None, 0, 0)
    configured = score_threshold(observations, configured_threshold)
    candidates = _candidate_thresholds([score for score, _ in observations])
    scored = [score_threshold(observations, threshold) for threshold in candidates]
    best = max(
        scored,
        key=lambda item: (item.macro_f1, item.unanswerable_f1, -item.false_answerable),
    )
    auroc = _auroc(answerable, unanswerable) if answerable and unanswerable else None
    return RefusalAnalysis(auroc, best, configured, len(answerable), len(unanswerable))


def score_threshold(
    observations: list[tuple[float, bool]], threshold: float
) -> ThresholdMetrics:
    true_answerable = false_answerable = true_refusal = false_refusal = 0
    for score, answerable in observations:
        predicted_answerable = score >= threshold
        if answerable and predicted_answerable:
            true_answerable += 1
        elif not answerable and predicted_answerable:
            false_answerable += 1
        elif not answerable:
            true_refusal += 1
        else:
            false_refusal += 1
    answerable_f1 = _f1(true_answerable, false_answerable, false_refusal)
    unanswerable_f1 = _f1(true_refusal, false_refusal, false_answerable)
    return ThresholdMetrics(
        threshold=threshold,
        macro_f1=(answerable_f1 + unanswerable_f1) / 2,
        answerable_f1=answerable_f1,
        unanswerable_f1=unanswerable_f1,
        accuracy=(true_answerable + true_refusal) / len(observations),
        true_answerable=true_answerable,
        false_answerable=false_answerable,
        true_refusal=true_refusal,
        false_refusal=false_refusal,
    )


def _candidate_thresholds(scores: list[float]) -> list[float]:
    unique = sorted(set(scores))
    if not unique:
        return []
    epsilon = 1e-12
    return [
        unique[0] - epsilon,
        *((left + right) / 2 for left, right in pairwise(unique)),
        unique[-1] + epsilon,
    ]


def _f1(true_positive: int, false_positive: int, false_negative: int) -> float:
    denominator = 2 * true_positive + false_positive + false_negative
    return 2 * true_positive / denominator if denominator else 0.0


def _auroc(answerable: list[float], unanswerable: list[float]) -> float:
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in answerable
        for negative in unanswerable
    )
    return wins / (len(answerable) * len(unanswerable))
