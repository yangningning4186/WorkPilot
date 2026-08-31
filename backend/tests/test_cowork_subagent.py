import asyncio
import dataclasses
import json
from typing import Any, cast

import pytest
from uuid6 import uuid7

from app.core.config import Settings
from app.cowork.subagent import register_readonly_subagent
from app.cowork.tools import CoworkToolContext, CoworkToolError, build_default_cowork_registry
from workpilot_ai.types import CompletionResult, Message, ToolCall, ToolDefinition, Usage


class _InjectingGateway:
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
        if len(self.histories) == 1:
            return CompletionResult(
                text="",
                model="fake-chat",
                provider="deterministic_test",
                usage=Usage(input_tokens=3, output_tokens=2),
                tool_calls=(
                    ToolCall(
                        id="injected-write",
                        name="write_text_file",
                        arguments=json.dumps({"path": "/tmp/escape.txt", "content": "owned"}),
                        thought_signature="subagent-gemini-signature",
                    ),
                ),
            )
        return CompletionResult(
            text="没有执行未授权工具。",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=3, output_tokens=2),
        )


@pytest.mark.asyncio
async def test_registry_rejects_tool_outside_execution_allowlist() -> None:
    registry = build_default_cowork_registry()

    with pytest.raises(CoworkToolError, match="不在本次允许执行的工具集合中"):
        await registry.execute(
            "write_text_file",
            {},
            context=cast("CoworkToolContext", object()),
            allowed=frozenset({"read_text_file"}),
        )


@pytest.mark.asyncio
async def test_readonly_subagent_cannot_execute_undeclared_write_tool() -> None:
    registry = build_default_cowork_registry()
    register_readonly_subagent(registry)
    gateway = _InjectingGateway()

    result = await registry.execute(
        "explore",
        {"question": "检查项目结构", "max_rounds": 2},
        context=CoworkToolContext(
            session=cast("Any", object()),
            gateway=cast("Any", gateway),
            settings=Settings(),
            conversation_id=uuid7(),
            run_id=uuid7(),
            worker_id="test-worker",
            plan_step_id=uuid7(),
            tool_call_id="explore-call",
        ),
    )

    assert result.output["answer"] == "没有执行未授权工具。"
    assistant_message = gateway.histories[1][-2]
    assert assistant_message.tool_calls[0].thought_signature == "subagent-gemini-signature"
    tool_message = gateway.histories[1][-1]
    assert tool_message.role == "tool"
    payload = json.loads(tool_message.content)
    assert payload["ok"] is False
    assert "不在本次允许执行的工具集合中" in payload["error"]


class _CountingGateway:
    """每一轮都点名一只只读工具，永不自己收尾——用来逼出轮次与调用上限的行为。"""

    def __init__(self, *, tool_name: str = "list_files") -> None:
        self.tool_name = tool_name
        self.tool_rounds = 0
        self.summaries = 0

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
        del messages, tools, parallel_tool_calls, task_type, max_tokens, temperature
        self.tool_rounds += 1
        return CompletionResult(
            text="",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=10, output_tokens=5),
            tool_calls=(
                ToolCall(
                    id=f"call-{self.tool_rounds}",
                    name=self.tool_name,
                    arguments=json.dumps({"path": "/tmp"}),
                ),
            ),
        )

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        del messages, task_type, max_tokens, temperature
        self.summaries += 1
        return CompletionResult(
            text="结论/证据/不确定项",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=7, output_tokens=3),
        )


class _ConcurrentGateway:
    """两条分支都进入模型调用后才放行；串行实现会直接超时。"""

    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0
        self.both_started = asyncio.Event()
        self.histories: dict[str, list[Message]] = {}
        self.tool_names: set[str] = set()

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
        question = messages[1].content
        self.histories[question] = list(messages)
        self.tool_names.update(tool.name for tool in tools)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        if self.active == 2:
            self.both_started.set()
        await self.both_started.wait()
        self.active -= 1
        return CompletionResult(
            text=f"回答：{question}",
            model="fake-chat",
            provider="deterministic_test",
            usage=Usage(input_tokens=4, output_tokens=2),
        )


