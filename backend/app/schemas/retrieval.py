from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MarkdownIngestRequest(BaseModel):
    path: Path
    max_chunk_chars: int = Field(default=2000, ge=200, le=20000)


class MarkdownIngestResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    version_no: int
    block_count: int
    chunk_count: int
    activated: bool
    unchanged: bool
    parser: str
    parser_version: str
    parse_meta: dict[str, Any]


class PdfIngestRequest(BaseModel):
    path: Path
    max_chunk_chars: int = Field(default=2000, ge=200, le=20000)


class DenseSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=10, ge=1, le=50)
    temporal_ctx: datetime | None = None


class DenseSearchHitResponse(BaseModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    version_no: int
    title: str
    source_uri: str
    content: str
    content_tokens: int
    char_start: int
    char_end: int
    score: float
    heading_path: list[str]
    blocks: list[dict[str, Any]]
    rerank_score: float | None
    dense_score: float | None
    lexical_score: float | None
    fusion_score: float | None


class DenseSearchResponse(BaseModel):
    hits: list[DenseSearchHitResponse]


class GroundedAnswerRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    temporal_ctx: datetime | None = None


class CitationResponse(BaseModel):
    citation_id: str
    block_id: UUID
    version_id: UUID
    document_id: UUID
    title: str
    source_uri: str
    quote: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]


class GroundedAnswerResponse(BaseModel):
    answer: str
    citations: list[CitationResponse]
    refused: bool
    refusal_reason: (
        Literal[
            "no_evidence",
            "below_threshold",
            "model_insufficient_evidence",
            "evidence_gate_invalid",
        ]
        | None
    )
    retrieved_chunks: int
    top_score: float | None
    second_score: float | None
    score_margin: float | None
    score_margin_ratio: float | None
    score_source: Literal["dense", "lexical", "fusion", "rerank"] | None
    score_threshold_applied: bool
    low_margin: bool
    threshold: float
    margin_threshold: float
    evidence_sufficient: bool | None
    evidence_reason: str | None
    evidence_model: str | None
    evidence_provider: str | None
    query_decomposed: bool
    retrieval_queries: list[str]
    query_plan_reason: str
    query_plan_model: str | None
    query_plan_provider: str | None
    coverage_selection_applied: bool
    coverage_requirement_count: int
    coverage_covered_requirement_count: int
    coverage_candidate_count: int
    coverage_reason: str
    rerank_applied: bool
    rerank_candidate_count: int
    rerank_reason: str
    rerank_model: str | None
    rerank_provider: str | None
    lexical_rrf_applied: bool
    lexical_candidate_count: int
    model: str | None
    provider: str | None
