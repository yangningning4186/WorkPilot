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
from app.cowork.memory import (
    apply_memory_operation,
    get_active_successor,
    get_curated_memory,
    list_curated_memories,
    set_memory_pinned,
)
from app.cowork_contracts import (
    MEMORY_CATEGORIES,
    CoworkMemoryRecord,
    MemoryNotFoundError,
    PinnedMemoryError,
)
from app.schemas.memory import MemoryCreate, MemoryListResponse, MemoryResponse, MemoryUpdate

router = APIRouter(
    prefix="/api/v1/memories",
    tags=["memory"],
    dependencies=[Depends(require_owner_identity)],
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
    target_id: UUID | None = None,
) -> CoworkMemoryRecord:
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
        )
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


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def post_memory(request: MemoryCreate) -> MemoryResponse:
    return _response(
        await _write_memory(
            operation="ADD",
            category=request.category,
            fact=request.fact,
            pinned=request.pinned,
        )
    )


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def patch_memory(memory_id: UUID, request: MemoryUpdate) -> MemoryResponse:
    existing = await get_curated_memory(memory_id)
    if existing is None or not existing.active:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    if request.fact is None and request.category is None:
        assert request.pinned is not None
        return _response(await set_memory_pinned(memory_id=memory_id, pinned=request.pinned))
    # 改内容走 UPDATE：旧的那条标失效并指向新的，历史视图里看得见改了什么。
    # 置顶会挡住自动改写，所以人工编辑前先松开，写完再按原样恢复。
    was_pinned = existing.pinned
    if was_pinned:
        await set_memory_pinned(memory_id=memory_id, pinned=False)
    updated = await _write_memory(
        operation="UPDATE",
        category=request.category or existing.category,
        fact=request.fact or existing.content,
        pinned=was_pinned if request.pinned is None else request.pinned,
        target_id=memory_id,
    )
    return _response(updated)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: UUID) -> Response:
    existing = await get_curated_memory(memory_id)
    if existing is None or not existing.active:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    if existing.pinned:
        await set_memory_pinned(memory_id=memory_id, pinned=False)
    try:
        await apply_memory_operation(
            operation="DELETE",
            category=existing.category,
            fact=existing.content,
            confidence=1.0,
            valid_from=datetime.now(UTC),
            actor="manual",
            target_id=memory_id,
        )
    except MemoryNotFoundError as error:  # 并发删除统一映射为 404
        raise HTTPException(status_code=404, detail="当前记忆不存在") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{memory_id}/restore", response_model=MemoryResponse)
async def restore_memory(memory_id: UUID) -> MemoryResponse:
    historical = await get_curated_memory(memory_id)
    if historical is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if historical.invalid_at is None:
        raise HTTPException(status_code=409, detail="记忆当前仍然有效")
    # 恢复不是"把旧记录改回生效"，而是以它的内容再写一条新的：历史必须保持只增不改，
    # 否则「这条什么时候不成立了」这个问题会在恢复之后变得无法回答。
    current = await get_active_successor(memory_id)
    if current is not None and current.pinned:
        await set_memory_pinned(memory_id=current.id, pinned=False)
    return _response(
        await _write_memory(
            operation="UPDATE" if current is not None else "ADD",
            category=historical.category,
            fact=historical.content,
            pinned=historical.pinned,
            target_id=None if current is None else current.id,
        )
    )
