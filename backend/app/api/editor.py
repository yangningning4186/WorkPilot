from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_editor_permission_store,
    get_model_gateway,
    require_editor_write_permission,
    require_owner_identity,
)
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.cowork.office_workspace import (
    OfficePlanError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    execute_workspace_instruction,
    get_workspace_file,
    list_workspace_files,
)
from app.platform.request_identity import RequestIdentity
from app.rag.editor import (
    DocumentConflictError,
    DocumentNotEditableError,
    EditableDocumentNotFoundError,
    EditProposalError,
    apply_document_content,
    get_editable_document,
    propose_document_edit,
)
from app.rag.editor_permissions import EditorPermissionStore
from app.rag.markdown_ingestion import LibraryPathError
from app.schemas.editor import (
    ApplyDocumentRequest,
    ApplyDocumentResponse,
    EditableDocumentResponse,
    EditorPermissionResponse,
    EditProposalRequest,
    ExecuteDocumentResponse,
    WorkspaceFileListResponse,
    WorkspaceFileResponse,
    WorkspaceInstructionRequest,
    WorkspaceInstructionResponse,
)
from workpilot_ai.gateway import ModelGateway

router = APIRouter(
    prefix="/api/v1/editor",
    tags=["editor"],
    dependencies=[Depends(require_owner_identity)],
)


def _permission_token(request: Request, settings: Settings) -> str:
    token = request.cookies.get(settings.admin_cookie_name)
    if token is None:
        raise HTTPException(status_code=401, detail="需要先登录 owner")
    return token


@router.get("/permission", response_model=EditorPermissionResponse)
async def read_editor_permission(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[EditorPermissionStore, Depends(get_editor_permission_store)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> EditorPermissionResponse:
    try:
        remaining = await store.ttl(_permission_token(request, settings))
    except RedisError as error:
        raise HTTPException(status_code=503, detail="文档权限服务不可用") from error
    return EditorPermissionResponse(
        granted=remaining > 0,
        scope="local_office_write",
        expires_in_s=max(0, remaining),
    )


@router.post("/permission", response_model=EditorPermissionResponse)
async def grant_editor_permission(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[EditorPermissionStore, Depends(get_editor_permission_store)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> EditorPermissionResponse:
    ttl_s = settings.editor_permission_ttl_s
    try:
        await store.grant(_permission_token(request, settings), ttl_s=ttl_s)
    except (RedisError, ValueError) as error:
        raise HTTPException(status_code=503, detail="文档权限授予失败") from error
    return EditorPermissionResponse(
        granted=True,
        scope="local_office_write",
        expires_in_s=ttl_s,
    )


@router.delete("/permission", status_code=204)
async def revoke_editor_permission(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[EditorPermissionStore, Depends(get_editor_permission_store)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> Response:
    try:
        await store.revoke(_permission_token(request, settings))
    except RedisError as error:
        raise HTTPException(status_code=503, detail="文档权限撤销失败") from error
    return Response(status_code=204)


@router.get("/files", response_model=WorkspaceFileListResponse)
async def read_workspace_files(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> WorkspaceFileListResponse:
    try:
        items = await list_workspace_files(session, settings=settings)
    except LibraryPathError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return WorkspaceFileListResponse(items=items)


@router.get("/files/{file_id}", response_model=WorkspaceFileResponse)
async def read_workspace_file(
    file_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> WorkspaceFileResponse:
    try:
        return await get_workspace_file(session, file_id=file_id, settings=settings)
    except WorkspaceFileNotFoundError as error:
        raise HTTPException(status_code=404, detail="办公文档不存在") from error
    except (WorkspaceFileTooLargeError, DocumentNotEditableError, LibraryPathError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/files/{file_id}/execute",
    response_model=WorkspaceInstructionResponse,
    dependencies=[Depends(require_editor_write_permission)],
)
async def execute_workspace_file_instruction(
    file_id: str,
    request: WorkspaceInstructionRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> WorkspaceInstructionResponse:
    try:
        return await execute_workspace_instruction(
            session,
            gateway,
            file_id=file_id,
            baseline_sha256=request.baseline_sha256,
            instruction=request.instruction,
            content=request.content,
            selection_start=request.selection_start,
            selection_end=request.selection_end,
            settings=settings,
        )
    except WorkspaceFileNotFoundError as error:
        raise HTTPException(status_code=404, detail="办公文档不存在") from error
    except DocumentConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "document_conflict", "current_sha256": error.current_sha256},
        ) from error
    except (
        WorkspaceFileTooLargeError,
        DocumentNotEditableError,
        EditProposalError,
        OfficePlanError,
        LibraryPathError,
    ) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/documents/{document_id}", response_model=EditableDocumentResponse)
async def read_editable_document(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> EditableDocumentResponse:
    try:
        return await get_editable_document(session, document_id=document_id, settings=settings)
    except EditableDocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail="文档或本地文件不存在") from error
    except (DocumentNotEditableError, LibraryPathError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/documents/{document_id}/execute",
    response_model=ExecuteDocumentResponse,
    dependencies=[Depends(require_editor_write_permission)],
)
async def execute_edit_instruction(
    document_id: UUID,
    request: EditProposalRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> ExecuteDocumentResponse:
    try:
        proposal = await propose_document_edit(
            session,
            gateway,
            document_id=document_id,
            baseline_sha256=request.baseline_sha256,
            content=request.content,
            instruction=request.instruction,
            selection_start=request.selection_start,
            selection_end=request.selection_end,
            settings=settings,
        )
        applied = await apply_document_content(
            session,
            gateway,
            document_id=document_id,
            baseline_sha256=request.baseline_sha256,
            content=proposal.proposed_content,
            settings=settings,
        )
        return ExecuteDocumentResponse(proposal=proposal, document=applied)
    except EditableDocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail="文档或本地文件不存在") from error
    except DocumentConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "document_conflict", "current_sha256": error.current_sha256},
        ) from error
    except (DocumentNotEditableError, EditProposalError, LibraryPathError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/documents/{document_id}/apply",
    response_model=ApplyDocumentResponse,
    dependencies=[Depends(require_editor_write_permission)],
)
async def apply_document(
    document_id: UUID,
    request: ApplyDocumentRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    gateway: Annotated[ModelGateway, Depends(get_model_gateway)],
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[RequestIdentity, Depends(require_owner_identity)],
) -> ApplyDocumentResponse:
    try:
        return await apply_document_content(
            session,
            gateway,
            document_id=document_id,
            baseline_sha256=request.baseline_sha256,
            content=request.content,
            settings=settings,
        )
    except EditableDocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail="文档或本地文件不存在") from error
    except DocumentConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "document_conflict", "current_sha256": error.current_sha256},
        ) from error
    except (DocumentNotEditableError, LibraryPathError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
