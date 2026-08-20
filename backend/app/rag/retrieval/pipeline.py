"""RAG 的唯一检索编排。

同步问答、流式问答、评测与 Cowork 都必须经由 ``SearchPipeline``。这里负责从
查询规划到最终候选与可溯源 evidence 的完整搜索阶段；拒答、证据充分性判断和
答案生成仍属于上层用例。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.retrieval.citations import EvidenceSegment, build_evidence_segments
from app.rag.retrieval.coverage import CoverageSelectionResult, coverage_aware_hybrid_search
from app.rag.retrieval.dense import DenseSearchHit, multi_query_dense_search
from app.rag.retrieval.fusion import apply_document_cap, reciprocal_rank_fusion
from app.rag.retrieval.lexical import lexical_search
from app.rag.retrieval.query_decomposition import (
    QueryPlan,
    fallback_query_plan,
    plan_retrieval_queries,
)
from app.rag.retrieval.reranker import RerankResult, rerank_candidates
from app.rag.retrieval.strategy import ChunkStrategy, validate_chunk_strategy
from workpilot_ai.gateway import ModelGateway

RetrievalMode = Literal["dense", "lexical", "hybrid"]


@dataclass(frozen=True)
class SearchPipelineRequest:
    query: str
    top_k: int = 5
    candidate_k: int = 50
    strategy: ChunkStrategy = "heading"
    temporal_ctx: datetime | None = None
    max_evidence_chars: int = 12_000
    retrieval_mode: RetrievalMode = "hybrid"
    query_decomposition_enabled: bool = False
    query_decomposition_max_subqueries: int = 4
    query_decomposition_max_tokens: int = 300
    coverage_selection_enabled: bool = False
    coverage_rank_cutoff: int = 10
    lexical_enabled: bool = True
    lexical_mode: str = "ts_rank"
    rrf_k: int = 60
    document_cap_per_version: int = 0
    rerank_enabled: bool = False
    force_rerank: bool = False
    reranker_base_url: str = "http://127.0.0.1:8011"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_timeout_s: float = 10.0
    rerank_max_candidate_chars: int = 1_200
    rerank_candidate_text_mode: str = "title_heading_content"


@dataclass(frozen=True)
class SearchPipelineResult:
    hits: tuple[DenseSearchHit, ...]
    evidence: tuple[EvidenceSegment, ...]
    query_plan: QueryPlan
    coverage: CoverageSelectionResult
    rerank: RerankResult
    lexical_applied: bool
    lexical_candidate_count: int
    strategy: ChunkStrategy


class SearchPipeline:
    """把所有检索增强能力收敛到一条可复用、可替换的执行路径。"""

    def __init__(self, session: AsyncSession, gateway: ModelGateway) -> None:
        self._session = session
        self._gateway = gateway

    async def search(self, request: SearchPipelineRequest) -> SearchPipelineResult:
        query = request.query.strip()
        if not query:
            raise ValueError("RAG query 不能为空")
        if not 1 <= request.top_k <= request.candidate_k <= 100:
            raise ValueError("RAG 必须满足 1 <= top_k <= candidate_k <= 100")
        strategy = validate_chunk_strategy(request.strategy)
        query_plan = await self._build_query_plan(query, request)
        self._validate_feature_combinations(request, query_plan)

        coverage = CoverageSelectionResult(
            hits=[],
            applied=False,
            requirement_count=0,
            covered_requirement_count=0,
            candidate_count=0,
            lexical_candidate_count=0,
            reason=(
                "coverage selector 已关闭"
                if not request.coverage_selection_enabled
                else "查询规划未分解，回退原 RRF"
            ),
        )
        lexical_hits: list[DenseSearchHit] = []
        if request.coverage_selection_enabled and query_plan.decomposed:
            coverage = await coverage_aware_hybrid_search(
                self._session,
                self._gateway,
                queries=query_plan.queries,
                top_k=request.top_k,
                candidate_k=request.candidate_k,
                rank_cutoff=request.coverage_rank_cutoff,
                lexical_enabled=request.lexical_enabled,
                lexical_mode=request.lexical_mode,
                rrf_k=request.rrf_k,
                strategy=strategy,
                temporal_ctx=request.temporal_ctx,
            )
            candidate_hits = coverage.hits
            lexical_candidate_count = coverage.lexical_candidate_count
        elif request.retrieval_mode == "lexical":
            candidate_hits = await lexical_search(
                self._session,
                query=query,
                top_k=request.candidate_k,
                mode=request.lexical_mode,
                strategy=strategy,
                temporal_ctx=request.temporal_ctx,
            )
            lexical_hits = candidate_hits
            lexical_candidate_count = len(lexical_hits)
        else:
            candidate_hits = await multi_query_dense_search(
                self._session,
                self._gateway,
                queries=query_plan.queries,
                top_k=request.candidate_k,
                strategy=strategy,
                temporal_ctx=request.temporal_ctx,
            )
            if request.retrieval_mode == "hybrid" and request.lexical_enabled:
                lexical_hits = await lexical_search(
                    self._session,
                    query=query,
                    top_k=request.candidate_k,
                    mode=request.lexical_mode,
                    strategy=strategy,
                    temporal_ctx=request.temporal_ctx,
                )
                candidate_hits = reciprocal_rank_fusion(
                    [candidate_hits, lexical_hits],
                    top_k=request.candidate_k,
                    rrf_k=request.rrf_k,
                    strategy=strategy,
                )
            lexical_candidate_count = len(lexical_hits)

        if request.document_cap_per_version:
            candidate_hits = apply_document_cap(
                candidate_hits,
                cap=request.document_cap_per_version,
            )
        if request.rerank_enabled and candidate_hits and (
            request.force_rerank or len(candidate_hits) > request.top_k
        ):
            rerank = await rerank_candidates(
                query=query,
                candidates=candidate_hits,
                top_k=min(request.top_k, len(candidate_hits)),
                base_url=request.reranker_base_url,
                model=request.reranker_model,
                timeout_s=request.reranker_timeout_s,
                max_candidate_chars=request.rerank_max_candidate_chars,
                candidate_text_mode=request.rerank_candidate_text_mode,
                strategy=strategy,
            )
        else:
            rerank = RerankResult(
                hits=candidate_hits[: request.top_k],
                applied=False,
                candidate_count=len(candidate_hits),
                reason=(
                    coverage.reason if coverage.applied else "rerank 已关闭或候选数不足"
                ),
                model=None,
                provider=None,
            )
        hits = rerank.hits
        return SearchPipelineResult(
            hits=tuple(hits),
            evidence=tuple(
                build_evidence_segments(hits, max_chars=request.max_evidence_chars)
            ),
            query_plan=query_plan,
            coverage=coverage,
            rerank=rerank,
            lexical_applied=(
                request.retrieval_mode == "lexical"
                or (request.retrieval_mode == "hybrid" and request.lexical_enabled)
            ),
            lexical_candidate_count=lexical_candidate_count,
            strategy=strategy,
        )

    async def _build_query_plan(
        self,
        query: str,
        request: SearchPipelineRequest,
    ) -> QueryPlan:
        if not request.query_decomposition_enabled:
            return fallback_query_plan(query, reason="查询分解已关闭")
        try:
            return await plan_retrieval_queries(
                self._gateway,
                query=query,
                max_subqueries=request.query_decomposition_max_subqueries,
                max_tokens=request.query_decomposition_max_tokens,
            )
        except Exception as error:
            # 查询规划是召回增强项；异常必须回退原查询，不能拖垮基础检索链路。
            return fallback_query_plan(
                query,
                reason=f"查询分解降级: {type(error).__name__}",
            )

    @staticmethod
    def _validate_feature_combinations(
        request: SearchPipelineRequest,
        query_plan: QueryPlan,
    ) -> None:
        if request.retrieval_mode not in {"dense", "lexical", "hybrid"}:
            raise ValueError("retrieval_mode 必须是 dense、lexical 或 hybrid")
        if request.coverage_selection_enabled and request.retrieval_mode != "hybrid":
            raise ValueError("coverage selector 只支持 hybrid retrieval_mode")
        if request.coverage_selection_enabled and not request.query_decomposition_enabled:
            raise ValueError("coverage selector 需要同时开启 query decomposition")
        if request.coverage_selection_enabled and request.rerank_enabled:
            raise ValueError("coverage selector 与 rerank 不能同时作为最终排序器")
        if request.coverage_selection_enabled and request.document_cap_per_version:
            raise ValueError("coverage selector 暂不与 document cap 叠加")
        if (
            request.coverage_selection_enabled
            and query_plan.decomposed
            and not 1 <= request.coverage_rank_cutoff <= request.candidate_k
        ):
            raise ValueError("coverage_rank_cutoff 必须位于 1 到 candidate_k")
