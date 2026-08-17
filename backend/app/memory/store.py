from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

MemoryCategory = Literal["preference", "profile", "interest", "fact"]
MemoryOperation = Literal["ADD", "UPDATE", "DELETE", "NOOP"]
MemoryActor = Literal["model", "manual"]
MemoryJobStatus = Literal["queued", "running", "done", "failed"]

MEMORY_CATEGORIES = frozenset({"preference", "profile", "interest", "fact"})
MEMORY_OPERATIONS = frozenset({"ADD", "UPDATE", "DELETE", "NOOP"})
EXTRACTOR_VERSION = "memory-op.v1"


class MemoryNotFoundError(LookupError):
    pass


class PinnedMemoryError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryRecord:
    id: UUID
    category: MemoryCategory
    fact: str
    valid_from: datetime
    invalid_at: datetime | None
    superseded_by: UUID | None
    source_type: Literal["conversation", "manual"]
    source_message_id: UUID | None
    confidence: float
    access_count: int
    last_used_at: datetime | None
    pinned: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MemoryWrite:
    operation: MemoryOperation
    memory: MemoryRecord | None
    target_id: UUID | None


@dataclass(frozen=True)
class MemoryExtractionJob:
    id: UUID
    run_id: UUID
    source_message_id: UUID
    extractor_version: str
    status: MemoryJobStatus
    attempts: int
    worker_id: str | None
    lease_until: datetime | None
    available_at: datetime
    operations: list[dict[str, Any]]
    error: str | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MemoryJobSource:
    job: MemoryExtractionJob
    conversation_id: UUID
    content: str
    message_created_at: datetime


_MEMORY_COLUMNS = """
    id, category, fact, valid_from, invalid_at, superseded_by, source_type,
    source_message_id, confidence, access_count, last_used_at, pinned, created_at, updated_at
"""

_JOB_COLUMNS = """
    id, run_id, source_message_id, extractor_version, status, attempts, worker_id,
    lease_until, available_at, operations, error, finished_at, created_at, updated_at
"""


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"


def _memory(row: Any) -> MemoryRecord:
    return MemoryRecord(**dict(row))


def _job(row: Any) -> MemoryExtractionJob:
    values = dict(row)
    values["operations"] = list(values["operations"] or [])
    return MemoryExtractionJob(**values)


async def list_memories(
    session: AsyncSession,
    *,
    active: bool | None = True,
    category: MemoryCategory | None = None,
    limit: int = 200,
) -> list[MemoryRecord]:
    if not 1 <= limit <= 1000:
        raise ValueError("limit 必须位于 1 到 1000")
    if category is not None and category not in MEMORY_CATEGORIES:
        raise ValueError("未知记忆类别")
    activity = "" if active is None else ("AND invalid_at IS NULL" if active else "AND invalid_at IS NOT NULL")
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_MEMORY_COLUMNS}
                    FROM memories
                    WHERE (CAST(:category AS text) IS NULL OR category = :category)
                      {activity}
                    ORDER BY pinned DESC, valid_from DESC, id
                    LIMIT :limit
                    """
                ),
                {"category": category, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [_memory(row) for row in rows]


async def list_pinned_memories(
    session: AsyncSession, *, limit: int = 3
) -> list[MemoryRecord]:
    if not 0 <= limit <= 20:
        raise ValueError("置顶记忆 limit 必须位于 0 到 20")
    if limit == 0:
        return []
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_MEMORY_COLUMNS}
                    FROM memories
                    WHERE invalid_at IS NULL AND pinned = true
                    ORDER BY valid_from DESC, id
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [_memory(row) for row in rows]


async def mark_memories_used(session: AsyncSession, memory_ids: list[UUID]) -> None:
    if not memory_ids:
        return
    await session.execute(
        text(
            """
            UPDATE memories
            SET access_count = access_count + 1, last_used_at = now()
            WHERE id = ANY(:ids) AND invalid_at IS NULL
            """
        ),
        {"ids": memory_ids},
    )


async def run_uses_owner_memory(session: AsyncSession, run_id: UUID) -> bool:
    return bool(
        (
            await session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM agent_runs ar
                        JOIN conversations c ON c.id = ar.conversation_id
                        WHERE ar.id = :run_id
                          AND c.scope = 'local_owner'
                          AND c.demo_session_id IS NULL
                    )
                    """
                ),
                {"run_id": run_id},
            )
        ).scalar_one()
    )


