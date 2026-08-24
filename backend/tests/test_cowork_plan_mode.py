"""计划模式：先出方案、经用户批准再动手。

重点不是"模型愿不愿意听话"，而是批准之前写工具在两道边界上都过不去：下发目录里没有，
执行边界也会拒绝。提示词只负责让模型知道自己在哪个阶段。
"""

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid6 import uuid7

from app.core.config import get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.interactions import (
    create_inbox_item,
    get_pending_inbox_item,
    resolve_inbox_item,
)
from app.cowork.permissions import create_session_root
from app.cowork.plans import (
    PLAN_TOOL_NAME,
    ProposePlanArgs,
    normalize_mode,
    plan_steps,
    plan_todos,
)
from app.cowork.runtime import (
    _ephemeral_context,
    _system_prompt,
    initialize_cowork_state,
    load_cowork_checkpoint,
    resume_cowork_after_human,
)
from app.cowork.tools import build_default_cowork_registry
from app.runstore.checkpoints import ensure_plan
from app.runstore.runs import append_message, create_run, ensure_conversation, get_run, list_events
from app.worker.cowork_run import cowork_run
from tests.test_cowork_runner import (
    NativeToolProvider,
    _final_completion,
    _tool_completion,
)
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import ToolCall, ToolDefinition

pytestmark = pytest.mark.integration


def test_plan_mode_allows_reading_and_asking_but_nothing_that_changes_anything() -> None:
    """判据是副作用落在哪里，不是一张会漏掉新成员的工具名单。"""

    registry = build_default_cowork_registry()

    for name in ("read_text_file", "list_files", "search_files", "read_pdf"):
        assert registry.plan_mode_allows(name), name
    # 交互工具每一次都要用户当场点头，本身就是征求同意的动作。
    for name in ("ask_user", "request_directory", "request_capability", PLAN_TOOL_NAME):
        assert registry.plan_mode_allows(name), name
    for name in (
        "write_text_file",
        "create_artifact",
        "run_shell",
    ):
        assert not registry.plan_mode_allows(name), name
    assert not registry.plan_mode_allows("不存在的工具")


def test_plan_mode_catalog_drops_writes_and_always_offers_the_plan_tool() -> None:
    registry = build_default_cowork_registry()
    full = registry.tool_definitions_for("把这些财务表整理成一份报告")
    assert any(item.name == "write_text_file" for item in full)

    planning = registry.plan_mode_definitions(full)
    names = {item.name for item in planning}

    assert "write_text_file" not in names
    assert "run_shell" not in names
    # propose_plan 不进 core 目录（执行模式下是纯噪声），所以必须由这里补上。
    assert PLAN_TOOL_NAME in names
    assert "read_text_file" in names


def test_plan_mode_definitions_do_not_duplicate_the_plan_tool() -> None:
    registry = build_default_cowork_registry()
    spec = registry.get(PLAN_TOOL_NAME)
    definitions = [
        ToolDefinition(
            name=spec.name, description=spec.description, parameters=spec.resolved_input_schema()
        )
    ]

    names = [item.name for item in registry.plan_mode_definitions(definitions)]

    assert names == [PLAN_TOOL_NAME]


def test_unknown_mode_falls_back_to_execute() -> None:
    """老 checkpoint 没有这个字段；判成计划模式会让在跑的任务卡在没人批准的计划上。"""

    assert normalize_mode(None) == "execute"
    assert normalize_mode("") == "execute"
    assert normalize_mode("planning") == "execute"
    assert normalize_mode("plan") == "plan"
    assert normalize_mode("execute") == "execute"


def test_plan_reminder_is_per_turn_because_the_mode_flips_mid_run() -> None:
    """批准会在 run 中途把 plan 翻成 execute，所以这段提醒不能烤进 system prompt。"""

    planning = _ephemeral_context(mode="plan", todos=[])
    assert "<plan_mode>" in planning
    assert "propose_plan" in planning
    assert "<plan_mode>" not in _ephemeral_context(mode="execute", todos=[])
    assert "<plan_mode>" not in _system_prompt("")


def test_approved_plan_becomes_the_checklist() -> None:
    """计划只留在消息里，压缩一次模型就忘了自己承诺过什么。"""

    request = ProposePlanArgs(
        summary="整理财务表并出报告",
        steps=["读取三张表", "汇总成月度数据", "写出 report.md"],
    ).model_dump()

    todos = plan_todos(plan_steps(request))

    assert [item["content"] for item in todos] == ["读取三张表", "汇总成月度数据", "写出 report.md"]
    assert {item["status"] for item in todos} == {"pending"}
    # 坏掉的载荷不该让恢复流程整个失败。
    assert plan_steps({"steps": ["ok", "", 7, None]}) == ["ok"]
    assert plan_steps({}) == []


