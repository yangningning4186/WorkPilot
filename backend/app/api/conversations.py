from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_identity
from app.core.db import get_db_session
from app.schemas.conversations import (
    ConversationCreate,
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationMessageResponse,
    ConversationResponse,
)
from app.services.conversations import (
    ConversationBusyError,
    delete_conversation,
    get_conversation,
    list_conversation_messages,
    list_conversations,
)
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
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ConversationListResponse:
    items = await list_conversations(
        session,
        scope=identity.scope,
        demo_session_id=identity.demo_session_id,
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
            ConversationMessageResponse.model_validate(item, from_attributes=True)
            for item in items
        ],
        total=len(items),
    )
