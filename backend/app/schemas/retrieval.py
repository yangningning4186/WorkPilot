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


class DenseSearchHitResponse(BaseModel):
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    version_no: int
    title: str
    source_uri: str
    content: str
    score: float
    heading_path: list[str]
    blocks: list[dict[str, Any]]


class DenseSearchResponse(BaseModel):
    hits: list[DenseSearchHitResponse]


class GroundedAnswerRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


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
    refusal_reason: Literal["no_evidence", "below_threshold", "model_insufficient_evidence"] | None
    retrieved_chunks: int
    top_score: float | None
    threshold: float
    model: str | None
    provider: str | None
