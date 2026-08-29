import asyncio
import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from workpilot_ai.errors import (
    ProviderContextOverflowError,
    ProviderNotDispatchedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderRetryableError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from workpilot_ai.overflow import is_context_overflow_response
from workpilot_ai.pricing import estimate_tokens
from workpilot_ai.types import (
    CompletionChunk,
    CompletionResult,
    CompletionStopReason,
    EmbeddingResult,
    Message,
    ToolCall,
    ToolCallDelta,
    ToolDefinition,
    Usage,
)


def _openai_stop_reason(raw: object, *, has_tool_calls: bool) -> CompletionStopReason:
    normalized = str(raw or "").casefold()
    if normalized in {"length", "max_tokens"}:
        return "length"
    if normalized in {"tool_calls", "function_call"} or has_tool_calls:
        return "tool_use"
    if normalized in {"", "stop", "end_turn"}:
        return "stop"
    return "error"


# 流式端点不给 usage 时用来估产出量。1 字符 1 token 是上界, 宁可高估:
# 低估会让 run 的 token 预算永远触不到顶, 熔断形同虚设(约束 5)。
_STREAM_CHARS_PER_TOKEN = 1.0

_DSML_TOOL_START = "<｜DSML｜tool_calls>"
_DSML_TOOL_END = "</｜DSML｜tool_calls>"
_DSML_INVOKE_END = "</｜DSML｜invoke>"
_DSML_PARAMETER_END = "</｜DSML｜parameter>"
_DSML_INVOKE_START = re.compile(r'<｜DSML｜invoke name="([A-Za-z_][A-Za-z0-9_.:-]{0,127})">')
_DSML_PARAMETER_START = re.compile(
    r'<｜DSML｜parameter name="([A-Za-z_][A-Za-z0-9_.:-]{0,127})" '
    r'string="(true|false)">'
)
_THINK_OPEN = re.compile(r"^\s*<think(?:ing)?\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)
_THINK_OPEN_PREFIXES = ("<think", "<thinking")
_THINK_CLOSE_HOLDBACK = len("</thinking>") - 1
# Some OpenAI-compatible endpoints omit the opening tag but still append a closing
# ``</think>`` before the answer.  Treat only a bounded leading preamble as protocol:
# a literal tag much later in a long answer is more likely user-requested content.
_ORPHAN_THINK_PREAMBLE_LIMIT = 8_192
# 未知端点先只扣住很短的开头：足够识别常见的孤立 ``</think>``，又不会让正常模型
# 的首轮流式输出长时间没有任何反馈。一旦识别成功，provider 实例后续轮次会直接按
# orphan 协议实时分流。
_ORPHAN_THINK_PROBE_CHARS = 256


class _LeadingThinkStreamFilter:
    """把兼容端点塞进 ``content`` 的前置思考块拆出正文。

    一些本地 OpenAI-compatible 模型不使用 ``reasoning_content``，而是把
    ``<think>…</think>`` 直接逐片写进 ``content``。标签可能跨 SSE chunk；在确认
    前必须扣住短尾巴，否则开标签或闭标签的半截会先作为正文发给客户端。

    只识别回答开头的 think/thinking 块。正文中的同名标签可能是用户要求展示的示例，
    不能把任意位置的内容都当作模型内部协议删除。
    """

    def __init__(self, *, orphan_mode: bool | None = False) -> None:
        self._buffer = ""
        self._mode = (
            "orphan_reasoning"
            if orphan_mode is True
            else "probe"
            if orphan_mode is None
            else "prefix"
        )
        self.detected_orphan = False

    def feed(self, fragment: str) -> tuple[str, str]:
        self._buffer += fragment
        visible: list[str] = []
        reasoning: list[str] = []
        while self._buffer:
            if self._mode == "text":
                visible.append(self._buffer)
                self._buffer = ""
                break
            if self._mode == "probe":
                opening = _THINK_OPEN.match(self._buffer)
                if opening is not None:
                    self._buffer = self._buffer[opening.end() :]
                    self._mode = "reasoning"
                    continue
                closing = _THINK_CLOSE.search(self._buffer)
                if closing is not None and closing.start() <= _ORPHAN_THINK_PREAMBLE_LIMIT:
                    reasoning.append(self._buffer[: closing.start()])
                    self._buffer = self._buffer[closing.end() :].lstrip()
                    self.detected_orphan = True
                    self._mode = "prefix"
                    continue
                if self._could_be_opening_tag() or len(self._buffer) <= _ORPHAN_THINK_PROBE_CHARS:
                    break
                self._mode = "text"
                continue
            if self._mode == "prefix":
                opening = _THINK_OPEN.match(self._buffer)
                if opening is not None:
                    self._buffer = self._buffer[opening.end() :]
                    self._mode = "reasoning"
                    continue
                if self._could_be_opening_tag():
                    break
                self._mode = "text"
                continue

            if self._mode == "orphan_reasoning":
                # 已识别过会丢开标签的端点；后续轮次从第一片起就能实时显示思考，
                # 同时仍兼容它偶尔恢复完整 <think> 开标签的情况。
                opening = _THINK_OPEN.match(self._buffer)
                if opening is not None:
                    self._buffer = self._buffer[opening.end() :]
                closing = _THINK_CLOSE.search(self._buffer)
                if closing is not None:
                    reasoning.append(self._buffer[: closing.start()])
                    self._buffer = self._buffer[closing.end() :].lstrip()
                    self.detected_orphan = True
                    self._mode = "prefix"
                    continue
                if len(self._buffer) > _THINK_CLOSE_HOLDBACK:
                    reasoning.append(self._buffer[:-_THINK_CLOSE_HOLDBACK])
                    self._buffer = self._buffer[-_THINK_CLOSE_HOLDBACK:]
                break

            closing = _THINK_CLOSE.search(self._buffer)
            if closing is not None:
                reasoning.append(self._buffer[: closing.start()])
                self._buffer = self._buffer[closing.end() :].lstrip()
                # 允许端点输出连续的前置思考块；遇到第一段真正正文后就永久透传。
                self._mode = "prefix"
                continue
            if len(self._buffer) > _THINK_CLOSE_HOLDBACK:
                reasoning.append(self._buffer[:-_THINK_CLOSE_HOLDBACK])
                self._buffer = self._buffer[-_THINK_CLOSE_HOLDBACK:]
            break
        return "".join(visible), "".join(reasoning)

    def finish(self) -> tuple[str, str]:
        """排空收流时仍扣住的尾巴；未闭合思考块也绝不回灌正文。"""

        visible = self._buffer if self._mode in {"prefix", "probe", "text"} else ""
        reasoning = self._buffer if self._mode in {"reasoning", "orphan_reasoning"} else ""
        self._buffer = ""
        return visible, reasoning

    def _could_be_opening_tag(self) -> bool:
        candidate = self._buffer.lstrip().casefold()
        if not candidate:
            return True
        if ">" in candidate:
            return False
        return candidate.startswith("<think") or any(
            prefix.startswith(candidate) for prefix in _THINK_OPEN_PREFIXES
        )


def _strip_leading_think_blocks(content: str) -> str:
    parser = _LeadingThinkStreamFilter()
    visible, _ = parser.feed(content)
    tail, _ = parser.finish()
    cleaned = visible + tail

    # Qwen/DeepSeek-compatible servers sometimes lose ``<think>`` at the chat
    # template boundary while retaining ``</think>``.  The stream parser cannot
    # retroactively retract deltas that were already shown, but the terminal
    # CompletionResult is canonical and becomes an atomic ``message.snapshot``.
    # Removing this malformed leading protocol here therefore keeps it out of the
    # persisted assistant message and out of every completed/reloaded view.
    closing = _THINK_CLOSE.search(cleaned)
    if closing is None or closing.start() > _ORPHAN_THINK_PREAMBLE_LIMIT:
        return cleaned
    prefix = cleaned[: closing.start()]
    # Preserve raw tags inside Markdown code examples.  Exact internal protocol is
    # emitted as plain text, as in ``reasoning...\n</think>\nanswer``.
    if prefix.count("```") % 2 == 1 or prefix.count("`") % 2 == 1:
        return cleaned
    return cleaned[closing.end() :].lstrip()


def _has_orphan_think_preamble(content: str) -> bool:
    """判断原始 content 是否使用了缺失开标签的前置思考协议。"""

    if _THINK_OPEN.match(content) is not None:
        return False
    closing = _THINK_CLOSE.search(content)
    if closing is None or closing.start() > _ORPHAN_THINK_PREAMBLE_LIMIT:
        return False
    prefix = content[: closing.start()]
    return prefix.count("```") % 2 == 0 and prefix.count("`") % 2 == 0


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
    return is_context_overflow_response(status_code=response.status_code, body=body)


def _raise_with_body(response: httpx.Response, body: str) -> None:
    """把服务端的解释带进异常。

    `raise_for_status()` 只给出 "Client error '400 Bad Request'"，而真正有用的信息
    （超了多少 token、哪个参数非法）全在响应体里。少了它，一个 400 要靠 curl 复现
    才能定位——这正是约束 4 说的"错误信息要写成可执行的下一步"。
    """

    detail = body.strip()
    if len(detail) > 600:
        detail = detail[:600] + "…"
    message = f"模型服务返回 {response.status_code}：{detail or '（响应体为空）'}"
    if _is_context_overflow(response, body):
        raise ProviderContextOverflowError(message)
    if response.status_code == 429 and not _is_quota_exhausted(body):
        raise ProviderRateLimitError(
            message,
            status_code=response.status_code,
            retry_after_s=_retry_after_seconds(response),
        )
    if response.status_code in {408, 409, 425, 500, 502, 503, 504}:
        raise ProviderRetryableError(
            message,
            status_code=response.status_code,
            retry_after_s=_retry_after_seconds(response),
        )
    raise ProviderResponseError(message)


def _is_quota_exhausted(body: str) -> bool:
    normalized = body.casefold()
    return any(
        marker in normalized
        for marker in (
            "insufficient_quota",
            "quota exhausted",
            "quota has been exhausted",
            "billing hard limit",
            "billing_limit",
            "credit balance",
            "credits exhausted",
            "账户余额不足",
            "配额已耗尽",
        )
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


class OpenAICompatibleProvider:
    """覆盖 OpenAI-compatible Chat Completions 与 Embeddings 接口。"""

    name = "openai_compatible"
    supports_omitting_max_tokens = True

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
        # None = 尚未探测；部分兼容端点会省略 <think> 却保留 </think>。识别一次后，
        # 同一 run 的后续工具轮就能从首 token 开始走 reasoning_delta。
        self._orphan_think_protocol: bool | None = None
        self._clean_think_probe_responses = 0
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
        max_tokens: int | None,
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
        max_tokens: int | None,
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
        max_tokens: int | None,
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
            prompt_cache_key=(prompt_cache_key if self._prompt_cache_key_supported else None),
        )

    async def _complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
        tools: list[ToolDefinition] | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_key: str | None = None,
    ) -> CompletionResult:
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [await self._message_payload(message) for message in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            request_payload["max_tokens"] = max_tokens
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
        if isinstance(content, str):
            content = _strip_leading_think_blocks(content)
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
        # reasoning 模型在推理耗尽输出额度时会回 content=null。此处若直接 str()
        # 会得到字符串 "None"，把"模型没给内容"静默伪装成内容，调用方拿到的是假数据。
        finish_reason = payload["choices"][0].get("finish_reason")
        normalized_stop_reason = _openai_stop_reason(finish_reason, has_tool_calls=bool(tool_calls))
        if content is None and not tool_calls and normalized_stop_reason != "length":
            raise ProviderResponseError(
                f"模型返回空 content(finish_reason={finish_reason})；"
                + (
                    "reasoning 模型可能已耗尽 max_tokens，调大后重试"
                    if max_tokens is not None
                    else "请求未设置客户端输出上限，请检查模型自身输出额度或服务状态"
                )
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
                prompt_cache_write_tokens=int(prompt_details.get("cache_write_tokens", 0)),
            ),
            tool_calls=tuple(tool_calls),
            stop_reason=normalized_stop_reason,
        )

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
    ) -> AsyncIterator[CompletionChunk]:
        """SSE 流式 tool-calling：一路 delta，最后一块给出完整结果。

        三处不显然的地方：

        * **tool_call 按 `index` 拼，不按 `id` 拼。** 首片才带 id 和 name，之后的片只带
          `index` 和一段 `arguments` 碎片；用 id 当键会把同一只工具的后续片全都丢掉，
          而且丢得很安静——参数拼不全，最后 json 解析失败才暴露，那时已经离现场很远。
        * **正文留一段尾巴不发。** DeepSeek 的 DSML wrapper 会从 content 里漏出来，而它
          可能横跨两片 SSE。按 marker 长度扣住尾部再发，就不会把内部协议的前半截当正文
          推给用户；收流时再统一走和非流式同一个 DSML 解析。
        * **拿不到 usage 就按产出字符估。** `stream_options.include_usage` 不是所有兼容
          端点都认。给零会让每轮花费记成 0，run 的 token 预算永远不触顶——熔断失效比多
          估一点严重得多（约束 5）。
        """

        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [await self._message_payload(message) for message in messages],
            "temperature": temperature,
            "stream": True,
            # 不支持的端点会忽略这个字段；支持的会在末尾多给一块带 usage 的 chunk。
            "stream_options": {"include_usage": True},
            "tools": [
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
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": parallel_tool_calls,
        }
        if max_tokens is not None:
            request_payload["max_tokens"] = max_tokens
        if self._enable_thinking is not None:
            request_payload["chat_template_kwargs"] = {"enable_thinking": self._enable_thinking}

        content_parts: list[str] = []
        raw_content_parts: list[str] = []
        sent_chars = 0
        # 累积中的 tool_call，按 SSE 的 index 归位。
        partial: dict[int, dict[str, str]] = {}
        model = self.chat_model
        finish_reason: object = None
        terminal_seen = False
        usage_payload: dict[str, Any] = {}
        generated_chars = 0
        # DSML wrapper 可能横跨两片；扣住这么多字符就足以在拼完之前认出它的开头。
        holdback = len(_DSML_TOOL_START) - 1
        think_filter = _LeadingThinkStreamFilter(orphan_mode=self._orphan_think_protocol)

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
                if not data:
                    continue
                if data == "[DONE]":
                    terminal_seen = True
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as error:
                    raise ProviderResponseError("模型流式响应包含非法 JSON 片段") from error
                if not isinstance(payload, dict):
                    continue
                if payload.get("model"):
                    model = str(payload["model"])
                if isinstance(payload.get("usage"), dict):
                    usage_payload = payload["usage"]
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                # reasoning_content 是 DeepSeek/Qwen 的字段名，reasoning 是另一些端点的。
                for key in ("reasoning_content", "reasoning"):
                    value = delta.get(key)
                    if isinstance(value, str) and value:
                        generated_chars += len(value)
                        yield CompletionChunk(reasoning_delta=value)
                        break
                chunk_content = delta.get("content")
                if isinstance(chunk_content, str) and chunk_content:
                    generated_chars += len(chunk_content)
                    raw_content_parts.append(chunk_content)
                    visible, tagged_reasoning = think_filter.feed(chunk_content)
                    if tagged_reasoning:
                        yield CompletionChunk(reasoning_delta=tagged_reasoning)
                    if visible:
                        content_parts.append(visible)
                        joined = "".join(content_parts)
                        marker = joined.find(_DSML_TOOL_START)
                        safe_upto = marker if marker >= 0 else max(0, len(joined) - holdback)
                        if safe_upto > sent_chars:
                            yield CompletionChunk(text_delta=joined[sent_chars:safe_upto])
                            sent_chars = safe_upto
                raw_calls = delta.get("tool_calls")
                if isinstance(raw_calls, list):
                    for raw in raw_calls:
                        if not isinstance(raw, dict):
                            continue
                        index = raw.get("index")
                        if not isinstance(index, int):
                            raise ProviderResponseError("模型流式 tool_call 缺少 index")
                        slot = partial.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        call_id = str(raw.get("id") or "")
                        if call_id:
                            slot["id"] = call_id
                        name_delta = ""
                        arguments_delta = ""
                        function = raw.get("function")
                        if isinstance(function, dict):
                            if function.get("name"):
                                name_delta = str(function["name"])
                                if name_delta == slot["name"]:
                                    name_delta = ""
                                else:
                                    slot["name"] += name_delta
                            fragment = function.get("arguments")
                            if isinstance(fragment, str):
                                arguments_delta = fragment
                                slot["arguments"] += arguments_delta
                        if call_id or name_delta or arguments_delta:
                            yield CompletionChunk(
                                tool_call_delta=ToolCallDelta(
                                    index=index,
                                    id=call_id,
                                    name_delta=name_delta,
                                    arguments_delta=arguments_delta,
                                )
                            )
        finally:
            await stream.__aexit__(None, None, None)

        if not terminal_seen:
            raise ProviderRetryableError("OpenAI-compatible stream ended before [DONE]")

        visible_tail, reasoning_tail = think_filter.finish()
        if reasoning_tail:
            yield CompletionChunk(reasoning_delta=reasoning_tail)
        if visible_tail:
            content_parts.append(visible_tail)
        raw_content = "".join(raw_content_parts)
        if think_filter.detected_orphan or _has_orphan_think_preamble(raw_content):
            self._orphan_think_protocol = True
            self._clean_think_probe_responses = 0
        elif self._orphan_think_protocol is None:
            # 标题请求与主任务共用 provider：不能让一条正常的短标题响应抢先把探测
            # 关掉。连续两条都干净后才结束短前缀探测；orphan 命中仍可从 False 回到 True。
            self._clean_think_probe_responses += 1
            if self._clean_think_probe_responses >= 2:
                self._orphan_think_protocol = False
        text = _strip_leading_think_blocks(raw_content)
        tool_calls: list[ToolCall] = []
        seen_call_ids: set[str] = set()
        for index in sorted(partial):
            slot = partial[index]
            call_id, name = slot["id"], slot["name"]
            if not call_id or not name or call_id in seen_call_ids:
                raise ProviderResponseError("模型流式响应包含空名称或重复 tool_call id")
            seen_call_ids.add(call_id)
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=slot["arguments"]))
        if _DSML_TOOL_START in text:
            if tool_calls:
                # 结构化调用优先，但内部协议不得进入 assistant 正文。
                text = text[: text.find(_DSML_TOOL_START)].rstrip()
            else:
                parsed = _parse_dsml_tool_calls(text, finish_reason=finish_reason)
                assert parsed is not None
                text, parsed_calls = parsed
                tool_calls.extend(parsed_calls)
        normalized_stop_reason = _openai_stop_reason(finish_reason, has_tool_calls=bool(tool_calls))
        if not text and not tool_calls and normalized_stop_reason != "length":
            raise ProviderResponseError(
                f"模型流式响应为空(finish_reason={finish_reason})；"
                + (
                    "reasoning 模型可能已耗尽 max_tokens，调大后重试"
                    if max_tokens is not None
                    else "请求未设置客户端输出上限，请检查模型自身输出额度或服务状态"
                )
            )
        # 扣住的尾巴在这里补齐；DSML 已经剥掉，剩下的都是正文。
        if len(text) > sent_chars:
            yield CompletionChunk(text_delta=text[sent_chars:])

        prompt_details = usage_payload.get("prompt_tokens_details") or {}
        output_tokens = int(usage_payload.get("completion_tokens", 0))
        if output_tokens <= 0:
            # Count the raw generated reasoning too.  Sanitising internal protocol
            # must not make a reasoning-heavy call appear free to the budget meter.
            produced = generated_chars + sum(len(item["arguments"]) for item in partial.values())
            estimated_output = estimate_tokens(produced, chars_per_token=_STREAM_CHARS_PER_TOKEN)
            output_tokens = (
                estimated_output if max_tokens is None else min(max_tokens, estimated_output)
            )
        yield CompletionChunk(
            result=CompletionResult(
                text=text,
                model=model,
                provider=self.name,
                usage=Usage(
                    input_tokens=int(usage_payload.get("prompt_tokens", 0)),
                    output_tokens=output_tokens,
                    prompt_cache_read_tokens=int(prompt_details.get("cached_tokens", 0)),
                    prompt_cache_write_tokens=int(prompt_details.get("cache_write_tokens", 0)),
                ),
                tool_calls=tuple(tool_calls),
                stop_reason=normalized_stop_reason,
            )
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
        max_tokens: int | None,
        temperature: float,
    ) -> AsyncIterator[str]:
        request_payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [await self._message_payload(message) for message in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            request_payload["max_tokens"] = max_tokens
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
        terminal_seen = False
        try:
            if response.is_error:
                # 流式响应要先把体读出来才有内容, 否则拿到的是空串。
                _raise_with_body(response, (await response.aread()).decode("utf-8", "replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    terminal_seen = True
                    break
                if not data:
                    continue
                payload = json.loads(data)
                delta = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield str(delta)
        finally:
            await stream.__aexit__(None, None, None)
        if not terminal_seen:
            raise ProviderRetryableError("OpenAI-compatible stream ended before [DONE]")

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
