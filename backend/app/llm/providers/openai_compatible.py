import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from app.llm.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ProviderNotDispatchedError,
    ToolCall,
    ToolDefinition,
    Usage,
)


class ProviderResponseError(RuntimeError):
    pass


class ProviderContextOverflowError(ProviderResponseError):
    """Provider 已接收请求，并明确拒绝了超出上下文窗口的输入。"""


# 只有在建连阶段就失败的错误才能证明请求没有发出去; 读超时和连接中断都可能已经计费,
# 因此不在这个白名单里(docs/12 §2.2)。
NOT_DISPATCHED_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
)


@contextmanager
def _dispatch_guard() -> Iterator[None]:
    try:
        yield
    except NOT_DISPATCHED_ERRORS as error:
        raise ProviderNotDispatchedError(str(error)) from error


def _is_context_overflow(response: httpx.Response, body: str) -> bool:
    if response.status_code not in {400, 413, 422}:
        return False
    normalized = body.casefold()
    markers = (
        "context_length_exceeded",
        "maximum context length",
        "max context length",
        "context window",
        "max_model_len",
        "maximum model length",
        "prompt is too long",
        "input is too long",
        "too many tokens",
    )
    return any(marker in normalized for marker in markers)


def _raise_with_body(response: httpx.Response, body: str) -> None:
    """把服务端的解释带进异常。

    `raise_for_status()` 只给出 "Client error '400 Bad Request'"，而真正有用的信息
    （超了多少 token、哪个参数非法）全在响应体里。少了它，一个 400 要靠 curl 复现
    才能定位——这正是约束 4 说的"错误信息要写成可执行的下一步"。
    """

    detail = body.strip()
    if len(detail) > 600:
        detail = detail[:600] + "…"
    error_type = (
        ProviderContextOverflowError
        if _is_context_overflow(response, body)
        else ProviderResponseError
    )
    raise error_type(f"模型服务返回 {response.status_code}：{detail or '（响应体为空）'}")


class OpenAICompatibleProvider:
    """覆盖 OpenAI-compatible Chat Completions 与 Embeddings 接口。"""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        chat_model: str,
        embedding_model: str,
        enable_thinking: bool | None = None,
        timeout_s: float = 30.0,
        trust_env: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self._enable_thinking = enable_thinking
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            trust_env=trust_env,
        )

    @property
    def request_fingerprint(self) -> str:
        """会改变输出、但不体现在 messages 里的请求参数。

        精确缓存的键必须带上它：`enable_thinking` 一开一关，同一个 prompt 的输出
        完全不同，而 base_url / model 都没变——不带就会拿到上一种设置下的答案。
        """

        return f"thinking={self._enable_thinking}"

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        return await self._complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        return await self._complete(
            messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
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
        parallel_tool_calls: bool = False,
    ) -> CompletionResult:
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [self._message_payload(message) for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools is not None:
            request_payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "strict": tool.strict,
                    },
                }
                for tool in tools
            ]
            request_payload["tool_choice"] = "auto"
            request_payload["parallel_tool_calls"] = parallel_tool_calls
        if self._enable_thinking is not None:
            request_payload["chat_template_kwargs"] = {"enable_thinking": self._enable_thinking}
        with _dispatch_guard():
            response = await self._client.post(
                "chat/completions",
                headers=self._headers,
                json=request_payload,
            )
        if response.is_error:
            _raise_with_body(response, response.text)
        payload: dict[str, Any] = response.json()
        try:
            message = payload["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message")
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderResponseError("模型响应缺少 choices[0].message") from error
        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raise ProviderResponseError("模型响应的 tool_calls 不是数组")
        tool_calls: list[ToolCall] = []
        seen_call_ids: set[str] = set()
        for raw in raw_tool_calls:
            try:
                call_id = str(raw["id"])
                if raw.get("type") != "function":
                    raise KeyError("type")
                function = raw["function"]
                name = str(function["name"])
                arguments = function["arguments"]
                if not isinstance(arguments, str):
                    raise TypeError("arguments")
            except (KeyError, TypeError) as error:
                raise ProviderResponseError("模型响应包含非法 function tool_call") from error
            if not call_id or not name or call_id in seen_call_ids:
                raise ProviderResponseError("模型响应包含空名称或重复 tool_call id")
            seen_call_ids.add(call_id)
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        # reasoning 模型在推理耗尽 max_tokens 时会回 content=null。此处若直接 str()
        # 会得到字符串 "None"，把"模型没给内容"静默伪装成内容，调用方拿到的是假数据。
        if content is None and not tool_calls:
            finish_reason = payload["choices"][0].get("finish_reason")
            raise ProviderResponseError(
                f"模型返回空 content(finish_reason={finish_reason})；"
                "reasoning 模型可能已耗尽 max_tokens，调大后重试"
            )
        text = "" if content is None else str(content)
        usage = payload.get("usage") or {}
        return CompletionResult(
            text=text,
            model=str(payload.get("model") or self.chat_model),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            tool_calls=tuple(tool_calls),
        )

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            if message.role != "assistant":
                raise ValueError("只有 assistant 消息可以携带 tool_calls")
            payload["content"] = message.content or None
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            if message.role != "tool":
                raise ValueError("只有 tool 消息可以携带 tool_call_id")
            payload["tool_call_id"] = message.tool_call_id
        return payload

    async def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [self._message_payload(message) for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if self._enable_thinking is not None:
            request_payload["chat_template_kwargs"] = {"enable_thinking": self._enable_thinking}
        with _dispatch_guard():
            stream = self._client.stream(
                "POST",
                "chat/completions",
                headers=self._headers,
                json=request_payload,
            )
            response = await stream.__aenter__()
        try:
            if response.is_error:
                # 流式响应要先把体读出来才有内容, 否则拿到的是空串。
                _raise_with_body(response, (await response.aread()).decode("utf-8", "replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                payload = json.loads(data)
                delta = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield str(delta)
        finally:
            await stream.__aexit__(None, None, None)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        with _dispatch_guard():
            response = await self._client.post(
                "embeddings",
                headers=self._headers,
                json={"model": self.embedding_model, "input": texts},
            )
        if response.is_error:
            _raise_with_body(response, response.text)
        payload: dict[str, Any] = response.json()
        rows = sorted(payload.get("data", []), key=lambda row: int(row["index"]))
        embeddings = [[float(value) for value in row["embedding"]] for row in rows]
        if len(embeddings) != len(texts):
            raise ProviderResponseError("embedding 响应数量与输入不一致")
        usage = payload.get("usage") or {}
        return EmbeddingResult(
            embeddings=embeddings,
            model=str(payload.get("model") or self.embedding_model),
            provider=self.name,
            usage=Usage(input_tokens=int(usage.get("prompt_tokens", 0))),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
