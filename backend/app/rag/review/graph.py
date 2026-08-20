"""LangGraph 驱动的固定综述图；节点顺序固定，不做自由规划。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Hashable
from copy import deepcopy
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.budget import BudgetMeter, RunBudgetExceededError
from app.agent_core.contracts import BudgetState
from app.agent_core.state import PlanStepState, json_state, normalize_budget
from app.core.run_bus import RunBus
from app.rag.review.state import ReviewCard, ReviewDocument, ReviewGroup, ReviewState
from app.runstore.checkpoints import (
    AgentCheckpoint,
    ensure_plan,
    load_latest_checkpoint,
    next_attempt_no,
    record_attempt,
    save_checkpoint,
    update_plan_step,
)
from app.runstore.runs import add_run_usage, append_events, finish_run, get_run

_DIMENSION_HINTS = {
    "tokens": "token 预算",
    "calls": "模型调用次数",
    "wall_ms": "执行时长",
}

READONLY_NODE_NAMES = (
    "select_documents",
    "extract_cards",
    "group_cards",
    "compare_documents",
    "generate_preview",
)


class ReviewTools(Protocol):
    async def list_documents(self, document_ids: list[str]) -> list[ReviewDocument]: ...

    async def extract_card(self, document: ReviewDocument) -> ReviewCard: ...

    async def group_cards(self, cards: list[ReviewCard]) -> list[ReviewGroup]: ...

    async def compare_documents(
        self, cards: list[ReviewCard], groups: list[ReviewGroup]
    ) -> str: ...

    async def generate_review(
        self,
        *,
        goal: str,
        cards: list[ReviewCard],
        groups: list[ReviewGroup],
        comparison: str,
    ) -> str: ...


def fixed_review_plan(run_id: UUID) -> list[PlanStepState]:
    definitions: tuple[tuple[str, str, list[int]], ...] = (
        ("筛选并校验文档", "list_documents", []),
        ("逐篇抽取结构化卡片", "extract_card", [0]),
        ("按方法族分组", "group_cards", [1]),
        ("横向比较文档", "compare_docs", [2]),
        ("生成综述预览", "generate_review", [3]),
        ("人工确认后写入笔记", "write_note", [4]),
    )
    return [
        {
            "id": str(uuid5(run_id, f"literature-review-step:{idx}")),
            "idx": idx,
            "description": description,
            "tool": tool,
            "depends_on": depends_on,
            "status": "pending",
        }
        for idx, (description, tool, depends_on) in enumerate(definitions)
    ]


async def initialize_review_state(
    session: AsyncSession,
    *,
    run_id: UUID,
    document_ids: list[UUID],
    output_path: str,
    bus: RunBus | None = None,
) -> ReviewState:
    run = await get_run(session, run_id)
    if run is None:
        raise LookupError(f"run 不存在: {run_id}")
    if run.workflow_type != "literature_review":
        raise ValueError("只有 literature_review run 可以初始化固定综述图")
    if len(set(document_ids)) < 2:
        raise ValueError("固定综述至少需要两篇不同文档")
    if not output_path.strip():
        raise ValueError("output_path 不能为空")
    plan = fixed_review_plan(run_id)
    await ensure_plan(session, run_id=run_id, steps=plan)
    state: ReviewState = {
        "schema_version": "literature-review.v1",
        "run_id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "goal": run.goal,
        "document_ids": [str(item) for item in document_ids],
        "plan": plan,
        "cursor": 0,
        "documents": [],
        "cards": [],
        "groups": [],
        "comparison": "",
        "draft": "",
        "output_path": output_path,
        "artifacts": {},
        "budget": {
            "max_tokens": run.budget_tokens,
            "used_tokens": run.used_tokens,
            "max_calls": run.budget_calls,
            "used_calls": run.used_calls,
            "max_wall_ms": run.budget_wall_ms,
            "used_wall_ms": 0,
            "started_at_ms": int(time.time() * 1000),
        },
        "interrupt": None,
        "status": "executing",
        "error": None,
    }
    await append_events(
        session,
        run_id=run_id,
        events=[("plan", {"workflow_type": "literature_review", "steps": plan})],
    )
    await save_checkpoint(session, run_id=run_id, state=state, parent_id=None)
    await session.commit()
    if bus is not None:
        await bus.publish(run_id)
    return state


class _ReviewGraphExecution:
    def __init__(
        self,
        session: AsyncSession,
        tools: ReviewTools,
        *,
        parent_checkpoint_id: str,
        meter: BudgetMeter,
        bus: RunBus | None = None,
    ) -> None:
        self.session = session
        self.tools = tools
        self.parent_checkpoint_id = parent_checkpoint_id
        self.meter = meter
        self.bus = bus
        # run 行里已经记过的用量; 每个节点只把增量写库, 避免重复累加。
        self._flushed_tokens = meter.budget["used_tokens"]
        self._flushed_calls = meter.budget["used_calls"]

    async def _commit_and_publish(self, run_id: UUID) -> None:
        await self.session.commit()
        if self.bus is not None:
            await self.bus.publish(run_id)

    async def _flush_usage(self, run_id: UUID) -> None:
        """把自上次落库以来的用量增量写进 run 行, 与 checkpoint 同事务。"""

        tokens = self.meter.budget["used_tokens"] - self._flushed_tokens
        calls = self.meter.budget["used_calls"] - self._flushed_calls
        await add_run_usage(self.session, run_id=run_id, used_tokens=tokens, used_calls=calls)
        self._flushed_tokens = self.meter.budget["used_tokens"]
        self._flushed_calls = self.meter.budget["used_calls"]

    def _with_budget(self, state: ReviewState) -> ReviewState:
        """把计量器的当前读数写回 state, 让 checkpoint 带上真实消耗。"""

        self.meter.settle_wall()
        state["budget"] = cast("BudgetState", dict(self.meter.budget))
        return state

    async def execute(self, state: ReviewState, step_idx: int) -> ReviewState:
        run_id = UUID(state["run_id"])
        step = state["plan"][step_idx]
        step_id = UUID(step["id"])
        node = READONLY_NODE_NAMES[step_idx]
        attempt_no = await next_attempt_no(
            self.session,
            run_id=run_id,
            plan_step_id=step_id,
            node=node,
        )
        await update_plan_step(self.session, run_id=run_id, step_id=step_id, status="running")
        await append_events(
            self.session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {"step_id": step["id"], "step_idx": step_idx, "status": "running"},
                )
            ],
        )
        await self._commit_and_publish(run_id)
        started = time.monotonic()
        try:
            # 节点入口先查墙钟: 抽卡这类节点会按文档数循环, 也可能整节点不产生模型调用,
            # 只在 BudgetedGateway 里查会漏掉这两种失控形态。
            self.meter.check_wall()
            updates, summary, result = await self._operation(state, step_idx)
        except RunBudgetExceededError as error:
            return await self._trip_budget(
                state,
                step_idx,
                error,
                run_id=run_id,
                step_id=step_id,
                attempt_no=attempt_no,
                node=node,
                started=started,
            )
        except Exception as error:
            # 失败节点已经烧掉的 token 必须留在账上, 否则反复重试等于无限预算。
            failed = self._with_budget(json_state(deepcopy(state)))
            failed["plan"][step_idx]["status"] = "failed"
            failed["error"] = str(error)
            await self._flush_usage(run_id)
            await update_plan_step(self.session, run_id=run_id, step_id=step_id, status="failed")
            await record_attempt(
                self.session,
                run_id=run_id,
                plan_step_id=step_id,
                attempt_no=attempt_no,
                node=node,
                tool_name=step["tool"],
                tool_args={"document_ids": state["document_ids"]},
                status="failed",
                latency_ms=round((time.monotonic() - started) * 1000),
                error_model=f"步骤“{step['description']}”失败：{error}。请修正输入后重试。",
            )
            checkpoint = await save_checkpoint(
                self.session,
                run_id=run_id,
                state=failed,
                parent_id=self.parent_checkpoint_id,
            )
            self.parent_checkpoint_id = checkpoint.checkpoint_id
            await append_events(
                self.session,
                run_id=run_id,
                events=[
                    (
                        "step.update",
                        {
                            "step_id": step["id"],
                            "step_idx": step_idx,
                            "status": "failed",
                            "summary": str(error),
                        },
                    )
                ],
            )
            await self._commit_and_publish(run_id)
            raise

        combined: dict[str, Any] = dict(json_state(deepcopy(state)))
        combined.update(updates)
        completed = self._with_budget(json_state(cast("ReviewState", combined)))
        completed["plan"][step_idx]["status"] = "done"
        completed["cursor"] = step_idx + 1
        completed["error"] = None
        await self._flush_usage(run_id)
        events: list[tuple[str, dict[str, Any]]] = [
            (
                "step.update",
                {
                    "step_id": step["id"],
                    "step_idx": step_idx,
                    "status": "done",
                    "summary": summary,
                },
            )
        ]
        if step_idx == len(READONLY_NODE_NAMES) - 1:
            resume_token = str(uuid5(run_id, "literature-review-write-confirm"))
            completed["status"] = "waiting_human"
            completed["interrupt"] = {
                "kind": "write_confirm",
                "payload": {
                    "title": "确认写入综述笔记",
                    "output_path": completed["output_path"] or "",
                    "preview": completed["draft"],
                },
                "resume_token": resume_token,
            }
            events.extend(
                [
                    (
                        "artifact",
                        {
                            "kind": "review_preview",
                            "title": "综述预览",
                            "content": completed["draft"],
                        },
                    ),
                    ("interrupt", cast("dict[str, Any]", completed["interrupt"])),
                ]
            )
            await self.session.execute(
                text(
                    """
                    UPDATE agent_runs
                    SET status = 'waiting_human', worker_id = NULL, lease_until = NULL,
                        heartbeat_at = NULL, updated_at = now()
                    WHERE id = :run_id
                    """
                ),
                {"run_id": run_id},
            )
        await update_plan_step(self.session, run_id=run_id, step_id=step_id, status="done")
        await record_attempt(
            self.session,
            run_id=run_id,
            plan_step_id=step_id,
            attempt_no=attempt_no,
            node=node,
            tool_name=step["tool"],
            tool_args={"document_ids": state["document_ids"]},
            tool_result=result,
            status="ok",
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        checkpoint = await save_checkpoint(
            self.session,
            run_id=run_id,
            state=completed,
            parent_id=self.parent_checkpoint_id,
        )
        self.parent_checkpoint_id = checkpoint.checkpoint_id
        await append_events(self.session, run_id=run_id, events=events)
        await self._commit_and_publish(run_id)
        return completed

    async def _trip_budget(
        self,
        state: ReviewState,
        step_idx: int,
        error: RunBudgetExceededError,
        *,
        run_id: UUID,
        step_id: UUID,
        attempt_no: int,
        node: str,
        started: float,
    ) -> ReviewState:
        """预算熔断: 落 `budget_exceeded` 终态并**返回**而不是抛。

        抛出去会被 worker 的重试循环接住, 重跑同一节点只会继续烧同一份预算。
        返回后 `route` 看到非 executing 状态直接走 END, run 已在此处收尾。
        """

        step = state["plan"][step_idx]
        tripped = self._with_budget(json_state(deepcopy(state)))
        tripped["plan"][step_idx]["status"] = "failed"
        tripped["status"] = "budget_exceeded"
        tripped["error"] = str(error)
        tripped["interrupt"] = None
        await self._flush_usage(run_id)
        await update_plan_step(self.session, run_id=run_id, step_id=step_id, status="failed")
        await record_attempt(
            self.session,
            run_id=run_id,
            plan_step_id=step_id,
            attempt_no=attempt_no,
            node=node,
            tool_name=step["tool"],
            tool_args={"document_ids": state["document_ids"]},
            status="failed",
            latency_ms=round((time.monotonic() - started) * 1000),
            error_model=(
                f"步骤“{step['description']}”因 run 预算熔断中止：{error}"
                "不要重试本步骤，预算不会因为重试而恢复。"
            ),
        )
        checkpoint = await save_checkpoint(
            self.session,
            run_id=run_id,
            state=tripped,
            parent_id=self.parent_checkpoint_id,
        )
        self.parent_checkpoint_id = checkpoint.checkpoint_id
        await append_events(
            self.session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {
                        "step_id": step["id"],
                        "step_idx": step_idx,
                        "status": "failed",
                        "summary": str(error),
                    },
                ),
                (
                    "error",
                    {
                        "user_message": (
                            f"本次综述已达到 {_DIMENSION_HINTS[error.dimension]}上限并停止，"
                            "已完成的步骤保留在时间线中。"
                        ),
                        "retryable": False,
                        "code": "run_budget_exceeded",
                        "dimension": error.dimension,
                        "used": error.used,
                        "limit": error.limit,
                    },
                ),
            ],
        )
        # 用量已经逐节点写过, 这里只落终态, 不再重复累加。
        await finish_run(self.session, run_id=run_id, status="budget_exceeded", error=str(error))
        await self._commit_and_publish(run_id)
        return tripped

    async def _operation(
        self, state: ReviewState, step_idx: int
    ) -> tuple[dict[str, object], str, dict[str, Any]]:
        if step_idx == 0:
            documents = await self.tools.list_documents(state["document_ids"])
            actual_ids = {item["document_id"] for item in documents}
            expected_ids = set(state["document_ids"])
            if actual_ids != expected_ids:
                missing = sorted(expected_ids - actual_ids)
                raise ValueError(f"文档不可用或没有活跃版本: {missing}")
            return (
                {"documents": documents},
                f"已确认 {len(documents)} 篇文档",
                {"document_count": len(documents)},
            )
        if step_idx == 1:
            cards = [await self.tools.extract_card(item) for item in state["documents"]]
            return {"cards": cards}, f"已抽取 {len(cards)} 张卡片", {"card_count": len(cards)}
        if step_idx == 2:
            groups = await self.tools.group_cards(state["cards"])
            return (
                {"groups": groups},
                f"已形成 {len(groups)} 个方法组",
                {"group_count": len(groups)},
            )
        if step_idx == 3:
            comparison = await self.tools.compare_documents(state["cards"], state["groups"])
            return (
                {"comparison": comparison},
                "已完成横向比较",
                {"comparison_chars": len(comparison)},
            )
        if step_idx == 4:
            draft = await self.tools.generate_review(
                goal=state["goal"],
                cards=state["cards"],
                groups=state["groups"],
                comparison=state["comparison"],
            )
            if not draft.strip():
                raise ValueError("综述预览为空")
            return {"draft": draft}, "已生成待确认预览", {"draft_chars": len(draft)}
        raise AssertionError(f"未知固定步骤: {step_idx}")


async def run_readonly_review(
    session: AsyncSession,
    *,
    run_id: UUID,
    tools: ReviewTools,
    meter: BudgetMeter,
    bus: RunBus | None = None,
) -> ReviewState:
    checkpoint: AgentCheckpoint[ReviewState] | None = await load_latest_checkpoint(
        session, run_id=run_id
    )
    if checkpoint is None:
        raise LookupError("固定综述尚未初始化 checkpoint")
    state = normalize_budget(checkpoint.state)
    if state["status"] == "waiting_human":
        return state
    if state["status"] == "budget_exceeded":
        # 熔断是终态。想继续必须先提额并重新发起, 不能靠再投一次队列绕过去。
        return state
    # checkpoint 是墙钟的唯一存储(run 行只有 token 与调用数), 恢复执行时必须接管,
    # 否则每次重启墙钟都从零起算, 等于这一维没有上限。上限与 token/调用数消耗
    # 一律以 run 行为准, 不从 checkpoint 抄回来。
    meter.adopt_wall(state["budget"]["used_wall_ms"])
    # 失败 checkpoint 仍保持 executing + 原 cursor；重新执行只会重跑失败节点。
    execution = _ReviewGraphExecution(
        session,
        tools,
        parent_checkpoint_id=checkpoint.checkpoint_id,
        meter=meter,
        bus=bus,
    )
    builder = StateGraph(ReviewState)

    async def route_node(current: ReviewState) -> ReviewState:
        return current

    def route(current: ReviewState) -> str:
        if current["status"] != "executing" or current["cursor"] >= len(READONLY_NODE_NAMES):
            return END
        return READONLY_NODE_NAMES[current["cursor"]]

    # LangGraph 1.2 的 overload 尚不能识别 async TypedDict 节点，运行时接口支持该形态。
    builder.add_node("route", route_node)  # type: ignore[call-overload]
    builder.add_edge(START, "route")
    destinations: dict[Hashable, str] = {name: name for name in READONLY_NODE_NAMES}
    destinations[END] = END
    builder.add_conditional_edges("route", route, destinations)

    def make_operation(step_idx: int) -> Callable[[ReviewState], Awaitable[ReviewState]]:
        async def operation_node(current: ReviewState) -> ReviewState:
            return await execution.execute(current, step_idx)

        return operation_node

    for step_idx, name in enumerate(READONLY_NODE_NAMES):
        operation = make_operation(step_idx)
        builder.add_node(name, operation)  # type: ignore[call-overload]
        builder.add_edge(name, "route")
    graph = builder.compile()
    result = await graph.ainvoke(state)
    return json_state(cast("ReviewState", result))
