"""Cowork 的 out-of-band steering 与运行中人工交互。"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.cowork.permissions import (
    AccessMode,
    Capability,
    create_session_root,
    grant_capability,
)
from app.cowork_contracts import (
    InboxRecord as InboxRecord,
)
from app.cowork_contracts import (
    InteractionKind as InteractionKind,
)
from app.cowork_contracts import (
    InteractionStatus as InteractionStatus,
)
from app.cowork_contracts import (
    SteeringRecord as SteeringRecord,
)
from app.cowork_contracts import (
    UnattendedInboxRecord as UnattendedInboxRecord,
)
from app.cowork_store.routing import configured_cowork_store

_STEERING_COLUMNS = """
    id, run_id, conversation_id, content, status, created_at, consumed_at
"""
_INBOX_COLUMNS = """
    id, run_id, conversation_id, kind, status, resume_token, tool_call_id,
    plan_step_id, request, response, created_at, responded_at, unattended
"""
_INBOX_COLUMN_NAMES = [column.strip() for column in _INBOX_COLUMNS.split(",")]


def _inbox_record(row: Any) -> InboxRecord:
    return InboxRecord(**row)


async def enqueue_steering(
    session: AsyncSession,
    *,
    run_id: UUID,
    conversation_id: UUID,
    content: str,
) -> SteeringRecord:
    store = configured_cowork_store()
    if store is not None:
        return await store.enqueue_steering(
            run_id=run_id, conversation_id=conversation_id, content=content
        )
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


async def consume_pending_steering(session: AsyncSession, *, run_id: UUID) -> list[SteeringRecord]:
    """锁住并消费当前已排队 steering；调用方必须和 checkpoint 一起提交。"""

    store = configured_cowork_store()
    if store is not None:
        return await store.consume_pending_steering(run_id=run_id)

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
    return sorted(
        (SteeringRecord(**row) for row in rows), key=lambda item: (item.created_at, item.id)
    )


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
    store = configured_cowork_store()
    if store is not None:
        return await store.create_inbox_item(
            run_id=run_id,
            conversation_id=conversation_id,
            kind=kind,
            tool_call_id=tool_call_id,
            plan_step_id=plan_step_id,
            request=request,
        )
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO cowork_inbox_items
                        (id, run_id, conversation_id, kind, resume_token, tool_call_id,
                         plan_step_id, request, unattended)
                    SELECT
                        :id, :run_id, :conversation_id, :kind, :resume_token, :tool_call_id,
                        :plan_step_id, CAST(:request AS jsonb), runs.unattended
                    FROM agent_runs AS runs
                    WHERE runs.id = :run_id
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


async def list_unattended_inbox(
    session: AsyncSession,
    *,
    include_resolved: bool = False,
    limit: int = 100,
) -> list[UnattendedInboxRecord]:
    store = configured_cowork_store()
    if store is not None:
        return await store.list_unattended_inbox(include_resolved=include_resolved, limit=limit)
    if not 1 <= limit <= 200:
        raise ValueError("inbox limit 必须位于 1 到 200")
    status_clause = "" if include_resolved else "AND inbox.status = 'pending'"
    qualified = ", ".join(f"inbox.{column}" for column in _INBOX_COLUMN_NAMES)
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {qualified}, runs.goal AS run_goal, runs.status AS run_status,
                           runs.schedule_id, schedules.title AS schedule_title
                    FROM cowork_inbox_items AS inbox
                    JOIN agent_runs AS runs ON runs.id = inbox.run_id
                    JOIN conversations AS conversations ON conversations.id = inbox.conversation_id
                    LEFT JOIN cowork_schedules AS schedules ON schedules.id = runs.schedule_id
                    WHERE inbox.unattended = true
                      AND conversations.scope = 'local_owner'
                      AND conversations.demo_session_id IS NULL
                      {status_clause}
                    ORDER BY (inbox.status = 'pending') DESC, inbox.created_at DESC, inbox.id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    records: list[UnattendedInboxRecord] = []
    for row in rows:
        item_values = {column: row[column] for column in _INBOX_COLUMN_NAMES}
        records.append(
            UnattendedInboxRecord(
                item=InboxRecord(**item_values),
                run_goal=str(row["run_goal"]),
                run_status=str(row["run_status"]),
                schedule_id=row["schedule_id"],
                schedule_title=row["schedule_title"],
            )
        )
    return records


async def get_pending_inbox_item(
    session: AsyncSession,
    *,
    run_id: UUID,
    resume_token: UUID,
    for_update: bool = False,
) -> InboxRecord | None:
    store = configured_cowork_store()
    if store is not None:
        return await store.get_inbox_item(run_id=run_id, resume_token=resume_token)
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
        if approved:
            normalized = (answer or "").strip()
            if not normalized:
                raise ValueError("请填写对 Cowork 的答复")
            if len(normalized) > 4000:
                raise ValueError("答复不能超过 4000 个字符")
            response["answer"] = normalized
            status = "answered"
        else:
            response["reason"] = "用户选择不回答；请基于现有信息自行判断，不要重复询问同一问题"
            status = "rejected"
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
    elif item.kind == "plan_approval":
        # 退回时用户的意见就是模型下一轮的输入，必须落进 tool 结果；只回一个
        # "被拒绝"会让它原样再提一遍同一个计划。
        feedback = (answer or "").strip()
        if len(feedback) > 4000:
            raise ValueError("修改意见不能超过 4000 个字符")
        if feedback:
            response["feedback"] = feedback
        if approved:
            status = "approved"
        else:
            response.setdefault(
                "feedback", "用户没有批准这个计划，请重新拟定后再提交，不要直接开始执行"
            )
            status = "rejected"
    else:
        status = "approved" if approved else "rejected"
        response["command_sha256"] = item.request.get("command_sha256")

    response["status"] = status
    store = configured_cowork_store()
    if store is not None:
        updated = await store.update_inbox_item(item_id=item.id, status=status, response=response)
        if updated is None:
            raise ValueError("这条运行中请求已经处理")
        return updated, response

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
    store = configured_cowork_store()
    if store is not None:
        await store.cancel_pending_interaction(run_id=run_id)
        return
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
