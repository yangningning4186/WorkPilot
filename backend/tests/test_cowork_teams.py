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
from app.core.queue import InProcessRunQueue
from app.core.run_bus import InMemoryRunBus
from app.cowork.authorization import arguments_sha256
from app.cowork.interactions import get_pending_inbox_item, resolve_inbox_item
from app.cowork.permissions import (
    create_session_root,
    grant_capability,
    list_capability_grants,
    revoke_capability_grant,
)
from app.cowork.personas import load_persona_catalog, snapshot_persona
from app.cowork.runtime import initialize_cowork_state, resume_cowork_after_human
from app.cowork.semantic_approvals import (
    build_semantic_approval_evidence,
    build_trusted_approval_evidence,
)
from app.cowork.teams import (
    BOARD_ASSIGN_TASK_TOOL_NAME,
    BOARD_CREATE_TASK_TOOL_NAME,
    BOARD_RESOLVE_TASK_TOOL_NAME,
    BOARD_REVIEW_TASK_TOOL_NAME,
    PROPOSE_TEAM_TOOL_NAME,
    TEAM_MANAGE_TOOL_NAME,
    ProposeTeamArgs,
    _bounded_worker_tool_error,
    _bounded_worker_tool_result,
    _initial_worker_state,
    _materialize_team_members,
    _worker_state,
    register_team_tools,
    team_run_summary,
    worker_limits,
)
from app.cowork.tools import CoworkToolContext, CoworkToolResult, build_default_cowork_registry
from app.cowork_contracts import BoardTaskRecord
from app.cowork_store.routing import cowork_store
from app.runstore.conversations import update_conversation_runtime
from app.runstore.runs import append_message, create_run, ensure_conversation, get_run, list_events
from app.worker.cowork_run import cowork_run
from app.worker.maintenance import team_wake_dispatch_tick
from tests.test_cowork_runner import NativeToolProvider, _final_completion, _tool_completion
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import CompletionResult, Message, ToolCall, ToolDefinition, Usage

pytestmark = pytest.mark.integration

_APPROVAL_SIGNING_KEY = "7" * 64


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
                        thought_signature="worker-gemini-signature",
                    ),
                ),
            )
        return CompletionResult(
            text="验收标准：已核对资源边界。证据：越界读取被拒绝。未完成项：无。",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=8, output_tokens=5),
        )


class _FinalWorkerGateway:
    def __init__(self) -> None:
        self.histories: list[list[Message]] = []

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
        del tools, parallel_tool_calls, task_type, max_tokens, temperature
        self.histories.append(list(messages))
        return CompletionResult(
            text="验收标准：恢复后完成。证据：沿用崩溃前 Worker state。未完成项：无。",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=6, output_tokens=4),
        )


def test_team_worker_persisted_tool_content_is_bounded_and_errors_are_stable() -> None:
    content = _bounded_worker_tool_result(
        CoworkToolResult(content={"content": "x" * 100_000, "source": "remote"}),
        max_chars=100_000,
    )
    assert len(content) <= 20_000
    decoded = json.loads(content)
    assert decoded["result_truncated"] is True
    assert decoded["result_original_chars"] > 100_000
    assert len(decoded["result_sha256"]) == 64

    token = "sk-super-secret-token-value"
    redacted = _bounded_worker_tool_result(
        CoworkToolResult(
            content={
                "authorization": f"Bearer {token}",
                "nested": {"url": f"https://example.test/?token={token}"},
                "text": f"credential={token}",
            }
        ),
        max_chars=20_000,
    )
    assert token not in redacted
    assert "<redacted>" in redacted

    secret = "Bearer sk-secret-value https://example.test/?token=secret"
    error_content = _bounded_worker_tool_error(RuntimeError(secret))
    assert secret not in error_content
    assert "sk-secret-value" not in error_content
    assert json.loads(error_content) == {
        "ok": False,
        "error": {"code": "tool_error:builtins.RuntimeError"},
    }


