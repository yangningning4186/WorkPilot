from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_identity
from app.core.config import get_settings
from app.core.db import get_db_session
from app.schemas.conversations import (
    ConversationArchiveUpdate,
    ConversationContextUsageResponse,
    ConversationCreate,
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationMessageResponse,
    ConversationResponse,
    ConversationRuntimeUpdate,
)
from app.services.conversations import (
    ConversationBusyError,
    delete_conversation,
    get_conversation,
    list_conversation_messages,
    list_conversations,
    set_conversation_archived,
    update_conversation_runtime,
)
from app.services.cowork_context_usage import get_cowork_context_usage
from app.services.request_identity import RequestIdentity
from app.services.runs import ensure_conversation

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]


def _conversation_response(value: object) -> ConversationResponse:
    return ConversationResponse.model_validate(value, from_attributes=True)


@router.get("", response_model=ConversationListResponse)
async def get_conversations(
    session: DbSession,
    identity: Identity,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ConversationListResponse:
    items = await list_conversations(
        session,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
        archived=archived,
        limit=limit,
    )
    return ConversationListResponse(
        items=[_conversation_response(item) for item in items],
        total=len(items),
    )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def post_conversation(
    request: ConversationCreate,
    session: DbSession,
    identity: Identity,
) -> ConversationResponse:
    conversation_id = await ensure_conversation(
        session,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
        title=request.title.strip(),
    )
    await session.commit()
    created = await get_conversation(
        session,
        conversation_id=conversation_id,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
    )
    if created is None:  # pragma: no cover - 同一事务内必然可见
        raise HTTPException(status_code=500, detail="会话创建失败")
    return _conversation_response(created)


@router.put("/{conversation_id}/runtime", response_model=ConversationResponse)
async def put_conversation_runtime(
    conversation_id: UUID,
    request: ConversationRuntimeUpdate,
    session: DbSession,
    identity: Identity,
) -> ConversationResponse:
    try:
        updated = await update_conversation_runtime(
            session,
            conversation_id=conversation_id,
            scope=identity.scope,
            demo_session_id=identity.demo_session_id,
            provider_profile_id=request.provider_profile_id,
            model_override=request.model_override,
            unattended=request.unattended,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return _conversation_response(updated)


@router.put("/{conversation_id}/archive", response_model=ConversationResponse)
async def put_conversation_archive(
    conversation_id: UUID,
    request: ConversationArchiveUpdate,
    session: DbSession,
    identity: Identity,
) -> ConversationResponse:
    try:
        updated = await set_conversation_archived(
            session,
            conversation_id=conversation_id,
            scope=identity.scope,
            demo_session_id=identity.demo_session_id,
            archived=request.archived,
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return _conversation_response(updated)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation_route(
    conversation_id: UUID,
    session: DbSession,
    identity: Identity,
) -> Response:
    try:
        deleted = await delete_conversation(
            session,
            conversation_id=conversation_id,
            scope=identity.scope,
            demo_session_id=identity.demo_session_id,
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{conversation_id}/messages", response_model=ConversationMessageListResponse)
async def get_messages(
    conversation_id: UUID,
    session: DbSession,
    identity: Identity,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConversationMessageListResponse:
    items = await list_conversation_messages(
        session,
        conversation_id=conversation_id,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
        limit=limit,
    )
    if items is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return ConversationMessageListResponse(
        items=[
            ConversationMessageResponse.model_validate(item, from_attributes=True) for item in items
        ],
        total=len(items),
    )


@router.get(
    "/{conversation_id}/context-usage",
    response_model=ConversationContextUsageResponse,
)
async def get_context_usage(
    conversation_id: UUID,
    session: DbSession,
    identity: Identity,
) -> ConversationContextUsageResponse:
    conversation = await get_conversation(
        session,
        conversation_id=conversation_id,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    usage = await get_cowork_context_usage(
        session,
        conversation_id=conversation_id,
        settings=get_settings(),
    )
    return ConversationContextUsageResponse.model_validate(usage)
