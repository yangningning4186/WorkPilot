"""Cowork 自动化计划、错过补跑与重叠保护。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.extensions import register_skill_tools
from app.cowork.runtime import initialize_cowork_state
from app.cowork.tools import build_default_cowork_registry
from app.cowork_contracts import (
    ScheduleKind as ScheduleKind,
)
from app.cowork_contracts import (
    ScheduleRecord as ScheduleRecord,
)
from app.cowork_contracts import (
    ScheduleView as ScheduleView,
)
from app.cowork_store.routing import cowork_store
from app.runstore.runs import (
    append_events,
    append_message,
    create_run,
    finish_run,
)

RunTrigger = Literal["manual", "schedule", "catchup"]
_ACTIVE_STATUSES = "('queued','executing','waiting_human','sleeping')"


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
    store = cowork_store()
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


async def get_schedule(
    session: AsyncSession, *, schedule_id: UUID, for_update: bool = False
) -> ScheduleRecord | None:
    store = cowork_store()
    return await store.get_schedule(schedule_id=schedule_id)


async def list_schedules(session: AsyncSession, *, limit: int = 100) -> list[ScheduleView]:
    store = cowork_store()
    return await store.list_schedules(limit=limit)


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
    store = cowork_store()
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


async def delete_schedule(session: AsyncSession, *, schedule_id: UUID) -> bool:
    current = await get_schedule(session, schedule_id=schedule_id, for_update=True)
    if current is None:
        return False
    store = cowork_store()
    return await store.delete_schedule(schedule_id=schedule_id)


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
    store = cowork_store()
    return await store.conversation_has_active_run(conversation_id=conversation_id)


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
    store = cowork_store()
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
    store = cowork_store()
    await store.update_schedule_fields(
        schedule_id=schedule.id,
        values={
            "last_run_at": datetime.now(UTC),
            "last_run_id": run_id,
            "run_count": schedule.run_count + 1,
        },
    )
    return run_id, runnable


async def claim_due_sleeping_runs(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> list[UUID]:
    """把睡到点的 run 转成 queued 并交给调用方入队。

    状态翻转和取出在同一条语句里完成：tick 每十几秒跑一次，可能与另一个 worker 撞车，
    分两步做会把同一个 run 入队两次，等于同一份 checkpoint 被两个 worker 同时恢复。
    """

    moment = now or datetime.now(UTC)
    store = cowork_store()
    return await store.claim_due_sleeping_runs(now=moment, limit=limit)


async def list_dispatchable_scheduled_runs(
    session: AsyncSession, *, limit: int = 100
) -> list[UUID]:
    store = cowork_store()
    return [
        run.id
        for run in await store.list_queued_runs(limit=limit)
        if run.schedule_id is not None
    ]
