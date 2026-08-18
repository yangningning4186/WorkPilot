"""会话生命周期与按请求身份隔离的消息读取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ConversationRecord:
    id: UUID
    title: str | None
    message_count: int
    latest_message: str | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationMessageRecord:
    id: UUID
    seq: int
    role: str
    content: str
    status: str
    run_id: UUID | None
    citations: list[dict[str, Any]]
    answer_mode: str | None
    created_at: datetime


class ConversationBusyError(RuntimeError):
    """会话仍有非终态任务，不能在 worker 写入期间删除。"""


def _identity_clause() -> str:
    return """
        c.scope = :scope
        AND (
            (:scope = 'local_owner' AND c.demo_session_id IS NULL)
            OR (:scope = 'demo' AND c.demo_session_id = :demo_session_id)
        )
    """


async def list_conversations(
    session: AsyncSession,
    *,
    scope: str,
    demo_session_id: UUID | None,
    limit: int = 100,
) -> list[ConversationRecord]:
    if not 1 <= limit <= 200:
        raise ValueError("conversation limit 必须位于 1 到 200")
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           COUNT(m.id) FILTER (WHERE m.role IN ('user', 'assistant')) AS message_count,
                           (ARRAY_AGG(NULLIF(left(m.content, 160), '') ORDER BY m.seq DESC)
                               FILTER (WHERE m.content <> ''))[1] AS latest_message,
                           MAX(m.created_at) AS last_message_at
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    WHERE {_identity_clause()}
                    GROUP BY c.id
                    ORDER BY COALESCE(MAX(m.created_at), c.updated_at) DESC, c.id DESC
                    LIMIT :limit
                    """
                ),
                {
                    "scope": scope,
                    "demo_session_id": demo_session_id,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    return [ConversationRecord(**dict(row)) for row in rows]


async def get_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
) -> ConversationRecord | None:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           COUNT(m.id) FILTER (WHERE m.role IN ('user', 'assistant')) AS message_count,
                           (ARRAY_AGG(NULLIF(left(m.content, 160), '') ORDER BY m.seq DESC)
                               FILTER (WHERE m.content <> ''))[1] AS latest_message,
                           MAX(m.created_at) AS last_message_at
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    WHERE c.id = :conversation_id AND {_identity_clause()}
                    GROUP BY c.id
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "scope": scope,
                    "demo_session_id": demo_session_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else ConversationRecord(**dict(row))


async def delete_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
) -> bool:
    """删除归属当前身份的会话；数据库外键负责级联消息与运行记录。

    先锁定会话行，避免检查非终态 run 后又并发创建新 run。已经抽取为 owner
    长期记忆的事实是独立数据，不随会话删除；其来源消息外键会置空。
    """

    owned_id = (
        await session.execute(
            text(
                f"""
                SELECT c.id
                FROM conversations c
                WHERE c.id = :conversation_id AND {_identity_clause()}
                FOR UPDATE
                """
            ),
            {
                "conversation_id": conversation_id,
                "scope": scope,
                "demo_session_id": demo_session_id,
            },
        )
    ).scalar_one_or_none()
    if owned_id is None:
        return False

    has_active_run = (
        await session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_runs
                    WHERE conversation_id = :conversation_id
                      AND status NOT IN ('done', 'failed', 'cancelled', 'budget_exceeded')
                )
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one()
    if has_active_run:
        raise ConversationBusyError("会话仍有任务在运行")

    await session.execute(
        text("DELETE FROM conversations WHERE id = :conversation_id"),
        {"conversation_id": conversation_id},
    )
    return True


async def list_conversation_messages(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
    limit: int = 100,
) -> list[ConversationMessageRecord] | None:
    if not 1 <= limit <= 500:
        raise ValueError("message limit 必须位于 1 到 500")
    owns = (
        await session.execute(
            text(
                f"""
                SELECT EXISTS (
                    SELECT 1 FROM conversations c
                    WHERE c.id = :conversation_id AND {_identity_clause()}
                )
                """
            ),
            {
                "conversation_id": conversation_id,
                "scope": scope,
                "demo_session_id": demo_session_id,
            },
        )
    ).scalar_one()
    if not owns:
        return None
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT * FROM (
                        SELECT m.id, m.seq, m.role, m.content, m.status, m.run_id,
                               m.citations, ar.answer_mode, m.created_at
                        FROM messages m
                        LEFT JOIN agent_runs ar ON ar.id = m.run_id
                        WHERE m.conversation_id = :conversation_id
                          AND m.role IN ('user', 'assistant')
                        ORDER BY m.seq DESC
                        LIMIT :limit
                    ) recent
                    ORDER BY seq
                    """
                ),
                {"conversation_id": conversation_id, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [
        ConversationMessageRecord(
            **{
                **dict(row),
                "citations": list(row["citations"] or []),
            }
        )
        for row in rows
    ]
