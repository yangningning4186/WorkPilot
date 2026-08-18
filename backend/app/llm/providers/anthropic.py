"""Anthropic Messages API 适配器；只由统一 ModelGateway 构造。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.providers.openai_compatible import (
    ProviderResponseError,
    _dispatch_guard,
    _raise_with_body,
)
from app.llm.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ProviderNotDispatchedError,
    ToolCall,
    ToolDefinition,
    Usage,
)


class AnthropicProvider:
    name = "anthropic"
    embedding_model = "unsupported"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        chat_model: str,
        timeout_s: float,
        trust_env: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.chat_model = chat_model
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_s,
            trust_env=trust_env,
        )

    @property
    def request_fingerprint(self) -> str:
        return "anthropic-messages-v1"

    async def complete(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> CompletionResult:
        return await self._complete(messages, max_tokens=max_tokens, temperature=temperature)

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del parallel_tool_calls
        return await self._complete(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def _complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        system, converted = _anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        with _dispatch_guard():
            response = await self._client.post("messages", headers=self._headers, json=payload)
        if response.is_error:
            _raise_with_body(response, response.text)
        body: dict[str, Any] = response.json()
        content = body.get("content")
        if not isinstance(content, list):
            raise ProviderResponseError("Anthropic 响应缺少 content 数组")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                call_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                if not call_id or not name:
                    raise ProviderResponseError("Anthropic tool_use 缺少 id 或 name")
                calls.append(
                    ToolCall(
                        id=call_id,
                        name=name,
                        arguments=json.dumps(
                            block.get("input") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
        if not text_parts and not calls:
            raise ProviderResponseError("Anthropic 返回了空响应")
        usage = body.get("usage") or {}
        return CompletionResult(
            text="".join(text_parts),
            model=str(body.get("model") or self.chat_model),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            ),
            tool_calls=tuple(calls),
        )

    async def stream(
        self, messages: list[Message], *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        # 会话级 Provider 当前用于 Cowork；保留完整 stream 协议接口，避免旁路 Gateway。
        result = await self.complete(messages, max_tokens=max_tokens, temperature=temperature)
        if result.text:
            yield result.text

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        del texts
        raise ProviderNotDispatchedError("Anthropic 配置只用于对话，embedding 继续使用系统档位")

    async def aclose(self) -> None:
        await self._client.aclose()


def _anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError as error:
                    raise ValueError(f"tool_call {call.id} arguments 不是 JSON") from error
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": arguments}
                )
            _append_anthropic(converted, "assistant", blocks or [{"type": "text", "text": ""}])
            continue
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ValueError("Anthropic tool result 缺少 tool_call_id")
            _append_anthropic(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                        "is_error": False,
                    }
                ],
            )
            continue
        _append_anthropic(
            converted,
            "user",
            [{"type": "text", "text": message.content}],
        )
    return "\n\n".join(system_parts), converted


def _append_anthropic(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})
