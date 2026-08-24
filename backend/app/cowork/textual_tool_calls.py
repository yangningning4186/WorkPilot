"""Recover explicit tool calls that a compatible model leaked into assistant text.

This is intentionally narrower than a general XML/JSON parser.  Recovery is only
attempted for explicit ``<tool_call>`` envelopes, and callers must provide both
the schemas exposed in the current model request and the registry validator.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from workpilot_ai.types import ToolCall

_TOOL_CALL_BLOCK = re.compile(
    r"<tool_call(?:\s[^>]*)?>(?P<body>.*?)</tool_call\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_FUNCTION_BLOCK = re.compile(
    r"<function\s*=\s*(?P<name>[A-Za-z0-9_.:-]+)\s*>(?P<body>.*?)</function\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_PARAMETER_BLOCK = re.compile(
    r"<parameter\s*=\s*(?P<name>[A-Za-z0-9_.:-]+)\s*>(?P<value>.*?)</parameter\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_TOOL_CALL = re.compile(r"<tool_call(?:\s[^>]*)?>", flags=re.IGNORECASE)


class TextualToolCallError(ValueError):
    """The text looked like a tool call but could not be safely recovered."""


def contains_textual_tool_call(text: str) -> bool:
    return _EXPLICIT_TOOL_CALL.search(text) is not None


def _decode_parameter(value: str) -> Any:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return normalized


def _decode_json_calls(body: str) -> list[tuple[str, dict[str, Any]]]:
    try:
        payload = json.loads(body.strip())
    except json.JSONDecodeError as error:
        raise TextualToolCallError("正文工具调用不是合法 JSON 或 Hermes XML") from error
    items = payload if isinstance(payload, list) else [payload]
    decoded: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise TextualToolCallError("正文工具调用必须是 JSON object")
        function = item.get("function")
        source = function if isinstance(function, Mapping) else item
        name = source.get("name")
        arguments = source.get("arguments", {})
        if not isinstance(name, str) or not name.strip():
            raise TextualToolCallError("正文工具调用缺少工具名")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise TextualToolCallError(f"工具 {name} 的 arguments 不是合法 JSON") from error
        if not isinstance(arguments, dict):
            raise TextualToolCallError(f"工具 {name} 的 arguments 必须是 JSON object")
        decoded.append((name.strip(), arguments))
    return decoded


def _decode_block(body: str) -> list[tuple[str, dict[str, Any]]]:
    functions = list(_FUNCTION_BLOCK.finditer(body))
    if not functions:
        return _decode_json_calls(body)
    decoded: list[tuple[str, dict[str, Any]]] = []
    for function in functions:
        arguments = {
            parameter.group("name"): _decode_parameter(parameter.group("value"))
            for parameter in _PARAMETER_BLOCK.finditer(function.group("body"))
        }
        decoded.append((function.group("name"), arguments))
    return decoded


def recover_textual_tool_calls(
    text: str,
    *,
    visible_tool_names: Iterable[str],
    validate: Callable[[str, dict[str, Any]], dict[str, Any]],
    id_prefix: str,
) -> tuple[str, tuple[ToolCall, ...]]:
    """Convert explicit text calls into canonical calls after visibility/schema checks."""

    visible = frozenset(visible_tool_names)
    blocks = list(_TOOL_CALL_BLOCK.finditer(text))
    if not blocks:
        raise TextualToolCallError("正文工具调用标签不完整")
    calls: list[ToolCall] = []
    for block in blocks:
        for name, arguments in _decode_block(block.group("body")):
            if name not in visible:
                raise TextualToolCallError(f"工具 {name!r} 的 schema 本轮未下发")
            try:
                canonical = validate(name, arguments)
            except Exception as error:
                raise TextualToolCallError(str(error)) from error
            calls.append(
                ToolCall(
                    id=f"{id_prefix}-{len(calls) + 1}",
                    name=name,
                    arguments=json.dumps(
                        canonical,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                )
            )
    if not calls:
        raise TextualToolCallError("正文工具调用中没有可执行的函数")
    cleaned = _TOOL_CALL_BLOCK.sub("", text).strip()
    return cleaned, tuple(calls)
