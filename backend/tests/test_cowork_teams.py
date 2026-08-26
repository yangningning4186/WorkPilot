import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from uuid6 import uuid7

from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.interactions import get_pending_inbox_item, resolve_inbox_item
from app.cowork.permissions import create_session_root
from app.cowork.runtime import initialize_cowork_state, resume_cowork_after_human
from app.cowork.teams import (
    BOARD_ASSIGN_TASK_TOOL_NAME,
    BOARD_CREATE_TASK_TOOL_NAME,
    BOARD_RESOLVE_TASK_TOOL_NAME,
    BOARD_REVIEW_TASK_TOOL_NAME,
    PROPOSE_TEAM_TOOL_NAME,
    _initial_worker_state,
    register_team_tools,
    team_run_summary,
    worker_limits,
)
from app.cowork.tools import CoworkToolContext, build_default_cowork_registry
from app.cowork_store.routing import cowork_store
from app.runstore.conversations import update_conversation_runtime
from app.runstore.runs import append_message, create_run, ensure_conversation, get_run, list_events
from app.worker.cowork_run import cowork_run
from tests.test_cowork_runner import NativeToolProvider, _final_completion, _tool_completion
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import CompletionResult, Message, ToolCall, ToolDefinition, Usage

pytestmark = pytest.mark.integration


class _WorkerGateway:
    """第一次故意越过 Board scope；第二次根据工具错误交付报告。"""

    def __init__(self, outside_path: Path) -> None:
        self.outside_path = outside_path
        self.histories: list[list[Message]] = []
        self.tools: list[ToolDefinition] = []

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        del parallel_tool_calls, task_type, max_tokens, temperature
        self.histories.append(list(messages))
        self.tools = tools
        if len(self.histories) == 1:
            return CompletionResult(
                text="",
                model="fake-chat",
                provider="deterministic_test",
                usage=Usage(input_tokens=5, output_tokens=2),
                tool_calls=(
                    ToolCall(
                        id="read-outside",
                        name="read_file",
                        arguments=json.dumps({"path": str(self.outside_path)}),
                    ),
                ),
            )
        return CompletionResult(
            text="验收标准：已核对资源边界。证据：越界读取被拒绝。未完成项：无。",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=8, output_tokens=5),
        )


def _context(
    session: AsyncSession,
    *,
    gateway: object,
    conversation_id: UUID,
    run_id: UUID,
    tool_call_id: str,
) -> CoworkToolContext:
    return CoworkToolContext(
        session=session,
        gateway=cast("Any", gateway),
        settings=Settings(),
        conversation_id=conversation_id,
        run_id=run_id,
        worker_id="team-test-worker",
        plan_step_id=uuid7(),
        tool_call_id=tool_call_id,
    )


