import math
from dataclasses import asdict, dataclass

from eval.mapping import GoldSpan, RetrievedChunk, hits, overlap_ratio


@dataclass(frozen=True)
class RetrievalMetrics:
    span_recall_at_k: float
    budget_span_recall: float
    ndcg_at_k: float
    alpha_ndcg_at_k: float
    mrr: float
    context_precision: float
    gold_doc_recall_at_k: float
    max_doc_share_at_k: float
    distinct_docs_at_k: int
    gold_doc_count: int
    retrieved_tokens: int
    budget_retrieved_tokens: int
    budget_chunk_count: int
    relevant_chunks: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def evaluate_retrieval(
    gold_spans: list[GoldSpan],
    retrieved: list[RetrievedChunk],
    candidates: list[RetrievedChunk],
    *,
    top_k: int,
    token_budget: int,
    theta: float = 0.5,
    alpha: float = 0.5,
) -> RetrievalMetrics:
    if not gold_spans:
        raise ValueError("answerable 样本必须包含 gold span")
    if top_k < 1 or token_budget < 1:
        raise ValueError("top_k/token_budget 必须为正数")
    if not 0 <= alpha < 1:
        raise ValueError("alpha 必须位于 [0,1)")
    ranked = retrieved[:top_k]
    covered = _covered_span_indexes(ranked, gold_spans, theta)
    budget_ranked = take_token_budget(retrieved, token_budget)
    budget_covered = _covered_span_indexes(budget_ranked, gold_spans, theta)
    relevances = [_chunk_relevance(chunk, gold_spans) for chunk in ranked]
    ideal_relevances = sorted(
        (_chunk_relevance(chunk, gold_spans) for chunk in candidates), reverse=True
    )[:top_k]
    ndcg = _safe_ratio(_dcg(relevances), _dcg(ideal_relevances))
    alpha_dcg = _alpha_dcg(ranked, gold_spans, theta=theta, alpha=alpha)
    ideal_alpha_dcg = _ideal_alpha_dcg(
        candidates, gold_spans, top_k=top_k, theta=theta, alpha=alpha
    )
    first_relevant = next(
        (
            index
            for index, chunk in enumerate(ranked, start=1)
            if _is_relevant(chunk, gold_spans, theta)
        ),
        None,
    )
    relevant_chunks = sum(_is_relevant(chunk, gold_spans, theta) for chunk in ranked)
    diversity = _document_diversity(ranked, gold_spans, theta)
    return RetrievalMetrics(
        span_recall_at_k=len(covered) / len(gold_spans),
        budget_span_recall=len(budget_covered) / len(gold_spans),
        ndcg_at_k=ndcg,
        alpha_ndcg_at_k=_safe_ratio(alpha_dcg, ideal_alpha_dcg),
        mrr=1 / first_relevant if first_relevant else 0.0,
        context_precision=relevant_chunks / len(ranked) if ranked else 0.0,
        gold_doc_recall_at_k=diversity.gold_doc_recall,
        max_doc_share_at_k=diversity.max_doc_share,
        distinct_docs_at_k=diversity.distinct_docs,
        gold_doc_count=diversity.gold_doc_count,
        retrieved_tokens=sum(chunk.content_tokens for chunk in ranked),
        budget_retrieved_tokens=sum(chunk.content_tokens for chunk in budget_ranked),
        budget_chunk_count=len(budget_ranked),
        relevant_chunks=relevant_chunks,
    )


@dataclass(frozen=True)
class _DocumentDiversity:
    gold_doc_recall: float
    max_doc_share: float
    distinct_docs: int
    gold_doc_count: int