def test_expert_roster_materializes_trusted_prompts_roles_and_tool_boundaries(
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    expert = load_persona_catalog(settings).get("expert-council")
    digest = snapshot_persona(expert, settings)["sha256"]
    args = ProposeTeamArgs.model_validate(
        {
            "expert": "expert-council",
            "expert_sha256": digest,
            "members": [
                {
                    "name": "evidence",
                    "profile": "evidence-researcher",
                    "reason": "核对本次材料",
                },
                {
                    "name": "critic",
                    "profile": "critical-reviewer",
                },
            ],
        }
    )

    members = _materialize_team_members(args, expert=expert)
    evidence = members[0]
    evidence_state = cast("dict[str, Any]", evidence["state"])

    assert evidence["role"] == "收集并核验与问题直接相关的事实、来源、时间边界和证据缺口"
    assert evidence["reason"] == "核对本次材料"
    assert evidence_state["expert_profile"] == {
        "expert": "expert-council",
        "manifest_sha256": digest,
        "profile": "evidence-researcher",
        "label": "证据研究专家",
    }
    assert evidence_state["tool_patterns"] == [
        "list_files",
        "read_file",
        "read_text_file",
        "search_files",
        "read_pdf",
    ]
    assert "你是证据研究专家" in evidence_state["messages"][0]["content"]

    with pytest.raises(ValueError, match="不包含 profile unknown"):
        _materialize_team_members(
            ProposeTeamArgs.model_validate(
                {
                    "expert": "expert-council",
                    "expert_sha256": digest,
                    "members": [{"name": "unknown", "profile": "unknown"}],
                }
            ),
            expert=expert,
        )
    with pytest.raises(ValueError, match="role 由 profile 固化"):
        ProposeTeamArgs.model_validate(
            {
                "expert": "expert-council",
                "expert_sha256": digest,
                "members": [
                    {
                        "name": "spoofed",
                        "profile": "evidence-researcher",
                        "role": "模型临时编造的职责",
                    }
                ],
            }
        )
    corrupted = cast("dict[str, Any]", _initial_worker_state())
    corrupted["expert_profile"] = {"expert": "expert-council"}
    corrupted["tool_patterns"] = ["*"]
    with pytest.raises(ValueError, match="expert_profile 形状无效"):
        _worker_state(corrupted)


async def _drive_team_wake_until_review(
    *,
    conversation_id: UUID,
    task_id: UUID,
    gateway: object,
    registry: object,
    expected_attempt: int = 1,
) -> BoardTaskRecord:
    queue = InProcessRunQueue()
    ctx: dict[str, Any] = {
        "settings": get_settings(),
        "session_factory": session_factory,
        "bus": InMemoryRunBus(),
        "run_queue": queue,
        "cowork_gateway": gateway,
        "cowork_registry": registry,
    }
    try:
        for _ in range(40):
            await team_wake_dispatch_tick(ctx)
            tasks = await cowork_store().list_board_tasks(lead_conversation_id=conversation_id)
            task = next(item for item in tasks if item.id == task_id)
            if task.status in {"review", "blocked"} and task.attempt_count >= expected_attempt:
                return task
    finally:
        await queue.close()
    raise AssertionError("durable Team Worker wake 未在有界 tick 内收敛")


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
        semantic_approval_signing_key=_APPROVAL_SIGNING_KEY,
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
                            "write_delegation_scope": [{"path": str(tmp_path)}],
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
    assert interrupt.payload["payload"]["arguments"]["write_delegation_scope"] == [
        {"path": str(tmp_path)}
    ]
    assert "Worker 可写边界" in interrupt.payload["payload"]["warning"]

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
    assert team.write_delegation_scope == [{"path": str(tmp_path), "access_mode": "read_write"}]
    assert team.write_delegation_receipt is not None
    assert team.write_delegation_receipt["approval_inbox_id"] == str(item.id)
    workers = await cowork_store().list_team_workers(team_id=team.id)
    assert [worker.name for worker in workers] == ["files", "checks"]
    rows = store_sql(
        "SELECT status, active_task_id, state FROM cowork_team_worker_sessions ORDER BY created_at"
    )
    assert [row["status"] for row in rows] == ["idle", "idle"]
    assert all(row["active_task_id"] is None for row in rows)
    assert all(len(json.loads(str(row["state"]))["messages"]) == 1 for row in rows)


async def test_approved_expert_team_persists_specialist_identity_and_prompt(
    db_session: AsyncSession,
    store_sql,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Expert council")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="让证据专家和审阅专家独立会诊",
        budget_tokens=20_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    proposal = registry.parse_arguments(
        PROPOSE_TEAM_TOOL_NAME,
        {
            "expert": "expert-council",
            "expert_sha256": snapshot_persona(
                load_persona_catalog(Settings()).get("expert-council"),
                Settings(),
            )["sha256"],
            "members": [
                {"name": "evidence", "profile": "evidence-researcher"},
                {"name": "critic", "profile": "critical-reviewer"},
            ],
            "note": "按专家包固化身份",
        },
    )
    stale_proposal = {**proposal, "expert_sha256": "0" * 64}
    stale_call_id = "stale-expert-council"
    stale_context = replace(
        _context(
            db_session,
            gateway=_FinalWorkerGateway(),
            conversation_id=conversation_id,
            run_id=run.id,
            tool_call_id=stale_call_id,
        ),
        approved_call_ids=frozenset({stale_call_id}),
        approval_evidence={
            stale_call_id: build_trusted_approval_evidence(
                signing_key=_APPROVAL_SIGNING_KEY,
                source="user",
                run_id=run.id,
                tool_call_id=stale_call_id,
                tool=PROPOSE_TEAM_TOOL_NAME,
                arguments_sha256=arguments_sha256(stale_proposal),
                details={"inbox_id": str(uuid7()), "standing_rule_id": None},
            )
        },
    )
    with pytest.raises(ValueError, match="专家团定义已变化"):
        await registry.execute(
            PROPOSE_TEAM_TOOL_NAME,
            stale_proposal,
            context=stale_context,
        )
    assert await cowork_store().get_team_for_lead(lead_conversation_id=conversation_id) is None

    call_id = "approved-expert-council"
    context = replace(
        _context(
            db_session,
            gateway=_FinalWorkerGateway(),
            conversation_id=conversation_id,
            run_id=run.id,
            tool_call_id=call_id,
        ),
        approved_call_ids=frozenset({call_id}),
        approval_evidence={
            call_id: build_trusted_approval_evidence(
                signing_key=_APPROVAL_SIGNING_KEY,
                source="user",
                run_id=run.id,
                tool_call_id=call_id,
                tool=PROPOSE_TEAM_TOOL_NAME,
                arguments_sha256=arguments_sha256(proposal),
                details={"inbox_id": str(uuid7()), "standing_rule_id": None},
            )
        },
    )

    proposed = await registry.execute(PROPOSE_TEAM_TOOL_NAME, proposal, context=context)

    assert proposed.output["expert"] == "expert-council"
    assert proposed.output["workers"][0]["role"].startswith("收集并核验")
    assert proposed.output["workers"][0]["expert_profile"] == {
        "expert": "expert-council",
        "manifest_sha256": proposal["expert_sha256"],
        "profile": "evidence-researcher",
        "label": "证据研究专家",
    }
    rows = store_sql(
        """SELECT worker.name, session.state
           FROM cowork_team_worker_sessions AS session
           JOIN cowork_team_workers AS worker ON worker.id = session.worker_id"""
    )
    states = {str(row["name"]): json.loads(str(row["state"])) for row in rows}
    assert states["evidence"]["expert_profile"]["profile"] == "evidence-researcher"
    assert states["critic"]["expert_profile"]["profile"] == "critical-reviewer"
    assert "你是证据研究专家" in states["evidence"]["messages"][0]["content"]
    assert states["evidence"]["tool_patterns"] == [
        "list_files",
        "read_file",
        "read_text_file",
        "search_files",
        "read_pdf",
    ]


async def test_team_manage_requires_fresh_human_approval(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Human Team lifecycle")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="pause team only after a human approves",
        budget_tokens=10_000,
        budget_calls=5,
        budget_wall_ms=30_000,
        workflow_type="cowork",
    )
    await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="approved-team-proposal",
        note="",
        members=[
            {
                "name": "worker",
                "role": "wait for lifecycle control",
                "reason": "approval test",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    arguments = registry.parse_arguments(
        TEAM_MANAGE_TOOL_NAME,
        {"action": "pause", "reason": "human requested pause"},
    )
    base = _context(
        db_session,
        gateway=_FinalWorkerGateway(),
        conversation_id=conversation_id,
        run_id=run.id,
        tool_call_id="manage-pause",
    )

    with pytest.raises(Exception, match="尚未获得本次调用的用户批准"):
        await registry.execute(TEAM_MANAGE_TOOL_NAME, arguments, context=base)

    policy_context = replace(
        base,
        approved_call_ids=frozenset({"manage-pause"}),
        approval_evidence={
            "manage-pause": build_semantic_approval_evidence(
                signing_key=_APPROVAL_SIGNING_KEY,
                run_id=run.id,
                tool_call_id="manage-pause",
                tool=TEAM_MANAGE_TOOL_NAME,
                arguments_sha256=arguments_sha256(arguments),
                review_receipt_id="8" * 64,
            )
        },
    )
    with pytest.raises(ValueError, match="不可豁免的人工批准"):
        await registry.execute(TEAM_MANAGE_TOOL_NAME, arguments, context=policy_context)

    approved_context = replace(
        base,
        approved_call_ids=frozenset({"manage-pause"}),
        approval_evidence={
            "manage-pause": build_trusted_approval_evidence(
                signing_key=_APPROVAL_SIGNING_KEY,
                source="user",
                run_id=run.id,
                tool_call_id="manage-pause",
                tool=TEAM_MANAGE_TOOL_NAME,
                arguments_sha256=arguments_sha256(arguments),
                details={"inbox_id": str(uuid7()), "standing_rule_id": None},
            )
        },
    )
    result = await registry.execute(TEAM_MANAGE_TOOL_NAME, arguments, context=approved_context)
    assert result.output["status"] == "paused"
    assert result.output["action"] == "pause"
    team = await cowork_store().get_team_for_lead(lead_conversation_id=conversation_id)
    assert team is not None
    assert (team.status, team.pause_reason) == ("paused", "human requested pause")


async def test_write_scope_is_bound_to_non_waivable_team_approval_receipt(
    db_session: AsyncSession,
    tmp_path: Path,
    store_sql,
) -> None:
    delegated = tmp_path / "delegated"
    delegated.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    conversation_id = await ensure_conversation(db_session, title="Team write delegation")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="批准 Worker 只写 delegated",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    proposal = registry.parse_arguments(
        PROPOSE_TEAM_TOOL_NAME,
        {
            "members": [{"name": "writer", "role": "写入批准目录", "reason": "隔离委派"}],
            "note": "只允许明确批准的写目录",
            "write_delegation_scope": [{"path": str(delegated)}],
        },
    )
    worker_gateway = _WorkerGateway(outside)
    base = _context(
        db_session,
        gateway=worker_gateway,
        conversation_id=conversation_id,
        run_id=run.id,
        tool_call_id="proposal-policy",
    )
    policy_evidence = build_semantic_approval_evidence(
        signing_key=_APPROVAL_SIGNING_KEY,
        run_id=run.id,
        tool_call_id="proposal-policy",
        tool=PROPOSE_TEAM_TOOL_NAME,
        arguments_sha256=arguments_sha256(proposal),
        review_receipt_id="9" * 64,
    )
    with pytest.raises(ValueError, match="不可豁免的人工批准"):
        await registry.execute(
            PROPOSE_TEAM_TOOL_NAME,
            proposal,
            context=replace(
                base,
                approved_call_ids=frozenset({"proposal-policy"}),
                approval_evidence={"proposal-policy": policy_evidence},
            ),
        )

    proposal_call_id = "proposal-user"
    approved = replace(
        base,
        plan_step_id=uuid7(),
        tool_call_id=proposal_call_id,
        approved_call_ids=frozenset({proposal_call_id}),
        approval_evidence={
            proposal_call_id: build_trusted_approval_evidence(
                signing_key=_APPROVAL_SIGNING_KEY,
                source="user",
                run_id=run.id,
                tool_call_id=proposal_call_id,
                tool=PROPOSE_TEAM_TOOL_NAME,
                arguments_sha256=arguments_sha256(proposal),
                details={"inbox_id": str(uuid7()), "standing_rule_id": None},
            )
        },
    )
    proposed = await registry.execute(PROPOSE_TEAM_TOOL_NAME, proposal, context=approved)
    assert proposed.output["write_delegation_scope"] == [
        {"path": str(delegated.resolve()), "access_mode": "read_write"}
    ]
    assert proposed.output["write_delegation_receipt_id"]

    create_base = replace(
        base,
        plan_step_id=uuid7(),
        tool_call_id="create-read-only",
    )
    read_only = await registry.execute(
        BOARD_CREATE_TASK_TOOL_NAME,
        {
            "title": "只读检查",
            "description": "读取未委派写权限的目录",
            "acceptance_criteria": "给出报告",
            "resource_scope": [{"path": str(outside), "access_mode": "read_only"}],
        },
        context=create_base,
    )
    assert read_only.output["scope_receipt"] is None
    assert read_only.authorization_receipt is not None
    assert read_only.authorization_receipt["approval"]["required"] is False

    with pytest.raises(ValueError, match=r"超出用户.*批准"):
        await registry.execute(
            BOARD_CREATE_TASK_TOOL_NAME,
            {
                "title": "越权写入",
                "description": "尝试把未批准目录委派给 Worker",
                "acceptance_criteria": "不得执行",
                "resource_scope": [{"path": str(outside), "access_mode": "read_write"}],
            },
            context=replace(create_base, plan_step_id=uuid7(), tool_call_id="create-outside-write"),
        )

    async def create_write(call_id: str) -> dict[str, Any]:
        result = await registry.execute(
            BOARD_CREATE_TASK_TOOL_NAME,
            {
                "title": f"已批准写任务 {call_id}",
                "description": "只在批准目录内工作",
                "acceptance_criteria": "提交边界证据",
                "resource_scope": [{"path": str(delegated), "access_mode": "read_write"}],
            },
            context=replace(create_base, plan_step_id=uuid7(), tool_call_id=call_id),
        )
        assert result.output["scope_receipt"]["mechanism"] == "team_board_write_scope"
        assert result.authorization_receipt is not None
        assert any(
            item.get("mechanism") == "team_write_scope_receipt"
            for item in result.authorization_receipt["decisions"]
        )
        return result.output

    valid = await create_write("create-valid-write")
    assigned = await registry.execute(
        BOARD_ASSIGN_TASK_TOOL_NAME,
        {"task_id": valid["task_id"], "worker": "writer"},
        context=replace(create_base, plan_step_id=uuid7(), tool_call_id="assign-valid-write"),
    )
    assert assigned.output["status"] == "in_progress"
    assert assigned.output["assignment_state"] == "accepted_pending_worker"
    assert assigned.output["task_complete"] is False
    assert assigned.authorization_receipt is not None
    assert any(
        item.get("mechanism") == "team_write_scope_receipt"
        for item in assigned.authorization_receipt["decisions"]
    )
    completed = await _drive_team_wake_until_review(
        conversation_id=conversation_id,
        task_id=UUID(str(valid["task_id"])),
        gateway=worker_gateway,
        registry=registry,
    )
    assert completed.status == "review"
    assignment = json.loads(worker_gateway.histories[0][-1].content)
    assert assignment["worker_identity"] == {
        "name": "writer",
        "role": "写入批准目录",
        "reason": "隔离委派",
        "expert_profile": None,
    }

    tampered = await create_write("create-tampered-write")
    store_sql(
        "UPDATE cowork_board_tasks SET resource_scope = ? WHERE id = ?",
        (
            json.dumps([{"path": str(outside.resolve()), "access_mode": "read_write"}]),
            tampered["task_id"],
        ),
    )
    with pytest.raises(ValueError, match="创建后发生变化"):
        await registry.execute(
            BOARD_ASSIGN_TASK_TOOL_NAME,
            {"task_id": tampered["task_id"], "worker": "writer"},
            context=replace(
                create_base,
                plan_step_id=uuid7(),
                tool_call_id="assign-tampered-write",
            ),
        )
    row = store_sql(
        "SELECT status, attempt_count FROM cowork_board_tasks WHERE id = ?",
        (tampered["task_id"],),
    )[0]
    assert row == {"status": "open", "attempt_count": 0}

    rotated = await create_write("create-rotated-grant")
    grants = await list_capability_grants(db_session, conversation_id=conversation_id)
    write_grant = next(grant for grant in grants if grant.capability == "filesystem.write")
    assert write_grant.session_root_id is not None
    assert await revoke_capability_grant(
        db_session,
        conversation_id=conversation_id,
        grant_id=write_grant.id,
    )
    replacement = await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="filesystem.write",
        session_root_id=write_grant.session_root_id,
        grant_source="user",
    )
    assert replacement.id != write_grant.id
    with pytest.raises(ValueError, match="grant identity"):
        await registry.execute(
            BOARD_ASSIGN_TASK_TOOL_NAME,
            {"task_id": rotated["task_id"], "worker": "writer"},
            context=replace(
                create_base,
                plan_step_id=uuid7(),
                tool_call_id="assign-rotated-grant",
            ),
        )


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

    assert assigned.output["status"] == "in_progress"
    assert assigned.output["assignment_state"] == "accepted_pending_worker"
    assert assigned.output["next_signal"] == "wait_for_durable_lead_wake"
    completed = await _drive_team_wake_until_review(
        conversation_id=conversation_id,
        task_id=task_id,
        gateway=gateway,
        registry=registry,
    )
    assert completed.status == "review"
    assert "越界读取被拒绝" in str(completed.worker_report)
    first_history = gateway.histories[0]
    user_messages = [message for message in first_history if message.role == "user"]
    assert len(user_messages) == 1
    envelope = json.loads(user_messages[0].content)
    assert set(envelope) == {
        "worker_identity",
        "task_description",
        "acceptance_criteria",
        "resource_scope",
    }
    assert envelope["worker_identity"] == {
        "name": "files",
        "role": "核对文件边界",
        "reason": "独立执行",
        "expert_profile": None,
    }
    assert envelope["resource_scope"] == [
        {"path": str(allowed.resolve()), "access_mode": "read_only"}
    ]
    assert "LEAD_SECRET_SHOULD_NOT_REACH_WORKER" not in json.dumps(
        [message.content for message in first_history], ensure_ascii=False
    )
    tool_result = gateway.histories[1][-1]
    assistant_message = gateway.histories[1][-2]
    assert assistant_message.tool_calls[0].thought_signature == "worker-gemini-signature"
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
    assert reassigned.output["status"] == "in_progress"
    assert reassigned.output["attempt_count"] == 2
    completed_retry = await _drive_team_wake_until_review(
        conversation_id=conversation_id,
        task_id=task_id,
        gateway=gateway,
        registry=registry,
        expected_attempt=2,
    )
    assert completed_retry.status == "review"
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


async def test_board_assignment_resumes_persisted_worker_state_after_process_restart(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Team worker resume")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_only",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="恢复崩溃前的 Worker task",
        budget_tokens=20_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    _, workers = await cowork_store().create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="resume-proposal",
        note="",
        members=[
            {
                "name": "resumer",
                "role": "恢复任务",
                "reason": "验证 checkpoint",
                "state": cast("dict[str, Any]", _initial_worker_state()),
            }
        ],
    )
    task = await cowork_store().create_board_task(
        lead_conversation_id=conversation_id,
        title="继续分析",
        description="不能重复初始化 assignment",
        acceptance_criteria="保留崩溃前状态",
        resource_scope=[{"path": str(tmp_path), "access_mode": "read_only"}],
    )
    started, _, session = await cowork_store().start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=workers[0].name,
        assignment_call_id="resume-assignment",
        source_run_id=run.id,
    )
    state = _initial_worker_state()
    state["messages"].append(
        {
            "role": "user",
            "content": "persisted-before-crash",
            "tool_calls": [],
            "tool_call_id": None,
        }
    )
    state["status"] = "active"
    state["active_task_id"] = str(started.id)
    state["rounds_used"] = 2
    state["calls_used"] = 1
    await cowork_store().save_team_worker_session(
        session_id=session.id,
        task_id=started.id,
        state=cast("dict[str, Any]", state),
    )

    # 用全新 registry/gateway 模拟进程重启；同 assignment_call_id 必须接续，而非重开。
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    gateway = _FinalWorkerGateway()
    resumed = await registry.execute(
        BOARD_ASSIGN_TASK_TOOL_NAME,
        {"task_id": str(started.id), "worker": "resumer"},
        context=_context(
            db_session,
            gateway=gateway,
            conversation_id=conversation_id,
            run_id=run.id,
            tool_call_id="resume-assignment",
        ),
    )

    assert resumed.output["status"] == "in_progress"
    assert resumed.output["attempt_count"] == 1
    completed = await _drive_team_wake_until_review(
        conversation_id=conversation_id,
        task_id=started.id,
        gateway=gateway,
        registry=registry,
    )
    assert completed.status == "review"
    assert len(gateway.histories) == 1
    assert [message.content for message in gateway.histories[0]].count(
        "persisted-before-crash"
    ) == 1
    persisted = await cowork_store().list_team_workers(team_id=started.team_id)
    assert persisted[0].name == "resumer"


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
        event.type == "run.done" and event.payload["status"] == "partial" for event in events
    )
