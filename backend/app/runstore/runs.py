"""run 生命周期与事件溯源。

`run_events` 是流式输出的唯一真相源(ADR-0007): 实时推送和刷新回放读同一份数据,
因此不存在"实时看到的和刷新后看到的不一致"。普通问答同样走 run, 让刷新恢复、
断线续传、时间线渲染只有一套实现。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

from app.agent_core.contracts import (
    TERMINAL_RUN_STATUSES as TERMINAL_RUN_STATUSES,
)
from app.agent_core.contracts import (
    RunEvent as RunEvent,
)
from app.agent_core.contracts import (
    RunRecord as RunRecord,
)
from app.agent_core.contracts import (
    WorkflowType as WorkflowType,
)
from app.agent_core.errors import RunNotFoundError as RunNotFoundError
from app.core.db import DbSession as AsyncSession
from app.cowork_store.jsonl import JsonlMessage
from app.cowork_store.routing import cowork_store
from app.run_events import RunEventDraft

# 内联进 SQL 的常量白名单, 不接受外部输入。
_TERMINAL_SQL = "(" + ", ".join(f"'{status}'" for status in sorted(TERMINAL_RUN_STATUSES)) + ")"
MESSAGE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class ScheduledRunRetry:
    attempt: int
    wake_at: datetime


_RUN_COLUMNS = """
    id, conversation_id, goal, status, worker_id, lease_until, cancel_requested_at,
    budget_tokens, budget_calls, budget_wall_ms, used_tokens, used_calls, next_seq, error,
    answer_mode, workflow_type, schedule_id, unattended, run_trigger, retrieval_top_k
