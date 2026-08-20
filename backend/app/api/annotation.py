import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin_session
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.rag.annotation import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    create_dataset,
    create_item,
    delete_item,
    list_blocks,
    list_datasets,
    list_documents,
    list_items,
    render_pdf_page,
    resolve_source_file,
    resolve_span,
    update_item,
)
from app.schemas.annotation import (
    AnnotationBlockPageResponse,
    AnnotationDatasetCreate,
    AnnotationDatasetResponse,
    AnnotationDocumentResponse,
    AnnotationItemListResponse,
    AnnotationItemResponse,
    AnnotationItemUpsert,
    GoldSpanResponse,
    ResolveSpanRequest,
)

router = APIRouter(
    prefix="/api/v1/annotation",
    tags=["annotation"],
    dependencies=[Depends(require_admin_session)],
)
page_router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_admin_session)],
)
STATIC_ROOT = Path(__file__).parents[1] / "static" / "annotation"


def require_annotation_tool(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.annotation_tool_enabled or settings.app_env == "production":
        raise HTTPException(status_code=404, detail="本地标注工具未启用")


AnnotationEnabled = Annotated[None, Depends(require_annotation_tool)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@page_router.get("/annotation")
async def annotation_page(_: AnnotationEnabled) -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@page_router.get("/annotation/assets/{filename}")
async def annotation_asset(filename: str, _: AnnotationEnabled) -> FileResponse:
    if filename not in {"app.js", "styles.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(STATIC_ROOT / filename)


@router.get("/datasets", response_model=list[AnnotationDatasetResponse])
async def get_datasets(session: DbSession, _: AnnotationEnabled) -> list[AnnotationDatasetResponse]:
    return await list_datasets(session)


@router.post("/datasets", response_model=AnnotationDatasetResponse)
async def post_dataset(
    request: AnnotationDatasetCreate, session: DbSession, _: AnnotationEnabled
) -> AnnotationDatasetResponse:
    return await create_dataset(session, request)


@router.get("/documents", response_model=list[AnnotationDocumentResponse])
async def get_documents(
    session: DbSession,
    _: AnnotationEnabled,
    query: str = Query(default="", max_length=200),
) -> list[AnnotationDocumentResponse]:
    return await list_documents(session, query=query)


@router.get("/documents/{version_id}/blocks", response_model=AnnotationBlockPageResponse)
async def get_blocks(
    version_id: UUID,
    session: DbSession,
    _: AnnotationEnabled,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    query: str = Query(default="", max_length=500),
    block_type: str = Query(default="", max_length=50),
) -> AnnotationBlockPageResponse:
    try:
        return await list_blocks(
            session,
            version_id,
            offset=offset,
            limit=limit,
            query=query,
            block_type=block_type,
        )
    except AnnotationNotFoundError as error:
        raise HTTPException(status_code=404, detail="文档版本不存在") from error


@router.get("/documents/{version_id}/file")
async def get_source_file(
    version_id: UUID, session: DbSession, _: AnnotationEnabled
) -> FileResponse:
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
    )


@router.get("/documents/{version_id}/pages/{page_no}.png")
async def get_pdf_page(
    version_id: UUID, page_no: int, session: DbSession, _: AnnotationEnabled
) -> Response:
    try:
        path, _source_uri = await resolve_source_file(session, version_id)
        content = await asyncio.to_thread(render_pdf_page, path, page_no)
    except (AnnotationNotFoundError, AnnotationConflictError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/spans/resolve", response_model=GoldSpanResponse)
async def post_resolve_span(
    request: ResolveSpanRequest, session: DbSession, _: AnnotationEnabled
) -> GoldSpanResponse:
    try:
        return await resolve_span(session, request)
    except AnnotationNotFoundError as error:
        raise HTTPException(status_code=404, detail="block 不存在") from error
    except AnnotationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/items", response_model=AnnotationItemListResponse)
async def get_items(
    dataset_id: UUID, session: DbSession, _: AnnotationEnabled
) -> AnnotationItemListResponse:
    return await list_items(session, dataset_id=dataset_id)


@router.post("/items", response_model=AnnotationItemResponse)
async def post_item(
    request: AnnotationItemUpsert, session: DbSession, _: AnnotationEnabled
) -> AnnotationItemResponse:
    try:
        return await create_item(session, request)
    except AnnotationNotFoundError as error:
        raise HTTPException(status_code=404, detail="数据集不存在") from error
    except AnnotationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/items/{item_id}", response_model=AnnotationItemResponse)
async def put_item(
    item_id: UUID,
    request: AnnotationItemUpsert,
    session: DbSession,
    _: AnnotationEnabled,
) -> AnnotationItemResponse:
    try:
        return await update_item(session, item_id, request)
    except AnnotationNotFoundError as error:
        raise HTTPException(status_code=404, detail="样本或数据集不存在") from error
    except AnnotationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/items/{item_id}", status_code=204)
async def remove_item(item_id: UUID, session: DbSession, _: AnnotationEnabled) -> Response:
    try:
        await delete_item(session, item_id)
    except AnnotationNotFoundError as error:
        raise HTTPException(status_code=404, detail="样本不存在") from error
    return Response(status_code=204)
