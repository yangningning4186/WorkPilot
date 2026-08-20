import asyncio
import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from workpilot_ai.errors import (
    ProviderContextOverflowError,
    ProviderNotDispatchedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from workpilot_ai.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)

_DSML_TOOL_START = "<｜DSML｜tool_calls>"
_DSML_TOOL_END = "</｜DSML｜tool_calls>"
_DSML_INVOKE_END = "</｜DSML｜invoke>"
_DSML_PARAMETER_END = "</｜DSML｜parameter>"
_DSML_INVOKE_START = re.compile(r'<｜DSML｜invoke name="([A-Za-z_][A-Za-z0-9_.:-]{0,127})">')
_DSML_PARAMETER_START = re.compile(
    r'<｜DSML｜parameter name="([A-Za-z_][A-Za-z0-9_.:-]{0,127})" '
    r'string="(true|false)">'
)


def _parse_dsml_tool_calls(
    content: str, *, finish_reason: object
) -> tuple[str, tuple[ToolCall, ...]] | None:
    """把 DeepSeek V4 泄漏到 content 的完整 DSML 转为 canonical tool calls。

    只接受完整、精确的 wrapper；任何截断或畸形都会显式失败，绝不能把内部协议
    当正文交给用户，也不能靠宽松 XML 容错把不可信页面里的近似标签变成执行动作。
    """

    start = content.find(_DSML_TOOL_START)
    if start < 0:
        return None
    end = content.find(_DSML_TOOL_END, start + len(_DSML_TOOL_START))
    if end < 0:
        raise ProviderResponseError(
            f"模型工具调用输出被截断（finish_reason={finish_reason}），请缩短单次交付物内容后重试"
        )
    suffix = content[end + len(_DSML_TOOL_END) :]
    if suffix.strip():
        raise ProviderResponseError("模型工具调用结束后包含意外内容，请重新生成")

    body = content[start + len(_DSML_TOOL_START) : end]
    calls: list[ToolCall] = []
    cursor = 0
    while cursor < len(body):
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor == len(body):
            break
        invoke = _DSML_INVOKE_START.match(body, cursor)
        if invoke is None:
            raise ProviderResponseError("模型返回了无法解析的 DSML 工具调用")
        invoke_end = body.find(_DSML_INVOKE_END, invoke.end())
        if invoke_end < 0:
            raise ProviderResponseError("模型返回了未闭合的 DSML invoke")
        name = invoke.group(1)
        arguments = _parse_dsml_parameters(body[invoke.end() : invoke_end])
        serialized = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(f"{len(calls)}\0{name}\0{serialized}".encode()).hexdigest()[:24]
        calls.append(ToolCall(id=f"call_dsml_{digest}", name=name, arguments=serialized))
        cursor = invoke_end + len(_DSML_INVOKE_END)
    if not calls:
        raise ProviderResponseError("模型返回了空的 DSML tool_calls")
    return content[:start].rstrip(), tuple(calls)