def _context(
    gateway: object,
    *,
    cancel_event: asyncio.Event | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> CoworkToolContext:
    async def emit(name: str, payload: dict[str, Any]) -> None:
        assert events is not None
        events.append((name, payload))

    return CoworkToolContext(
        session=cast("Any", object()),
        gateway=cast("Any", gateway),
        settings=Settings(),
        conversation_id=uuid7(),
        run_id=uuid7(),
        worker_id="test-worker",
        plan_step_id=uuid7(),
        tool_call_id="explore-call",
        cancel_event=cancel_event,
        emit_progress=None if events is None else emit,
    )


@pytest.mark.asyncio
async def test_multiple_explores_run_concurrently_with_isolated_histories() -> None:
    registry = build_default_cowork_registry()
    register_readonly_subagent(registry)
    gateway = _ConcurrentGateway()
    base_context = _context(gateway)

    assert registry.get("explore").parallel_safe is True
    assert registry.parallel_safe(["explore", "explore"]) is True

    first, second = await asyncio.wait_for(
        asyncio.gather(
            registry.execute(
                "explore",
                {"question": "只调查模块 A", "max_rounds": 1},
                context=dataclasses.replace(base_context, tool_call_id="explore-a"),
            ),
            registry.execute(
                "explore",
                {"question": "只调查模块 B", "max_rounds": 1},
                context=dataclasses.replace(base_context, tool_call_id="explore-b"),
            ),
        ),
        timeout=1,
    )

    assert gateway.peak_active == 2
    assert first.output["answer"] == "回答：只调查模块 A"
    assert second.output["answer"] == "回答：只调查模块 B"
    assert [
        message.content for message in gateway.histories["只调查模块 A"] if message.role == "user"
    ] == ["只调查模块 A"]
    assert [
        message.content for message in gateway.histories["只调查模块 B"] if message.role == "user"
    ] == ["只调查模块 B"]
    # read/effect=none 仍不等于并发安全；共享浏览器状态和 todo 写入都不能进入分支工具集。
    assert "browser_snapshot" not in gateway.tool_names
    assert "todo_write" not in gateway.tool_names
    assert gateway.tool_names
    assert all(registry.get(name).parallel_safe for name in gateway.tool_names)


@pytest.mark.asyncio
async def test_subagent_stops_at_the_next_call_after_the_user_hits_stop() -> None:
    """取消要在下一次调用之前生效，而不是等整轮调查跑完。

    主循环的取消判定在两批工具之间；explore 一次就是四轮模型调用加八次工具调用，
    子 Agent 自己不看这面旗，用户按下停止之后仍要干等一整轮。
    """

    registry = build_default_cowork_registry()
    register_readonly_subagent(registry)
    gateway = _CountingGateway()
    cancel = asyncio.Event()
    cancel.set()

    result = await registry.execute(
        "explore",
        {"question": "调查项目结构", "max_rounds": 4},
        context=_context(gateway, cancel_event=cancel),
    )

    assert result.output["status"] == "cancelled"
    # 一次模型调用都不该发生：旗子在进第一轮之前就已经竖起来了。
    assert gateway.tool_rounds == 0
    assert gateway.summaries == 0


@pytest.mark.asyncio
async def test_subagent_cancels_between_tool_calls_and_keeps_the_evidence() -> None:
    registry = build_default_cowork_registry()
    register_readonly_subagent(registry)
    gateway = _CountingGateway()
    cancel = asyncio.Event()
    events: list[tuple[str, dict[str, Any]]] = []
    context = _context(gateway, cancel_event=cancel, events=events)

    # 第一轮跑完（工具会因为没有授权目录而失败，但轮次照样计），随后用户按停止。
    async def stop_after_first_round(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))
        if payload["phase"] == "tool":
            cancel.set()

    result = await registry.execute(
        "explore",
        {"question": "调查项目结构", "max_rounds": 4},
        context=cast(
            "CoworkToolContext",
            dataclasses.replace(context, emit_progress=stop_after_first_round),
        ),
    )

    assert result.output["status"] == "cancelled"
    assert gateway.tool_rounds == 1
    assert gateway.summaries == 0
    assert result.output["rounds"] == 1
    assert result.output["calls_used"] == 1
    # 中止不该把已经花掉的账一起丢掉。
    assert result.output["used_tokens"] == 15
    assert [payload["phase"] for _, payload in events][-1] == "finished"


@pytest.mark.asyncio
async def test_subagent_reports_its_own_budget_and_progress_events() -> None:
    """委派了多少钱必须单独看得见，否则事后只有主循环的总量。"""

    registry = build_default_cowork_registry()
    register_readonly_subagent(registry)
    gateway = _CountingGateway()
    events: list[tuple[str, dict[str, Any]]] = []

    result = await registry.execute(
        "explore",
        {"question": "调查项目结构", "max_rounds": 2},
        context=_context(gateway, events=events),
    )

    assert result.output["status"] == "round_limit"
    assert result.output["rounds"] == 2
    assert result.output["calls_used"] == 2
    # 两轮 tool-calling（各 15）+ 一次收尾（10）。
    assert result.output["used_tokens"] == 40
    assert gateway.summaries == 1

    names = {name for name, _ in events}
    assert names == {"subagent.progress"}
    phases = [payload["phase"] for _, payload in events]
    assert phases[0] == "started"
    assert phases[-1] == "finished"
    assert "round" in phases and "tool" in phases
    # 每条都挂得回时间线上那一步，否则前端只知道"有个子 Agent 在跑"，不知道是哪一步。
    assert len({payload["step_id"] for _, payload in events}) == 1
    assert all(payload["tool_call_id"] == "explore-call" for _, payload in events)
    assert events[-1][1]["used_tokens"] == 40