async def get_memory(
    session: AsyncSession, memory_id: UUID, *, for_update: bool = False
) -> MemoryRecord | None:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_MEMORY_COLUMNS}
                    FROM memories
                    WHERE id = :id
                    {"FOR UPDATE" if for_update else ""}
                    """
                ),
                {"id": memory_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _memory(row)


async def get_active_successor(session: AsyncSession, memory_id: UUID) -> MemoryRecord | None:
    """沿版本链找到当前有效版本；删除链没有 successor 时返回 None。"""

    row = (
        (
            await session.execute(
                text(
                    f"""
                    WITH RECURSIVE chain AS (
                        SELECT m.*, 0 AS depth
                        FROM memories m
                        WHERE m.id = :id
                        UNION ALL
                        SELECT next_memory.*, chain.depth + 1
                        FROM chain
                        JOIN memories next_memory ON next_memory.id = chain.superseded_by
                        WHERE chain.depth < 100
                    )
                    SELECT {_MEMORY_COLUMNS}
                    FROM chain
                    WHERE invalid_at IS NULL
                    ORDER BY depth DESC
                    LIMIT 1
                    """
                ),
                {"id": memory_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _memory(row)


async def search_active_memories(
    session: AsyncSession,
    *,
    embedding: list[float],
    embedding_model: str,
    embedding_provider: str,
    embedding_revision: str,
    top_k: int = 5,
) -> list[MemoryRecord]:
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    vector = _vector_literal(embedding)
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_MEMORY_COLUMNS}
                    FROM memories
                    WHERE invalid_at IS NULL
                      AND embedding IS NOT NULL
                      AND embedding_model = :embedding_model
                      AND embedding_provider = :embedding_provider
                      AND embedding_revision = :embedding_revision
                    ORDER BY embedding <=> CAST(:embedding AS vector), id
                    LIMIT :top_k
                    """
                ),
                {
                    "embedding": vector,
                    "embedding_model": embedding_model,
                    "embedding_provider": embedding_provider,
                    "embedding_revision": embedding_revision,
                    "top_k": top_k,
                },
            )
        )
        .mappings()
        .all()
    )
    return [_memory(row) for row in rows]


async def apply_memory_operation(
    session: AsyncSession,
    *,
    operation: MemoryOperation,
    category: MemoryCategory,
    fact: str,
    confidence: float,
    valid_from: datetime,
    actor: MemoryActor,
    source_message_id: UUID | None,
    embedding: list[float] | None,
    embedding_model: str | None,
    embedding_provider: str | None,
    embedding_revision: str | None,
    target_id: UUID | None = None,
    pinned: bool = False,
) -> MemoryWrite:
    normalized_fact = " ".join(fact.split())
    if operation not in MEMORY_OPERATIONS:
        raise ValueError("未知记忆操作")
    if category not in MEMORY_CATEGORIES:
        raise ValueError("未知记忆类别")
    if not normalized_fact or len(normalized_fact) > 2000:
        raise ValueError("记忆事实长度必须位于 1 到 2000")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence 必须位于 0 到 1")
    if actor == "model" and source_message_id is None:
        raise ValueError("模型记忆必须锚定来源消息")
    if (embedding is None) != (embedding_model is None):
        raise ValueError("embedding 与身份字段必须同时提供")
    if embedding is not None and (embedding_provider is None or embedding_revision is None):
        raise ValueError("embedding 身份字段不完整")
    if embedding is None and (embedding_provider is not None or embedding_revision is not None):
        raise ValueError("没有 embedding 时不能提供身份字段")

    target: MemoryRecord | None = None
    if operation != "ADD":
        if target_id is None:
            raise ValueError(f"{operation} 需要 target_id")
        target = await get_memory(session, target_id, for_update=True)
        if target is None or target.invalid_at is not None:
            raise MemoryNotFoundError(str(target_id))
        if actor == "model" and target.pinned and operation in {"UPDATE", "DELETE"}:
            raise PinnedMemoryError("置顶记忆不允许被模型自动失效")

    if operation == "NOOP":
        assert target is not None
        row = (
            (
                await session.execute(
                    text(
                        f"""
                        UPDATE memories
                        SET access_count = access_count + 1,
                            last_used_at = now()
                        WHERE id = :id
                        RETURNING {_MEMORY_COLUMNS}
                        """
                    ),
                    {"id": target.id},
                )
            )
            .mappings()
            .one()
        )
        return MemoryWrite(operation=operation, memory=_memory(row), target_id=target.id)

    if operation == "DELETE":
        assert target is not None
        await _invalidate(session, target.id, superseded_by=None)
        return MemoryWrite(operation=operation, memory=None, target_id=target.id)

    new_memory = await _insert_memory(
        session,
        category=category,
        fact=normalized_fact,
        confidence=confidence,
        valid_from=valid_from,
        source_type="conversation" if actor == "model" else "manual",
        source_message_id=source_message_id,
        embedding=embedding,
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        embedding_revision=embedding_revision,
        pinned=pinned,
    )
    if operation == "UPDATE":
        assert target is not None
        await _invalidate(session, target.id, superseded_by=new_memory.id)
    return MemoryWrite(operation=operation, memory=new_memory, target_id=target_id)


