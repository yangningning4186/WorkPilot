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

from app.agent.persistence import (
    ensure_plan,
    load_latest_checkpoint,
    next_attempt_no,
    record_attempt,
    save_checkpoint,
    update_plan_step,
)
from app.agent.state import (
    AgentState,
    PlanStepState,
    ReviewCard,
    ReviewDocument,
    ReviewGroup,
    json_state,
)
from app.core.run_bus import RunBus
from app.services.runs import append_events, get_run

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
) -> AgentState:
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
    state: AgentState = {
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
    await save_checkpoint(
        session, run_id=run_id, state=state, parent_id=None
    )
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
        bus: RunBus | None = None,
    ) -> None:
        self.session = session
        self.tools = tools
        self.parent_checkpoint_id = parent_checkpoint_id
        self.bus = bus

    async def _commit_and_publish(self, run_id: UUID) -> None:
        await self.session.commit()
        if self.bus is not None:
            await self.bus.publish(run_id)

    async def execute(self, state: AgentState, step_idx: int) -> AgentState:
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
        await update_plan_step(
            self.session, run_id=run_id, step_id=step_id, status="running"
        )
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
            updates, summary, result = await self._operation(state, step_idx)
        except Exception as error:
            failed = json_state(deepcopy(state))
            failed["plan"][step_idx]["status"] = "failed"
            failed["error"] = str(error)
            await update_plan_step(
                self.session, run_id=run_id, step_id=step_id, status="failed"
            )
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
        completed = json_state(cast("AgentState", combined))
        completed["plan"][step_idx]["status"] = "done"
        completed["cursor"] = step_idx + 1
        completed["error"] = None
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
        await update_plan_step(
            self.session, run_id=run_id, step_id=step_id, status="done"
        )
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

    async def _operation(
        self, state: AgentState, step_idx: int
    ) -> tuple[dict[str, object], str, dict[str, Any]]:
        if step_idx == 0:
            documents = await self.tools.list_documents(state["document_ids"])
            actual_ids = {item["document_id"] for item in documents}
            expected_ids = set(state["document_ids"])
            if actual_ids != expected_ids:
                missing = sorted(expected_ids - actual_ids)
                raise ValueError(f"文档不可用或没有活跃版本: {missing}")
            return {"documents": documents}, f"已确认 {len(documents)} 篇文档", {
                "document_count": len(documents)
            }
        if step_idx == 1:
            cards = [await self.tools.extract_card(item) for item in state["documents"]]
            return {"cards": cards}, f"已抽取 {len(cards)} 张卡片", {
                "card_count": len(cards)
            }
        if step_idx == 2:
            groups = await self.tools.group_cards(state["cards"])
            return {"groups": groups}, f"已形成 {len(groups)} 个方法组", {
                "group_count": len(groups)
            }
        if step_idx == 3:
            comparison = await self.tools.compare_documents(state["cards"], state["groups"])
            return {"comparison": comparison}, "已完成横向比较", {
                "comparison_chars": len(comparison)
            }
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
    bus: RunBus | None = None,
) -> AgentState:
    checkpoint = await load_latest_checkpoint(session, run_id=run_id)
    if checkpoint is None:
        raise LookupError("固定综述尚未初始化 checkpoint")
    state = checkpoint.state
    if state["status"] == "waiting_human":
        return state
    # 失败 checkpoint 仍保持 executing + 原 cursor；重新执行只会重跑失败节点。
    execution = _ReviewGraphExecution(
        session, tools, parent_checkpoint_id=checkpoint.checkpoint_id, bus=bus
    )
    builder = StateGraph(AgentState)

    async def route_node(current: AgentState) -> AgentState:
        return current

    def route(current: AgentState) -> str:
        if current["status"] != "executing" or current["cursor"] >= len(
            READONLY_NODE_NAMES
        ):
            return END
        return READONLY_NODE_NAMES[current["cursor"]]

    # LangGraph 1.2 的 overload 尚不能识别 async TypedDict 节点，运行时接口支持该形态。
    builder.add_node("route", route_node)  # type: ignore[call-overload]
    builder.add_edge(START, "route")
    destinations: dict[Hashable, str] = {name: name for name in READONLY_NODE_NAMES}
    destinations[END] = END
    builder.add_conditional_edges("route", route, destinations)
    def make_operation(step_idx: int) -> Callable[[AgentState], Awaitable[AgentState]]:
        async def operation_node(current: AgentState) -> AgentState:
            return await execution.execute(current, step_idx)

        return operation_node

    for step_idx, name in enumerate(READONLY_NODE_NAMES):
        operation = make_operation(step_idx)
        builder.add_node(name, operation)  # type: ignore[call-overload]
        builder.add_edge(name, "route")
    graph = builder.compile()
    result = await graph.ainvoke(state)
    return json_state(cast("AgentState", result))