def _parse_dsml_parameters(body: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    cursor = 0
    while cursor < len(body):
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor == len(body):
            break
        parameter = _DSML_PARAMETER_START.match(body, cursor)
        if parameter is None:
            raise ProviderResponseError("模型返回了无法解析的 DSML parameter")
        parameter_end = body.find(_DSML_PARAMETER_END, parameter.end())
        if parameter_end < 0:
            raise ProviderResponseError("模型返回了未闭合的 DSML parameter")
        name, is_string = parameter.groups()
        if name in arguments:
            raise ProviderResponseError(f"模型重复提供了工具参数 {name}")
        raw_value = body[parameter.end() : parameter_end]
        if is_string == "true":
            arguments[name] = raw_value
        else:
            try:
                arguments[name] = json.loads(raw_value)
            except json.JSONDecodeError as error:
                raise ProviderResponseError(f"工具参数 {name} 不是合法 JSON") from error
        cursor = parameter_end + len(_DSML_PARAMETER_END)
    return arguments


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
        raise ProviderNotDispatchedError(
            "无法连接模型服务，请检查服务地址、网络连接或代理设置"
        ) from error
    except httpx.TimeoutException as error:
        # ReadTimeout/WriteTimeout 的 str(error) 经常是空字符串。若把原异常直接抛到
        # worker，最终数据库 error 也会为空，客户端只能显示“未返回具体原因”。
        raise ProviderTimeoutError(
            "模型服务响应超时，请重试；若持续发生，请检查所选模型服务状态"
        ) from error
    except httpx.HTTPError as error:
        raise ProviderTransportError(
            "模型服务连接中断，请检查网络或所选模型服务状态后重试"
        ) from error


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
        provider_name: str = "openai_compatible",
        base_url: str,
        api_key: str,
        chat_model: str,
        embedding_model: str,
        enable_thinking: bool | None = None,
        prompt_cache_key_supported: bool = False,
        timeout_s: float = 30.0,
        trust_env: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = provider_name
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self._enable_thinking = enable_thinking
        self._prompt_cache_key_supported = prompt_cache_key_supported
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

    async def complete_with_tools_prompt_cache(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
        prompt_cache_key: str,
    ) -> CompletionResult:
        """发送官方 OpenAI cache key；兼容端点需显式声明支持才发送。"""

        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        return await self._complete(
            messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            max_tokens=max_tokens,
            temperature=temperature,
            prompt_cache_key=(
                prompt_cache_key if self._prompt_cache_key_supported else None
            ),
        )

    async def _complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        tools: list[ToolDefinition] | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_key: str | None = None,
    ) -> CompletionResult:
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [await self._message_payload(message) for message in messages],
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
        if prompt_cache_key is not None:
            request_payload["prompt_cache_key"] = prompt_cache_key
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
        if isinstance(content, str) and tools is not None and _DSML_TOOL_START in content:
            if tool_calls:
                # 某些兼容服务同时返回结构化调用和原始 DSML。结构化结果优先，但内部
                # 协议仍不得进入 assistant 正文。
                content = content[: content.find(_DSML_TOOL_START)].rstrip()
            else:
                parsed_dsml = _parse_dsml_tool_calls(
                    content,
                    finish_reason=payload["choices"][0].get("finish_reason"),
                )
                assert parsed_dsml is not None
                content, parsed_calls = parsed_dsml
                tool_calls.extend(parsed_calls)
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
        prompt_details = usage.get("prompt_tokens_details") or {}
        return CompletionResult(
            text=text,
            model=str(payload.get("model") or self.chat_model),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                prompt_cache_read_tokens=int(prompt_details.get("cached_tokens", 0)),
                prompt_cache_write_tokens=int(
                    prompt_details.get("cache_write_tokens", 0)
                ),
            ),
            tool_calls=tuple(tool_calls),
        )

    @staticmethod
    async def _message_payload(message: Message) -> dict[str, Any]:
        content: Any = message.content
        if message.attachments:
            if message.role != "user":
                raise ValueError("只有 user 消息可以携带输入附件")
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for attachment in message.attachments:
                if attachment.kind == "image":
                    try:
                        raw = await asyncio.to_thread(Path(attachment.path).read_bytes)
                    except OSError as error:
                        raise ProviderResponseError(
                            f"输入附件已丢失，请重新上传：{attachment.filename}"
                        ) from error
                    encoded = base64.b64encode(raw).decode("ascii")
                    blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{attachment.media_type};base64,{encoded}",
                            },
                        }
                    )
                else:
                    text_content = attachment.extracted_text or "（未提取到可读文本）"
                    blocks.append(
                        {
                            "type": "text",
                            "text": (
                                f'<attachment name="{attachment.filename}" '
                                f'type="{attachment.media_type}" untrusted="true">\n'
                                f"{text_content}\n</attachment>"
                            ),
                        }
                    )
            content = blocks
        payload: dict[str, Any] = {"role": message.role, "content": content}
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
            "messages": [await self._message_payload(message) for message in messages],
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
