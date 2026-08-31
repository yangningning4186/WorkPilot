"""Google Gemini generateContent API 适配器；只由统一 ModelGateway 构造。"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    CompletionResult,
    CompletionStopReason,
    EmbeddingResult,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)


def _gemini_stop_reason(raw: object, *, has_tool_calls: bool) -> CompletionStopReason:
    normalized = str(raw or "").casefold()
    if normalized in {"max_tokens", "max_output_tokens", "length"}:
        return "length"
    if has_tool_calls:
        return "tool_use"
    if normalized in {"", "stop", "finish_reason_unspecified"}:
        return "stop"
    return "error"


class GeminiProvider:
    name = "gemini"
    embedding_model = "unsupported"
    supports_omitting_max_tokens = True

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
        self.chat_model = chat_model.removeprefix("models/")
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_s,
            trust_env=trust_env,
        )

    @property
    def request_fingerprint(self) -> str:
        return "gemini-generate-content-v1"

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

    async def _complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        system, contents = await _gemini_contents(messages)
        generation_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            # ``parameters`` is Google's restricted OpenAPI Schema
                            # protobuf. Cowork tool definitions are JSON Schema and
                            # commonly include keywords such as
                            # ``additionalProperties``; sending them through the
                            # protobuf field makes generateContent reject the whole
                            # request before the model runs. The JSON-Schema-native
                            # field preserves those constraints instead.
                            "parametersJsonSchema": tool.parameters,
                        }
                        for tool in tools
                    ]
                }
            ]
        endpoint = f"models/{self.chat_model}:generateContent"
        with _dispatch_guard():
            response = await self._client.post(endpoint, headers=self._headers, json=payload)
        if response.is_error:
            _raise_with_body(response, response.text)
        body: dict[str, Any] = response.json()
        try:
            candidate = body["candidates"][0]
            parts = candidate["content"]["parts"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderResponseError("Gemini 响应缺少 candidates[0].content.parts") from error
        finish_reason = candidate.get("finishReason")
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            raise ProviderRetryableError("Gemini response ended without candidates[0].finishReason")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            function = part.get("functionCall")
            if isinstance(function, dict):
                name = str(function.get("name") or "")
                if not name:
                    raise ProviderResponseError("Gemini functionCall 缺少 name")
                raw_thought_signature = part.get("thoughtSignature")
                if raw_thought_signature is not None and not isinstance(
                    raw_thought_signature, str
                ):
                    raise ProviderResponseError("Gemini thoughtSignature 不是字符串")
                calls.append(
                    ToolCall(
                        id=f"gemini-{uuid4().hex}",
                        name=name,
                        arguments=json.dumps(
                            function.get("args") or {}, ensure_ascii=False, separators=(",", ":")
                        ),
                        thought_signature=raw_thought_signature or "",
                    )
                )
        normalized_stop_reason = _gemini_stop_reason(finish_reason, has_tool_calls=bool(calls))
        if not text_parts and not calls and normalized_stop_reason != "length":
            raise ProviderResponseError("Gemini 返回了空响应")
        usage = body.get("usageMetadata") or {}
        return CompletionResult(
            text="".join(text_parts),
            model=self.chat_model,
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage.get("promptTokenCount", 0)),
                output_tokens=int(usage.get("candidatesTokenCount", 0)),
                prompt_cache_read_tokens=int(usage.get("cachedContentTokenCount", 0)),
            ),
            tool_calls=tuple(calls),
            stop_reason=normalized_stop_reason,
        )

    async def stream(
        self, messages: list[Message], *, max_tokens: int | None, temperature: float
    ) -> AsyncIterator[str]:
        result = await self.complete(messages, max_tokens=max_tokens, temperature=temperature)
        if result.text:
            yield result.text

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        del texts
        raise ProviderNotDispatchedError("Gemini 会话配置不接管资料库 embedding")

    async def aclose(self) -> None:
        await self._client.aclose()


async def _gemini_contents(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError as error:
                    raise ValueError(f"tool_call {call.id} arguments 不是 JSON") from error
                call_names[call.id] = call.name
                function_part: dict[str, Any] = {
                    "functionCall": {"name": call.name, "args": arguments}
                }
                if call.thought_signature:
                    # Gemini signs the Part, not the nested FunctionCall. Preserve its
                    # original position verbatim; moving or merging it invalidates the
                    # next function-response turn.
                    function_part["thoughtSignature"] = call.thought_signature
                parts.append(function_part)
            _append_gemini(contents, "model", parts or [{"text": ""}])
            continue
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ValueError("Gemini tool result 缺少 tool_call_id")
            name = call_names.get(message.tool_call_id)
            if name is None:
                raise ValueError(f"找不到 Gemini tool_call 名称: {message.tool_call_id}")
            try:
                response: Any = json.loads(message.content)
            except json.JSONDecodeError:
                response = {"content": message.content}
            if not isinstance(response, dict):
                response = {"result": response}
            _append_gemini(
                contents,
                "user",
                [{"functionResponse": {"name": name, "response": response}}],
            )
            continue
        parts = [{"text": message.content}] if message.content else []
        for attachment in message.attachments:
            if attachment.kind in {"image", "pdf"}:
                try:
                    raw = await asyncio.to_thread(Path(attachment.path).read_bytes)
                except OSError as error:
                    raise ProviderResponseError(
                        f"输入附件已丢失，请重新上传：{attachment.filename}"
                    ) from error
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": attachment.media_type,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    }
                )
            else:
                parts.append(
                    {
                        "text": (
                            f'<attachment name="{attachment.filename}" '
                            f'type="{attachment.media_type}" untrusted="true">\n'
                            f"{attachment.extracted_text}\n</attachment>"
                        )
                    }
                )
        _append_gemini(contents, "user", parts or [{"text": ""}])
    return "\n\n".join(system_parts), contents


def _append_gemini(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})
