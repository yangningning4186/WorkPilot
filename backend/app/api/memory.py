"""长期记忆面板。

两套记忆合并之后这里读写的就是 Cowork 的那一份存储，不再有独立的 RAG 记忆表。
`fact` / `source_type` 这两个对外字段保留原名，映射到记录的 `content` / `source`：
前端已经按这套契约渲染，为一次内部合并去改 API 字段名不值得。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.memory import (
    apply_memory_operation,
    get_active_successor,
    get_curated_memory,
    list_curated_memories,
    set_memory_pinned,
)
from app.cowork.memory_policy import (
    EffectiveMemoryPolicy,
    MemoryPolicyDeniedError,
    get_conversation_memory_policy,
    get_effective_memory_policy,
    get_owner_memory_policy,
    memory_policy_message,
    require_memory_save_enabled,
    resolve_effective_memory_policy,
    set_conversation_memory_policy,
    set_owner_memory_policy,
)
from app.cowork_contracts import (
    MEMORY_CATEGORIES,
    ConversationMemoryPolicy,
    CoworkMemoryRecord,
    MemoryNotFoundError,
    MemoryPolicyConflictError,
    OwnerMemoryPolicy,
    PinnedMemoryError,
)
from app.runstore.conversations import get_conversation
from app.schemas.memory import (
    ConversationMemoryPolicyResponse,
    ConversationMemoryPolicyUpdate,
    MemoryCreate,
    MemoryListResponse,
    MemoryResponse,
    MemoryUpdate,
    OwnerMemoryPolicyResponse,
    OwnerMemoryPolicyUpdate,
)

router = APIRouter(
    prefix="/api/v1/memories",
    tags=["memory"],
    dependencies=[Depends(require_owner_identity)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _policy_denied(error: MemoryPolicyDeniedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": error.reason, "message": memory_policy_message(error.reason)},
    )


async def _require_save_policy(
    settings: Settings, *, conversation_id: UUID | None
) -> EffectiveMemoryPolicy:
    policy = await get_effective_memory_policy(settings, conversation_id=conversation_id)
    try:
        require_memory_save_enabled(policy)
    except MemoryPolicyDeniedError as error:
        raise _policy_denied(error) from error
    return policy


def _owner_policy_response(
    settings: Settings, owner: OwnerMemoryPolicy
) -> OwnerMemoryPolicyResponse:
    effective = resolve_effective_memory_policy(settings, owner=owner, conversation=None)
    return OwnerMemoryPolicyResponse(
        revision=owner.revision,
        save_enabled=owner.save_enabled,
        recall_enabled=owner.recall_enabled,
        standing_rules=owner.standing_rules,
        deployment_save_enabled=settings.memory_save_enabled,
        deployment_recall_enabled=settings.memory_recall_enabled,
        effective_save_enabled=effective.save_enabled,
        effective_recall_enabled=effective.recall_enabled,
        save_disabled_reason=effective.save_disabled_reason,
        recall_disabled_reason=effective.recall_disabled_reason,
    )


def _conversation_policy_response(
    effective: EffectiveMemoryPolicy,
    conversation: ConversationMemoryPolicy,
) -> ConversationMemoryPolicyResponse:
    return ConversationMemoryPolicyResponse(
        conversation_id=conversation.conversation_id,
        revision=conversation.revision,
        save_mode=conversation.save_mode,
        recall_mode=conversation.recall_mode,
        effective_save_enabled=effective.save_enabled,
        effective_recall_enabled=effective.recall_enabled,
        save_disabled_reason=effective.save_disabled_reason,
        recall_disabled_reason=effective.recall_disabled_reason,
    )


def _response(memory: CoworkMemoryRecord) -> MemoryResponse:
    return MemoryResponse(
        id=memory.id,
        category=memory.category,
        fact=memory.content,
        valid_from=memory.valid_from or memory.created_at,
        invalid_at=memory.invalid_at,
        superseded_by=memory.superseded_by,
        source_type="manual" if memory.source == "user" else "conversation",
        source_message_id=memory.source_message_id,
        confidence=memory.confidence,
        access_count=memory.access_count,
        last_used_at=memory.last_used_at,
        pinned=memory.pinned,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


async def _write_memory(
    *,
    operation: Literal["ADD", "UPDATE"],
    category: str,
    fact: str,
    pinned: bool,
    settings: Settings,
    conversation_id: UUID | None = None,
    target_id: UUID | None = None,
    key: str | None = None,
) -> CoworkMemoryRecord:
    # API 预检只能改善 UX；真正副作用前必须再读一次策略，避免 owner 在请求执行期间关掉保存。
    policy = await _require_save_policy(settings, conversation_id=conversation_id)
    try:
        write = await apply_memory_operation(
            operation=operation,
            category=category,  # type: ignore[arg-type]
            fact=fact,
            confidence=1.0,
            valid_from=datetime.now(UTC),
            actor="manual",
            target_id=target_id,
            pinned=pinned,
            key=key,
            settings=settings,
            effective_policy=policy,
        )
    except MemoryPolicyDeniedError as error:
        raise _policy_denied(error) from error
    except PinnedMemoryError as error:
        raise HTTPException(status_code=409, detail="置顶记忆需要先取消置顶") from error
    except MemoryNotFoundError as error:
        raise HTTPException(status_code=404, detail="当前记忆不存在") from error
    assert write.memory is not None
    return write.memory


@router.get("", response_model=MemoryListResponse)
async def get_memories(
    view: Annotated[Literal["current", "history"], Query()] = "current",
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> MemoryListResponse:
    if category is not None and category not in MEMORY_CATEGORIES:
        raise HTTPException(status_code=422, detail="未知记忆类别")
    items = await list_curated_memories(active=view == "current", limit=limit)
    if category is not None:
        items = [item for item in items if item.category == category]
    return MemoryListResponse(items=[_response(item) for item in items], total=len(items))


@router.get("/policy", response_model=OwnerMemoryPolicyResponse)
async def get_memory_policy(
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerMemoryPolicyResponse:
    """读取 owner 级保存/召回策略与常驻规则。路由整体已经是 owner-only。"""

    return _owner_policy_response(settings, await get_owner_memory_policy())


@router.put("/policy", response_model=OwnerMemoryPolicyResponse)
async def put_memory_policy(
    request: OwnerMemoryPolicyUpdate,
    settings: Annotated[Settings, Depends(get_settings)],
) -> OwnerMemoryPolicyResponse:
    current = await get_owner_memory_policy()
    try:
        saved = await set_owner_memory_policy(
            save_enabled=(
                current.save_enabled if request.save_enabled is None else request.save_enabled
            ),
            recall_enabled=(
                current.recall_enabled if request.recall_enabled is None else request.recall_enabled
            ),
            standing_rules=(
                current.standing_rules if request.standing_rules is None else request.standing_rules
            ),
            expected_revision=request.expected_revision,
        )
    except MemoryPolicyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.reason, "message": memory_policy_message(error.reason)},
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _owner_policy_response(settings, saved)


@router.get(
    "/conversations/{conversation_id}/policy",
    response_model=ConversationMemoryPolicyResponse,
)
async def get_conversation_policy(
    conversation_id: UUID,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationMemoryPolicyResponse:
    if await get_conversation(session, conversation_id=conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    conversation = await get_conversation_memory_policy(conversation_id=conversation_id)
    effective = await get_effective_memory_policy(settings, conversation_id=conversation_id)
    return _conversation_policy_response(effective, conversation)


@router.put(
    "/conversations/{conversation_id}/policy",
    response_model=ConversationMemoryPolicyResponse,
)
async def put_conversation_policy(
    conversation_id: UUID,
    request: ConversationMemoryPolicyUpdate,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationMemoryPolicyResponse:
    if await get_conversation(session, conversation_id=conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    current = await get_conversation_memory_policy(conversation_id=conversation_id)
    try:
        saved = await set_conversation_memory_policy(
            conversation_id=conversation_id,
            save_mode=current.save_mode if request.save_mode is None else request.save_mode,
            recall_mode=current.recall_mode if request.recall_mode is None else request.recall_mode,
            expected_revision=request.expected_revision,
        )
    except MemoryPolicyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": error.reason, "message": memory_policy_message(error.reason)},
        ) from error
    effective = await get_effective_memory_policy(settings, conversation_id=conversation_id)
    return _conversation_policy_response(effective, saved)


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def post_memory(
    request: MemoryCreate,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MemoryResponse:
    return _response(
        await _write_memory(
            operation="ADD",
            category=request.category,
            fact=request.fact,
            pinned=request.pinned,
            settings=settings,
        )
    )


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def patch_memory(
    memory_id: UUID,
    request: MemoryUpdate,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MemoryResponse:
    existing = await get_curated_memory(memory_id)
    if existing is None or not existing.active:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    if request.fact is None and request.category is None:
        assert request.pinned is not None
        policy = await _require_save_policy(settings, conversation_id=None)
        try:
            return _response(
                await set_memory_pinned(
                    memory_id=memory_id,
                    pinned=request.pinned,
                    settings=settings,
                    effective_policy=policy,
                )
            )
        except MemoryPolicyDeniedError as error:
            raise _policy_denied(error) from error
    await _require_save_policy(settings, conversation_id=None)
    # 改内容走 UPDATE：旧的那条标失效并指向新的，历史视图里看得见改了什么。
    # 置顶只挡住模型；owner 的修改在 successor 事务里原子完成，无需先原地松开。
    was_pinned = existing.pinned
    updated = await _write_memory(
        operation="UPDATE",
        category=request.category or existing.category,
        fact=request.fact or existing.content,
        pinned=was_pinned if request.pinned is None else request.pinned,
        settings=settings,
        target_id=memory_id,
    )
    return _response(updated)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: UUID) -> Response:
    existing = await get_curated_memory(memory_id)
    if existing is None or not existing.active:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    try:
        await apply_memory_operation(
            operation="DELETE",
            category=existing.category,
            fact=existing.content,
            confidence=1.0,
            valid_from=datetime.now(UTC),
            actor="manual",
            target_id=memory_id,
            allow_policy_bypass=True,
        )
    except MemoryNotFoundError as error:  # 并发删除统一映射为 404
        raise HTTPException(status_code=404, detail="当前记忆不存在") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{memory_id}/restore", response_model=MemoryResponse)
async def restore_memory(
    memory_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MemoryResponse:
    historical = await get_curated_memory(memory_id)
    if historical is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if historical.invalid_at is None:
        raise HTTPException(status_code=409, detail="记忆当前仍然有效")
    await _require_save_policy(settings, conversation_id=None)
    # 恢复不是"把旧记录改回生效"，而是以它的内容再写一条新的：历史必须保持只增不改，
    # 否则「这条什么时候不成立了」这个问题会在恢复之后变得无法回答。
    current = await get_active_successor(memory_id)
    return _response(
        await _write_memory(
            operation="UPDATE" if current is not None else "ADD",
            category=historical.category,
            fact=historical.content,
            pinned=historical.pinned,
            settings=settings,
            target_id=None if current is None else current.id,
            key=historical.key,
        )
    )