def _document_diversity(
    ranked: list[RetrievedChunk], spans: list[GoldSpan], theta: float
) -> _DocumentDiversity:
    """文档级多样性观测。

    α-nDCG 的 subtopic 是 gold span（每一跳一个 subtopic），这个语义是对的：
    同文档多跳必须两跳都拿满增益，不能因为"同一篇"就打折。
    但它对"单文档霸榜导致另一篇 gold 文档一条都没召回"没有区分度——
    那是**覆盖**失败而非**冗余**失败。这里用文档维度直接观测该失败模式。

    - gold_doc_recall: 命中的 gold 文档数 / gold 文档总数（霸榜时直接掉到 0.5）
    - max_doc_share: top-k 里单篇文档占比（28/28 霸榜 = 1.0），含非 gold chunk
    """
    gold_docs = {span.version_id for span in spans}
    hit_docs = {
        span.version_id
        for span in spans
        if any(hits(chunk, span, theta=theta) for chunk in ranked)
    }
    counts: dict[object, int] = {}
    for chunk in ranked:
        counts[chunk.version_id] = counts.get(chunk.version_id, 0) + 1
    return _DocumentDiversity(
        gold_doc_recall=len(hit_docs) / len(gold_docs) if gold_docs else 0.0,
        max_doc_share=max(counts.values()) / len(ranked) if ranked else 0.0,
        distinct_docs=len(counts),
        gold_doc_count=len(gold_docs),
    )


def _covered_span_indexes(
    chunks: list[RetrievedChunk], spans: list[GoldSpan], theta: float
) -> set[int]:
    return {
        index
        for index, span in enumerate(spans)
        if any(hits(chunk, span, theta=theta) for chunk in chunks)
    }


def _chunk_relevance(chunk: RetrievedChunk, spans: list[GoldSpan]) -> float:
    return min(1.0, sum(overlap_ratio(chunk, span) for span in spans))


def _is_relevant(chunk: RetrievedChunk, spans: list[GoldSpan], theta: float) -> bool:
    return any(hits(chunk, span, theta=theta) for span in spans)


def take_token_budget(
    chunks: list[RetrievedChunk], budget: int
) -> list[RetrievedChunk]:
    selected: list[RetrievedChunk] = []
    consumed = 0
    for chunk in chunks:
        if consumed + chunk.content_tokens > budget:
            break
        selected.append(chunk)
        consumed += chunk.content_tokens
        if consumed >= budget:
            break
    return selected


def _dcg(relevances: list[float]) -> float:
    return sum(
        relevance / math.log2(rank + 1) for rank, relevance in enumerate(relevances, 1)
    )


def _alpha_dcg(
    chunks: list[RetrievedChunk],
    spans: list[GoldSpan],
    *,
    theta: float,
    alpha: float,
) -> float:
    previous_hits = [0] * len(spans)
    score = 0.0
    for rank, chunk in enumerate(chunks, start=1):
        gain = 0.0
        for index, span in enumerate(spans):
            if hits(chunk, span, theta=theta):
                gain += (1 - alpha) ** previous_hits[index]
                previous_hits[index] += 1
        score += gain / math.log2(rank + 1)
    return score


def _ideal_alpha_dcg(
    candidates: list[RetrievedChunk],
    spans: list[GoldSpan],
    *,
    top_k: int,
    theta: float,
    alpha: float,
) -> float:
    remaining = list(candidates)
    selected: list[RetrievedChunk] = []
    previous_hits = [0] * len(spans)
    for _ in range(min(top_k, len(remaining))):
        gains = [
            sum(
                (1 - alpha) ** previous_hits[index]
                for index, span in enumerate(spans)
                if hits(chunk, span, theta=theta)
            )
            for chunk in remaining
        ]
        best_index = max(range(len(remaining)), key=lambda index: gains[index])
        best = remaining.pop(best_index)
        selected.append(best)
        for index, span in enumerate(spans):
            if hits(best, span, theta=theta):
                previous_hits[index] += 1
    return _alpha_dcg(selected, spans, theta=theta, alpha=alpha)


def _safe_ratio(value: float, denominator: float) -> float:
    return value / denominator if denominator else 0.0