async def test_propose_team_always_waits_for_hitl_then_prespawns_idle_sessions(
    db_session: AsyncSession,
    tmp_path: Path,
    store_sql,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Agent Team approval")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await update_conversation_runtime(
        db_session,
        conversation_id=conversation_id,
        provider_profile_id=None,
        model_override=None,
        unattended=False,
        approval_mode="auto",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="组建两个 Worker 协作调查",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    assert registry.get(PROPOSE_TEAM_TOOL_NAME).approval_can_be_waived is False
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="load-team",
                    name="load_tools",
                    arguments=json.dumps({"names": [PROPOSE_TEAM_TOOL_NAME]}),
                )
            ),
            _tool_completion(
                ToolCall(
                    id="propose-team-1",
                    name=PROPOSE_TEAM_TOOL_NAME,
                    arguments=json.dumps(
                        {
                            "members": [
                                {"name": "files", "role": "核对本地材料", "reason": "隔离调查"},
                                {"name": "checks", "role": "检查验收标准", "reason": "独立复核"},
                            ],
                            "note": "只在批准后创建",
                        },
                        ensure_ascii=False,
                    ),
                )
            ),
            _final_completion("团队已经按批准的 roster 创建。"),
        ]
    )
    context = {
        "settings": get_settings(),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    waiting = await get_run(db_session, run.id)
    assert waiting is not None and waiting.status == "waiting_human"
    assert await cowork_store().get_team_for_lead(lead_conversation_id=conversation_id) is None
    events = await list_events(db_session, run_id=run.id, limit=200)
    interrupt = next(event for event in events if event.type == "interrupt")
    assert interrupt.payload["kind"] == "external_approval"
    assert interrupt.payload["payload"]["tool"] == PROPOSE_TEAM_TOOL_NAME

    item = await get_pending_inbox_item(
        db_session,
        run_id=run.id,
        resume_token=UUID(interrupt.payload["resume_token"]),
    )
    assert item is not None
    item, response = await resolve_inbox_item(db_session, item=item, approved=True)
    await resume_cowork_after_human(db_session, run_id=run.id, item=item, response=response)
    # 用户点批准只恢复原调用；真正的持久 Session 在获批工具执行时创建。
    assert await cowork_store().get_team_for_lead(lead_conversation_id=conversation_id) is None

    await cowork_run(context, str(run.id))

    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    team = await cowork_store().get_team_for_lead(lead_conversation_id=conversation_id)
    assert team is not None
    workers = await cowork_store().list_team_workers(team_id=team.id)
    assert [worker.name for worker in workers] == ["files", "checks"]
    rows = store_sql(
        "SELECT status, active_task_id, state FROM cowork_team_worker_sessions ORDER BY created_at"
    )
    assert [row["status"] for row in rows] == ["idle", "idle"]
    assert all(row["active_task_id"] is None for row in rows)
    assert all(len(json.loads(str(row["state"]))["messages"]) == 1 for row in rows)


async def test_lead_uses_board_and_worker_gets_only_assignment_envelope_and_scope(
    db_session: AsyncSession,
    tmp_path: Path,
    store_sql,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "inside.md").write_text("inside", encoding="utf-8")
    outside = tmp_path / "lead-secret.md"
    outside.write_text("LEAD_SECRET_SHOULD_NOT_REACH_WORKER", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="Agent Team Board")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="LEAD_SECRET_SHOULD_NOT_REACH_WORKER",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    team, workers = await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="approved-proposal",
        note="",
        members=[
            {
                "name": "files",
                "role": "核对文件边界",
                "reason": "独立执行",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    assert len(workers) == 1
    precreated = store_sql(
        "SELECT status, active_task_id, state FROM cowork_team_worker_sessions WHERE team_id = ?",
        (str(team.id),),
    )[0]
    assert precreated["status"] == "idle"
    assert precreated["active_task_id"] is None

    registry = build_default_cowork_registry()
    register_team_tools(registry)
    gateway = _WorkerGateway(outside)
    base_context = _context(
        db_session,
        gateway=gateway,
        conversation_id=conversation_id,
        run_id=run.id,
        tool_call_id="board-create-1",
    )
    created = await registry.execute(
        BOARD_CREATE_TASK_TOOL_NAME,
        {
            "title": "验证资源边界",
            "description": "读取分配范围内的材料并报告边界行为",
            "acceptance_criteria": "不得读取 resource_scope 之外的文件",
            "resource_scope": [{"path": str(allowed), "access_mode": "read_only"}],
        },
        context=base_context,
    )
    task_id = UUID(str(created.output["task_id"]))

    assigned = await registry.execute(
        BOARD_ASSIGN_TASK_TOOL_NAME,
        {"task_id": str(task_id), "worker": "files"},
        context=replace(
            base_context,
            plan_step_id=uuid7(),
            tool_call_id="board-assign-1",
        ),
    )

    assert assigned.output["status"] == "review"
    assert "越界读取被拒绝" in str(assigned.output["worker_report"])
    first_history = gateway.histories[0]
    user_messages = [message for message in first_history if message.role == "user"]
    assert len(user_messages) == 1
    envelope = json.loads(user_messages[0].content)
    assert set(envelope) == {"task_description", "acceptance_criteria", "resource_scope"}
    assert envelope["resource_scope"] == [
        {"path": str(allowed.resolve()), "access_mode": "read_only"}
    ]
    assert "LEAD_SECRET_SHOULD_NOT_REACH_WORKER" not in json.dumps(
        [message.content for message in first_history], ensure_ascii=False
    )
    tool_result = gateway.histories[1][-1]
    assert tool_result.role == "tool"
    assert "不在当前 Board task" in tool_result.content
    assert {tool.name for tool in gateway.tools}
    assert all(registry.get(tool.name).path_argument is not None for tool in gateway.tools)

    rejected = await registry.execute(
        BOARD_REVIEW_TASK_TOOL_NAME,
        {"task_id": str(task_id), "accepted": False, "feedback": "补充重试证据"},
        context=replace(
            base_context,
            plan_step_id=uuid7(),
            tool_call_id="board-review-1",
        ),
    )
    assert rejected.output["status"] == "open"
    assert rejected.output["rejection_reason"] == "补充重试证据"

    reassigned = await registry.execute(
        BOARD_ASSIGN_TASK_TOOL_NAME,
        {"task_id": str(task_id), "worker": "files"},
        context=replace(
            base_context,
            plan_step_id=uuid7(),
            tool_call_id="board-assign-2",
        ),
    )
    assert reassigned.output["status"] == "review"
    assert reassigned.output["attempt_count"] == 2
    retry_users = [message for message in gateway.histories[2] if message.role == "user"]
    retry_envelope = json.loads(retry_users[-1].content)
    assert retry_envelope["review_feedback"] == "补充重试证据"
    assert "越界读取被拒绝" in retry_envelope["previous_worker_report"]
    assert retry_envelope["attempt"] == 2

    reviewed = await registry.execute(
        BOARD_REVIEW_TASK_TOOL_NAME,
        {"task_id": str(task_id), "accepted": True, "feedback": "证据满足验收标准"},
        context=replace(
            base_context,
            plan_step_id=uuid7(),
            tool_call_id="board-review-2",
        ),
    )
    assert reviewed.output["status"] == "done"
    assert reviewed.output["completion_kind"] == "complete"
    assert reviewed.output["rejection_reason"] == "补充重试证据"
    stored_task = store_sql(
        """SELECT status, attempt_count, completion_kind, worker_report, review_comment,
                  last_rejection_comment FROM cowork_board_tasks WHERE id = ?""",
        (str(task_id),),
    )[0]
    assert stored_task["status"] == "done"
    assert stored_task["attempt_count"] == 2
    assert stored_task["completion_kind"] == "complete"
    assert "越界读取被拒绝" in str(stored_task["worker_report"])
    assert stored_task["review_comment"] == "证据满足验收标准"
    assert stored_task["last_rejection_comment"] == "补充重试证据"
    persisted_session = store_sql(
        "SELECT status, active_task_id, state FROM cowork_team_worker_sessions WHERE team_id = ?",
        (str(team.id),),
    )[0]
    assert persisted_session["status"] == "idle"
    assert persisted_session["active_task_id"] is None
    worker_state = json.loads(str(persisted_session["state"]))
    # 首轮 5 条 + 返工 assignment + 第二次 final report。
    assert len(worker_state["messages"]) == 7
    assert "LEAD_SECRET_SHOULD_NOT_REACH_WORKER" not in json.dumps(
        worker_state["messages"], ensure_ascii=False
    )


async def test_board_can_resolve_open_task_as_partial_or_cancelled(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Agent Team resolution")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="显式收束未完成任务",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="resolution-proposal",
        note="",
        members=[
            {
                "name": "reviewer",
                "role": "检查材料",
                "reason": "独立执行",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    context = _context(
        db_session,
        gateway=_WorkerGateway(tmp_path / "outside"),
        conversation_id=conversation_id,
        run_id=run.id,
        tool_call_id="create-partial",
    )

    async def create(title: str, call_id: str) -> UUID:
        result = await registry.execute(
            BOARD_CREATE_TASK_TOOL_NAME,
            {
                "title": title,
                "description": "检查已有材料",
                "acceptance_criteria": "给出结论",
                "resource_scope": [{"path": str(tmp_path), "access_mode": "read_only"}],
            },
            context=replace(context, plan_step_id=uuid7(), tool_call_id=call_id),
        )
        return UUID(str(result.output["task_id"]))

    partial_id = await create("保留部分结果", "create-partial")
    cancelled_id = await create("不再继续", "create-cancelled")
    partial = await registry.execute(
        BOARD_RESOLVE_TASK_TOOL_NAME,
        {
            "task_id": str(partial_id),
            "resolution": "accept_partial",
            "reason": "用户确认接受当前结果",
        },
        context=replace(context, plan_step_id=uuid7(), tool_call_id="resolve-partial"),
    )
    cancelled = await registry.execute(
        BOARD_RESOLVE_TASK_TOOL_NAME,
        {
            "task_id": str(cancelled_id),
            "resolution": "cancel",
            "reason": "用户决定停止该分支",
        },
        context=replace(context, plan_step_id=uuid7(), tool_call_id="resolve-cancelled"),
    )

    assert (partial.output["status"], partial.output["completion_kind"]) == ("done", "partial")
    assert (cancelled.output["status"], cancelled.output["completion_kind"]) == (
        "cancelled",
        "cancelled",
    )
    summary = await team_run_summary(lead_conversation_id=conversation_id)
    assert summary is not None and summary["completion_status"] == "partial"


async def test_worker_limits_expand_for_wider_retried_tasks(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Agent Team limits")
    await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="limits-proposal",
        note="",
        members=[
            {
                "name": "worker",
                "role": "分析",
                "reason": "独立执行",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    task = await cowork_store().create_board_task(
        lead_conversation_id=conversation_id,
        title="复杂任务",
        description="分析两个相互独立的目录",
        acceptance_criteria="检查架构；检查测试；检查安全；提供证据；给出建议",
        resource_scope=[
            {"path": str(tmp_path / "a"), "access_mode": "read_only"},
            {"path": str(tmp_path / "b"), "access_mode": "read_only"},
        ],
    )
    initial = worker_limits(task, decision_cap=8_192)
    retried = worker_limits(replace(task, attempt_count=3), decision_cap=8_192)

    assert initial.rounds > 4 and initial.tool_calls > 8
    assert initial.decision_tokens > 2_048 and initial.summary_tokens > 1_536
    assert retried.rounds > initial.rounds
    assert retried.tool_calls > initial.tool_calls


async def test_run_finishes_partial_when_team_board_still_has_open_tasks(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Agent Team partial run")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="汇报团队当前结果",
        budget_tokens=20_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="partial-run-proposal",
        note="",
        members=[
            {
                "name": "worker",
                "role": "分析",
                "reason": "独立执行",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    await cowork_store().create_board_task(
        lead_conversation_id=conversation_id,
        title="仍待完成",
        description="尚未分配",
        acceptance_criteria="完成分析",
        resource_scope=[{"path": str(tmp_path), "access_mode": "read_only"}],
    )
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    bus = InMemoryRunBus()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider([_final_completion("当前只能交付已有结果。")])

    await cowork_run(
        {
            "settings": get_settings(),
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "partial"
    events = await list_events(db_session, run_id=run.id, limit=200)
    summary = next(event for event in events if event.type == "team.summary")
    assert summary.payload["completion_status"] == "partial"
    assert summary.payload["tasks"][0]["status"] == "open"
    assert any(
        event.type == "run.done" and event.payload["status"] == "partial"
        for event in events
    )
