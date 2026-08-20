"""会话生命周期与按请求身份隔离的消息读取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cowork_contracts import ConversationBusyError as ConversationBusyError
from app.cowork_store.routing import configured_cowork_store


@dataclass(frozen=True)
class ConversationRecord:
    id: UUID
    title: str | None
    message_count: int
    latest_message: str | None
    last_message_at: datetime | None
    provider_profile_id: UUID | None
    provider_name: str | None
    provider: str | None
    selected_model: str | None
    unattended: bool
    archived_at: datetime | None
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
    attachments: list[dict[str, Any]]
    created_at: datetime


def _identity_clause() -> str:
    return """
        c.scope = :scope
        AND (
            (:scope = 'local_owner' AND c.demo_session_id IS NULL)
            OR (:scope = 'demo' AND c.demo_session_id = :demo_session_id)
        )
    """


async def _local_conversation_record(
    session: AsyncSession, row: dict[str, Any]
) -> ConversationRecord:
    from app.cowork_store.factory import local_cowork_stores

    conversation_id = UUID(str(row["id"]))
    messages = await local_cowork_stores().conversations.read(conversation_id)
    visible = [item for item in messages if item.role in {"user", "assistant"}]
    latest = next((item.content[:160] for item in reversed(visible) if item.content), None)
    profile_id = (
        None
        if row["provider_profile_id"] is None
        else UUID(str(row["provider_profile_id"]))
    )
    provider_name: str | None = None
    provider: str | None = None
    selected_model = row["model_override"]
    if profile_id is not None:
        profile = (
            (
                await session.execute(
                    text(
                        """SELECT name, provider, default_model
                           FROM provider_profiles WHERE id = :id"""
                    ),
                    {"id": profile_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile is not None:
            provider_name = str(profile["name"])
            provider = str(profile["provider"])
            selected_model = selected_model or profile["default_model"]
    return ConversationRecord(
        id=conversation_id,
        title=row["title"],
        message_count=len(visible),
        latest_message=latest,
        last_message_at=None if not visible else datetime.fromisoformat(visible[-1].created_at),
        provider_profile_id=profile_id,
        provider_name=provider_name,
        provider=provider,
        selected_model=selected_model,
        unattended=bool(row["unattended"]),
        archived_at=(
            None
            if row.get("archived_at") is None
            else datetime.fromisoformat(str(row["archived_at"]))
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


async def list_conversations(
    session: AsyncSession,
    *,
    scope: str,
    demo_session_id: UUID | None,
    archived: bool = False,
    limit: int = 100,
) -> list[ConversationRecord]:
    if not 1 <= limit <= 200:
        raise ValueError("conversation limit 必须位于 1 到 200")
    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        return [
            await _local_conversation_record(session, row)
            for row in await store.list_conversation_metadata(archived=archived, limit=limit)
        ]
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           c.provider_profile_id, p.name AS provider_name,
                           p.provider, COALESCE(c.model_override, p.default_model) AS selected_model,
                           c.unattended,
                           c.archived_at,
                           COUNT(m.id) FILTER (WHERE m.role IN ('user', 'assistant')) AS message_count,
                           (ARRAY_AGG(NULLIF(left(m.content, 160), '') ORDER BY m.seq DESC)
                               FILTER (WHERE m.content <> ''))[1] AS latest_message,
                           MAX(m.created_at) AS last_message_at
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    LEFT JOIN provider_profiles p ON p.id = c.provider_profile_id
                    WHERE {_identity_clause()}
                      AND c.archived_at IS {"NOT NULL" if archived else "NULL"}
                    GROUP BY c.id, p.id
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
    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        rows = await store.list_conversation_metadata(
            conversation_id=conversation_id,
            archived=None,
            limit=1,
        )
        if not rows:
            return None
        return await _local_conversation_record(session, rows[0])
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.created_at, c.updated_at,
                           c.provider_profile_id, p.name AS provider_name,
                           p.provider, COALESCE(c.model_override, p.default_model) AS selected_model,
                           c.unattended,
                           c.archived_at,
                           COUNT(m.id) FILTER (WHERE m.role IN ('user', 'assistant')) AS message_count,
                           (ARRAY_AGG(NULLIF(left(m.content, 160), '') ORDER BY m.seq DESC)
                               FILTER (WHERE m.content <> ''))[1] AS latest_message,
                           MAX(m.created_at) AS last_message_at
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    LEFT JOIN provider_profiles p ON p.id = c.provider_profile_id
                    WHERE c.id = :conversation_id AND {_identity_clause()}
                    GROUP BY c.id, p.id
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


async def update_conversation_runtime(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
    provider_profile_id: UUID | None,
    model_override: str | None,
    unattended: bool,
) -> ConversationRecord | None:
    """更新会话运行时选择；demo 身份不能启用无人值守。"""

    if scope != "local_owner" and unattended:
        raise ValueError("演示会话不能启用无人值守")
    if provider_profile_id is not None:
        exists = (
            await session.execute(
                text("SELECT enabled FROM provider_profiles WHERE id = :id"),
                {"id": provider_profile_id},
            )
        ).scalar_one_or_none()
        if exists is None:
            raise LookupError("Provider 不存在")
        if not exists:
            raise ValueError("Provider 已停用")
    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        changed = await store.update_conversation_runtime(
            conversation_id=conversation_id,
            provider_profile_id=provider_profile_id,
            model_override=model_override.strip() if model_override else None,
            unattended=unattended,
        )
        if not changed:
            return None
        return await get_conversation(
            session,
            conversation_id=conversation_id,
            scope=scope,
            demo_session_id=demo_session_id,
        )
    result = await session.execute(
        text(
            f"""
            UPDATE conversations c
            SET provider_profile_id = :provider_profile_id,
                model_override = :model_override,
                unattended = :unattended
            WHERE c.id = :conversation_id AND {_identity_clause()}
            RETURNING c.id
            """
        ),
        {
            "conversation_id": conversation_id,
            "scope": scope,
            "demo_session_id": demo_session_id,
            "provider_profile_id": provider_profile_id,
            "model_override": model_override.strip() if model_override else None,
            "unattended": unattended,
        },
    )
    if result.scalar_one_or_none() is None:
        return None
    return await get_conversation(
        session,
        conversation_id=conversation_id,
        scope=scope,
        demo_session_id=demo_session_id,
    )


async def set_conversation_archived(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
    archived: bool,
) -> ConversationRecord | None:
    """归档或恢复会话；运行中的会话不能从列表隐藏。"""

    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        changed = await store.set_conversation_archived(
            conversation_id=conversation_id, archived=archived
        )
        if not changed:
            return None
        return await get_conversation(
            session,
            conversation_id=conversation_id,
            scope=scope,
            demo_session_id=demo_session_id,
        )

    owned_id = (
        await session.execute(
            text(
                f"""SELECT c.id FROM conversations c
                    WHERE c.id = :conversation_id AND {_identity_clause()}
                    FOR UPDATE"""
            ),
            {
                "conversation_id": conversation_id,
                "scope": scope,
                "demo_session_id": demo_session_id,
            },
        )
    ).scalar_one_or_none()
    if owned_id is None:
        return None
    has_active_run = (
        await session.execute(
            text(
                """SELECT EXISTS (
                       SELECT 1 FROM agent_runs
                       WHERE conversation_id = :conversation_id
                         AND status NOT IN ('done','failed','cancelled','budget_exceeded')
                   )"""
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one()
    if has_active_run:
        raise ConversationBusyError("会话仍有任务在运行")
    await session.execute(
        text(
            """UPDATE conversations
               SET archived_at = CASE WHEN :archived THEN now() ELSE NULL END
               WHERE id = :conversation_id"""
        ),
        {"conversation_id": conversation_id, "archived": archived},
    )
    return await get_conversation(
        session,
        conversation_id=conversation_id,
        scope=scope,
        demo_session_id=demo_session_id,
    )


async def delete_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
) -> bool:
    """删除归属当前身份的会话；数据库外键负责级联消息与运行记录。

    等待用户、尚未领取及租约已过期的 run 不会再安全写入，可以随会话一并
    删除；只有仍持有效租约的 worker 会阻止操作。已经抽取为 owner 长期记忆的
    事实是独立数据，不随会话删除；其来源消息外键会置空。
    """

    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        deleted = await store.delete_conversation(conversation_id=conversation_id)
        if deleted:
            from app.cowork_store.factory import local_cowork_stores

            await local_cowork_stores().conversations.delete(conversation_id)
        return deleted

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

    has_leased_worker = (
        await session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_runs
                    WHERE conversation_id = :conversation_id
                      AND status NOT IN ('done', 'failed', 'cancelled', 'budget_exceeded')
                      AND worker_id IS NOT NULL
                      AND lease_until IS NOT NULL
                      AND lease_until > now()
                )
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one()
    if has_leased_worker:
        raise ConversationBusyError("会话任务正在执行")

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
    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        if not await store.conversation_exists(conversation_id):
            return None
        from app.cowork_store.factory import local_cowork_stores

        messages = await local_cowork_stores().conversations.read(conversation_id)
        output: list[ConversationMessageRecord] = []
        for item in [value for value in messages if value.role in {"user", "assistant"}][-limit:]:
            run = None if item.run_id is None else await store.get_run(item.run_id)
            output.append(
                ConversationMessageRecord(
                    id=item.record_id,
                    seq=item.seq,
                    role=item.role,
                    content=item.content,
                    status=item.status,
                    run_id=item.run_id,
                    citations=list(item.citations),
                    answer_mode=None if run is None else run.answer_mode,
                    attachments=list(item.attachments),
                    created_at=datetime.fromisoformat(item.created_at),
                )
            )
        return output
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
                               m.citations, ar.answer_mode, m.created_at,
                               COALESCE(
                                   (
                                       SELECT jsonb_agg(
                                           jsonb_build_object(
                                               'id', a.id,
                                               'conversation_id', a.conversation_id,
                                               'message_id', a.message_id,
                                               'run_id', a.run_id,
                                               'kind', a.kind,
                                               'filename', a.filename,
                                               'media_type', a.media_type,
                                               'size_bytes', a.size_bytes,
                                               'sha256', a.sha256
                                           ) ORDER BY a.created_at, a.id
                                       )
                                       FROM cowork_attachments a
                                       WHERE a.message_id = m.id
                                   ),
                                   '[]'::jsonb
                               ) AS attachments
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
                "attachments": list(row["attachments"] or []),
            }
        )
        for row in rows
    ]
