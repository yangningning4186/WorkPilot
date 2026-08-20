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
    tool_message = gateway.histories[1][-1]
    assert tool_message.role == "tool"
    payload = json.loads(tool_message.content)
    assert payload["ok"] is False
    assert "不在本次允许执行的工具集合中" in payload["error"]
