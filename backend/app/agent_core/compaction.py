"""canonical 历史的 outbound-only 压缩视图（框架层，见 ADR-0011）。

`agent_checkpoints.state.messages` 是审计与恢复用的完整事实记录，本模块从不改它。
压缩摘要、边界和 emergency trim 上限单独存放；每次调用模型时再据此渲染视图。

本模块不知道自己在给哪个 Agent 压缩历史：摘要提示词、包裹标签与路由 task_type
全部由产品层通过 :class:`CompactionPrompts` 注入。Cowork 的那一份在
`app/agent/cowork_runtime.py`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from app.agent_core.budget import BudgetedGateway, RunBudgetExceededError
from workpilot_ai.gateway import PromptBudget
from workpilot_ai.types import Message, MessageAttachment, ToolCall, ToolDefinition


@dataclass(frozen=True)
class CompactionPrompts:
    """压缩机制里唯一与具体产品有关的部分。

    框架层负责"什么时候压、压到哪个边界、失败怎么退化"，产品层负责"用什么话去压"。
    把这五项做成参数而不是模块常量，是这块逻辑能被两条业务线共用的前提。

    `system_prompt` 必须要求模型只返回 `{"summary": "..."}`——`_parse_summary`
    按这个契约解析，产品换措辞可以，换输出格式不行。
    """

    system_prompt: str
    outbound_prefix: str
    outbound_suffix: str
    # 生成摘要走哪个路由档位。
    summary_task_type: str
    # 估算 prompt 预算时按哪个 task_type 取上下文窗口。
    decision_task_type: str


class CompactionState(TypedDict):
    summary: str
    summary_upto: int
    revision: int
    overflow_recoveries: int
    tool_content_max_chars: int
    last_before_tokens: int
    last_after_tokens: int
    last_mode: Literal["none", "summary", "summary_fallback", "trim"]


@dataclass(frozen=True)
class PreparedOutbound:
    messages: list[Message]
    compaction: CompactionState
    changed: bool
    archived_messages: int
    before_tokens: int
    after_tokens: int
    mode: Literal["none", "summary", "summary_fallback", "trim"]


def default_compaction_state() -> CompactionState:
    return {
        "summary": "",
        "summary_upto": 0,
        "revision": 0,
        "overflow_recoveries": 0,
        "tool_content_max_chars": 0,
        "last_before_tokens": 0,
        "last_after_tokens": 0,
        "last_mode": "none",
    }


def normalize_compaction_state(raw: object, *, message_count: int) -> CompactionState:
    default = default_compaction_state()
    if not isinstance(raw, dict):
        return default
    summary_upto = raw.get("summary_upto", 0)
    if not isinstance(summary_upto, int) or summary_upto < 0 or summary_upto > message_count:
        return default
    mode = raw.get("last_mode", "none")
    if mode not in {"none", "summary", "summary_fallback", "trim"}:
        mode = "none"
    return {
        "summary": str(raw.get("summary", "")),
        "summary_upto": summary_upto,
        "revision": max(0, int(raw.get("revision", 0))),
        "overflow_recoveries": max(0, int(raw.get("overflow_recoveries", 0))),
        "tool_content_max_chars": max(0, int(raw.get("tool_content_max_chars", 0))),
        "last_before_tokens": max(0, int(raw.get("last_before_tokens", 0))),
        "last_after_tokens": max(0, int(raw.get("last_after_tokens", 0))),
        "last_mode": cast("Literal['none', 'summary', 'summary_fallback', 'trim']", mode),
    }


class OutboundCompactor:
    def __init__(
        self,
        gateway: BudgetedGateway,
        *,
        tools: list[ToolDefinition],
        system_prompt: str,
        prompts: CompactionPrompts,
        enabled: bool,
        trigger_ratio: float,
        keep_recent_tool_rounds: int,
        max_summary_chars: int,
        max_input_chars: int,
        max_tokens: int,
        decision_max_tokens: int,
    ) -> None:
        self.gateway = gateway
        self.tools = tools
        self.system_prompt = system_prompt
        self.prompts = prompts
        self.enabled = enabled
        self.trigger_ratio = trigger_ratio
        self.keep_recent_tool_rounds = keep_recent_tool_rounds
        self.max_summary_chars = max_summary_chars
        self.max_input_chars = max_input_chars
        self.max_tokens = max_tokens
        self.decision_max_tokens = decision_max_tokens

    def prompt_budget(self) -> PromptBudget:
        return self.gateway.prompt_budget(
            self.prompts.decision_task_type, max_tokens=self.decision_max_tokens
        )

    def build(
        self,
        canonical: list[dict[str, Any]],
        compaction: CompactionState,
    ) -> list[Message]:
        return build_outbound_messages(
            canonical,
            compaction,
            system_prompt=self.system_prompt,
            prompts=self.prompts,
        )

    async def prepare(
        self,
        canonical: list[dict[str, Any]],
        current: CompactionState,
        *,
        forced: bool,
    ) -> PreparedOutbound:
        budget = self.prompt_budget()
        before_view = self.build(canonical, current)
        before_tokens = budget.estimate_messages_tokens(before_view, self.tools)
        trigger_tokens = max(1, int(budget.max_input_tokens * self.trigger_ratio))
        if not self.enabled or (not forced and before_tokens < trigger_tokens):
            return PreparedOutbound(
                before_view, current, False, 0, before_tokens, before_tokens, "none"
            )

        updated = cast("CompactionState", dict(current))
        previous_boundary = updated["summary_upto"]
        target = _target_boundary(
            canonical,
            after=previous_boundary,
            keep_recent=self.keep_recent_tool_rounds,
            forced=forced or before_tokens > budget.max_input_tokens,
        )
        archived = 0
        mode: Literal["none", "summary", "summary_fallback", "trim"] = "none"
        if target is not None:
            start = previous_boundary
            source = canonical[start:target]
            summary, fallback = await self._summarize(updated["summary"], source)
            updated["summary"] = summary
            updated["summary_upto"] = target
            updated["revision"] += 1
            archived = target - previous_boundary
            mode = "summary_fallback" if fallback else "summary"

        after_view = self.build(canonical, updated)
        after_tokens = budget.estimate_messages_tokens(after_view, self.tools)
        # Provider 报超窗时，本地估算可能偏乐观；或者这次可归档的历史太少。
        # 此时只收紧 outbound tool result，不碰 canonical 内容与 call/result 关联。
        if forced and after_tokens >= before_tokens:
            largest = max(
                (len(message.content) for message in after_view if message.role == "tool"),
                default=0,
            )
            current_limit = updated["tool_content_max_chars"] or largest
            if current_limit > 256:
                updated["tool_content_max_chars"] = max(256, current_limit // 2)
                updated["revision"] += 1
                after_view = self.build(canonical, updated)
                after_tokens = budget.estimate_messages_tokens(after_view, self.tools)
                mode = "trim"

        changed = (
            updated["summary_upto"] != previous_boundary
            or updated["tool_content_max_chars"] != current["tool_content_max_chars"]
        )
        if changed:
            updated["last_before_tokens"] = before_tokens
            updated["last_after_tokens"] = after_tokens
            updated["last_mode"] = mode
            if forced:
                updated["overflow_recoveries"] += 1
        return PreparedOutbound(
            messages=after_view,
            compaction=updated,
            changed=changed,
            archived_messages=archived,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            mode=mode,
        )

    async def _summarize(
        self, previous_summary: str, source: list[dict[str, Any]]
    ) -> tuple[str, bool]:
        rendered = _render_summary_input(previous_summary, source, max_chars=self.max_input_chars)
        last_error: Exception | None = None
        for attempt in range(2):
            if attempt == 1:
                rendered = _truncate_middle(rendered, max(1_000, self.max_input_chars // 2))
            try:
                completion = await self.gateway.complete(
                    [
                        Message(role="system", content=self.prompts.system_prompt),
                        Message(role="user", content=rendered),
                    ],
                    task_type=self.prompts.summary_task_type,
                    max_tokens=self.max_tokens,
                    temperature=0.0,
                )
                summary = _parse_summary(completion.text)
                return _truncate_middle(summary, self.max_summary_chars), False
            except RunBudgetExceededError:
                raise
            except Exception as error:  # provider/格式失败统一只重试一次
                last_error = error
        fallback = _deterministic_summary(previous_summary, source, self.max_summary_chars)
        if not fallback:
            assert last_error is not None
            raise last_error
        return fallback, True


def build_outbound_messages(
    canonical: list[dict[str, Any]],
    compaction: CompactionState,
    *,
    system_prompt: str,
    prompts: CompactionPrompts,
) -> list[Message]:
    """按 checkpoint 的压缩边界构造当前真实发送给模型的消息视图。"""

    messages = [Message(role="system", content=system_prompt)]
    boundary = compaction["summary_upto"]
    if boundary > 0 and canonical:
        if compaction["summary"]:
            messages.append(
                Message(
                    role="user",
                    content=(
                        prompts.outbound_prefix + compaction["summary"] + prompts.outbound_suffix
                    ),
                )
            )
        raw_suffix = canonical[boundary:]
        if not raw_suffix:
            # 强制溢出恢复允许归档到历史末尾，但当前用户请求必须仍作为原始消息
            # 锚定在 summary 之后，不能只依赖模型生成的摘要转述目标。
            current_user = next(
                (item for item in reversed(canonical[:boundary]) if item.get("role") == "user"),
                None,
            )
            if current_user is not None:
                raw_suffix = [current_user]
    else:
        raw_suffix = canonical
    messages.extend(_message_from_canonical(item) for item in raw_suffix)
    limit = compaction["tool_content_max_chars"]
    return _limit_tool_contents(messages, limit) if limit > 0 else messages


def _message_from_canonical(raw: dict[str, Any]) -> Message:
    role = raw.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"非法 canonical message role: {role!r}")
    calls: list[ToolCall] = []
    raw_calls = raw.get("tool_calls", [])
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise ValueError("非法 canonical tool_call")
            function = item["function"]
            calls.append(
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=str(function.get("arguments", "")),
                )
            )
    return Message(
        role=role,
        content=str(raw.get("content", "")),
        tool_calls=tuple(calls),
        tool_call_id=(None if raw.get("tool_call_id") is None else str(raw["tool_call_id"])),
        attachments=tuple(
            MessageAttachment(
                kind=cast("Any", item["kind"]),
                filename=str(item["filename"]),
                media_type=str(item["media_type"]),
                path=str(item["path"]),
                size_bytes=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
                extracted_text=str(item.get("extracted_text", "")),
            )
            for item in raw.get("attachments", [])
            if isinstance(item, dict)
        ),
    )


def _complete_history_boundaries(canonical: list[dict[str, Any]]) -> list[int]:
    """返回不会拆断 assistant tool_calls 与对应 tool result 的归档边界。"""

    boundaries: list[int] = []
    index = 0
    while index < len(canonical):
        message = canonical[index]
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(calls, list) or not calls:
            if message.get("role") != "tool":
                boundaries.append(index + 1)
            index += 1
            continue
        expected = {
            str(item.get("id")) for item in calls if isinstance(item, dict) and item.get("id")
        }
        found: set[str] = set()
        cursor = index + 1
        while cursor < len(canonical) and canonical[cursor].get("role") == "tool":
            call_id = canonical[cursor].get("tool_call_id")
            if call_id is not None:
                found.add(str(call_id))
            cursor += 1
        if expected and found == expected:
            boundaries.append(cursor)
        index = max(cursor, index + 1)
    return boundaries


def _target_boundary(
    canonical: list[dict[str, Any]],
    *,
    after: int,
    keep_recent: int,
    forced: bool,
) -> int | None:
    available = [item for item in _complete_history_boundaries(canonical) if item > after]
    if not available:
        return None
    archive_count = max(0, len(available) - keep_recent)
    if archive_count > 0:
        return available[archive_count - 1]
    return available[-1] if forced else None


def _render_summary_input(
    previous_summary: str,
    source: list[dict[str, Any]],
    *,
    max_chars: int,
) -> str:
    payload = json.dumps(
        {
            "previous_summary": previous_summary or None,
            "canonical_history_to_compact": source,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _truncate_middle(payload, max_chars)


def _parse_summary(text: str) -> str:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    raise ValueError("压缩响应缺少 summary")


def _deterministic_summary(
    previous_summary: str,
    source: list[dict[str, Any]],
    max_chars: int,
) -> str:
    lines = [previous_summary.strip()] if previous_summary.strip() else []
    for message in source:
        role = message.get("role")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    continue
                function = call["function"]
                lines.append(
                    "调用 "
                    f"{function.get('name', 'unknown')}[{call.get('id', '')}] "
                    f"参数={_truncate_middle(str(function.get('arguments', '')), 240)}"
                )
        elif role == "tool":
            lines.append(
                f"结果[{message.get('tool_call_id', '')}]="
                f"{_truncate_middle(str(message.get('content', '')), 500)}"
            )
        elif message.get("content"):
            lines.append(f"{role}: {_truncate_middle(str(message['content']), 300)}")
    return _truncate_middle("\n".join(item for item in lines if item), max_chars)


def _limit_tool_contents(messages: list[Message], limit: int) -> list[Message]:
    limited: list[Message] = []
    for message in messages:
        if message.role != "tool" or len(message.content) <= limit:
            limited.append(message)
            continue
        content = json.dumps(
            {
                "ok": True,
                "outbound_only_truncated": _truncate_middle(message.content, limit),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        limited.append(Message(role="tool", content=content, tool_call_id=message.tool_call_id))
    return limited


def _truncate_middle(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"[:limit]
    head = (limit - 1) // 2
    tail = limit - head - 1
    return value[:head] + "…" + value[-tail:]
