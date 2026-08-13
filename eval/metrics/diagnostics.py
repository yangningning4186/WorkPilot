import math
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Literal

from eval.mapping import GoldSpan, RetrievedChunk, hits, overlap_ratio
from eval.metrics.retrieval import take_token_budget

SpanStatus = Literal[
    "hit",
    "outside_token_budget",
    "outside_top_k",
    "document_not_retrieved",
    "relevant_chunk_not_ranked",
    "no_relevant_indexed_chunk",
]


@dataclass(frozen=True)
class SpanDiagnostic:
    span_index: int
    version_id: str
    char_start: int
    char_end: int
    quote: str
    status: SpanStatus
    first_hit_rank: int | None
    best_retrieved_overlap: float
    mapped_chunk_count: int
    same_version_retrieved: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def diagnose_spans(
    spans: list[GoldSpan],
    retrieved: list[RetrievedChunk],
    candidates: list[RetrievedChunk],
    *,
    top_k: int,
    token_budget: int,
    theta: float,
) -> list[SpanDiagnostic]:
    budget_ranked = take_token_budget(retrieved, token_budget)
    diagnostics: list[SpanDiagnostic] = []
    for index, span in enumerate(spans):
        first_hit_rank = next(
            (
                rank
                for rank, chunk in enumerate(retrieved, start=1)
                if hits(chunk, span, theta=theta)
            ),
            None,
        )
        mapped = [chunk for chunk in candidates if hits(chunk, span, theta=theta)]
        same_version = [
            chunk for chunk in retrieved if chunk.version_id == span.version_id
        ]
        best_overlap = max(
            (overlap_ratio(chunk, span) for chunk in same_version), default=0.0
        )
        if first_hit_rank is not None and first_hit_rank <= top_k:
            status: SpanStatus = (
                "hit"
                if any(hits(chunk, span, theta=theta) for chunk in budget_ranked)
                else "outside_token_budget"
            )
        elif first_hit_rank is not None:
            status = "outside_top_k"
        elif not mapped:
            status = "no_relevant_indexed_chunk"
        elif same_version:
            status = "relevant_chunk_not_ranked"
        else:
            status = "document_not_retrieved"
        diagnostics.append(
            SpanDiagnostic(
                span_index=index,
                version_id=str(span.version_id),
                char_start=span.char_start,
                char_end=span.char_end,
                quote=span.quote,
                status=status,
                first_hit_rank=first_hit_rank,
                best_retrieved_overlap=best_overlap,
                mapped_chunk_count=len(mapped),
                same_version_retrieved=bool(same_version),
            )
        )
    return diagnostics


def summarize_scores(
    scores: list[float], *, histogram_step: float = 0.05
) -> dict[str, object]:
    if histogram_step <= 0:
        raise ValueError("histogram_step 必须大于 0")
    if not scores:
        return {
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p95": None,
            "max": None,
            "mean": None,
            "histogram": {},
        }
    ordered = sorted(scores)
    histogram: dict[str, int] = {}
    for score in ordered:
        lower = math.floor(score / histogram_step) * histogram_step
        upper = lower + histogram_step
        label = f"[{lower:.2f},{upper:.2f})"
        histogram[label] = histogram.get(label, 0) + 1
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": percentile(ordered, 0.25),
        "median": percentile(ordered, 0.5),
        "p75": percentile(ordered, 0.75),
        "p95": percentile(ordered, 0.95),
        "max": ordered[-1],
        "mean": fmean(ordered),
        "histogram": histogram,
    }


def percentile(values: list[int] | list[float], quantile: float) -> float | None:
    if not values:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile 必须位于 [0,1]")
    position = min(len(values) - 1, max(0.0, (len(values) - 1) * quantile))
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction
