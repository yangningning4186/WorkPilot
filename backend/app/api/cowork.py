from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.schemas.cowork import (
    ArtifactListResponse,
    ArtifactResponse,
    CapabilityGrantCreate,
    CapabilityGrantListResponse,
    CapabilityGrantResponse,
    SessionRootCreate,
    SessionRootListResponse,
    SessionRootResponse,
)
from app.services.artifacts import list_artifacts
from app.services.cowork_permissions import (
    CapabilityDeniedError,
    ConversationNotFoundError,
    CoworkPermissionError,
    SessionRootNotFoundError,
    create_session_root,
    grant_capability,
    list_capability_grants,
    list_session_roots,
    revoke_capability_grant,
    revoke_session_root,
)


async def require_cowork_enabled(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.cowork_enabled:
        raise HTTPException(status_code=404, detail="Cowork 功能尚未启用")


router = APIRouter(
    prefix="/api/v1/cowork",
    tags=["cowork"],
    dependencies=[Depends(require_owner_identity), Depends(require_cowork_enabled)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _root_response(value: object) -> SessionRootResponse:
    return SessionRootResponse.model_validate(value, from_attributes=True)


def _grant_response(value: object) -> CapabilityGrantResponse:
    return CapabilityGrantResponse.model_validate(value, from_attributes=True)


def _not_found(error: ConversationNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail="Cowork 会话不存在")


@router.get(
    "/sessions/{conversation_id}/roots", response_model=SessionRootListResponse
)
async def get_session_roots(
    conversation_id: UUID, session: DbSession
) -> SessionRootListResponse:
    try:
        items = await list_session_roots(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return SessionRootListResponse(items=[_root_response(item) for item in items])


@router.post(
    "/sessions/{conversation_id}/roots",
    response_model=SessionRootResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_session_root(
    conversation_id: UUID, request: SessionRootCreate, session: DbSession
) -> SessionRootResponse:
    try:
        root = await create_session_root(
            session,
            conversation_id=conversation_id,
            requested_path=request.path,
            access_mode=request.access_mode,
            label=request.label,
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    except (CoworkPermissionError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return _root_response(root)


@router.delete(
    "/sessions/{conversation_id}/roots/{root_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session_root(
    conversation_id: UUID, root_id: UUID, session: DbSession
) -> Response:
    try:
        deleted = await revoke_session_root(
            session, conversation_id=conversation_id, root_id=root_id
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="会话目录不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{conversation_id}/grants", response_model=CapabilityGrantListResponse
)
async def get_capability_grants(
    conversation_id: UUID, session: DbSession
) -> CapabilityGrantListResponse:
    try:
        items = await list_capability_grants(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return CapabilityGrantListResponse(items=[_grant_response(item) for item in items])


@router.post(
    "/sessions/{conversation_id}/grants",
    response_model=CapabilityGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_capability_grant(
    conversation_id: UUID, request: CapabilityGrantCreate, session: DbSession
) -> CapabilityGrantResponse:
    try:
        grant = await grant_capability(
            session,
            conversation_id=conversation_id,
            capability=request.capability,
            session_root_id=request.session_root_id,
            expires_in_s=request.expires_in_s,
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    except SessionRootNotFoundError as error:
        raise HTTPException(status_code=404, detail="会话目录不存在") from error
    except (CapabilityDeniedError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return _grant_response(grant)


@router.delete(
    "/sessions/{conversation_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_capability_grant(
    conversation_id: UUID, grant_id: UUID, session: DbSession
) -> Response:
    try:
        deleted = await revoke_capability_grant(
            session, conversation_id=conversation_id, grant_id=grant_id
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="能力授权不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{conversation_id}/artifacts", response_model=ArtifactListResponse
)
async def get_artifacts(
    conversation_id: UUID, session: DbSession
) -> ArtifactListResponse:
    try:
        items = await list_artifacts(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return ArtifactListResponse(
        items=[ArtifactResponse.model_validate(item, from_attributes=True) for item in items]
    )
