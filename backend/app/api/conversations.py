from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.context_usage import get_cowork_context_usage
from app.cowork.permissions import list_session_roots
from app.cowork.personas import (
    approval_mode_for_persona_change,
    load_persona_catalog,
    snapshot_persona,
)
from app.cowork.provider_profiles import ensure_default_provider_binding, get_provider_profile
from app.cowork.runtime import record_persona_reselection
from app.rag.kb import local_kb_service
from app.runstore.conversations import (
    ConversationBusyError,
    ConversationRecord,
    delete_conversation,
    fork_conversation,
    get_conversation,
    list_conversation_entries,
    list_conversation_messages,
    list_conversations,
    navigate_conversation_lane,
    set_conversation_archived,
    update_conversation_runtime,
)
from app.runstore.runs import ensure_conversation
from app.schemas.conversations import (
    ConversationArchiveUpdate,
    ConversationContextUsageResponse,
    ConversationCreate,
    ConversationForkRequest,
    ConversationLaneNavigateRequest,
    ConversationLaneNavigateResponse,
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationMessageResponse,
    ConversationResponse,
    ConversationRuntimeUpdate,
    SessionEntryListResponse,
    SessionEntryResponse,
)

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RuntimeSettings = Annotated[Settings, Depends(get_settings)]
Owner = Annotated[None, Depends(require_owner_identity)]


def _conversation_response(record: ConversationRecord, settings: Settings) -> ConversationResponse:
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
                    "active_run_id",
                    "message_count",
                    "latest_message",
                    "last_message_at",
                    "provider_profile_id",
                    "unattended",
                    "approval_mode",
                    "persona_name",
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
    await ensure_default_provider_binding(
        conversation_id=conversation_id,
        settings=settings,
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
    current = await get_conversation(session, conversation_id=conversation_id)
    if current is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    roots = await list_session_roots(session, conversation_id=conversation_id)
    project_roots = tuple(Path(item.canonical_path) for item in roots)
    try:
        persona = load_persona_catalog(
            settings,
            project_roots=project_roots,
        ).get(request.persona_name)
        persona_snapshot = snapshot_persona(
            persona,
            settings,
            project_roots=project_roots,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    approval_mode = approval_mode_for_persona_change(
        current_name=current.persona_name,
        requested_mode=request.approval_mode,
        selected=persona,
    )
    # A same-name PUT with no unrelated runtime change is the explicit "choose this Persona
    # again" action. Provider/model/autonomy-only updates must never re-baseline a drifted
    # Persona merely because the request body also carries its current name.
    same_name_reselection = (
        persona.name == current.persona_name
        and request.provider_profile_id == current.provider_profile_id
        and request.model_override == current.model_override
        and request.unattended == current.unattended
        and approval_mode == current.approval_mode
    )
    try:
        updated = await update_conversation_runtime(
            session,
            conversation_id=conversation_id,
            provider_profile_id=request.provider_profile_id,
            model_override=request.model_override,
            unattended=request.unattended,
            # 选择 Persona 是一次显式产品动作；它的默认审批档在这里落进会话，之后用户
            # 仍可单独切换。运行时 Persona 本身没有越过审批边界的能力。
            approval_mode=approval_mode,
            persona_name=persona.name,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if same_name_reselection:
        await record_persona_reselection(
            session,
            conversation_id=conversation_id,
            persona_snapshot=persona_snapshot,
        )
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
    lane: Annotated[str, Query(min_length=1, max_length=80)] = "main",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConversationMessageListResponse:
    items = await list_conversation_messages(
        session,
        conversation_id=conversation_id,
        lane=lane,
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


@router.get("/{conversation_id}/entries", response_model=SessionEntryListResponse)
async def get_entries(
    conversation_id: UUID,
    session: DbSession,
    _: Owner,
    lane: Annotated[str, Query(min_length=1, max_length=80)] = "main",
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> SessionEntryListResponse:
    entries = await list_conversation_entries(
        session,
        conversation_id=conversation_id,
        lane=None if lane == "all" else lane,
        limit=limit,
    )
    if entries is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    items = [
        SessionEntryResponse.model_validate(
            {
                "id": item.id,
                "parent_id": item.parent_id,
                "seq": item.seq,
                "kind": item.kind,
                "payload": item.payload,
                "created_at": item.created_at,
            }
        )
        for item in entries
    ]
    return SessionEntryListResponse(items=items, total=len(items))


@router.post(
    "/{conversation_id}/lanes/main/navigate",
    response_model=ConversationLaneNavigateResponse,
)
async def post_lane_navigation(
    conversation_id: UUID,
    request: ConversationLaneNavigateRequest,
    session: DbSession,
    _: Owner,
) -> ConversationLaneNavigateResponse:
    try:
        result = await navigate_conversation_lane(
            session,
            conversation_id=conversation_id,
            target_entry_id=request.target_entry_id,
            position=request.position,
            summarize=request.summarize,
        )
    except ConversationBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return ConversationLaneNavigateResponse.model_validate(result, from_attributes=True)


@router.post("/{conversation_id}/fork", response_model=ConversationResponse)
async def post_conversation_fork(
    conversation_id: UUID,
    request: ConversationForkRequest,
    session: DbSession,
    settings: RuntimeSettings,
    _: Owner,
) -> ConversationResponse:
    try:
        fork_id = await fork_conversation(
            session,
            conversation_id=conversation_id,
            message_id=request.message_id,
            position=request.position,
            title=request.title,
        )
    except ConversationBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    created = await get_conversation(session, conversation_id=fork_id)
    if created is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="分支会话创建失败")
    return _conversation_response(created, settings)


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
    settings = get_settings()
    # RAG 实现在入口这一层装配：`app.cowork` 不许 import `app.rag`，估算侧的工具面
    # 必须和 worker 那次装配用同一个 `RagService`，否则占用条量的不是真正发出去的东西。
    usage = await get_cowork_context_usage(
        session,
        conversation_id=conversation_id,
        settings=settings,
        rag=local_kb_service(settings),
    )
    return ConversationContextUsageResponse.model_validate(usage)
