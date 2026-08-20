"""让 Cowork 在统一审批与幂等入口管理 Scheduler。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cowork.permissions import list_session_roots
from app.cowork.schedules import (
    ScheduleRecord,
    ScheduleView,
    create_schedule,
    delete_schedule,
    list_schedules,
    update_schedule,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
)


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListSchedulesArgs(_StrictArgs):
    pass


class CreateScheduleArgs(_StrictArgs):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)
    schedule_kind: Literal["once", "cron"]
    cron_expression: str | None = Field(default=None, max_length=100)
    run_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> CreateScheduleArgs:
        if self.schedule_kind == "cron" and not (self.cron_expression or "").strip():
            raise ValueError("周期计划必须填写 cron_expression")
        if self.schedule_kind == "once" and self.run_at is None:
            raise ValueError("单次计划必须填写 run_at")
        return self


class ManageScheduleArgs(_StrictArgs):
    schedule_id: UUID
    action: Literal["pause", "resume", "delete"]


def _schedule_json(schedule: ScheduleRecord) -> dict[str, object]:
    return {
        "id": str(schedule.id),
        "conversation_id": str(schedule.conversation_id),
        "title": schedule.title,
        "goal": schedule.goal,
        "schedule_kind": schedule.schedule_kind,
        "cron_expression": schedule.cron_expression,
        "run_at": schedule.run_at.isoformat() if schedule.run_at else None,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "last_run_id": str(schedule.last_run_id) if schedule.last_run_id else None,
        "run_count": schedule.run_count,
        "skipped_count": schedule.skipped_count,
        "created_at": schedule.created_at.isoformat(),
        "updated_at": schedule.updated_at.isoformat(),
    }


def _schedule_view_json(view: ScheduleView) -> dict[str, object]:
    return {
        **_schedule_json(view.schedule),
        "last_run_status": view.last_run_status,
        "pending_inbox_count": view.pending_inbox_count,
    }


def register_scheduler_tools(registry: CoworkToolRegistry) -> None:
    async def list_handler(context: CoworkToolContext, _: BaseModel) -> CoworkToolResult:
        values = await list_schedules(context.session)
        return CoworkToolResult(
            output={"schedules": [_schedule_view_json(view) for view in values]}
        )

    async def create_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = CreateScheduleArgs.model_validate(raw.model_dump())
        roots = await list_session_roots(
            context.session,
            conversation_id=context.conversation_id,
        )
        if not roots:
            raise ValueError("创建自动化前必须先给当前会话选择工作目录")
        created = await create_schedule(
            context.session,
            conversation_id=context.conversation_id,
            title=args.title,
            goal=args.goal,
            schedule_kind=args.schedule_kind,
            cron_expression=args.cron_expression,
            run_at=args.run_at,
            timezone=args.timezone,
        )
        return CoworkToolResult(
            output={"schedule": _schedule_json(created), "unattended": True},
            effect_ref=f"schedule:{created.id}",
        )

    async def manage_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = ManageScheduleArgs.model_validate(raw.model_dump())
        if args.action == "delete":
            if not await delete_schedule(context.session, schedule_id=args.schedule_id):
                raise LookupError("计划不存在")
            return CoworkToolResult(
                output={"schedule_id": str(args.schedule_id), "deleted": True},
                effect_ref=f"schedule:{args.schedule_id}:deleted",
            )
        updated = await update_schedule(
            context.session,
            schedule_id=args.schedule_id,
            changes={"enabled": args.action == "resume"},
        )
        return CoworkToolResult(
            output={"schedule": _schedule_json(updated)},
            effect_ref=f"schedule:{updated.id}:{args.action}",
        )

    registry.register(
        CoworkToolSpec(
            name="list_schedules",
            description="列出本机 Scheduler 计划、最近运行状态和待处理收件箱数量。只读。",
            args_model=ListSchedulesArgs,
            capability="filesystem.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=list_handler,
        )
    )
    for name, description, args_model, handler in (
        (
            "create_schedule",
            "创建单次或五段 cron 无人值守计划；会复用当前会话目录和能力，执行前必须批准。",
            CreateScheduleArgs,
            create_handler,
        ),
        (
            "manage_schedule",
            "暂停、恢复或删除无人值守计划；执行前必须批准。",
            ManageScheduleArgs,
            manage_handler,
        ),
    ):
        registry.register(
            CoworkToolSpec(
                name=name,
                description=description,
                args_model=args_model,
                capability="external.action",
                risk="external",
                effect="external",
                parallel_safe=False,
                handler=handler,
                approval_required=True,
            )
        )
