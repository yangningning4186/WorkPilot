from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.gateway import ModelGateway
from app.retrieval.dense import DenseSearchHit, multi_query_dense_rankings
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy


@dataclass(frozen=True)
class CoverageSelectionResult:
    hits: list[DenseSearchHit]
    applied: bool
    requirement_count: int
    covered_requirement_count: int
    candidate_count: int
    lexical_candidate_count: int
    reason: str


@dataclass(frozen=True)
class HybridQueryRankings:
    rankings: list[list[DenseSearchHit]]
    lexical_candidate_count: int


async def coverage_aware_hybrid_search(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    queries: list[str],
    top_k: int,
    candidate_k: int,
    rank_cutoff: int,
    lexical_enabled: bool,
    lexical_mode: str,
    rrf_k: int,
    strategy: ChunkStrategy = "heading",
    temporal_ctx: datetime | None = None,
) -> CoverageSelectionResult:
    """为原问题和真实子问题保留独立排名，再做非 oracle 覆盖选择。"""

    if len(queries) < 3:
        raise ValueError("coverage-aware 检索至少需要原问题 + 2 个子问题")
    if not 1 <= top_k <= candidate_k:
        raise ValueError("必须满足 1 <= top_k <= candidate_k")
    if not 1 <= rank_cutoff <= candidate_k:
        raise ValueError("rank_cutoff 必须位于 1 到 candidate_k")
    strategy = validate_chunk_strategy(strategy)
    query_rankings = await hybrid_rankings_for_queries(
        session,
        gateway,
        queries=queries,
        candidate_k=candidate_k,
        lexical_enabled=lexical_enabled,
        lexical_mode=lexical_mode,
        rrf_k=rrf_k,
        strategy=strategy,
        temporal_ctx=temporal_ctx,
    )
    result = coverage_aware_top_k(
        query_rankings.rankings[0],
        query_rankings.rankings[1:],
        top_k=top_k,
        rank_cutoff=rank_cutoff,
        rrf_k=rrf_k,
    )
    return replace(
        result,
        lexical_candidate_count=query_rankings.lexical_candidate_count,
    )


async def hybrid_rankings_for_queries(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    queries: list[str],
    candidate_k: int,
    lexical_enabled: bool,
    lexical_mode: str,
    rrf_k: int,
    strategy: ChunkStrategy = "heading",
    temporal_ctx: datetime | None = None,
) -> HybridQueryRankings:
    strategy = validate_chunk_strategy(strategy)
    dense_rankings = await multi_query_dense_rankings(
        session,
        gateway,
        queries=queries,
        per_query_top_k=candidate_k,
        strategy=strategy,
        temporal_ctx=temporal_ctx,
    )
    lexical_rankings: list[list[DenseSearchHit]] = []
    if lexical_enabled:
        for query in queries:
            lexical_rankings.append(
                await lexical_search(
                    session,
                    query=query,
                    top_k=candidate_k,
                    mode=lexical_mode,
                    strategy=strategy,
                    temporal_ctx=temporal_ctx,
                )
            )
    rankings = [
        (
            reciprocal_rank_fusion(
                [dense, lexical_rankings[index]],
                top_k=candidate_k,
                rrf_k=rrf_k,
                strategy=strategy,
            )
            if lexical_enabled
            else dense
        )
        for index, dense in enumerate(dense_rankings)
    ]
    return HybridQueryRankings(
        rankings=rankings,
        lexical_candidate_count=len(
            {hit.chunk_id for ranking in lexical_rankings for hit in ranking}
        ),
    )


def coverage_aware_top_k(
    original_ranking: list[DenseSearchHit],
    requirement_rankings: list[list[DenseSearchHit]],
    *,
    top_k: int,
    rank_cutoff: int = 10,
    rrf_k: int = 60,
) -> CoverageSelectionResult:
    """用真实子查询排名近似集合覆盖，再按原始 RRF 顺序补满 Top-K。"""

    if top_k < 1:
        raise ValueError("top_k 必须为正数")
    if rank_cutoff < 1:
        raise ValueError("rank_cutoff 必须为正数")
    if rrf_k < 1:
        raise ValueError("rrf_k 必须为正数")
    if not requirement_rankings:
        return CoverageSelectionResult(
            hits=original_ranking[:top_k],
            applied=False,
            requirement_count=0,
            covered_requirement_count=0,
            candidate_count=len(original_ranking),
            lexical_candidate_count=0,
            reason="查询未分解，不应用 coverage selector",
        )

    all_rankings = [original_ranking, *requirement_rankings]
    rank_maps = [
        {hit.chunk_id: rank for rank, hit in enumerate(ranking, start=1)}
        for ranking in all_rankings
    ]
    candidates = _candidate_map(all_rankings)
    aggregate_scores = {
        chunk_id: sum(
            1.0 / (rrf_k + rank)
            for ranks in rank_maps
            if (rank := ranks.get(chunk_id)) is not None
        )
        for chunk_id in candidates
    }
    selected_ids: list[UUID] = []
    covered_requirements = 0
    # 不把“同一泛主题 chunk 出现在多个子问题 Top-N”误当成真的多事实覆盖。
    # 每个子问题保守分配一个不同候选；若头名重复，沿该子问题排名向下找第一个未选项。
    for ranking in requirement_rankings:
        if len(selected_ids) >= top_k:
            break
        chosen = next(
            (
                hit.chunk_id
                for hit in ranking[:rank_cutoff]
                if hit.chunk_id not in selected_ids
            ),
            None,
        )
        if chosen is not None:
            selected_ids.append(chosen)
            covered_requirements += 1

    for hit in original_ranking:
        if len(selected_ids) >= top_k:
            break
        if hit.chunk_id not in selected_ids:
            selected_ids.append(hit.chunk_id)
    if len(selected_ids) < top_k:
        remaining = sorted(
            (chunk_id for chunk_id in candidates if chunk_id not in selected_ids),
            key=lambda chunk_id: (-aggregate_scores[chunk_id], str(chunk_id)),
        )
        selected_ids.extend(remaining[: top_k - len(selected_ids)])

    hits = [
        replace(candidates[chunk_id], fusion_score=aggregate_scores[chunk_id])
        for chunk_id in selected_ids
    ]
    baseline_ids = [hit.chunk_id for hit in original_ranking[:top_k]]
    changed = selected_ids != baseline_ids
    return CoverageSelectionResult(
        hits=hits,
        applied=True,
        requirement_count=len(requirement_rankings),
        covered_requirement_count=covered_requirements,
        candidate_count=len(candidates),
        lexical_candidate_count=0,
        reason=(
            f"coverage selector 分配 {covered_requirements}/{len(requirement_rankings)} 个子问题；"
            f"Top-{top_k}{'发生变化' if changed else '与原 RRF 相同'}"
        ),
    )


def _candidate_map(rankings: list[list[DenseSearchHit]]) -> dict[UUID, DenseSearchHit]:
    candidates: dict[UUID, DenseSearchHit] = {}
    for ranking in rankings:
        for hit in ranking:
            candidates.setdefault(hit.chunk_id, hit)
    return candidates
