from typing import Any

import pytest

from app.agent_core.hooks import (
    AsyncHookBus,
    AsyncHookPipeline,
    DuplicateHookIdError,
    SyncHookBus,
)
from app.agent_core.loop import (
    AgentActionInfo,
    AgentActionPhase,
    AgentLoopConfig,
    AgentLoopLane,
    AgentToolActionInfo,
    AgentToolActionPhase,
    ToolActionEventHook,
    ToolActionUpdateHook,
    ToolBatchExecutionMode,
    ToolBatchResult,
    run_tool_loop,
)


async def test_async_hook_pipeline_is_stably_ordered_and_transforming() -> None:
    pipeline = AsyncHookPipeline[list[str]]()

    async def append(value: list[str], marker: str) -> list[str]:
        return [*value, marker]

    pipeline.register("later-b", lambda value: append(value, "b"), order=20)
    pipeline.register("later-a", lambda value: append(value, "a"), order=20)
    pipeline.register("first", lambda value: append(value, "first"), order=0)

    assert pipeline.ids() == ("first", "later-a", "later-b")
    assert await pipeline.run([]) == ["first", "a", "b"]

    with pytest.raises(DuplicateHookIdError):
        pipeline.register("first", lambda value: append(value, "duplicate"))


def test_sync_hook_bus_has_the_same_registration_contract() -> None:
    observed: list[str] = []
    bus = SyncHookBus[str]()
    bus.register("second", lambda value: observed.append(f"second:{value}"), order=10)
    bus.register("first", lambda value: observed.append(f"first:{value}"), order=0)

    bus.emit("event")

    assert bus.ids() == ("first", "second")
    assert observed == ["first:event", "second:event"]


async def test_async_hook_bus_observes_in_stable_order_without_replacing_value() -> None:
    observed: list[str] = []
    bus = AsyncHookBus[str]()

    async def observe(value: str, marker: str) -> None:
        observed.append(f"{marker}:{value}")

    bus.register("second", lambda value: observe(value, "second"), order=10)
    bus.register("first", lambda value: observe(value, "first"), order=0)

    await bus.emit("event")

    assert bus.ids() == ("first", "second")
    assert observed == ["first:event", "second:event"]


async def test_agent_loop_runs_inner_tools_then_outer_follow_up() -> None:
    async def decide(state: dict[str, object]) -> dict[str, object]:
        events = list(state["events"])
        events.append("decide")
        return {**state, "events": events, "pending": True}

    async def execute(state: dict[str, object]) -> dict[str, object]:
        events = list(state["events"])
        events.append("tool")
        return {**state, "events": events, "active": False, "pending": False}

    async def steering(state: dict[str, object]) -> dict[str, object]:
        events = list(state["events"])
        events.append("steering")
        return {**state, "events": events}

    async def follow_up(state: dict[str, object]) -> dict[str, object] | None:
        if state["followed"]:
            return None
        events = list(state["events"])
        events.append("follow-up")
        return {**state, "events": events, "active": True, "followed": True}

    result = await run_tool_loop(
        {"active": True, "pending": False, "followed": False, "events": []},
        decide=decide,
        execute_tools=execute,
        is_active=lambda state: bool(state["active"]),
        has_pending_tools=lambda state: bool(state["pending"]),
        config=AgentLoopConfig(
            get_steering_messages=steering,
            get_follow_up_messages=follow_up,
        ),
    )

    assert result["events"] == [
        "steering",
        "decide",
        "tool",
        "follow-up",
        "steering",
        "decide",
        "tool",
    ]


async def test_agent_loop_exposes_prepare_dispatch_materialize_execute_actions() -> None:
    action_events: list[str] = []

    async def dispatch(state: dict[str, object]) -> str:
        assert state["prepared"] is True
        return "model-result"

    async def materialize(state: dict[str, object], decision: str) -> dict[str, object]:
        assert decision == "model-result"
        return {**state, "pending": True}

    async def execute(state: dict[str, object]) -> dict[str, object]:
        return {**state, "pending": False, "active": False}

    async def prepare(state: dict[str, object]) -> dict[str, object]:
        return {**state, "prepared": True}

    async def record(action: AgentActionInfo, phase: AgentActionPhase) -> None:
        action_events.append(f"{action.kind}:{phase}")

    result = await run_tool_loop(
        {"active": True, "pending": False, "prepared": False},
        dispatch=dispatch,
        materialize=materialize,
        execute_tools=execute,
        is_active=lambda state: bool(state["active"]),
        has_pending_tools=lambda state: bool(state["pending"]),
        config=AgentLoopConfig(
            transform_context=prepare,
            action_info=lambda state, kind: AgentActionInfo(
                kind=kind,
                operation_id=f"test:{kind}",
                iteration=0,
            ),
            action_event=record,
        ),
    )

    assert result["active"] is False
    assert action_events == [
        "prepare:started",
        "prepare:completed",
        "dispatch:started",
        "dispatch:completed",
        "materialize:started",
        "materialize:completed",
        "execute:started",
        "execute:completed",
    ]


