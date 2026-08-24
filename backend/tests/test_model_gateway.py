import json

import httpx
import pytest

from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import EmbeddingDimensionError, EmbeddingIdentityError, ModelGateway
from workpilot_ai.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderContextOverflowError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from workpilot_ai.types import Message, ToolCall, ToolDefinition


async def test_gateway_exposes_complete_stream_and_embed() -> None:
    gateway = ModelGateway(DeterministicProvider(4), embedding_dimensions=4)

    completion = await gateway.complete([Message(role="user", content="hello")])
    streamed = [part async for part in gateway.stream([Message(role="user", content="world")])]
    embeddings = await gateway.embed(["hello", "world"])

    assert completion.text == "hello"
    assert streamed == ["world"]
    assert len(embeddings.embeddings) == 2
    assert len(embeddings.embeddings[0]) == 4


async def test_gateway_rejects_wrong_embedding_dimensions() -> None:
    gateway = ModelGateway(DeterministicProvider(3), embedding_dimensions=4)
    with pytest.raises(EmbeddingDimensionError):
        await gateway.embed(["hello"])


async def test_gateway_rejects_unexpected_embedding_identity() -> None:
    provider = DeterministicProvider(4)
    provider.embedding_model = "configured-model"
    gateway = ModelGateway(provider, embedding_dimensions=4)
    provider.embedding_model = "changed-model"

    with pytest.raises(EmbeddingIdentityError):
        await gateway.embed(["hello"])


async def test_openai_compatible_provider_maps_wire_format() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "served-chat",
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "served-embedding",
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        enable_thinking=False,
        client=client,
    )

    completion = await provider.complete(
        [Message(role="user", content="question")], max_tokens=20, temperature=0.0
    )
    embeddings = await provider.embed(["one", "two"])
    await client.aclose()

    assert completion.text == "answer"
    assert completion.usage.input_tokens == 5
    assert embeddings.embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert [request.url.path for request in requests] == [
        "/v1/chat/completions",
        "/v1/embeddings",
    ]
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert json.loads(requests[0].content)["chat_template_kwargs"] == {"enable_thinking": False}
    assert json.loads(requests[1].content)["input"] == ["one", "two"]


async def test_openai_compatible_provider_strips_tagged_reasoning_from_complete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "local-reasoner",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "<thinking>先判断模型身份</thinking>\n我是 WorkPilot"
                        },
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="local-reasoner",
        embedding_model="embed",
        client=client,
    )

    completion = await provider.complete(
        [Message(role="user", content="你是什么模型")], max_tokens=64, temperature=0.0
    )
    await client.aclose()

    assert completion.text == "我是 WorkPilot"


async def test_openai_compatible_provider_strips_orphan_reasoning_close_from_complete() -> None:
    """兼容端点偶尔丢开标签；孤立的闭标签及其前置推理也不能落入正文。"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "local-reasoner",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                "用户要求读取文件并提取字段。直接回答即可。\n"
                                "</think>\n\n项目代号：Silver Heron"
                            )
                        },
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="local-reasoner",
        embedding_model="embed",
        enable_thinking=False,
        client=client,
    )

    completion = await provider.complete(
        [Message(role="user", content="读取项目代号")], max_tokens=64, temperature=0.0
    )
    await client.aclose()

    assert completion.text == "项目代号：Silver Heron"


async def test_cowork_decision_omits_provider_max_tokens_for_complete_and_stream() -> None:
    """8192 只做上下文/费用预留，不再截断 Cowork reasoning + 正文。"""

    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload.get("stream"):
            body = "".join(
                (
                    'data: {"model":"served","choices":[{"delta":{"content":"完成"}}]}\n\n',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
                    "data: [DONE]\n\n",
                )
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "model": "served",
                "choices": [{"finish_reason": "stop", "message": {"content": "完成"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="reasoner",
        embedding_model="embed",
        client=client,
    )
    gateway = ModelGateway(provider, embedding_dimensions=2)
    tools = [ToolDefinition(name="inspect", description="检查", parameters={"type": "object"})]

    completed = await gateway.complete_with_tools(
        [Message(role="user", content="处理任务")],
        tools=tools,
        task_type="cowork_decision",
        max_tokens=8_192,
    )
    streamed = [
        chunk
        async for chunk in gateway.stream_with_tools(
            [Message(role="user", content="处理任务")],
            tools=tools,
            task_type="cowork_decision",
            max_tokens=8_192,
        )
    ]
    await client.aclose()

    assert completed.text == "完成"
    assert streamed[-1].result is not None and streamed[-1].result.text == "完成"
    assert len(payloads) == 2
    assert all("max_tokens" not in payload for payload in payloads)


async def test_non_cowork_tasks_keep_provider_max_tokens() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "served",
                "choices": [{"finish_reason": "stop", "message": {"content": "短回答"}}],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        client=client,
    )
    gateway = ModelGateway(provider, embedding_dimensions=2)

    await gateway.complete(
        [Message(role="user", content="起标题")], task_type="conversation_title", max_tokens=80
    )
    await client.aclose()

    assert payloads[0]["max_tokens"] == 80


async def test_gateway_maps_native_parallel_tool_calls_and_canonical_history() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "served-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-a",
                                    "type": "function",
                                    "function": {"name": "inspect", "arguments": '{"path":"a"}'},
                                },
                                {
                                    "id": "call-b",
                                    "type": "function",
                                    "function": {"name": "inspect", "arguments": '{"path":"b"}'},
                                },
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        client=client,
    )
    gateway = ModelGateway(provider, embedding_dimensions=2)
    result = await gateway.complete_with_tools(
        [
            Message(role="user", content="inspect both"),
            Message(
                role="assistant",
                tool_calls=(ToolCall(id="previous", name="inspect", arguments='{"path":"old"}'),),
            ),
            Message(role="tool", tool_call_id="previous", content='{"ok":true}'),
        ],
        tools=[
            ToolDefinition(
                name="inspect",
                description="Inspect one file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        parallel_tool_calls=True,
    )
    await client.aclose()

    assert [call.id for call in result.tool_calls] == ["call-a", "call-b"]
    payload = json.loads(requests[0].content)
    assert payload["parallel_tool_calls"] is True
    assert payload["tools"][0]["function"]["strict"] is True
    assert payload["messages"][1]["tool_calls"][0]["id"] == "previous"
    assert payload["messages"][2] == {
        "role": "tool",
        "content": '{"ok":true}',
        "tool_call_id": "previous",
    }


async def test_openai_compatible_provider_converts_complete_deepseek_dsml() -> None:
    dsml = """准备生成文件。