async def _insert_memory(
    session: AsyncSession,
    *,
    category: MemoryCategory,
    fact: str,
    confidence: float,
    valid_from: datetime,
    source_type: Literal["conversation", "manual"],
    source_message_id: UUID | None,
    embedding: list[float] | None,
    embedding_model: str | None,
    embedding_provider: str | None,
    embedding_revision: str | None,
    pinned: bool,
) -> MemoryRecord:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO memories
                        (id, category, fact, embedding, embedding_model, embedding_provider,
                         embedding_revision, valid_from, source_type, source_message_id,
                         confidence, pinned)
                    VALUES
                        (:id, :category, :fact, CAST(:embedding AS vector), :embedding_model,
                         :embedding_provider, :embedding_revision, :valid_from, :source_type,
                         :source_message_id, :confidence, :pinned)
                    RETURNING {_MEMORY_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "category": category,
                    "fact": fact,
                    "embedding": None if embedding is None else _vector_literal(embedding),
                    "embedding_model": embedding_model,
                    "embedding_provider": embedding_provider,
                    "embedding_revision": embedding_revision,
                    "valid_from": valid_from,
                    "source_type": source_type,
                    "source_message_id": source_message_id,
                    "confidence": confidence,
                    "pinned": pinned,
                },
            )
        )
        .mappings()
        .one()
    )
    return _memory(row)


async def _invalidate(
    session: AsyncSession, memory_id: UUID, *, superseded_by: UUID | None
) -> None:
    updated = (
        await session.execute(
            text(
                """
                UPDATE memories
                SET invalid_at = GREATEST(clock_timestamp(), valid_from + interval '1 microsecond'),
                    superseded_by = :superseded_by
                WHERE id = :id AND invalid_at IS NULL
                RETURNING id
                """
            ),
            {"id": memory_id, "superseded_by": superseded_by},
        )
    ).scalar_one_or_none()
    if updated is None:
        raise MemoryNotFoundError(str(memory_id))


async def set_memory_pinned(
    session: AsyncSession, *, memory_id: UUID, pinned: bool
) -> MemoryRecord:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE memories SET pinned = :pinned
                    WHERE id = :id AND invalid_at IS NULL
                    RETURNING {_MEMORY_COLUMNS}
                    """
                ),
                {"id": memory_id, "pinned": pinned},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise MemoryNotFoundError(str(memory_id))
    return _memory(row)


async def schedule_memory_extraction(
    session: AsyncSession,
    *,
    run_id: UUID,
    extractor_version: str = EXTRACTOR_VERSION,
) -> MemoryExtractionJob | None:
    """仅为已完成的 local_owner answer run 建立一次抽取作业。"""

    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO memory_extraction_jobs
                        (id, run_id, source_message_id, extractor_version)
                    SELECT :id, ar.id, m.id, :extractor_version
                    FROM agent_runs ar
                    JOIN conversations c ON c.id = ar.conversation_id
                    JOIN LATERAL (
                        SELECT candidate.id
                        FROM messages candidate
                        WHERE candidate.run_id = ar.id AND candidate.role = 'user'
                        ORDER BY candidate.created_at DESC, candidate.id DESC
                        LIMIT 1
                    ) m ON true
                    WHERE ar.id = :run_id
                      AND ar.status = 'done'
                      AND ar.workflow_type = 'answer'
                      AND c.scope = 'local_owner'
                      AND c.demo_session_id IS NULL
                    ON CONFLICT (run_id) DO NOTHING
                    RETURNING {_JOB_COLUMNS}
                    """
                ),
                {"id": uuid7(), "run_id": run_id, "extractor_version": extractor_version},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is not None:
        return _job(row)
    existing = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_JOB_COLUMNS} FROM memory_extraction_jobs WHERE run_id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if existing is None else _job(existing)


