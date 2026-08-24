"""只读子 Agent 的进度必须进事件流。

`explore` 一次最多四轮模型调用加八次工具调用。没有这些事件，用户在整段时间里只看得到
时间线上一张不动的卡片，事后也查不到这次委派花了多少——而"委派到底省了还是费了"正是
决定要不要往可写子 Agent 走的那个判据（docs/15 §5 条件 3）。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.permissions import create_session_root
from app.cowork.runtime import initialize_cowork_state
from app.cowork.subagent import register_readonly_subagent
from app.cowork.tools import build_default_cowork_registry
from app.runstore.runs import append_message, create_run, ensure_conversation, list_events
from app.worker.cowork_run import cowork_run
from tests.test_cowork_runner import NativeToolProvider, _final_completion, _tool_completion
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import ToolCall

pytestmark = pytest.mark.integration


async def test_explore_reports_its_rounds_and_its_own_token_bill_on_the_event_stream(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, store_sql
) -> None:
    (tmp_path / "notes.md").write_text("# 记事\n", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="Cowork explore")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="这个目录里有什么",
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
    register_readonly_subagent(registry)
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="load-explore",
                    name="load_tools",
                    arguments=json.dumps({"names": ["explore"]}),
                )
            ),
            _tool_completion(
                ToolCall(
                    id="explore-1",
                    name="explore",
                    arguments=json.dumps(
                        {"question": "这个目录里有什么", "max_rounds": 1}, ensure_ascii=False
                    ),
                )
            ),
            # 这一条是**子 Agent** 要的工具：它和主循环共用同一个网关，所以也共用这条队列。
            _tool_completion(
                ToolCall(
                    id="sub-1",
                    name="list_files",
                    arguments=json.dumps({"path": str(tmp_path)}),
                )
            ),
            _final_completion("目录里只有一份记事。"),
        ],
        regular_completions=["结论：只有 notes.md。证据：list_files。不确定项：无。"],
    )
    context = {
        "settings": get_settings().model_copy(update={"cowork_decision_max_tokens": 2048}),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    events = await list_events(db_session, run_id=run.id, limit=200)
    progress = [event for event in events if event.type == "subagent.progress"]
    phases = [event.payload["phase"] for event in progress]

    assert phases[0] == "started"
    assert phases[-1] == "finished"
    assert "tool" in phases
    assert progress[-1].payload["status"] == "round_limit"
    # 委派花掉的那一份账要单独看得见，否则事后只有主循环的总量。
    assert progress[-1].payload["used_tokens"] > 0
    assert {event.payload["tool_call_id"] for event in progress} == {"explore-1"}

    # 进度必须落在这次调用**结束之前**：攒到 tool.result 一起发，等于没发。
    order = [event.type for event in events]
    result_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "tool.result" and event.payload.get("tool") == "explore"
    )
    assert order.index("subagent.progress") < result_index
