import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_demo_session, get_model_gateway, require_admin_session
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
    PdfIngestRequest,
)
from app.services.annotation import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    render_pdf_page,
    resolve_source_file,
)
from app.services.demo_sessions import DemoSession
from app.services.grounded_answer import answer_with_citations
from app.services.markdown_ingestion import LibraryPathError, ingest_markdown_file
from app.services.pdf_ingestion import ingest_pdf_file, pdf_parser_config_from_settings
from app.services.runs import demo_session_can_access_version

router = APIRouter(prefix="/api/v1", tags=["retrieval"])
AdminRequired = Annotated[None, Depends(require_admin_session)]


@router.get("/documents/{version_id}/file")
async def get_document_file(
    version_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
) -> FileResponse:
    """返回引用锚定版本对应的原文件；路径始终由已注册 local_dir 反解。"""

    if not await demo_session_can_access_version(
        session, version_id=version_id, demo_session_id=demo_session.id
    ):
        raise HTTPException(status_code=404, detail="文档版本不存在")
    try:
        path, source_uri = await resolve_source_file(session, version_id)
    except (AnnotationNotFoundError, AnnotationConflictError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/markdown"
    return FileResponse(
        path,
        media_type=media_type,
        filename=Path(source_uri).name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/documents/{version_id}/pages/{page_no}.png")
async def get_document_page(
    version_id: UUID,
    page_no: int,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
) -> Response:
    """把 PDF 页渲染为图片，供前端按归一化 bbox 叠加引用高亮。"""

    if not await demo_session_can_access_version(
        session, version_id=version_id, demo_session_id=demo_session.id
    ):
        raise HTTPException(status_code=404, detail="文档版本不存在")
    try:
        path, _source_uri = await resolve_source_file(session, version_id)
        content = await asyncio.to_thread(render_pdf_page, path, page_no)
    except (AnnotationNotFoundError, AnnotationConflictError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post("/documents/ingest-markdown", response_model=MarkdownIngestResponse)
async def ingest_markdown(
    request: MarkdownIngestRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    _: AdminRequired,
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


@router.post("/documents/ingest-pdf", response_model=MarkdownIngestResponse)
async def ingest_pdf(
    request: PdfIngestRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    _: AdminRequired,
) -> MarkdownIngestResponse:
    settings = get_settings()
    try:
        result = await ingest_pdf_file(
            session,
            gateway,
            path=request.path,
            library_root=settings.local_library_path,
            max_chunk_chars=request.max_chunk_chars,
            timeout_s=settings.pdf_parse_timeout_s,
            max_pages=settings.pdf_max_pages,
            max_bytes=settings.pdf_max_bytes,
            memory_mb=settings.pdf_worker_memory_mb,
            cpu_seconds=settings.pdf_worker_cpu_s,
            parser_config=pdf_parser_config_from_settings(settings),
        )
    except (LibraryPathError, FileNotFoundError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return MarkdownIngestResponse(**vars(result))


@router.post("/search/dense", response_model=DenseSearchResponse)
async def search_dense(
    request: DenseSearchRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    _: AdminRequired,
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
    _: AdminRequired,
) -> GroundedAnswerResponse:
    settings = get_settings()
    try:
        result = await answer_with_citations(
            session,
            gateway,
            query=request.query,
            top_k=request.top_k,
            refusal_threshold=settings.refusal_threshold,
            refusal_margin_threshold=settings.refusal_margin_threshold,
            evidence_gate_max_chars=settings.evidence_gate_max_chars,
            rerank_evidence_gate_max_chars=settings.rerank_evidence_gate_max_chars,
            evidence_gate_max_tokens=settings.evidence_gate_max_tokens,
            query_decomposition_enabled=settings.query_decomposition_enabled,
            query_decomposition_max_subqueries=settings.query_decomposition_max_subqueries,
            query_decomposition_max_tokens=settings.query_decomposition_max_tokens,
            rerank_enabled=settings.rerank_enabled,
            rerank_candidate_k=settings.rerank_candidate_k,
            reranker_base_url=settings.reranker_base_url,
            reranker_model=settings.reranker_model,
            reranker_timeout_s=settings.reranker_timeout_s,
            rerank_max_candidate_chars=settings.rerank_max_candidate_chars,
            rerank_candidate_text_mode=settings.rerank_candidate_text_mode,
            lexical_rrf_enabled=settings.lexical_rrf_enabled,
            lexical_mode=settings.lexical_mode,
            rrf_k=settings.rrf_k,
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
        refusal_reason=result.refusal_reason,
        retrieved_chunks=result.retrieved_chunks,
        top_score=result.top_score,
        second_score=result.second_score,
        score_margin=result.score_margin,
        low_margin=result.low_margin,
        threshold=result.threshold,
        margin_threshold=result.margin_threshold,
        evidence_sufficient=result.evidence_sufficient,
        evidence_reason=result.evidence_reason,
        evidence_model=result.evidence_model,
        evidence_provider=result.evidence_provider,
        query_decomposed=result.query_decomposed,
        retrieval_queries=result.retrieval_queries,
        query_plan_reason=result.query_plan_reason,
        query_plan_model=result.query_plan_model,
        query_plan_provider=result.query_plan_provider,
        rerank_applied=result.rerank_applied,
        rerank_candidate_count=result.rerank_candidate_count,
        rerank_reason=result.rerank_reason,
        rerank_model=result.rerank_model,
        rerank_provider=result.rerank_provider,
        lexical_rrf_applied=result.lexical_rrf_applied,
        lexical_candidate_count=result.lexical_candidate_count,
        model=result.model,
        provider=result.provider,
    )