async def claim_memory_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_s: int,
    max_attempts: int,
) -> MemoryJobSource | None:
    if lease_s <= 0 or max_attempts <= 0:
        raise ValueError("lease_s 与 max_attempts 必须为正数")
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE memory_extraction_jobs j
                    SET status = 'running',
                        attempts = attempts + 1,
                        worker_id = :worker_id,
                        lease_until = now() + make_interval(secs => :lease_s),
                        error = NULL
                    FROM messages m, conversations c
                    WHERE j.id = :job_id
                      AND j.source_message_id = m.id
                      AND m.conversation_id = c.id
                      AND c.scope = 'local_owner'
                      AND c.demo_session_id IS NULL
                      AND j.attempts < :max_attempts
                      AND (
                        (j.status = 'queued' AND j.available_at <= now())
                        OR (j.status = 'running' AND j.lease_until < now())
                      )
                    RETURNING {_qualified_job_columns("j")},
                              m.conversation_id, m.content, m.created_at AS message_created_at
                    """
                ),
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_s": lease_s,
                    "max_attempts": max_attempts,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    values = dict(row)
    job_values = {column.strip(): values[column.strip()] for column in _JOB_COLUMNS.split(",")}
    return MemoryJobSource(
        job=_job(job_values),
        conversation_id=values["conversation_id"],
        content=values["content"],
        message_created_at=values["message_created_at"],
    )


def _qualified_job_columns(alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in _JOB_COLUMNS.split(","))


async def complete_memory_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    operations: list[dict[str, Any]],
) -> bool:
    updated = (
        await session.execute(
            text(
                """
                UPDATE memory_extraction_jobs
                SET status = 'done', operations = CAST(:operations AS jsonb),
                    lease_until = NULL, finished_at = now(), error = NULL
                WHERE id = :id AND status = 'running' AND worker_id = :worker_id
                RETURNING id
                """
            ),
            {
                "id": job_id,
                "worker_id": worker_id,
                "operations": json.dumps(operations, ensure_ascii=False),
            },
        )
    ).scalar_one_or_none()
    return updated is not None


async def retry_or_fail_memory_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    error: str,
    max_attempts: int,
    retry_delay_s: int = 30,
) -> MemoryJobStatus | None:
    if max_attempts <= 0 or retry_delay_s < 0:
        raise ValueError("重试参数不合法")
    status = (
        await session.execute(
            text(
                """
                UPDATE memory_extraction_jobs
                SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
                    available_at = CASE
                        WHEN attempts >= :max_attempts THEN available_at
                        ELSE now() + make_interval(secs => :retry_delay_s)
                    END,
                    lease_until = NULL,
                    worker_id = NULL,
                    finished_at = CASE WHEN attempts >= :max_attempts THEN now() ELSE NULL END,
                    error = :error
                WHERE id = :id AND status = 'running' AND worker_id = :worker_id
                RETURNING status
                """
            ),
            {
                "id": job_id,
                "worker_id": worker_id,
                "error": error[:4000],
                "max_attempts": max_attempts,
                "retry_delay_s": retry_delay_s,
            },
        )
    ).scalar_one_or_none()
    return status


async def list_dispatchable_memory_jobs(
    session: AsyncSession, *, max_attempts: int, limit: int = 50
) -> list[tuple[UUID, int]]:
    if not 1 <= limit <= 500 or max_attempts <= 0:
        raise ValueError("dispatch 参数不合法")
    await session.execute(
        text(
            """
            UPDATE memory_extraction_jobs
            SET status = 'failed', finished_at = now(), lease_until = NULL,
                worker_id = NULL,
                error = COALESCE(error, 'worker 租约过期且已达到最大重试次数')
            WHERE status = 'running' AND lease_until < now() AND attempts >= :max_attempts
            """
        ),
        {"max_attempts": max_attempts},
    )
    rows = (
        await session.execute(
            text(
                """
                SELECT id, attempts FROM memory_extraction_jobs
                WHERE attempts < :max_attempts
                  AND (
                    (status = 'queued' AND available_at <= now())
                    OR (status = 'running' AND lease_until < now())
                  )
                ORDER BY COALESCE(lease_until, available_at), id
                LIMIT :limit
                """
            ),
            {"limit": limit, "max_attempts": max_attempts},
        )
    ).all()
    return [(row.id, row.attempts) for row in rows]