async def test_agent_loop_lane_can_be_peeked_and_driven_one_action_at_a_time() -> None:
    async def prepare(state: dict[str, object]) -> dict[str, object]:
        return {**state, "prepared": True}

    async def dispatch(state: dict[str, object]) -> str:
        assert state["prepared"] is True
        return "decision"

    async def materialize(state: dict[str, object], decision: str) -> dict[str, object]:
        assert decision == "decision"
        return {**state, "pending": True}

    async def execute(state: dict[str, object]) -> dict[str, object]:
        return {**state, "pending": False, "active": False}

    lane = AgentLoopLane[dict[str, object], str](
        {"active": True, "pending": False, "prepared": False},
        dispatch=dispatch,
        materialize=materialize,
        execute_tools=execute,
        is_active=lambda state: bool(state["active"]),
        has_pending_tools=lambda state: bool(state["pending"]),
        config=AgentLoopConfig(transform_context=prepare),
    )

    observed: list[str] = []
    while (action := lane.peek_action()).kind != "done":
        observed.append(action.kind)
        await lane.execute_action(action)

    assert observed == ["prepare", "dispatch", "materialize", "execute"]
    assert lane.state["active"] is False


async def test_agent_loop_lane_rejects_a_stale_manual_action() -> None:
    async def decide(state: dict[str, object]) -> dict[str, object]:
        return {**state, "active": False}

    async def execute(state: dict[str, object]) -> dict[str, object]:
        return state

    lane = AgentLoopLane[dict[str, object], object](
        {"active": True, "pending": False},
        decide=decide,
        execute_tools=execute,
        is_active=lambda state: bool(state["active"]),
        has_pending_tools=lambda state: bool(state["pending"]),
    )
    prepare = lane.peek_action()
    await lane.execute_action(prepare)

    with pytest.raises(ValueError, match="manual drive action 已变化"):
        await lane.execute_action(prepare)


async def test_agent_loop_owns_tool_batch_mode_events_and_explicit_termination() -> None:
    decisions = 0
    tool_events: list[str] = []
    tool_updates: list[str] = []

    async def decide(state: dict[str, object]) -> dict[str, object]:
        nonlocal decisions
        decisions += 1
        return {**state, "pending": True}

    async def execute_batch(
        state: dict[str, object],
        mode: ToolBatchExecutionMode,
        emit: ToolActionEventHook | None,
        update: ToolActionUpdateHook | None,
    ) -> ToolBatchResult[dict[str, object]]:
        assert mode == "parallel"
        assert callable(emit)
        assert callable(update)
        for index, name in enumerate(("read_a", "read_b")):
            info = AgentToolActionInfo(
                operation_id=f"tool:{index}",
                tool_call_id=f"call:{index}",
                tool_name=name,
                index=index,
            )
            await emit(info, "started")
            await update(info, "download.progress", {"percent": 50})
            await emit(info, "completed")
        return ToolBatchResult(state={**state, "pending": False}, terminate=True)

    async def record_tool(tool: AgentToolActionInfo, phase: AgentToolActionPhase) -> None:
        tool_events.append(f"{tool.tool_name}:{phase}")

    async def record_update(
        tool: AgentToolActionInfo,
        update_type: str,
        payload: dict[str, Any],
    ) -> None:
        tool_updates.append(f"{tool.tool_name}:{update_type}:{payload['percent']}")

    result = await run_tool_loop(
        {"active": True, "pending": False},
        decide=decide,
        execute_tool_batch=execute_batch,
        is_active=lambda state: bool(state["active"]),
        has_pending_tools=lambda state: bool(state["pending"]),
        config=AgentLoopConfig(
            tool_execution_mode=lambda state: "parallel",
            tool_action_event=record_tool,
            tool_action_update=record_update,
        ),
    )

    assert result["pending"] is False
    assert decisions == 1
    assert tool_events == [
        "read_a:started",
        "read_a:completed",
        "read_b:started",
        "read_b:completed",
    ]
    assert tool_updates == [
        "read_a:download.progress:50",
        "read_b:download.progress:50",
    ]
