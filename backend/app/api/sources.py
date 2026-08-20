from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_model_gateway, require_admin_session
from app.core.config import get_settings
from app.core.db import get_db_session
from app.rag.local_dir import (
    SourceBusyError,
    SourceNotFoundError,
    get_local_dir_source,
    register_local_dir,
    sync_local_dir,
)
from app.rag.markdown_ingestion import LibraryPathError
from app.schemas.sources import (
    LocalDirCreateRequest,
    LocalDirSourceResponse,
    LocalDirSyncRequest,
    LocalDirSyncResponse,
)
from workpilot_ai.gateway import ModelGateway

router = APIRouter(
    prefix="/api/v1/sources",
    tags=["sources"],
    dependencies=[Depends(require_admin_session)],
)


@router.post("/local-dir", response_model=LocalDirSourceResponse)
async def create_local_dir_source(
    request: LocalDirCreateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LocalDirSourceResponse:
    try:
        source = await register_local_dir(
            session,
            requested_root=request.root,
            allowed_root=get_settings().local_library_path,
            name=request.name,
        )
    except (LibraryPathError, FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return LocalDirSourceResponse(**vars(source))


@router.get("/{source_id}", response_model=LocalDirSourceResponse)
async def get_source(
    source_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LocalDirSourceResponse:
    try:
        source = await get_local_dir_source(session, source_id)
    except SourceNotFoundError as error:
        raise HTTPException(status_code=404, detail="source 不存在") from error
    return LocalDirSourceResponse(**vars(source))


@router.post("/{source_id}/sync", response_model=LocalDirSyncResponse)
async def sync_source(
    source_id: UUID,
    request: LocalDirSyncRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
) -> LocalDirSyncResponse:
    settings = get_settings()
    try:
        result = await sync_local_dir(
            session,
            gateway,
            source_id=source_id,
            allowed_root=settings.local_library_path,
            settings=settings,
            max_chunk_chars=request.max_chunk_chars,
        )
    except SourceNotFoundError as error:
        raise HTTPException(status_code=404, detail="source 不存在") from error
    except SourceBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (LibraryPathError, FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return LocalDirSyncResponse(**vars(result))
