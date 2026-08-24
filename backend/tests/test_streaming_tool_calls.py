"""流式 tool-calling：Provider 拼装、网关路由与预算记账。

参照 OpenWorker 的 `ASSISTANT_DELTA` / `REASONING_DELTA`：用户在工具循环跑着的时候
就该看到模型正在写什么，而不是对着一个转圈等一整轮结束。
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.agent_core.budget import BudgetedGateway, BudgetMeter
from workpilot_ai.errors import (
    ProviderResponseError,
    ProviderRouteTimeoutError,
    ProviderTimeoutError,
)
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.providers.anthropic import AnthropicProvider
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.types import (
    CompletionChunk,
    CompletionResult,
    Message,
    ToolDefinition,
    Usage,
)

TOOLS = [
    ToolDefinition(name="read_text_file", description="读文件", parameters={"type": "object"}),
]
ASK = [Message(role="user", content="读一下 README")]


def _sse(*events: dict[str, object]) -> str:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)


def _openai_provider(body: str) -> OpenAICompatibleProvider:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return OpenAICompatibleProvider(
        base_url="http://model.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        enable_thinking=False,
        client=httpx.AsyncClient(
            base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
        ),
    )


async def _collect(
    stream: AsyncIterator[CompletionChunk],
) -> tuple[str, str, CompletionResult | None]:
    text, reasoning, result = "", "", None
    async for chunk in stream:
        text += chunk.text_delta
        reasoning += chunk.reasoning_delta
        if chunk.result is not None:
            result = chunk.result
    return text, reasoning, result


async def test_openai_stream_accumulates_tool_calls_by_index() -> None:
    """参数片按 `index` 归位——首片之后就只剩 index 了，用 id 当键会安静地丢掉后续片。"""

    provider = _openai_provider(
        _sse(
            {"model": "served", "choices": [{"delta": {"content": "先看一下"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {"name": "read_text_file", "arguments": '{"pa'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'th":"README.md"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"usage": {"prompt_tokens": 30, "completion_tokens": 7}},
        )
    )

    text, _, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert text == "先看一下"
    assert result is not None
    assert result.text == "先看一下"
    assert [(call.id, call.name) for call in result.tool_calls] == [("call_a", "read_text_file")]
    assert json.loads(result.tool_calls[0].arguments) == {"path": "README.md"}
    assert result.usage.output_tokens == 7


async def test_openai_stream_reports_reasoning_separately() -> None:
    """思考不进正文：混成一路之后再想分开就只能靠猜标记。"""

    provider = _openai_provider(
        _sse(
            {"choices": [{"delta": {"reasoning_content": "先确认路径"}}]},
            {
                "choices": [
                    {"delta": {"content": "好的"}},
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )

    text, reasoning, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert reasoning == "先确认路径"
    assert text == "好的"
    assert result is not None and result.text == "好的"


async def test_openai_stream_extracts_tagged_reasoning_split_across_chunks() -> None:
    """本地兼容端点把 think 标签写进 content 时，跨片标签也不能混进正文。"""

    provider = _openai_provider(
        _sse(
            {"choices": [{"delta": {"content": "<thi"}}]},
            {"choices": [{"delta": {"content": "nk>先确认身份</th"}}]},
            {"choices": [{"delta": {"content": "ink>\n我是 WorkPilot"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )

    text, reasoning, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert reasoning == "先确认身份"
    assert text == "我是 WorkPilot"
    assert result is not None and result.text == "我是 WorkPilot"


async def test_openai_stream_repairs_orphan_reasoning_close_in_terminal_result() -> None:
    """闭标签跨片且没有开标签时，流式思考与终态正文仍能正确分流。"""

    provider = _openai_provider(
        _sse(
            {"choices": [{"delta": {"content": "用户要求读取文件并提取项目代号。"}}]},
            {"choices": [{"delta": {"content": "直接回答即可。\n</thi"}}]},
            {"choices": [{"delta": {"content": "nk>\n\n项目代号：Silver Heron"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )

    text, reasoning, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert reasoning == "用户要求读取文件并提取项目代号。直接回答即可。\n"
    assert text == "项目代号：Silver Heron"
    assert result is not None
    assert result.text == "项目代号：Silver Heron"
    assert "</think>" not in result.text


async def test_openai_stream_keeps_orphan_probe_after_concurrent_clean_title_response() -> None:
    """一条正常标题响应不能抢先关闭主任务需要的 orphan think 探测。"""

    bodies = iter(
        [
            _sse(
                {"choices": [{"delta": {"content": "Atlas 项目简报"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
            _sse(
                {"choices": [{"delta": {"content": "先读取项目资料。\n</thi"}}]},
                {"choices": [{"delta": {"content": "nk>\n开始处理"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            text=next(bodies),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        base_url="http://model.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        client=httpx.AsyncClient(
            base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
        ),
    )

    first_text, first_reasoning, _ = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )
    second_text, second_reasoning, _ = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )
    await provider.aclose()

    assert first_text == "Atlas 项目简报"
    assert first_reasoning == ""
    assert second_reasoning == "先读取项目资料。\n"
    assert second_text == "开始处理"


async def test_openai_stream_never_leaks_dsml_wrapper_into_text() -> None:
    """DSML wrapper 会横跨两片 SSE；扣住尾巴就不会把内部协议当正文推给用户。"""

    marker = "<｜DSML｜tool_calls>"
    body = _sse(
        {"choices": [{"delta": {"content": "这就去读。" + marker[:6]}}]},
        {
            "choices": [
                {
                    "delta": {
                        "content": (
                            marker[6:]
                            + '<｜DSML｜invoke name="read_text_file">'
                            + '<｜DSML｜parameter name="path" string="true">README.md'
                            + "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )
    provider = _openai_provider(body)

    text, _, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert marker not in text
    assert text == "这就去读。"
    assert result is not None
    assert [call.name for call in result.tool_calls] == ["read_text_file"]


async def test_openai_stream_estimates_usage_when_endpoint_omits_it() -> None:
    """给零会让 run 的 token 预算永远触不到顶，熔断形同虚设（约束 5）。"""

    provider = _openai_provider(
        _sse({"choices": [{"delta": {"content": "十个字的回答"}, "finish_reason": "stop"}]})
    )

    _, _, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert result is not None
    assert result.usage.output_tokens > 0


async def test_anthropic_stream_assembles_tool_use_from_input_json_delta() -> None:
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in (
            {
                "type": "message_start",
                "message": {"model": "claude-served", "usage": {"input_tokens": 20}},
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "想一下"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "读给你看"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_text_file"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 11},
            },
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = AnthropicProvider(
        base_url="http://anthropic.test/v1",
        api_key="secret",
        chat_model="claude",
        timeout_s=5.0,
        client=httpx.AsyncClient(
            base_url="http://anthropic.test/v1", transport=httpx.MockTransport(handler)
        ),
    )

    text, reasoning, result = await _collect(
        provider.stream_with_tools(
            ASK, tools=TOOLS, parallel_tool_calls=True, max_tokens=64, temperature=0.0
        )
    )

    assert text == "读给你看"
    assert reasoning == "想一下"
    assert result is not None
    assert json.loads(result.tool_calls[0].arguments) == {"path": "README.md"}
    assert result.usage.output_tokens == 11


class _NonStreamingProvider:
    """只会 complete_with_tools 的 Provider，例如 Gemini 的会话适配器。"""

    name = "fake"
    chat_model = "fake-chat"
    embedding_model = "fake-embed"

    def __init__(self) -> None:
        self.calls = 0

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text="一次性给完",
            model=self.chat_model,
            provider=self.name,
            usage=Usage(input_tokens=10, output_tokens=4),
        )

    async def complete(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> CompletionResult:  # pragma: no cover - 流式路径不会走到
        raise AssertionError("不该被调用")

    async def stream(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:  # pragma: no cover - 流式路径不会走到
        raise AssertionError("不该被调用")
        yield ""

    async def embed(self, texts: list[str]) -> object:  # pragma: no cover
        raise AssertionError("不该被调用")

    async def aclose(self) -> None:
        return None


async def test_gateway_degrades_within_the_same_endpoint_when_provider_cannot_stream() -> None:
    """不支持流式不是这个 endpoint 有问题，往下一档掉既解决不了问题又悄悄换了模型。"""

    provider = _NonStreamingProvider()
    gateway = ModelGateway(provider, embedding_dimensions=4)

    chunks = [
        chunk
        async for chunk in gateway.stream_with_tools(ASK, tools=TOOLS, task_type="cowork_decision")
    ]

    assert provider.calls == 1
    assert [chunk for chunk in chunks if chunk.text_delta] == []
    assert chunks[-1].result is not None
    assert chunks[-1].result.text == "一次性给完"


class _FailingStreamProvider(_NonStreamingProvider):
    """先吐两块 delta 再炸。"""

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text_delta="前半段")
        raise ProviderTimeoutError("断在半路")


async def test_gateway_does_not_switch_tiers_after_emitting_text() -> None:
    """已经吐出去的文本收不回来：换档会让一段话的前后半截由两个模型写成。"""

    gateway = ModelGateway(_FailingStreamProvider(), embedding_dimensions=4)

    seen: list[str] = []
    # 整条链只有超时、没有任何真正的失败，所以抛的是"路由全超时"那一条——调用方据此
    # 把 run 挂起等会儿重来，而不是判死。
    with pytest.raises(ProviderRouteTimeoutError):
        async for chunk in gateway.stream_with_tools(ASK, tools=TOOLS, task_type="cowork_decision"):
            seen.append(chunk.text_delta)

    assert seen == ["前半段"]


class _MissingTerminalProvider(_NonStreamingProvider):
    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[CompletionChunk]:
        yield CompletionChunk(text_delta="只有正文")


async def test_gateway_rejects_a_stream_without_a_terminal_chunk() -> None:
    """没有终块就不知道这一轮调了哪些工具、花了多少 token——不能当成功放过去。"""

    gateway = ModelGateway(_MissingTerminalProvider(), embedding_dimensions=4)

    with pytest.raises(ProviderResponseError):
        async for _ in gateway.stream_with_tools(ASK, tools=TOOLS, task_type="cowork_decision"):
            pass


async def test_budgeted_gateway_settles_tokens_from_the_terminal_chunk() -> None:
    """预留在第一块之前、结算在终块之后，与非流式逐字相同的口径。"""

    budget = {
        "max_tokens": 0,
        "used_tokens": 0,
        "max_calls": 0,
        "used_calls": 0,
        "max_wall_ms": 0,
        "used_wall_ms": 0,
        "started_at_ms": 0,
    }
    meter = BudgetMeter(budget, chars_per_token=1.0)  # type: ignore[arg-type]
    gateway = BudgetedGateway(ModelGateway(_NonStreamingProvider(), embedding_dimensions=4), meter)

    async for _ in gateway.stream_with_tools(ASK, tools=TOOLS, task_type="cowork_decision"):
        pass

    assert budget["used_tokens"] == 14
    assert budget["used_calls"] == 1
