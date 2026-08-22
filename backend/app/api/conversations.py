from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.context_usage import get_cowork_context_usage
from app.cowork.provider_profiles import get_provider_profile
from app.runstore.conversations import (
    ConversationBusyError,
    ConversationRecord,
    delete_conversation,
    get_conversation,
    list_conversation_messages,
    list_conversations,
    set_conversation_archived,
    update_conversation_runtime,
)
from app.runstore.runs import ensure_conversation
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

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RuntimeSettings = Annotated[Settings, Depends(get_settings)]
Owner = Annotated[None, Depends(require_owner_identity)]


def _conversation_response(
    record: ConversationRecord, settings: Settings
) -> ConversationResponse:
    """会话只记 Provider 的 id；名字与默认模型在这里解引用。

    Profile 出了数据库之后 id 可能悬空（用户删掉了 profile）。那种情况按"没选
    Provider"渲染，而不是 500——列表页要能打开，用户才有地方重新选一个。
    """

    profile = (
        None
        if record.provider_profile_id is None
        else get_provider_profile(settings, record.provider_profile_id)
    )
    return ConversationResponse.model_validate(
        {
            **{
                key: getattr(record, key)
                for key in (
                    "id",
                    "title",
                    "message_count",
                    "latest_message",
                    "last_message_at",
                    "provider_profile_id",
                    "unattended",
                    "approval_mode",
                    "archived_at",
                    "created_at",
                    "updated_at",
                )
            },
            "provider_name": None if profile is None else profile.name,
            "provider": None if profile is None else profile.provider,
            "selected_model": record.model_override
            or (None if profile is None else profile.default_model),
        }
    )


@router.get("", response_model=ConversationListResponse)
async def get_conversations(
    session: DbSession,
    settings: RuntimeSettings,
    _: Owner,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ConversationListResponse:
    items = await list_conversations(
        session,
        archived=archived,
        limit=limit,
    )
    return ConversationListResponse(
        items=[_conversation_response(item, settings) for item in items],
        total=len(items),
    )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def post_conversation(
    request: ConversationCreate,
    session: DbSession,
    settings: RuntimeSettings,
    _: Owner,
) -> ConversationResponse:
    conversation_id = await ensure_conversation(
        session,
        title=request.title.strip(),
    )
    await session.commit()
    created = await get_conversation(
        session,
        conversation_id=conversation_id,
    )
    if created is None:  # pragma: no cover - 同一事务内必然可见
        raise HTTPException(status_code=500, detail="会话创建失败")
    return _conversation_response(created, settings)


@router.put("/{conversation_id}/runtime", response_model=ConversationResponse)
async def put_conversation_runtime(
    conversation_id: UUID,
    request: ConversationRuntimeUpdate,
    session: DbSession,
    settings: RuntimeSettings,
    _: Owner,
) -> ConversationResponse:
    # Profile 已经不在数据库里，没有外键可以挡住悬空引用，这一层必须自己校验。
    if request.provider_profile_id is not None:
        profile = get_provider_profile(settings, request.provider_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Provider 不存在")
        if not profile.enabled:
            raise HTTPException(status_code=422, detail="Provider 已停用")
    try:
        updated = await update_conversation_runtime(
            session,
            conversation_id=conversation_id,
            provider_profile_id=request.provider_profile_id,
            model_override=request.model_override,
            unattended=request.unattended,
            approval_mode=request.approval_mode,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return _conversation_response(updated, settings)


@router.put("/{conversation_id}/archive", response_model=ConversationResponse)
async def put_conversation_archive(
    conversation_id: UUID,
    request: ConversationArchiveUpdate,
    session: DbSession,
    settings: RuntimeSettings,
    _: Owner,
) -> ConversationResponse:
    try:
        updated = await set_conversation_archived(
            session,
            conversation_id=conversation_id,
            archived=request.archived,
        )
    except ConversationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return _conversation_response(updated, settings)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation_route(
    conversation_id: UUID,
    session: DbSession,
    _: Owner,
) -> Response:
    try:
        deleted = await delete_conversation(
            session,
            conversation_id=conversation_id,
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
    _: Owner,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConversationMessageListResponse:
    items = await list_conversation_messages(
        session,
        conversation_id=conversation_id,
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
    _: Owner,
) -> ConversationContextUsageResponse:
    conversation = await get_conversation(
        session,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    usage = await get_cowork_context_usage(
        session,
        conversation_id=conversation_id,
        settings=get_settings(),
    )
    return ConversationContextUsageResponse.model_validate(usage)
