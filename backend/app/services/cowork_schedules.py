"""Cowork 自动化计划、错过补跑与重叠保护。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.agent.cowork_extensions import register_skill_tools
from app.agent.cowork_runtime import initialize_cowork_state
from app.agent.cowork_tools import build_default_cowork_registry
from app.core.config import Settings
from app.cowork_contracts import (
    ScheduleKind as ScheduleKind,
)
from app.cowork_contracts import (
    ScheduleRecord as ScheduleRecord,
)
from app.cowork_contracts import (
    ScheduleView as ScheduleView,
)
from app.cowork_store.routing import configured_cowork_store
from app.services.runs import (
    append_events,
    append_message,
    create_run,
    finish_run,
)

RunTrigger = Literal["manual", "schedule", "catchup"]
_ACTIVE_STATUSES = "('queued','executing','waiting_human')"


class ScheduleError(ValueError):
    pass


class ScheduleNotFoundError(LookupError):
    pass


class ScheduleOverlapError(RuntimeError):
    pass


_COLUMNS = """
    id, conversation_id, title, goal, schedule_kind, cron_expression, run_at,
    timezone, enabled, next_run_at, last_run_at, last_run_id, run_count,
    skipped_count, created_at, updated_at
"""


def _record(row: Any) -> ScheduleRecord:
    return ScheduleRecord(**row)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"未知时区: {name}") from error


def compute_next_run(
    *,
    schedule_kind: ScheduleKind,
    cron_expression: str | None,
    run_at: datetime | None,
    timezone: str,
    after: datetime,
) -> datetime:
    zone = _timezone(timezone)
    reference = after.astimezone(UTC)
    if schedule_kind == "once":
        if run_at is None:
            raise ScheduleError("单次计划缺少 run_at")
        if run_at.tzinfo is None:
            raise ScheduleError("run_at 必须包含时区")
        normalized = run_at.astimezone(UTC)
        if normalized <= reference:
            raise ScheduleError("单次计划时间必须晚于当前时间")
        return normalized
    expression = (cron_expression or "").strip()
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ScheduleError("cron_expression 必须是有效的五段 cron")
    local_reference = reference.astimezone(zone)
    value = croniter(expression, local_reference).get_next(datetime)
    if not isinstance(value, datetime):  # pragma: no cover - croniter 类型约定
        raise ScheduleError("无法计算下一次运行时间")
    if value.tzinfo is None:  # pragma: no cover - aware base 应返回 aware datetime
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


async def create_schedule(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    title: str,
    goal: str,
    schedule_kind: ScheduleKind,
    cron_expression: str | None,
    run_at: datetime | None,
    timezone: str,
    now: datetime | None = None,
) -> ScheduleRecord:
    normalized_title = title.strip()
    normalized_goal = goal.strip()
    if not normalized_title or not normalized_goal:
        raise ScheduleError("计划标题和任务不能为空")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    next_run_at = compute_next_run(
        schedule_kind=schedule_kind,
        cron_expression=cron_expression,
        run_at=run_at,
        timezone=timezone,
        after=current,
    )
    store = configured_cowork_store()
    if store is not None:
        return await store.create_schedule(
            conversation_id=conversation_id,
            title=normalized_title,
            goal=normalized_goal,
            schedule_kind=schedule_kind,
            cron_expression=cron_expression.strip() if cron_expression else None,
            run_at=run_at,
            timezone=timezone,
            next_run_at=next_run_at,
        )
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO cowork_schedules
                        (id, conversation_id, title, goal, schedule_kind,
                         cron_expression, run_at, timezone, next_run_at)
                    VALUES
                        (:id, :conversation_id, :title, :goal, :schedule_kind,
                         :cron_expression, :run_at, :timezone, :next_run_at)
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "conversation_id": conversation_id,
                    "title": normalized_title,
                    "goal": normalized_goal,
                    "schedule_kind": schedule_kind,
                    "cron_expression": cron_expression.strip() if cron_expression else None,
                    "run_at": run_at.astimezone(UTC) if run_at is not None else None,
                    "timezone": timezone,
                    "next_run_at": next_run_at,
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def get_schedule(
    session: AsyncSession, *, schedule_id: UUID, for_update: bool = False
) -> ScheduleRecord | None:
    store = configured_cowork_store()
    if store is not None:
        return await store.get_schedule(schedule_id=schedule_id)
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT schedules.{", schedules.".join(item.strip() for item in _COLUMNS.split(","))}
                    FROM cowork_schedules AS schedules
                    JOIN conversations AS conversations ON conversations.id = schedules.conversation_id
                    WHERE schedules.id = :schedule_id
                      AND conversations.scope = 'local_owner'
                      AND conversations.demo_session_id IS NULL
                    {suffix}
                    """
                ),
                {"schedule_id": schedule_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _record(row)


async def list_schedules(session: AsyncSession, *, limit: int = 100) -> list[ScheduleView]:
    store = configured_cowork_store()
    if store is not None:
        return await store.list_schedules(limit=limit)
    if not 1 <= limit <= 200:
        raise ScheduleError("schedule limit 必须位于 1 到 200")
    columns = ", ".join(f"schedules.{item.strip()}" for item in _COLUMNS.split(","))
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {columns}, runs.status AS last_run_status,
                           workspace.label AS workspace_label,
                           workspace.canonical_path AS workspace_path,
                           COUNT(inbox.id) FILTER (WHERE inbox.status = 'pending') AS pending_inbox_count
                    FROM cowork_schedules AS schedules
                    JOIN conversations AS conversations ON conversations.id = schedules.conversation_id
                    LEFT JOIN agent_runs AS runs ON runs.id = schedules.last_run_id
                    LEFT JOIN cowork_inbox_items AS inbox
                      ON inbox.run_id = runs.id AND inbox.unattended = true
                    LEFT JOIN LATERAL (
                        SELECT roots.label, roots.canonical_path
                        FROM session_roots AS roots
                        WHERE roots.conversation_id = schedules.conversation_id
                          AND roots.enabled = true
                        ORDER BY roots.created_at, roots.id
                        LIMIT 1
                    ) AS workspace ON true
                    WHERE conversations.scope = 'local_owner'
                      AND conversations.demo_session_id IS NULL
                    GROUP BY schedules.id, runs.status,
                             workspace.label, workspace.canonical_path
                    ORDER BY schedules.enabled DESC, schedules.next_run_at NULLS LAST,
                             schedules.created_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [
        ScheduleView(
            schedule=_record({key: row[key] for key in ScheduleRecord.__dataclass_fields__}),
            last_run_status=row["last_run_status"],
            pending_inbox_count=int(row["pending_inbox_count"]),
            workspace_label=row["workspace_label"],
            workspace_path=row["workspace_path"],
        )
        for row in rows
    ]


async def update_schedule(
    session: AsyncSession,
    *,
    schedule_id: UUID,
    changes: dict[str, Any],
    now: datetime | None = None,
) -> ScheduleRecord:
    current = await get_schedule(session, schedule_id=schedule_id, for_update=True)
    if current is None:
        raise ScheduleNotFoundError(str(schedule_id))
    for field in ("title", "goal", "enabled", "timezone"):
        if field in changes and changes[field] is None:
            raise ScheduleError(f"{field} 不能为 null")
    if current.schedule_kind == "once" and changes.get("cron_expression") is not None:
        raise ScheduleError("单次计划不能设置 cron_expression")
    if current.schedule_kind == "cron" and changes.get("run_at") is not None:
        raise ScheduleError("周期计划不能设置 run_at")
    title = str(changes.get("title", current.title)).strip()
    goal = str(changes.get("goal", current.goal)).strip()
    timezone = str(changes.get("timezone", current.timezone)).strip()
    enabled = bool(changes.get("enabled", current.enabled))
    cron_expression = changes.get("cron_expression", current.cron_expression)
    run_at = changes.get("run_at", current.run_at)
    if not title or not goal:
        raise ScheduleError("计划标题和任务不能为空")
    schedule_changed = any(key in changes for key in {"cron_expression", "run_at", "timezone"})
    reenabled = enabled and not current.enabled
    recomputed_next: datetime | None = None
    if schedule_changed or reenabled:
        # 暂停状态也要立即校验新表达式/时区，不能把坏配置存进 DB 等启用时才报错。
        recomputed_next = compute_next_run(
            schedule_kind=current.schedule_kind,
            cron_expression=cast("str | None", cron_expression),
            run_at=cast("datetime | None", run_at),
            timezone=timezone,
            after=now or datetime.now(UTC),
        )
    next_run_at: datetime | None
    if enabled and (schedule_changed or reenabled):
        next_run_at = recomputed_next
    elif enabled:
        next_run_at = current.next_run_at
    else:
        next_run_at = None
    store = configured_cowork_store()
    if store is not None:
        updated = await store.update_schedule_fields(
            schedule_id=schedule_id,
            values={
                "title": title,
                "goal": goal,
                "enabled": enabled,
                "cron_expression": cron_expression,
                "run_at": run_at,
                "timezone": timezone,
                "next_run_at": next_run_at,
            },
        )
        if updated is None:
            raise ScheduleNotFoundError(str(schedule_id))
        return updated
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE cowork_schedules
                    SET title = :title, goal = :goal, enabled = :enabled,
                        cron_expression = :cron_expression, run_at = :run_at,
                        timezone = :timezone, next_run_at = :next_run_at, updated_at = now()
                    WHERE id = :schedule_id
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "schedule_id": schedule_id,
                    "title": title,
                    "goal": goal,
                    "enabled": enabled,
                    "cron_expression": cron_expression,
                    "run_at": run_at,
                    "timezone": timezone,
                    "next_run_at": next_run_at,
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def delete_schedule(session: AsyncSession, *, schedule_id: UUID) -> bool:
    current = await get_schedule(session, schedule_id=schedule_id, for_update=True)
    if current is None:
        return False
    store = configured_cowork_store()
    if store is not None:
        return await store.delete_schedule(schedule_id=schedule_id)
    deleted = (
        await session.execute(
            text("DELETE FROM cowork_schedules WHERE id = :schedule_id RETURNING id"),
            {"schedule_id": schedule_id},
        )
    ).scalar_one_or_none()
    return deleted is not None


def _next_after_fire(schedule: ScheduleRecord, *, now: datetime) -> datetime | None:
    if schedule.schedule_kind == "once":
        return None
    return compute_next_run(
        schedule_kind="cron",
        cron_expression=schedule.cron_expression,
        run_at=None,
        timezone=schedule.timezone,
        after=now,
    )


async def _conversation_has_active_run(session: AsyncSession, *, conversation_id: UUID) -> bool:
    store = configured_cowork_store()
    if store is not None:
        return await store.conversation_has_active_run(conversation_id=conversation_id)
    active = (
        await session.execute(
            text(
                f"""
                SELECT id FROM agent_runs
                WHERE conversation_id = :conversation_id
                  AND status IN {_ACTIVE_STATUSES}
                LIMIT 1
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one_or_none()
    return active is not None


async def _create_schedule_run(
    session: AsyncSession,
    *,
    schedule: ScheduleRecord,
    settings: Settings,
    trigger: RunTrigger,
) -> tuple[UUID, bool]:
    run = await create_run(
        session,
        conversation_id=schedule.conversation_id,
        goal=schedule.goal,
        budget_tokens=settings.run_budget_tokens,
        budget_calls=settings.run_budget_calls,
        budget_wall_ms=settings.run_budget_wall_ms,
        workflow_type="cowork",
        schedule_id=schedule.id,
        unattended=True,
        run_trigger=trigger,
    )
    await append_message(
        session,
        conversation_id=schedule.conversation_id,
        role="user",
        content=schedule.goal,
        status="completed",
        run_id=run.id,
        trace_id=f"scheduler:{schedule.id}",
    )
    registry = build_default_cowork_registry()
    register_skill_tools(registry, settings)
    try:
        # 计划行锁、run/checkpoint 创建和 next_run_at 推进必须由外层同一事务提交。
        # 若这里提前 commit，会在计划仍显示到期时释放锁，形成重复派发窗口。
        await initialize_cowork_state(session, run_id=run.id, registry=registry, commit=False)
    except ValueError as error:
        message = f"自动化未能启动：{error}"
        await append_events(
            session,
            run_id=run.id,
            events=[
                (
                    "error",
                    {"code": "schedule_start_failed", "retryable": True, "user_message": message},
                )
            ],
        )
        await finish_run(session, run_id=run.id, status="failed", error=message)
        return run.id, False
    return run.id, True


async def dispatch_due_schedules(
    session: AsyncSession,
    *,
    settings: Settings,
    trigger: Literal["schedule", "catchup"],
    now: datetime | None = None,
    limit: int = 20,
) -> list[UUID]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    store = configured_cowork_store()
    if store is not None:
        schedule_ids = await store.claim_due_schedules(
            now_iso=current.isoformat(timespec="microseconds"), limit=limit
        )
        local_queued: list[UUID] = []
        for schedule_id in schedule_ids:
            schedule = await store.get_schedule(schedule_id=schedule_id)
            if schedule is None or not schedule.enabled:
                continue
            next_run = _next_after_fire(schedule, now=current)
            if await store.conversation_has_active_run(conversation_id=schedule.conversation_id):
                await store.update_schedule_fields(
                    schedule_id=schedule.id,
                    values={
                        "next_run_at": next_run,
                        "enabled": schedule.schedule_kind != "once",
                        "skipped_count": schedule.skipped_count + 1,
                    },
                )
                continue
            run_id, runnable = await _create_schedule_run(
                session, schedule=schedule, settings=settings, trigger=trigger
            )
            await store.update_schedule_fields(
                schedule_id=schedule.id,
                values={
                    "next_run_at": next_run,
                    "enabled": schedule.schedule_kind != "once",
                    "last_run_at": current,
                    "last_run_id": run_id,
                    "run_count": schedule.run_count + 1,
                },
            )
            if runnable:
                local_queued.append(run_id)
        return local_queued
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS}
                    FROM cowork_schedules
                    WHERE enabled = true AND next_run_at IS NOT NULL AND next_run_at <= :now
                    ORDER BY next_run_at, id
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"now": current, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    queued: list[UUID] = []
    for row in rows:
        schedule = _record(row)
        next_run = _next_after_fire(schedule, now=current)
        if await _conversation_has_active_run(session, conversation_id=schedule.conversation_id):
            await session.execute(
                text(
                    """
                    UPDATE cowork_schedules
                    SET next_run_at = :next_run_at,
                        enabled = CASE WHEN schedule_kind = 'once' THEN false ELSE enabled END,
                        skipped_count = skipped_count + 1,
                        updated_at = now()
                    WHERE id = :schedule_id
                    """
                ),
                {"schedule_id": schedule.id, "next_run_at": next_run},
            )
            continue
        run_id, runnable = await _create_schedule_run(
            session, schedule=schedule, settings=settings, trigger=trigger
        )
        await session.execute(
            text(
                """
                UPDATE cowork_schedules
                SET next_run_at = :next_run_at,
                    enabled = CASE WHEN schedule_kind = 'once' THEN false ELSE enabled END,
                    last_run_at = :last_run_at,
                    last_run_id = :last_run_id,
                    run_count = run_count + 1,
                    updated_at = now()
                WHERE id = :schedule_id
                """
            ),
            {
                "schedule_id": schedule.id,
                "next_run_at": next_run,
                "last_run_at": current,
                "last_run_id": run_id,
            },
        )
        if runnable:
            queued.append(run_id)
    return queued


async def run_schedule_now(
    session: AsyncSession,
    *,
    schedule_id: UUID,
    settings: Settings,
) -> tuple[UUID, bool]:
    schedule = await get_schedule(session, schedule_id=schedule_id, for_update=True)
    if schedule is None:
        raise ScheduleNotFoundError(str(schedule_id))
    if await _conversation_has_active_run(session, conversation_id=schedule.conversation_id):
        raise ScheduleOverlapError("这个工作区已有任务正在运行或等待处理")
    run_id, runnable = await _create_schedule_run(
        session, schedule=schedule, settings=settings, trigger="manual"
    )
    store = configured_cowork_store()
    if store is not None:
        await store.update_schedule_fields(
            schedule_id=schedule.id,
            values={
                "last_run_at": datetime.now(UTC),
                "last_run_id": run_id,
                "run_count": schedule.run_count + 1,
            },
        )
        return run_id, runnable
    await session.execute(
        text(
            """
            UPDATE cowork_schedules
            SET last_run_at = now(), last_run_id = :run_id,
                run_count = run_count + 1, updated_at = now()
            WHERE id = :schedule_id
            """
        ),
        {"schedule_id": schedule.id, "run_id": run_id},
    )
    return run_id, runnable


async def list_dispatchable_scheduled_runs(
    session: AsyncSession, *, limit: int = 100
) -> list[UUID]:
    store = configured_cowork_store()
    if store is not None:
        return [
            run.id
            for run in await store.list_queued_runs(limit=limit)
            if run.schedule_id is not None
        ]
    rows = (
        await session.execute(
            text(
                """
                SELECT id FROM agent_runs
                WHERE schedule_id IS NOT NULL
                  AND status = 'queued'
                  AND worker_id IS NULL
                ORDER BY created_at, id
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).scalars()
    return [UUID(str(value)) for value in rows]