<｜DSML｜tool_calls>
<｜DSML｜invoke name="create_native_artifact">
<｜DSML｜parameter name="path" string="true">日报.pptx</｜DSML｜parameter>
<｜DSML｜parameter name="slides" string="false">[{"title":"封面"}]</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": dsml},
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="deepseek-v4-flash",
        embedding_model="embed",
        client=client,
    )
    result = await provider.complete_with_tools(
        [Message(role="user", content="生成日报")],
        tools=[
            ToolDefinition(
                name="create_native_artifact",
                description="生成文件",
                parameters={"type": "object"},
            )
        ],
        parallel_tool_calls=True,
        max_tokens=8_192,
        temperature=0.0,
    )
    await client.aclose()

    assert result.text == "准备生成文件。"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "create_native_artifact"
    assert json.loads(result.tool_calls[0].arguments) == {
        "path": "日报.pptx",
        "slides": [{"title": "封面"}],
    }
    assert "DSML" not in result.text


async def test_openai_compatible_provider_rejects_truncated_deepseek_dsml() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": (
                                "即将生成。\n<｜DSML｜tool_calls>\n"
                                '<｜DSML｜invoke name="create_native_artifact">'
                            ),
                        },
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="deepseek-v4-flash",
        embedding_model="embed",
        client=client,
    )
    with pytest.raises(ProviderResponseError, match="工具调用输出被截断"):
        await provider.complete_with_tools(
            [Message(role="user", content="生成日报")],
            tools=[
                ToolDefinition(
                    name="create_native_artifact",
                    description="生成文件",
                    parameters={"type": "object"},
                )
            ],
            parallel_tool_calls=True,
            max_tokens=8_192,
            temperature=0.0,
        )
    await client.aclose()


async def test_openai_compatible_provider_classifies_context_overflow_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "This model's maximum context length is 4096 tokens.",
                }
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        client=client,
    )

    with pytest.raises(ProviderContextOverflowError, match="context_length_exceeded"):
        await provider.complete(
            [Message(role="user", content="oversized")],
            max_tokens=20,
            temperature=0.0,
        )
    await client.aclose()


async def test_openai_compatible_provider_translates_empty_read_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        client=client,
    )

    with pytest.raises(ProviderTimeoutError, match="模型服务响应超时"):
        await provider.complete(
            [Message(role="user", content="complex task")],
            max_tokens=20,
            temperature=0.0,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_null_content_raises_instead_of_becoming_the_string_none() -> None:
    """reasoning 模型推理耗尽 max_tokens 时回 content=null。

    此前实现是 str(content)，会得到字符串 "None" 并当作正常回答返回，
    把"模型没给内容"静默伪装成内容。调用方（Judge、evidence gate、生成轨）
    拿到的都是假数据，且报错会指向下游解析而非真正的原因。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "reasoner",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": None, "reasoning": "想了很久"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 500},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="reasoner",
        embedding_model="embed",
        client=client,
    )

    with pytest.raises(ProviderResponseError, match="空 content"):
        await provider.complete(
            [Message(role="user", content="question")], max_tokens=500, temperature=0.0
        )
    await client.aclose()
