"""`RagService` 契约的 PostgreSQL + pgvector 实现。

契约本身在 `app/knowledge_contracts.py`——Cowork 只依赖那一份，不依赖本模块。
当前实现仍在同一进程中调用 repository；以后拆 sidecar/HTTP 时无需改 Cowork 侧。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.knowledge_contracts import EvidenceBundle as EvidenceBundle
from app.knowledge_contracts import RagSearchRequest as RagSearchRequest
from app.knowledge_contracts import RagService as RagService
from app.rag.retrieval.pipeline import SearchPipeline, SearchPipelineRequest
from workpilot_ai.gateway import ModelGateway

# 兼容现有导入；新代码统一使用语义更明确的 EvidenceBundle。
RagSearchResult = EvidenceBundle


class PostgresRagService:
    """现有 PostgreSQL + pgvector 检索的服务适配器。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lexical_enabled: bool = True,
        lexical_mode: str = "ts_rank",
        rrf_k: int = 60,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._lexical_enabled = lexical_enabled
        self._lexical_mode = lexical_mode
        self._rrf_k = rrf_k
        self._settings = settings

    async def search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
    ) -> EvidenceBundle:
        settings = self._settings
        candidate_k = max(
            request.candidate_k,
            request.top_k,
            0 if settings is None else settings.rerank_candidate_k,
        )
        async with self._session_factory() as session:
            result = await SearchPipeline(session, gateway).search(
                SearchPipelineRequest(
                    query=request.query,
                    top_k=request.top_k,
                    candidate_k=candidate_k,
                    strategy=request.strategy,
                    max_evidence_chars=request.max_evidence_chars,
                    lexical_enabled=self._lexical_enabled,
                    lexical_mode=self._lexical_mode,
                    rrf_k=self._rrf_k,
                    query_decomposition_enabled=(
                        False if settings is None else settings.query_decomposition_enabled
                    ),
                    query_decomposition_max_subqueries=(
                        4 if settings is None else settings.query_decomposition_max_subqueries
                    ),
                    query_decomposition_max_tokens=(
                        300 if settings is None else settings.query_decomposition_max_tokens
                    ),
                    coverage_selection_enabled=(
                        False if settings is None else settings.coverage_selection_enabled
                    ),
                    coverage_rank_cutoff=(
                        10 if settings is None else settings.coverage_rank_cutoff
                    ),
                    document_cap_per_version=(
                        0 if settings is None else settings.document_cap_per_version
                    ),
                    rerank_enabled=False if settings is None else settings.rerank_enabled,
                    reranker_base_url=(
                        "http://127.0.0.1:8011"
                        if settings is None
                        else settings.reranker_base_url
                    ),
                    reranker_model=(
                        "BAAI/bge-reranker-v2-m3"
                        if settings is None
                        else settings.reranker_model
                    ),
                    reranker_timeout_s=(
                        10.0 if settings is None else settings.reranker_timeout_s
                    ),
                    rerank_max_candidate_chars=(
                        1_200 if settings is None else settings.rerank_max_candidate_chars
                    ),
                    rerank_candidate_text_mode=(
                        "title_heading_content"
                        if settings is None
                        else settings.rerank_candidate_text_mode
                    ),
                )
            )
        return EvidenceBundle(
            evidence=result.evidence,
            retrieved_chunks=len(result.hits),
            backend="postgres_pgvector",
        )
