"""Cowork 的 out-of-band steering 与运行中人工交互。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork.approvals import create_approval_rule
from app.cowork.permissions import (
    AccessMode,
    Capability,
    create_session_root,
    grant_capability,
)
from app.cowork_contracts import (
    ApprovalRememberScope as ApprovalRememberScope,
)
from app.cowork_contracts import (
    ApprovalRuleRecord,
    QueuedMessageDelivery,
    SteeringSource,
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
from app.cowork_store.routing import cowork_store

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
    source: SteeringSource = "unknown",
    source_wake_id: UUID | None = None,
) -> SteeringRecord:
    store = cowork_store()
    return await store.enqueue_steering(
        run_id=run_id,
        conversation_id=conversation_id,
        content=content,
        source=source,
        source_wake_id=source_wake_id,
    )


async def enqueue_queued_message(
    session: AsyncSession,
    *,
    run_id: UUID,
    conversation_id: UUID,
    content: str,
    source: SteeringSource,
    delivery: QueuedMessageDelivery,
    source_wake_id: UUID | None = None,
) -> SteeringRecord:
    del session
    return await cowork_store().enqueue_queued_message(
        run_id=run_id,
        conversation_id=conversation_id,
        content=content,
        source=source,
        delivery=delivery,
        source_wake_id=source_wake_id,
    )


async def consume_pending_steering(session: AsyncSession, *, run_id: UUID) -> list[SteeringRecord]:
    """锁住并消费当前已排队 steering；调用方必须和 checkpoint 一起提交。"""

    store = cowork_store()
    return await store.consume_pending_steering(run_id=run_id)


async def claim_follow_up_or_seal(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
) -> list[SteeringRecord]:
    del session
    return await cowork_store().claim_follow_up_or_seal(run_id=run_id, worker_id=worker_id)


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
    store = cowork_store()
    return await store.create_inbox_item(
        run_id=run_id,
        conversation_id=conversation_id,
        kind=kind,
        tool_call_id=tool_call_id,
        plan_step_id=plan_step_id,
        request=request,
    )


async def list_unattended_inbox(
    session: AsyncSession,
    *,
    include_resolved: bool = False,
    limit: int = 100,
) -> list[UnattendedInboxRecord]:
    store = cowork_store()
    return await store.list_unattended_inbox(include_resolved=include_resolved, limit=limit)


async def get_pending_inbox_item(
    session: AsyncSession,
    *,
    run_id: UUID,
    resume_token: UUID,
    for_update: bool = False,
) -> InboxRecord | None:
    store = cowork_store()
    return await store.get_inbox_item(run_id=run_id, resume_token=resume_token)


async def resolve_inbox_item(
    session: AsyncSession,
    *,
    item: InboxRecord,
    approved: bool,
    answer: str | None = None,
    path: str | None = None,
    remember: ApprovalRememberScope = "once",
) -> tuple[InboxRecord, dict[str, Any]]:
    """应用用户答复或授权，并产出要写入 canonical tool history 的结果。

    `remember` 不是 `once` 时，会顺带落一条常驻审批规则。规则**只能从这条 inbox 已经
    记下的请求里派生**——那份 payload 正是用户在卡片上看到的内容。若改成事后从模型输入
    重算，用户点的和最终生效的就可能不是同一条规则。
    """

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
            resource_scope_raw = item.request.get("resource_scope")
            resource_scope = resource_scope_raw if isinstance(resource_scope_raw, str) else None
            grant = await grant_capability(
                session,
                conversation_id=item.conversation_id,
                capability=cast("Capability", capability),
                session_root_id=root_id,
                resource_scope=resource_scope,
                grant_source="user",
            )
            response.update(
                {
                    "grant_id": str(grant.id),
                    "capability": grant.capability,
                    "resource_scope": grant.resource_scope,
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
        if approved and remember != "once":
            rule = await _remember_approval(session, item=item, remember=remember)
            response["standing_rule_id"] = str(rule.id)
            response["standing_rule"] = {
                "tool": rule.tool,
                "match_kind": rule.match_kind,
                "target": rule.target,
            }

    response["status"] = status
    store = cowork_store()
    updated = await store.update_inbox_item(item_id=item.id, status=status, response=response)
    if updated is None:
        raise ValueError("这条运行中请求已经处理")
    return updated, response


async def _remember_approval(
    session: AsyncSession,
    *,
    item: InboxRecord,
    remember: ApprovalRememberScope,
) -> ApprovalRuleRecord:
    """把这次批准固化成一条常驻规则。

    只认 shell 与外部动作两种 inbox：目录、能力、提问、计划批准各自有自己的持久化
    形态（session_roots / capability_grants / 计划状态），再叠一层规则只会出现两处
    真相。
    """

    if item.kind not in {"shell_approval", "external_approval"}:
        raise ValueError("这类请求不支持常驻授权")
    if item.request.get("human_only") is True:
        raise ValueError("这项受保护动作只能逐次人工批准，不能创建常驻授权")
    if remember == "command":
        pattern = item.request.get("standing_argv_pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("这条命令不能常驻授权：带 shell 操作符的命令只能逐次批准")
        return await create_approval_rule(
            session,
            conversation_id=item.conversation_id,
            tool="run_shell",
            match_kind="argv_pattern",
            target=pattern,
        )
    tool = item.request.get("tool") if item.kind == "external_approval" else "run_shell"
    if not isinstance(tool, str) or not tool:
        raise ValueError("这条请求没有记录工具名，无法常驻授权")
    target = item.request.get("standing_action_target")
    if not isinstance(target, str) or not target:
        raise ValueError("这只工具没有声明可复用的 action + target，只能逐次批准")
    return await create_approval_rule(
        session,
        conversation_id=item.conversation_id,
        tool=tool,
        match_kind="action_target",
        target=target,
    )


async def cancel_pending_interaction(session: AsyncSession, *, run_id: UUID) -> None:
    store = cowork_store()
    await store.cancel_pending_interaction(run_id=run_id)
    return
