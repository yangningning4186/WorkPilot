"""Cowork → RAG 的窄服务接口。

Cowork 只依赖可溯源证据契约，不读取 documents/chunks 表。当前实现仍在同一进程中
调用 PostgreSQL repository；以后拆 sidecar/HTTP 时无需改 Cowork 状态存储。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.retrieval.citations import EvidenceSegment
from app.retrieval.pipeline import SearchPipeline, SearchPipelineRequest
from app.retrieval.strategy import ChunkStrategy


@dataclass(frozen=True)
class RagSearchRequest:
    query: str
    top_k: int = 5
    candidate_k: int = 20
    strategy: ChunkStrategy = "heading"
    max_evidence_chars: int = 12_000


@dataclass(frozen=True)
class EvidenceBundle:
    """跨 Cowork/RAG 边界的稳定证据契约，不泄露 ORM 或裸 chunk。"""

    evidence: tuple[EvidenceSegment, ...]
    retrieved_chunks: int
    backend: str


# 兼容现有导入；新代码统一使用语义更明确的 EvidenceBundle。
RagSearchResult = EvidenceBundle


class RagService(Protocol):
    async def search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
    ) -> EvidenceBundle: ...


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
