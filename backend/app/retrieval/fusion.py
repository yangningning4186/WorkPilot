from dataclasses import replace
from uuid import UUID

from app.retrieval.dense import DenseSearchHit
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy


def reciprocal_rank_fusion(
    rankings: list[list[DenseSearchHit]],
    *,
    top_k: int = 50,
    rrf_k: int = 60,
    strategy: ChunkStrategy = "heading",
) -> list[DenseSearchHit]:
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    if rrf_k < 1:
        raise ValueError("rrf_k 必须为正数")
    strategy = validate_chunk_strategy(strategy)
    mismatched = {
        hit.strategy for ranking in rankings for hit in ranking if hit.strategy != strategy
    }
    if mismatched:
        raise ValueError(
            f"RRF 禁止混合 chunk strategy: expected={strategy}, actual={sorted(mismatched)}"
        )
    fused: dict[UUID, tuple[DenseSearchHit, float]] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            current = fused.get(hit.chunk_id)
            fusion_score = 1.0 / (rrf_k + rank)
            if current is None:
                fused[hit.chunk_id] = (hit, fusion_score)
                continue
            base, score = current
            dense_score = base.dense_score if base.dense_score is not None else hit.dense_score
            lexical_score = (
                base.lexical_score if base.lexical_score is not None else hit.lexical_score
            )
            if base.dense_score is None and hit.dense_score is not None:
                base = hit
            fused[hit.chunk_id] = (
                replace(base, dense_score=dense_score, lexical_score=lexical_score),
                score + fusion_score,
            )
    ordered = sorted(
        fused.values(),
        key=lambda item: (-item[1], str(item[0].chunk_id)),
    )
    return [replace(hit, fusion_score=score) for hit, score in ordered[:top_k]]
