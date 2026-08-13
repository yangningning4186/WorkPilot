from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_model_gateway
from app.core.config import get_settings
from app.core.db import get_db_session
from app.llm.gateway import ModelGateway
from app.retrieval.citations import CitationValidationError
from app.retrieval.dense import dense_search
from app.schemas.retrieval import (
    CitationResponse,
    DenseSearchHitResponse,
    DenseSearchRequest,
    DenseSearchResponse,
    GroundedAnswerRequest,
    GroundedAnswerResponse,
    MarkdownIngestRequest,
    MarkdownIngestResponse,
)
from app.services.grounded_answer import answer_with_citations
from app.services.markdown_ingestion import LibraryPathError, ingest_markdown_file

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


@router.post("/documents/ingest-markdown", response_model=MarkdownIngestResponse)
async def ingest_markdown(
    request: MarkdownIngestRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> MarkdownIngestResponse:
    try:
        result = await ingest_markdown_file(
            session,
            gateway,
            path=request.path,
            library_root=get_settings().local_library_path,
            max_chunk_chars=request.max_chunk_chars,
        )
    except (LibraryPathError, FileNotFoundError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return MarkdownIngestResponse(**vars(result))


@router.post("/search/dense", response_model=DenseSearchResponse)
async def search_dense(
    request: DenseSearchRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> DenseSearchResponse:
    hits = await dense_search(
        session,
        gateway,
        query=request.query,
        top_k=request.top_k,
    )
    await session.commit()
    return DenseSearchResponse(hits=[DenseSearchHitResponse(**vars(hit)) for hit in hits])


@router.post("/answer", response_model=GroundedAnswerResponse)
async def answer(
    request: GroundedAnswerRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> GroundedAnswerResponse:
    settings = get_settings()
    try:
        result = await answer_with_citations(
            session,
            gateway,
            query=request.query,
            top_k=request.top_k,
            max_evidence_chars=settings.answer_max_evidence_chars,
            max_tokens=settings.answer_max_tokens,
        )
    except CitationValidationError as error:
        await session.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "code": "invalid_model_citation",
                "message": str(error),
                "unknown_ids": error.unknown_ids,
                "missing": error.missing,
            },
        ) from error
    await session.commit()
    return GroundedAnswerResponse(
        answer=result.answer,
        citations=[CitationResponse(**vars(citation)) for citation in result.citations],
        refused=result.refused,
        retrieved_chunks=result.retrieved_chunks,
        model=result.model,
        provider=result.provider,
    )