"""
_RUN_COLUMNS_QUALIFIED = ", ".join(f"ar.{column.strip()}" for column in _RUN_COLUMNS.split(","))


async def ensure_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID | None = None,
    title: str | None = None,
) -> UUID:
    """复用已有对话或新建一个。"""

    store = cowork_store()
    if conversation_id is not None:
        active = await store.list_conversation_metadata(
            conversation_id=conversation_id, archived=False, limit=1
        )
        if not active:
            if await store.conversation_exists(conversation_id):
                raise LookupError("对话已归档，请先恢复")
            raise LookupError(f"对话不存在: {conversation_id}")
        return conversation_id
    return await store.create_conversation(title=title)


async def create_run(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    goal: str,
    budget_tokens: int,
    budget_calls: int,
    budget_wall_ms: int,
    answer_mode: str = "grounded",
    retrieval_top_k: int = 5,
    workflow_type: WorkflowType = "cowork",
    schedule_id: UUID | None = None,
    unattended: bool = False,
    run_trigger: Literal["manual", "schedule", "catchup"] = "manual",
    initializing: bool = False,
    source_wake_id: UUID | None = None,
) -> RunRecord:
    if not goal.strip():
        raise ValueError("run 目标不能为空")
    if answer_mode not in {"grounded", "general"}:
        raise ValueError("answer_mode 只能是 grounded 或 general")
    if workflow_type != "cowork":
        # answer / literature_review 已退役。库里仍有历史行，读得出来，但不再新建。
        raise ValueError("workflow_type 只能是 cowork（answer / literature_review 已退役）")
    if not 1 <= retrieval_top_k <= 20:
        raise ValueError("retrieval_top_k 必须位于 1 到 20")
    if run_trigger not in {"manual", "schedule", "catchup"}:
        raise ValueError("run_trigger 无效")
    if schedule_id is not None and workflow_type != "cowork":
        raise ValueError("只有 Cowork run 可以关联 schedule")
    store = cowork_store()
    return await store.create_run(
        conversation_id=conversation_id,
        goal=goal,
        budget_tokens=budget_tokens,
        budget_calls=budget_calls,
        budget_wall_ms=budget_wall_ms,
        answer_mode=answer_mode,  # type: ignore[arg-type]
        retrieval_top_k=retrieval_top_k,
        workflow_type=workflow_type,
        schedule_id=schedule_id,
        unattended=unattended,
        run_trigger=run_trigger,
        initializing=initializing,
        source_wake_id=source_wake_id,
    )


async def get_run(session: AsyncSession, run_id: UUID) -> RunRecord | None:
    return await cowork_store().get_run(run_id)


async def get_run_for_identity(
    session: AsyncSession,
    *,
    run_id: UUID,
) -> RunRecord | None:
    """读取 run。身份只剩 owner 一种，隔离不再需要额外条件。"""

    return await cowork_store().get_run(run_id)


async def append_events(
    session: AsyncSession,
    *,
    run_id: UUID,
    events: Sequence[RunEventDraft],
) -> list[RunEvent]:
    """原子发号并落库。

    发号走 `UPDATE ... RETURNING` 而不是 `MAX(seq)+1`: 后者在并发下会发出重复号,
    而 seq 连续正是断线续传的前提。单 run 由单 worker 处理, 这里不会有竞争,
    但 watchdog 也会写事件, 所以仍然按并发安全写。
    """

    if not events:
        return []
    store = cowork_store()
    return await store.append_events(run_id=run_id, events=events)


async def list_events(
    session: AsyncSession,
    *,
    run_id: UUID,
    after_seq: int = 0,
    limit: int = 200,
) -> list[RunEvent]:
    if after_seq < 0:
        raise ValueError("after_seq 不能为负数")
    if not 1 <= limit <= 1000:
        raise ValueError("limit 必须位于 1 到 1000")
    store = cowork_store()
    return await store.list_events(run_id=run_id, after_seq=after_seq, limit=limit)


async def claim_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
    lease_s: int,
) -> RunRecord | None:
    """抢占 run 并取得租约; 抢不到返回 None。

    条件 UPDATE 而不是"先读后写": 队列重投递时两个 worker 会同时看到 queued,
    先读后写必然双跑, 于是同一次回答被计费两遍。
    """

    if lease_s <= 0:
        raise ValueError("租约时长必须大于 0")
    store = cowork_store()
    return await store.claim_run(run_id=run_id, worker_id=worker_id, lease_s=lease_s)


async def renew_lease(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
    lease_s: int,
) -> RunRecord | None:
    """续租并返回最新状态; 租约已被别人接管时返回 None。"""

    store = cowork_store()
    renewed = await store.renew_run_lease(run_id=run_id, worker_id=worker_id, lease_s=lease_s)
    return await store.get_run(run_id) if renewed else None


async def add_run_usage(
    session: AsyncSession,
    *,
    run_id: UUID,
    used_tokens: int,
    used_calls: int,
) -> None:
    """把一段执行的实际消耗累加进 run 行。

    只累加增量, 由调用方保证与 checkpoint 在同一个事务里提交: 节点中途崩溃时
    消耗与 checkpoint 一起回滚, 恢复后重跑该节点会重新计费——漏记上限是一个节点,
    而不是整个 run。
    """

    if used_tokens < 0 or used_calls < 0:
        raise ValueError("run 用量增量不能为负")
    if used_tokens == 0 and used_calls == 0:
        return
    store = cowork_store()
    await store.add_run_usage(run_id=run_id, used_tokens=used_tokens, used_calls=used_calls)
    return


async def finish_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    worker_id: str | None = None,
    error: str | None = None,
    used_tokens: int = 0,
    used_calls: int = 0,
) -> bool:
    """把 run 落终态。worker_id 非空时只允许租约持有者收尾。"""

    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"不是终态: {status}")
    store = cowork_store()
    return await store.finish_run(
        run_id=run_id,
        status=status,
        worker_id=worker_id,
        error=error,
        used_tokens=used_tokens,
        used_calls=used_calls,
    )


async def finish_run_with_events(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    events: Sequence[RunEventDraft],
    worker_id: str | None = None,
    error: str | None = None,
    used_tokens: int = 0,
    used_calls: int = 0,
) -> tuple[bool, list[RunEvent]]:
    """原子落 run 终态与终态事件；只有状态转换成功才写事件。"""

    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"不是终态: {status}")
    return await cowork_store().finish_run_with_events(
        run_id=run_id,
        status=status,
        events=events,
        worker_id=worker_id,
        error=error,
        used_tokens=used_tokens,
        used_calls=used_calls,
    )


async def request_cancel(
    session: AsyncSession,
    *,
    run_id: UUID,
) -> RunRecord:
    """请求取消。

    还没被 worker 领走就直接落终态; 已经在跑则只打标记, 由 worker 在下一个检查点
    自己收尾——否则会出现"状态已终态但事件还在继续写"的不一致。
    """

    store = cowork_store()
    return await store.request_cancel(run_id=run_id)


async def schedule_run_retry(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
    max_recovery: int,
    base_delay_s: float,
    max_delay_s: float,
) -> ScheduledRunRetry | None:
    """把瞬时故障的 Cowork run 停在最新 checkpoint，并持久化退避唤醒时间。"""

    del session  # 本地控制面由 Cowork store 持有独立的短事务。
    if max_recovery < 0:
        raise ValueError("自动恢复次数上限不能为负")
    if base_delay_s <= 0 or max_delay_s <= 0 or base_delay_s > max_delay_s:
        raise ValueError("重试退避必须满足 0 < base_delay_s <= max_delay_s")
    scheduled = await cowork_store().schedule_run_retry(
        run_id=run_id,
        worker_id=worker_id,
        max_recovery=max_recovery,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
    )
    if scheduled is None:
        return None
    attempt, wake_at = scheduled
    return ScheduledRunRetry(attempt=attempt, wake_at=wake_at)


@dataclass(frozen=True)
class ReapedRuns:
    """一次 watchdog 扫描的两类结果。

    `recovered` 被退回 `queued` 并计数, 等待重新入队后由 `claim_run` 抢占;
    `failed` 已经落终态。

    退回 `queued` 而不是只清空 `lease_until`: `claim_run` 的条件是
    `status = 'queued' OR (非终态 AND lease_until < now())`, 而 `NULL < now()` 是 NULL
    不是 true —— 只清租约会让 run 变成谁都抢不到的僵尸。
    """

    failed: list[UUID]
    recovered_cowork: list[tuple[UUID, int]]
    cancelled: list[UUID]


async def reap_expired_runs(
    session: AsyncSession, *, limit: int = 50, max_recovery: int = 3
) -> ReapedRuns:
    """回收租约过期的 run: 可恢复的松开租约等待重投, 其余明确标记为失败。

    能自动恢复的只有**同时**满足三个条件的 run:
    `cowork` 工作流、已经落过 checkpoint、且自动恢复次数没用完
    (ADR-0007)。前两条保证重跑从最近 checkpoint 继续且副作用走幂等协议,
    第三条保证一个稳定把 worker 拖垮的 run 不会被无限重投。
    """

    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if max_recovery < 0:
        raise ValueError("自动恢复次数上限不能为负")
    store = cowork_store()
    local = await store.reap_expired_runs(limit=limit, max_recovery=max_recovery)
    local_failed = local["failed"]
    local_recovered = local["recovered"]
    local_cancelled = local["cancelled"]
    # 消息在 JSONL 里，store 的 SQL 收不了尾：一条卡在 streaming 的助手消息会让界面
    # 永远转圈，而 run 早就是终态了。
    for run_id, message_status in (
        *((item, "failed") for item in local_failed),
        *((item, "cancelled") for item in local_cancelled),
    ):
        for message_id in await store.list_streaming_message_ids(run_id=run_id):
            await finalize_message(
                session,
                message_id=message_id,
                status=message_status,
                content=None if message_status == "failed" else "Cowork 任务已停止。",
            )
    for run_id, attempt in local_recovered:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {
                        "status": "recovering",
                        "summary": f"worker 失联，正在从最近 checkpoint 恢复（第 {attempt} 次）。",
                    },
                )
            ],
        )
    for run_id in local_failed:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "code": "worker_lease_expired",
                        "retryable": True,
                        "user_message": "worker 失联，任务未能安全恢复。",
                    },
                )
            ],
        )
    for run_id in local_cancelled:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "code": "cancelled",
                        "retryable": True,
                        "user_message": "任务已取消。",
                    },
                ),
                ("run.done", {"workflow_type": "cowork", "status": "cancelled"}),
            ],
        )
    return ReapedRuns(
        failed=local_failed,
        recovered_cowork=local_recovered,
        cancelled=local_cancelled,
    )


async def append_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    role: str,
    content: str = "",
    status: str = "completed",
    run_id: UUID | None = None,
    trace_id: str | None = None,
    citations: Sequence[dict[str, Any]] = (),
) -> UUID:
    """在对话末尾追加消息, seq 由数据库同事务计算。"""

    store = cowork_store()
    from app.cowork_store.factory import local_cowork_stores

    message_id = uuid7()
    seq = await store.allocate_message(
        record_id=message_id,
        conversation_id=conversation_id,
        role=role,
        status=status,
        run_id=run_id,
        title_source=content,
    )
    await local_cowork_stores().conversations.append(
        JsonlMessage.create(
            record_id=message_id,
            conversation_id=conversation_id,
            seq=seq,
            role=role,  # type: ignore[arg-type]
            content=content,
            status=status,  # type: ignore[arg-type]
            run_id=run_id,
            citations=tuple(citations),
        )
    )
    await store.append_session_entry(
        conversation_id=conversation_id,
        kind="message",
        payload={"record_id": str(message_id), "role": role, "seq": seq},
        entry_id=f"message:{message_id}",
    )
    return message_id


async def finalize_message(
    session: AsyncSession,
    *,
    message_id: UUID,
    status: str,
    content: str | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> None:
    if status not in MESSAGE_TERMINAL_STATUSES:
        raise ValueError(f"不是消息终态: {status}")
    store = cowork_store()
    from dataclasses import replace

    from app.cowork_store.factory import local_cowork_stores

    messages = local_cowork_stores().conversations
    conversation_id = await store.get_message_conversation_id(record_id=message_id)
    current = (
        None
        if conversation_id is None
        else await messages.find(message_id, conversation_id=conversation_id)
    )
    if current is not None:
        await messages.append(
            replace(
                current,
                status=status,  # type: ignore[arg-type]
                content=current.content if content is None else content,
                citations=current.citations if citations is None else tuple(citations),
            )
        )
        await store.update_message_status(
            record_id=message_id,
            status=status,
            content_preview=current.content if content is None else content,
        )
        return
