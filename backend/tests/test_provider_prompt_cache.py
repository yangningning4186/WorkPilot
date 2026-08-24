from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from workpilot_ai.gateway import ModelGateway
from workpilot_ai.prompt_cache import prompt_cache_key
from workpilot_ai.providers.anthropic import AnthropicProvider
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.types import CompletionResult, EmbeddingResult, Message, ToolDefinition, Usage


def _tool(description: str = "读取文件") -> ToolDefinition:
    return ToolDefinition(
        name="read_file",
        description=description,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )


def test_prompt_cache_key_only_uses_stable_prefix() -> None:
    base = {
        "provider": "openai",
        "model": "gpt-test",
        "task_type": "cowork_decision",
        "tools": [_tool()],
    }
    first = prompt_cache_key(
        **base,
        messages=[Message(role="system", content="稳定策略"), Message(role="user", content="A")],
    )
    second = prompt_cache_key(
        **base,
        messages=[
            Message(role="system", content="稳定策略"),
            Message(role="user", content="B"),
            Message(role="tool", content='{"ok":true}', tool_call_id="call-1"),
        ],
    )

    assert first == second
    assert len(first) == 64
    assert first != prompt_cache_key(
        **{**base, "tools": [_tool("修改后的 schema 描述")]},
        messages=[Message(role="system", content="稳定策略")],
    )


async def test_openai_prompt_cache_key_and_usage_are_mapped() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1_500,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {
                        "cached_tokens": 1_024,
                        "cache_write_tokens": 256,
                    },
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        provider_name="openai",
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="gpt-test",
        embedding_model="unused",
        prompt_cache_key_supported=True,
        client=client,
    )

    result = await provider.complete_with_tools_prompt_cache(
        [
            Message(role="system", content="稳定策略"),
            Message(role="user", content="任务"),
            Message(role="user", content="每轮变化的临时上下文"),
        ],
        tools=[_tool()],
        parallel_tool_calls=True,
        max_tokens=100,
        temperature=0.0,
        prompt_cache_key="wp-cowork-" + "a" * 54,
    )
    await client.aclose()

    assert requests[0]["prompt_cache_key"] == "wp-cowork-" + "a" * 54
    assert result.usage == Usage(
        input_tokens=1_500,
        output_tokens=20,
        prompt_cache_read_tokens=1_024,
        prompt_cache_write_tokens=256,
    )


async def test_compatible_endpoint_does_not_receive_unverified_cache_key() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        provider_name="qwen",
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="qwen",
        embedding_model="unused",
        client=client,
    )
    await provider.complete_with_tools_prompt_cache(
        [Message(role="system", content="稳定策略"), Message(role="user", content="任务")],
        tools=[_tool()],
        parallel_tool_calls=True,
        max_tokens=100,
        temperature=0.0,
        prompt_cache_key="wp-cowork-" + "a" * 54,
    )
    await client.aclose()

    assert "prompt_cache_key" not in requests[0]


async def test_anthropic_marks_system_prefix_and_normalizes_usage() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 30,
                },
            },
        )

    client = httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        base_url="http://unused.test",
        api_key="secret",
        chat_model="claude-test",
        timeout_s=10,
        client=client,
    )
    result = await provider.complete_with_tools_prompt_cache(
        [
            Message(role="system", content="稳定策略"),
            Message(role="user", content="任务"),
            Message(role="user", content="每轮变化的临时上下文"),
        ],
        tools=[_tool()],
        parallel_tool_calls=True,
        max_tokens=100,
        temperature=0.0,
        prompt_cache_key="ignored",
    )
    await client.aclose()

    system = requests[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]
    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]  # type: ignore[index]
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in content[1]
    assert result.usage == Usage(
        input_tokens=1_100,
        output_tokens=30,
        prompt_cache_read_tokens=800,
        prompt_cache_write_tokens=200,
    )


async def test_anthropic_caches_single_user_turn_when_there_is_no_ephemeral_tail() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    client = httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(
        base_url="http://unused.test",
        api_key="secret",
        chat_model="claude-test",
        timeout_s=10,
        client=client,
    )
    await provider.complete_with_tools_prompt_cache(
        [Message(role="system", content="稳定策略"), Message(role="user", content="任务")],
        tools=[_tool()],
        parallel_tool_calls=True,
        max_tokens=100,
        temperature=0.0,
        prompt_cache_key="ignored",
    )
    await client.aclose()

    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]


class _PromptCacheProvider:
    name = "prompt-cache-test"
    chat_model = "test-model"
    embedding_model = "unused"

    def __init__(self) -> None:
        self.regular_calls = 0
        self.prompt_cache_keys: list[str] = []

    async def complete_with_tools(self, *args: object, **kwargs: object) -> CompletionResult:
        del args, kwargs
        self.regular_calls += 1
        return CompletionResult(text="regular", model=self.chat_model, provider=self.name)

    async def complete_with_tools_prompt_cache(
        self, *args: object, prompt_cache_key: str, **kwargs: object
    ) -> CompletionResult:
        del args, kwargs
        self.prompt_cache_keys.append(prompt_cache_key)
        return CompletionResult(text="cached-prefix", model=self.chat_model, provider=self.name)

    async def complete(self, *args: object, **kwargs: object) -> CompletionResult:
        del args, kwargs
        raise AssertionError("not used")

    def stream(self, *args: object, **kwargs: object) -> AsyncIterator[str]:
        del args, kwargs
        raise AssertionError("not used")

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise AssertionError(texts)

    async def aclose(self) -> None:
        return None


async def test_gateway_enables_prompt_cache_online_but_not_in_evaluation() -> None:
    online_provider = _PromptCacheProvider()
    online = ModelGateway(online_provider, embedding_dimensions=4, mode="online")
    result = await online.complete_with_tools(
        [Message(role="system", content="稳定策略"), Message(role="user", content="任务")],
        tools=[_tool()],
        task_type="cowork_decision",
    )
    assert result.text == "cached-prefix"
    assert len(online_provider.prompt_cache_keys) == 1
    assert online_provider.regular_calls == 0

    eval_provider = _PromptCacheProvider()
    evaluation = ModelGateway(eval_provider, embedding_dimensions=4, mode="evaluation")
    result = await evaluation.complete_with_tools(
        [Message(role="system", content="稳定策略"), Message(role="user", content="任务")],
        tools=[_tool()],
        task_type="cowork_decision",
    )
    assert result.text == "regular"
    assert eval_provider.prompt_cache_keys == []
    assert eval_provider.regular_calls == 1
