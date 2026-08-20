"""兼容旧导入；实现已移动到 ``app.rag.retrieval.reranker``。"""

from app.rag.retrieval.reranker import (
    CANDIDATE_TEXT_MODES,
    RerankResponseError,
    RerankResult,
    _candidate_text,
    build_candidate_text,
    candidate_content_offset,
    parse_cross_encoder_response,
    rerank_candidates,
)

__all__ = [
    "CANDIDATE_TEXT_MODES",
    "RerankResponseError",
    "RerankResult",
    "_candidate_text",
    "build_candidate_text",
    "candidate_content_offset",
    "parse_cross_encoder_response",
    "rerank_candidates",
]
