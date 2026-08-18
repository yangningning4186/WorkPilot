"""Cowork 的 out-of-band steering 与运行中人工交互。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.services.cowork_permissions import (
    AccessMode,
    Capability,
    create_session_root,
    grant_capability,
)

InteractionKind = Literal[
    "ask_user", "directory_request", "capability_request", "shell_approval"
]
InteractionStatus = Literal["pending", "answered", "approved", "rejected", "cancelled"]


@dataclass(frozen=True)
class SteeringRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    content: str
    status: str
    created_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class InboxRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    kind: InteractionKind
    status: InteractionStatus
    resume_token: UUID
    tool_call_id: str
    plan_step_id: UUID
    request: dict[str, Any]
    response: dict[str, Any] | None
    created_at: datetime
    responded_at: datetime | None


_STEERING_COLUMNS = """
    id, run_id, conversation_id, content, status, created_at, consumed_at
"""
_INBOX_COLUMNS = """
    id, run_id, conversation_id, kind, status, resume_token, tool_call_id,
    plan_step_id, request, response, created_at, responded_at
"""


def _inbox_record(row: Any) -> InboxRecord:
    return InboxRecord(**row)


async def enqueue_steering(
    session: AsyncSession,
    *,
    run_id: UUID,
    conversation_id: UUID,
    content: str,
) -> SteeringRecord:
    normalized = content.strip()
    if not 1 <= len(normalized) <= 4000:
        raise ValueError("steering 内容长度必须位于 1 到 4000")
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO cowork_steering_messages
                        (id, run_id, conversation_id, content)
                    VALUES (:id, :run_id, :conversation_id, :content)
                    RETURNING {_STEERING_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "content": normalized,
                },
            )
        )
        .mappings()
        .one()
    )
    return SteeringRecord(**row)


async def consume_pending_steering(
    session: AsyncSession, *, run_id: UUID
) -> list[SteeringRecord]:
    """锁住并消费当前已排队 steering；调用方必须和 checkpoint 一起提交。"""

    rows = (
        (
            await session.execute(
                text(
                    """
                    WITH selected AS (
                        SELECT id
                        FROM cowork_steering_messages
                        WHERE run_id = :run_id AND status = 'pending'
                        ORDER BY created_at, id
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE cowork_steering_messages AS steering
                    SET status = 'consumed', consumed_at = now()
                    FROM selected
                    WHERE steering.id = selected.id
                    RETURNING steering.id, steering.run_id, steering.conversation_id,
                              steering.content, steering.status, steering.created_at,
                              steering.consumed_at
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    return sorted((SteeringRecord(**row) for row in rows), key=lambda item: (item.created_at, item.id))


async def create_inbox_item(
    session: AsyncSession,
    *,
    run_id: UUID,
    conversation_id: UUID,
    kind: InteractionKind,
    tool_call_id: str,
    plan_step_id: UUID,
    request: dict[str, Any],
) -> InboxRecord:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO cowork_inbox_items
                        (id, run_id, conversation_id, kind, resume_token, tool_call_id,
                         plan_step_id, request)
                    VALUES
                        (:id, :run_id, :conversation_id, :kind, :resume_token, :tool_call_id,
                         :plan_step_id, CAST(:request AS jsonb))
                    RETURNING {_INBOX_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "resume_token": uuid7(),
                    "tool_call_id": tool_call_id,
                    "plan_step_id": plan_step_id,
                    "request": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                },
            )
        )
        .mappings()
        .one()
    )
    return _inbox_record(row)


async def get_pending_inbox_item(
    session: AsyncSession,
    *,
    run_id: UUID,
    resume_token: UUID,
    for_update: bool = False,
) -> InboxRecord | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_INBOX_COLUMNS}
                    FROM cowork_inbox_items
                    WHERE run_id = :run_id AND resume_token = :resume_token
                    {suffix}
                    """
                ),
                {"run_id": run_id, "resume_token": resume_token},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _inbox_record(row)


async def resolve_inbox_item(
    session: AsyncSession,
    *,
    item: InboxRecord,
    approved: bool,
    answer: str | None = None,
    path: str | None = None,
) -> tuple[InboxRecord, dict[str, Any]]:
    """应用用户答复或授权，并产出要写入 canonical tool history 的结果。"""

    if item.status != "pending":
        raise ValueError("这条运行中请求已经处理")

    response: dict[str, Any] = {"approved": approved}
    status: InteractionStatus
    if item.kind == "ask_user":
        normalized = (answer or "").strip()
        if not normalized:
            raise ValueError("请填写对 Cowork 的答复")
        if len(normalized) > 4000:
            raise ValueError("答复不能超过 4000 个字符")
        response["answer"] = normalized
        status = "answered"
    elif item.kind == "directory_request":
        if approved:
            normalized_path = (path or "").strip()
            if not normalized_path:
                raise ValueError("批准目录请求时必须选择目录")
            access_mode = item.request.get("access_mode", "read_only")
            if access_mode not in {"read_only", "read_write"}:
                raise ValueError("目录请求包含无效 access_mode")
            root = await create_session_root(
                session,
                conversation_id=item.conversation_id,
                requested_path=normalized_path,
                access_mode=cast("AccessMode", access_mode),
            )
            response.update(
                {
                    "root_id": str(root.id),
                    "canonical_path": root.canonical_path,
                    "access_mode": root.access_mode,
                }
            )
            status = "approved"
        else:
            status = "rejected"
    elif item.kind == "capability_request":
        if approved:
            capability = item.request.get("capability")
            if not isinstance(capability, str):
                raise ValueError("能力请求缺少 capability")
            root_id_raw = item.request.get("session_root_id")
            root_id = UUID(root_id_raw) if isinstance(root_id_raw, str) else None
            grant = await grant_capability(
                session,
                conversation_id=item.conversation_id,
                capability=cast("Capability", capability),
                session_root_id=root_id,
                grant_source="user",
            )
            response.update(
                {
                    "grant_id": str(grant.id),
                    "capability": grant.capability,
                    "session_root_id": (
                        str(grant.session_root_id) if grant.session_root_id is not None else None
                    ),
                }
            )
            status = "approved"
        else:
            status = "rejected"
    else:
        status = "approved" if approved else "rejected"
        response["command_sha256"] = item.request.get("command_sha256")

    response["status"] = status
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE cowork_inbox_items
                    SET status = :status, response = CAST(:response AS jsonb), responded_at = now()
                    WHERE id = :id AND status = 'pending'
                    RETURNING {_INBOX_COLUMNS}
                    """
                ),
                {
                    "id": item.id,
                    "status": status,
                    "response": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("这条运行中请求已经处理")
    return _inbox_record(row), response


async def cancel_pending_interaction(session: AsyncSession, *, run_id: UUID) -> None:
    await session.execute(
        text(
            """
            UPDATE cowork_inbox_items
            SET status = 'cancelled',
                response = '{"approved":false,"status":"cancelled"}'::jsonb,
                responded_at = now()
            WHERE run_id = :run_id AND status = 'pending'
            """
        ),
        {"run_id": run_id},
    )
    await session.execute(
        text(
            """
            UPDATE cowork_steering_messages
            SET status = 'cancelled'
            WHERE run_id = :run_id AND status = 'pending'
            """
        ),
        {"run_id": run_id},
    )
