from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_model_gateway, require_owner_identity
from app.core.db import get_db_session
from app.llm.gateway import ModelGateway
from app.memory.store import (
    MEMORY_CATEGORIES,
    MemoryNotFoundError,
    MemoryRecord,
    apply_memory_operation,
    get_active_successor,
    get_memory,
    list_memories,
    set_memory_pinned,
)
from app.schemas.memory import MemoryCreate, MemoryListResponse, MemoryResponse, MemoryUpdate

router = APIRouter(
    prefix="/api/v1/memories",
    tags=["memory"],
    dependencies=[Depends(require_owner_identity)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Gateway = Annotated[ModelGateway, Depends(get_model_gateway)]


def _response(memory: MemoryRecord) -> MemoryResponse:
    return MemoryResponse.model_validate(memory, from_attributes=True)


async def _embedding(gateway: ModelGateway, fact: str) -> list[float]:
    result = await gateway.embed([fact], task_type="memory_manual_embedding")
    return result.embeddings[0]


async def _write_memory(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    operation: Literal["ADD", "UPDATE"],
    category: str,
    fact: str,
    pinned: bool,
    target_id: UUID | None = None,
) -> MemoryRecord:
    vector = await _embedding(gateway, fact)
    write = await apply_memory_operation(
        session,
        operation=operation,
        category=category,  # type: ignore[arg-type]
        fact=fact,
        confidence=1.0,
        valid_from=datetime.now(UTC),
        actor="manual",
        source_message_id=None,
        embedding=vector,
        embedding_model=gateway.embedding_model,
        embedding_provider=gateway.embedding_provider,
        embedding_revision=gateway.embedding_revision,
        target_id=target_id,
        pinned=pinned,
    )
    assert write.memory is not None
    return write.memory


@router.get("", response_model=MemoryListResponse)
async def get_memories(
    session: DbSession,
    view: Annotated[Literal["current", "history", "all"], Query()] = "current",
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> MemoryListResponse:
    if category is not None and category not in MEMORY_CATEGORIES:
        raise HTTPException(status_code=422, detail="未知记忆类别")
    active = {"current": True, "history": False, "all": None}[view]
    items = await list_memories(
        session,
        active=active,
        category=category,  # type: ignore[arg-type]
        limit=limit,
    )
    return MemoryListResponse(items=[_response(item) for item in items], total=len(items))


@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def post_memory(
    request: MemoryCreate, session: DbSession, gateway: Gateway
) -> MemoryResponse:
    memory = await _write_memory(
        session,
        gateway,
        operation="ADD",
        category=request.category,
        fact=request.fact,
        pinned=request.pinned,
    )
    await session.commit()
    return _response(memory)


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def patch_memory(
    memory_id: UUID,
    request: MemoryUpdate,
    session: DbSession,
    gateway: Gateway,
) -> MemoryResponse:
    existing = await get_memory(session, memory_id, for_update=True)
    if existing is None or existing.invalid_at is not None:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    if request.fact is None and request.category is None:
        assert request.pinned is not None
        updated = await set_memory_pinned(session, memory_id=memory_id, pinned=request.pinned)
    else:
        updated = await _write_memory(
            session,
            gateway,
            operation="UPDATE",
            category=request.category or existing.category,
            fact=request.fact or existing.fact,
            pinned=existing.pinned if request.pinned is None else request.pinned,
            target_id=memory_id,
        )
    await session.commit()
    return _response(updated)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: UUID, session: DbSession) -> Response:
    existing = await get_memory(session, memory_id)
    if existing is None or existing.invalid_at is not None:
        raise HTTPException(status_code=404, detail="当前记忆不存在")
    try:
        await apply_memory_operation(
            session,
            operation="DELETE",
            category=existing.category,
            fact=existing.fact,
            confidence=1.0,
            valid_from=datetime.now(UTC),
            actor="manual",
            source_message_id=None,
            embedding=None,
            embedding_model=None,
            embedding_provider=None,
            embedding_revision=None,
            target_id=memory_id,
        )
    except MemoryNotFoundError as error:  # 并发删除统一映射为 404
        raise HTTPException(status_code=404, detail="当前记忆不存在") from error
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{memory_id}/restore", response_model=MemoryResponse)
async def restore_memory(memory_id: UUID, session: DbSession, gateway: Gateway) -> MemoryResponse:
    historical = await get_memory(session, memory_id)
    if historical is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if historical.invalid_at is None:
        raise HTTPException(status_code=409, detail="记忆当前仍然有效")
    current = await get_active_successor(session, memory_id)
    restored = await _write_memory(
        session,
        gateway,
        operation="UPDATE" if current is not None else "ADD",
        category=historical.category,
        fact=historical.fact,
        pinned=historical.pinned,
        target_id=None if current is None else current.id,
    )
    await session.commit()
    return _response(restored)
