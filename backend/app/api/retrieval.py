from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_model_gateway
from app.core.config import get_settings
from app.core.db import get_db_session
from app.llm.gateway import ModelGateway
from app.retrieval.dense import dense_search
from app.schemas.retrieval import (
    DenseSearchHitResponse,
    DenseSearchRequest,
    DenseSearchResponse,
    MarkdownIngestRequest,
    MarkdownIngestResponse,
)
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
