"""Anthropic Messages API 适配器；只由统一 ModelGateway 构造。"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from workpilot_ai.errors import (
    ProviderNotDispatchedError,
    ProviderResponseError,
    ProviderRetryableError,
)
from workpilot_ai.providers.openai_compatible import (
    _dispatch_guard,
    _raise_with_body,
)
from workpilot_ai.types import (
    CompletionChunk,
    CompletionResult,
    CompletionStopReason,
    EmbeddingResult,
    Message,
    MessageContentBlock,
    RedactedThinkingContentBlock,
    TextContentBlock,
    ThinkingContentBlock,
    ToolCall,
    ToolCallDelta,
    ToolDefinition,
    Usage,
)


def _anthropic_stop_reason(raw: object, *, has_tool_calls: bool) -> CompletionStopReason:
    normalized = str(raw or "").casefold()
    if normalized in {"max_tokens", "length"}:
        return "length"
    if normalized == "tool_use" or has_tool_calls:
        return "tool_use"
    if normalized in {"", "end_turn", "stop_sequence", "stop"}:
        return "stop"
    return "error"


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
        self, messages: list[Message], *, max_tokens: int | None, temperature: float
    ) -> CompletionResult:
        return await self._complete(messages, max_tokens=max_tokens, temperature=temperature)

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
    ) -> CompletionResult:
        del parallel_tool_calls
        return await self._complete(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def complete_with_tools_prompt_cache(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
        prompt_cache_key: str,
    ) -> CompletionResult:
        # Anthropic 不接收业务 cache key；该参数只表明 Gateway 已为稳定前缀分组。
        del parallel_tool_calls, prompt_cache_key
        return await self._complete(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            cache_stable_prefix=True,
        )

    async def _complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
        tools: list[ToolDefinition] | None = None,
        cache_stable_prefix: bool = False,
    ) -> CompletionResult:
        payload = await self._request_payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            cache_stable_prefix=cache_stable_prefix,
        )
        with _dispatch_guard():
            response = await self._client.post("messages", headers=self._headers, json=payload)
        if response.is_error:
            _raise_with_body(response, response.text)
        body: dict[str, Any] = response.json()
        content = body.get("content")
        if not isinstance(content, list):
            raise ProviderResponseError("Anthropic 响应缺少 content 数组")
        text_parts: list[str] = []
        content_blocks: list[MessageContentBlock] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
                content_blocks.append(TextContentBlock(text=block["text"]))
            elif block.get("type") == "thinking":
                thinking = block.get("thinking")
                signature = block.get("signature")
                if not isinstance(thinking, str) or not isinstance(signature, str) or not signature:
                    raise ProviderResponseError("Anthropic thinking 块缺少 thinking 或 signature")
                content_blocks.append(ThinkingContentBlock(thinking=thinking, signature=signature))
            elif block.get("type") == "redacted_thinking":
                data = block.get("data")
                if not isinstance(data, str) or not data:
                    raise ProviderResponseError("Anthropic redacted_thinking 块缺少 data")
                content_blocks.append(RedactedThinkingContentBlock(data=data))
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
        normalized_stop_reason = _anthropic_stop_reason(
            body.get("stop_reason"), has_tool_calls=bool(calls)
        )
        if not text_parts and not calls and normalized_stop_reason != "length":
            raise ProviderResponseError("Anthropic 返回了空响应")
        usage = body.get("usage") or {}
        cache_read_tokens = int(usage.get("cache_read_input_tokens", 0))
        cache_write_tokens = int(usage.get("cache_creation_input_tokens", 0))
        return CompletionResult(
            text="".join(text_parts),
            model=str(body.get("model") or self.chat_model),
            provider=self.name,
            usage=Usage(
                input_tokens=(
                    int(usage.get("input_tokens", 0)) + cache_read_tokens + cache_write_tokens
                ),
                output_tokens=int(usage.get("output_tokens", 0)),
                prompt_cache_read_tokens=cache_read_tokens,
                prompt_cache_write_tokens=cache_write_tokens,
            ),
            tool_calls=tuple(calls),
            stop_reason=normalized_stop_reason,
            content_blocks=tuple(content_blocks),
        )

    async def _request_payload(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
        tools: list[ToolDefinition] | None,
        cache_stable_prefix: bool,
    ) -> dict[str, Any]:
        """流式与非流式共用一份请求体。

        分成一个方法而不是复制一遍：两条路径的 system 块、cache breakpoint 与 tools
        必须逐字一致，否则 provider 侧的前缀缓存会在"这一轮流式、下一轮非流式"之间
        反复失效，而这件事不报错，只是账单变贵。
        """

        system, converted = await _anthropic_messages(messages)
        if max_tokens is None:
            raise ValueError("Anthropic Messages API 要求显式提供 max_tokens")
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = (
                [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if cache_stable_prefix
                else system
            )
        if cache_stable_prefix:
            payload["messages"] = _mark_conversation_cache_breakpoint(converted)
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
        return payload

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
    ) -> AsyncIterator[CompletionChunk]:
        """Messages API 的 SSE 流。

        `tool_use` 的参数是逐片 `input_json_delta` 拼出来的，所以整条流收完之前谈不上
        "这一轮要调哪几只工具"——这正是终块存在的理由。

        `thinking_delta` 单独走 `reasoning_delta`：思考不进 canonical 历史、不参与引用，
        混进正文之后再想分开只能靠猜标记。

        **流式仍然打上 cache breakpoint**（`cache_stable_prefix=True`）：Cowork 的 system
        prompt 在一次 run 内逐字不变，前缀缓存的收益在流式这条路上分毫不少。
        """

        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        del parallel_tool_calls  # Anthropic 没有对应开关, 与非流式一致地忽略。
        payload = await self._request_payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            cache_stable_prefix=True,
        )
        payload["stream"] = True

        text_parts: list[str] = []
        thinking_blocks: dict[int, dict[str, str]] = {}
        redacted_blocks: dict[int, RedactedThinkingContentBlock] = {}
        # index → 正在拼的 tool_use。Anthropic 的 content block 按序号寻址。
        blocks: dict[int, dict[str, str]] = {}
        model = self.chat_model
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        stop_reason: object = None
        terminal_seen = False

        with _dispatch_guard():
            stream = self._client.stream("POST", "messages", headers=self._headers, json=payload)
            response = await stream.__aenter__()
        try:
            if response.is_error:
                _raise_with_body(response, (await response.aread()).decode("utf-8", "replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as error:
                    raise ProviderResponseError("Anthropic 流式响应包含非法 JSON 片段") from error
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind == "error":
                    detail = event.get("error") or {}
                    raise ProviderResponseError(
                        f"Anthropic 流式响应报错: {detail.get('message') or detail}"
                    )
                if kind == "message_start":
                    message = event.get("message") or {}
                    model = str(message.get("model") or model)
                    usage = message.get("usage") or {}
                    input_tokens = int(usage.get("input_tokens", 0))
                    cache_read_tokens = int(usage.get("cache_read_input_tokens", 0))
                    cache_write_tokens = int(usage.get("cache_creation_input_tokens", 0))
                elif kind == "content_block_start":
                    block = event.get("content_block") or {}
                    index = event.get("index")
                    if not isinstance(index, int):
                        raise ProviderResponseError("Anthropic content block 缺少 index")
                    if block.get("type") == "tool_use":
                        blocks[index] = {
                            "id": str(block.get("id") or ""),
                            "name": str(block.get("name") or ""),
                            "json": "",
                        }
                        yield CompletionChunk(
                            tool_call_delta=ToolCallDelta(
                                index=index,
                                id=str(block.get("id") or ""),
                                name_delta=str(block.get("name") or ""),
                            )
                        )
                    elif block.get("type") == "thinking":
                        thinking_blocks[index] = {
                            "thinking": str(block.get("thinking") or ""),
                            "signature": str(block.get("signature") or ""),
                        }
                    elif block.get("type") == "redacted_thinking":
                        redacted_data = block.get("data")
                        if not isinstance(redacted_data, str) or not redacted_data:
                            raise ProviderResponseError("Anthropic redacted_thinking 块缺少 data")
                        redacted_blocks[index] = RedactedThinkingContentBlock(data=redacted_data)
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        fragment = str(delta.get("text") or "")
                        if fragment:
                            text_parts.append(fragment)
                            yield CompletionChunk(text_delta=fragment)
                    elif delta_type == "thinking_delta":
                        fragment = str(delta.get("thinking") or "")
                        if fragment:
                            index = event.get("index")
                            if not isinstance(index, int) or index not in thinking_blocks:
                                raise ProviderResponseError(
                                    "Anthropic thinking_delta 缺少对应 thinking block"
                                )
                            thinking_blocks[index]["thinking"] += fragment
                            yield CompletionChunk(reasoning_delta=fragment)
                    elif delta_type == "signature_delta":
                        index = event.get("index")
                        if not isinstance(index, int) or index not in thinking_blocks:
                            raise ProviderResponseError(
                                "Anthropic signature_delta 缺少对应 thinking block"
                            )
                        thinking_blocks[index]["signature"] += str(delta.get("signature") or "")
                    elif delta_type == "input_json_delta":
                        index = event.get("index")
                        if isinstance(index, int) and index in blocks:
                            fragment = str(delta.get("partial_json") or "")
                            blocks[index]["json"] += fragment
                            if fragment:
                                yield CompletionChunk(
                                    tool_call_delta=ToolCallDelta(
                                        index=index,
                                        arguments_delta=fragment,
                                    )
                                )
                elif kind == "message_delta":
                    usage = event.get("usage") or {}
                    output_tokens = int(usage.get("output_tokens", output_tokens))
                    stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                elif kind == "message_stop":
                    terminal_seen = True
        finally:
            await stream.__aexit__(None, None, None)

        if not terminal_seen:
            raise ProviderRetryableError("Anthropic stream ended before message_stop")

        calls: list[ToolCall] = []
        for index in sorted(blocks):
            block = blocks[index]
            if not block["id"] or not block["name"]:
                raise ProviderResponseError("Anthropic tool_use 缺少 id 或 name")
            # 空 input 的工具调用发不出 input_json_delta，此时补一个空对象而不是空串,
            # 否则下游 json.loads 会在一个本来完全合法的调用上失败。
            calls.append(
                ToolCall(id=block["id"], name=block["name"], arguments=block["json"] or "{}")
            )
        text = "".join(text_parts)
        protocol_blocks: list[tuple[int, MessageContentBlock]] = []
        for index, block in thinking_blocks.items():
            if not block["signature"]:
                raise ProviderResponseError("Anthropic thinking 块在 message_stop 前缺少 signature")
            protocol_blocks.append(
                (
                    index,
                    ThinkingContentBlock(thinking=block["thinking"], signature=block["signature"]),
                )
            )
        protocol_blocks.extend(redacted_blocks.items())
        # Text is still projected through ``CompletionResult.text`` for every adapter.  Keep a
        # matching block as well so Anthropic can replay the exact content-block protocol.
        if text:
            text_indexes = [index for index in (*thinking_blocks, *redacted_blocks, *blocks)]
            protocol_blocks.append(
                ((max(text_indexes) + 1) if text_indexes else 0, TextContentBlock(text=text))
            )
        normalized_stop_reason = _anthropic_stop_reason(stop_reason, has_tool_calls=bool(calls))
        if not text and not calls and normalized_stop_reason != "length":
            raise ProviderResponseError(f"Anthropic 流式响应为空(stop_reason={stop_reason})")
        yield CompletionChunk(
            result=CompletionResult(
                text=text,
                model=model,
                provider=self.name,
                usage=Usage(
                    input_tokens=input_tokens + cache_read_tokens + cache_write_tokens,
                    output_tokens=output_tokens,
                    prompt_cache_read_tokens=cache_read_tokens,
                    prompt_cache_write_tokens=cache_write_tokens,
                ),
                tool_calls=tuple(calls),
                stop_reason=normalized_stop_reason,
                content_blocks=tuple(block for _, block in sorted(protocol_blocks)),
            )
        )

    async def stream(
        self, messages: list[Message], *, max_tokens: int | None, temperature: float
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


async def _anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            has_text_block = False
            for block in message.content_blocks:
                if isinstance(block, TextContentBlock):
                    blocks.append({"type": "text", "text": block.text})
                    has_text_block = True
                elif isinstance(block, ThinkingContentBlock):
                    if not block.signature:
                        raise ValueError("Anthropic thinking block 缺少 signature")
                    blocks.append(
                        {
                            "type": "thinking",
                            "thinking": block.thinking,
                            "signature": block.signature,
                        }
                    )
                elif isinstance(block, RedactedThinkingContentBlock):
                    if not block.data:
                        raise ValueError("Anthropic redacted_thinking block 缺少 data")
                    blocks.append({"type": "redacted_thinking", "data": block.data})
            if message.content and not has_text_block:
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
        blocks = [{"type": "text", "text": message.content}] if message.content else []
        for attachment in message.attachments:
            if attachment.kind in {"image", "pdf"}:
                try:
                    raw = await asyncio.to_thread(Path(attachment.path).read_bytes)
                except OSError as error:
                    raise ProviderResponseError(
                        f"输入附件已丢失，请重新上传：{attachment.filename}"
                    ) from error
                source = {
                    "type": "base64",
                    "media_type": attachment.media_type,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
                block_type = "image" if attachment.kind == "image" else "document"
                blocks.append({"type": block_type, "source": source})
            else:
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f'<attachment name="{attachment.filename}" '
                            f'type="{attachment.media_type}" untrusted="true">\n'
                            f"{attachment.extracted_text}\n</attachment>"
                        ),
                    }
                )
        _append_anthropic(converted, "user", blocks or [{"type": "text", "text": ""}])
    return "\n\n".join(system_parts), converted


def _mark_conversation_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """缓存动态尾巴之前的最长对话前缀，不修改 canonical provider 消息。"""

    if not messages:
        return messages
    copied = [{**message, "content": list(message.get("content") or [])} for message in messages]
    final_content = copied[-1]["content"]
    if len(final_content) >= 2:
        target_message = copied[-1]
        target_index = len(final_content) - 2
    elif len(copied) >= 2 and copied[-2]["content"]:
        target_message = copied[-2]
        target_index = len(target_message["content"]) - 1
    elif final_content:
        # 普通的一问一答没有运行时临时尾巴，直接缓存这条用户输入；否则第一轮只会
        # 写入 tools + system，第二轮无法复用首条任务这一段会话前缀。
        target_message = copied[-1]
        target_index = len(final_content) - 1
    else:
        return copied
    content = list(target_message["content"])
    block = content[target_index]
    if isinstance(block, dict):
        content[target_index] = {
            **block,
            "cache_control": {"type": "ephemeral"},
        }
        target_message["content"] = content
    return copied


def _append_anthropic(
    messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})