async def test_rejected_plan_carries_the_users_edits_back_to_the_model(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """只回一个"被拒绝"，模型会原样再提一遍同一个计划。"""

    conversation_id = await ensure_conversation(db_session, title="计划退回")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理财务表",
        budget_tokens=20_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    step_id = uuid7()
    await ensure_plan(
        db_session,
        run_id=run.id,
        steps=[
            {
                "id": str(step_id),
                "idx": 0,
                "description": "等待批准计划",
                "tool": "propose_plan",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    item = await create_inbox_item(
        db_session,
        run_id=run.id,
        conversation_id=conversation_id,
        kind="plan_approval",
        tool_call_id="plan-1",
        plan_step_id=step_id,
        request={"summary": "先做 A", "steps": ["A"]},
    )

    rejected, response = await resolve_inbox_item(
        db_session, item=item, approved=False, answer="别动原表，另存一份"
    )

    assert rejected.status == "rejected"
    assert response["feedback"] == "别动原表，另存一份"


async def test_plan_mode_refuses_writes_then_executes_the_approved_plan(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    """一条完整路径：越权被拦 → 提计划暂停 → 批准 → 解锁并落盘。"""

    conversation_id = await ensure_conversation(db_session, title="计划模式")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="把结论写进 notes.md",
        budget_tokens=40_000,
        budget_calls=30,
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
    await db_session.commit()

    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(
        db_session, run_id=run.id, registry=registry, bus=bus, plan_mode=True
    )

    target = tmp_path / "notes.md"
    write_arguments = json.dumps({"path": str(target), "content": "结论"}, ensure_ascii=False)
    provider = NativeToolProvider(
        [
            # 1. 计划阶段抢跑写文件：必须被拦下，且文件不能出现。
            _tool_completion(
                ToolCall(id="write-early", name="write_text_file", arguments=write_arguments)
            ),
            # 2. 改为提交计划：运行在这里暂停。
            _tool_completion(
                ToolCall(
                    id="plan-1",
                    name=PLAN_TOOL_NAME,
                    arguments=json.dumps(
                        {
                            "summary": "写一份结论笔记",
                            "steps": ["确认目录", "写入 notes.md"],
                            "notes": "只新增文件，不改已有内容",
                        },
                        ensure_ascii=False,
                    ),
                )
            ),
            # 3. 批准之后同一个调用才落盘。
            _tool_completion(
                ToolCall(id="write-after", name="write_text_file", arguments=write_arguments)
            ),
            _final_completion("已经写好 notes.md。"),
        ]
    )
    context = {
        "settings": get_settings().model_copy(
            update={
                "cowork_decision_max_tokens": 2048,
                "run_heartbeat_s": 60.0,
            }
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    waiting = await get_run(db_session, run.id)
    assert waiting is not None and waiting.status == "waiting_human"
    assert not target.exists()

    # 计划阶段下发的目录里就没有写工具，模型伪造的调用也拿到了可执行的纠正指令。
    assert all(item.name != "write_text_file" for item in provider.last_tools)
    blocked_result = next(
        message
        for message in provider.tool_histories[1]
        if message.role == "tool" and message.tool_call_id == "write-early"
    )
    assert "propose_plan" in blocked_result.content
    assert "计划模式" in blocked_result.content

    events = await list_events(db_session, run_id=run.id, limit=200)
    interrupt = next(event for event in events if event.type == "interrupt")
    assert interrupt.payload["kind"] == "plan_approval"
    assert interrupt.payload["payload"]["steps"] == ["确认目录", "写入 notes.md"]

    item = await get_pending_inbox_item(
        db_session, run_id=run.id, resume_token=UUID(str(interrupt.payload["resume_token"]))
    )
    assert item is not None
    item, response = await resolve_inbox_item(db_session, item=item, approved=True)
    state = await resume_cowork_after_human(db_session, run_id=run.id, item=item, response=response)
    await db_session.commit()

    # 批准是运行时状态的翻转，批准的步骤同时成为清单。
    assert state["mode"] == "execute"
    assert [todo["content"] for todo in state["todos"]] == ["确认目录", "写入 notes.md"]

    await cowork_run(context, str(run.id))

    done = await get_run(db_session, run.id)
    assert done is not None and done.status == "done"
    assert target.read_text(encoding="utf-8") == "结论"

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None and checkpoint.state["mode"] == "execute"
    # 批准后的那一轮：计划提醒消失，批准的步骤作为清单出现在末尾的临时块里。
    # 两者都不在 system prompt 里——它在一次 run 内必须逐字不变。
    resumed = provider.tool_histories[-1]
    assert "<plan_mode>" not in resumed[0].content
    assert "写入 notes.md" not in resumed[0].content
    assert resumed[-1].role == "user"
    assert "<plan_mode>" not in resumed[-1].content
    assert "写入 notes.md" in resumed[-1].content
    # 计划阶段那一轮的末尾块里应当有提醒。
    assert "<plan_mode>" in provider.tool_histories[1][-1].content
