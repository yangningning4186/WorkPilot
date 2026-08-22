"""让 Cowork 在统一审批与幂等入口管理 Scheduler。"""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cowork.approvals import MAX_COMMAND_PREFIX_WORDS, create_approval_rule
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


class ScheduleStandingApproval(_StrictArgs):
    """计划要长期免审批的一类动作。

    这是无人值守能不能真正跑起来的关键：一条每天七点跑的计划，如果每天早上都停在
    "允许 `npm test` 吗"上，它就不是无人值守。但免审批必须在**创建时**就摊开给用户看，
    而不是让计划在运行中自己积累授权。
    """

    tool: str = Field(min_length=1, max_length=120)
    command_prefix: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "仅 run_shell 使用：要长期放行的 argv 前缀，例如 `npm test`。"
            f"最多 {MAX_COMMAND_PREFIX_WORDS} 个词；命令里出现 shell 操作符时一律不匹配。"
        ),
    )

    @model_validator(mode="after")
    def validate_prefix(self) -> ScheduleStandingApproval:
        if self.command_prefix is None:
            return self
        if self.tool != "run_shell":
            raise ValueError("command_prefix 只能用于 run_shell")
        try:
            words = shlex.split(self.command_prefix)
        except ValueError as error:
            raise ValueError(f"command_prefix 不是合法的命令片段：{error}") from error
        if not words:
            raise ValueError("command_prefix 不能为空")
        if len(words) > MAX_COMMAND_PREFIX_WORDS:
            raise ValueError(
                f"command_prefix 最多 {MAX_COMMAND_PREFIX_WORDS} 个词，"
                "再长就不是一类命令而是一条命令了，那种情况请逐次批准"
            )
        return self


class CreateScheduleArgs(_StrictArgs):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)
    schedule_kind: Literal["once", "cron"]
    cron_expression: str | None = Field(default=None, max_length=100)
    run_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    standing_approvals: list[ScheduleStandingApproval] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "这条计划运行时长期免审批的动作。只在这条计划的运行里生效，"
            "用户手工发起的对话不会继承；删除计划会连同这些授权一起消失。"
        ),
    )

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
        # 规则在这里才落库，不在参数校验时：只有走到这一步，`approval_required`
        # 已经证明用户看过并批准了这份创建请求，其中就包含这批免审批动作。
        rules = [
            await create_approval_rule(
                context.session,
                conversation_id=context.conversation_id,
                tool=item.tool,
                match_kind="command_prefix" if item.command_prefix else "tool",
                target=item.command_prefix,
                schedule_id=created.id,
                created_by="schedule",
            )
            for item in args.standing_approvals
        ]
        return CoworkToolResult(
            output={
                "schedule": _schedule_json(created),
                "unattended": True,
                "standing_approvals": [
                    {
                        "id": str(rule.id),
                        "tool": rule.tool,
                        "match_kind": rule.match_kind,
                        "target": rule.target,
                    }
                    for rule in rules
                ],
            },
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
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=list_handler,
        )
    )
    for name, description, args_model, handler in (
        (
            "create_schedule",
            "创建单次或五段 cron 无人值守计划；会复用当前会话目录和能力，执行前必须批准。"
            "如果这条计划每次都要跑同样的命令，用 standing_approvals 把它们一次性列出来——"
            "否则计划每次运行都会停在审批上，那就不是无人值守了。",
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
