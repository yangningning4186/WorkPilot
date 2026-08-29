"""canonical 历史的 outbound-only 压缩视图（框架层，见 ADR-0011）。

`agent_checkpoints.state.messages` 是审计与恢复用的完整事实记录，本模块从不改它。
压缩摘要、边界和 emergency trim 上限单独存放；每次调用模型时再据此渲染视图。

本模块不知道自己在给哪个 Agent 压缩历史：摘要提示词、包裹标签与路由 task_type
全部由产品层通过 :class:`CompactionPrompts` 注入。Cowork 的那一份在
`app/agent/cowork_runtime.py`。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast
from uuid import uuid4

from app.agent_core.budget import BudgetedGateway, RunBudgetExceededError
from app.agent_core.messages import (
    AgentMessage,
    compaction_summary,
    convert_to_llm_messages,
)
from app.agent_core.prompt_templates import PromptTemplate, PromptTemplateError
from workpilot_ai.gateway import PromptBudget
from workpilot_ai.types import CompletionResult, Message, ToolDefinition

# 摘要器是模型，不能把「是否保留用户约束」完全交给它决定。这里额外从 canonical
# 历史机械抽取最近的用户原话，形成一个有界的 continuation 轨道。上限刻意独立于
# summary：前者是确定性事实，后者是可压缩的模型产物。
_ARCHIVED_USER_MESSAGES_MAX_COUNT = 12
_ARCHIVED_USER_MESSAGES_MAX_CHARS = 4_000
_COMPACTION_DETAILS_MAX_ITEMS_PER_KIND = 32
_COMPACTION_DETAILS_MAX_ITEM_CHARS = 512
_COMPACTION_DETAILS_RENDER_CHARS_PER_KIND = 2_000


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
    turn_prefix_outbound_prefix: str = "<turn_prefix_summary>\n"
    turn_prefix_outbound_suffix: str = "\n</turn_prefix_summary>"


class CompactionDetails(TypedDict):
    """不依赖摘要模型、跨压缩边界续传的操作事实。"""

    read_files: list[str]
    modified_files: list[str]
    artifacts: list[str]


class CompactionState(TypedDict):
    summary: str
    summary_upto: int
    # A tool-heavy current user turn may itself cross the threshold.  Keep its completed prefix
    # separate from the summary of fully completed turns so “what this turn already did” is not
    # collapsed into generic conversation history.
    turn_prefix_summary: str
    turn_prefix_upto: int
    revision: int
    overflow_recoveries: int
    tool_content_max_chars: int
    last_before_tokens: int
    last_after_tokens: int
    last_mode: Literal["none", "summary", "summary_fallback", "trim"]
    # 最近一次主决策由 provider 报告的完整输入量，以及它覆盖到的 canonical 位置。
    # revision 防止压缩视图变化后继续把旧视图的 usage 当成当前视图基线。
    last_input_tokens: int
    last_usage_message_count: int
    last_usage_revision: int
    last_usage_tool_tokens: int
    details: CompactionDetails


@dataclass(frozen=True)
class PreparedOutbound:
    messages: list[Message]
    compaction: CompactionState
    changed: bool
    archived_messages: int
    before_tokens: int
    after_tokens: int
    mode: Literal["none", "summary", "summary_fallback", "trim"]
    trigger_tokens: int
    trigger_source: Literal["provider_usage", "estimate"]


type SummaryAttemptInvoker = Callable[
    [Callable[[], Awaitable[CompletionResult]]], Awaitable[CompletionResult]
]


def default_compaction_state() -> CompactionState:
    return {
        "summary": "",
        "summary_upto": 0,
        "turn_prefix_summary": "",
        "turn_prefix_upto": 0,
        "revision": 0,
        "overflow_recoveries": 0,
        "tool_content_max_chars": 0,
        "last_before_tokens": 0,
        "last_after_tokens": 0,
        "last_mode": "none",
        "last_input_tokens": 0,
        "last_usage_message_count": 0,
        "last_usage_revision": 0,
        "last_usage_tool_tokens": 0,
        "details": {"read_files": [], "modified_files": [], "artifacts": []},
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
    details = _normalize_compaction_details(raw.get("details"))
    turn_prefix_upto = raw.get("turn_prefix_upto", summary_upto)
    if (
        not isinstance(turn_prefix_upto, int)
        or turn_prefix_upto < summary_upto
        or turn_prefix_upto > message_count
    ):
        turn_prefix_upto = summary_upto
    return {
        "summary": str(raw.get("summary", "")),
        "summary_upto": summary_upto,
        "turn_prefix_summary": str(raw.get("turn_prefix_summary", "")),
        "turn_prefix_upto": turn_prefix_upto,
        "revision": max(0, int(raw.get("revision", 0))),
        "overflow_recoveries": max(0, int(raw.get("overflow_recoveries", 0))),
        "tool_content_max_chars": max(0, int(raw.get("tool_content_max_chars", 0))),
        "last_before_tokens": max(0, int(raw.get("last_before_tokens", 0))),
        "last_after_tokens": max(0, int(raw.get("last_after_tokens", 0))),
        "last_mode": cast("Literal['none', 'summary', 'summary_fallback', 'trim']", mode),
        "last_input_tokens": max(0, int(raw.get("last_input_tokens", 0))),
        "last_usage_message_count": max(0, int(raw.get("last_usage_message_count", 0))),
        "last_usage_revision": max(0, int(raw.get("last_usage_revision", 0))),
        "last_usage_tool_tokens": max(0, int(raw.get("last_usage_tool_tokens", 0))),
        "details": details,
    }


def record_input_usage(
    current: CompactionState,
    *,
    input_tokens: int,
    message_count: int,
    tool_tokens: int,
) -> CompactionState:
    """记录主决策的真实输入 usage；无有效 usage 时保留上一条可用基线。"""

    if input_tokens <= 0:
        return current
    if message_count < 0 or tool_tokens < 0:
        raise ValueError("usage 对应的消息数和工具 token 不能为负")
    updated = cast("CompactionState", dict(current))
    updated["last_input_tokens"] = input_tokens
    updated["last_usage_message_count"] = message_count
    updated["last_usage_revision"] = current["revision"]
    updated["last_usage_tool_tokens"] = tool_tokens
    return updated


def _usage_based_context_tokens(
    canonical: list[dict[str, Any]],
    current: CompactionState,
    budget: PromptBudget,
    current_tool_tokens: int,
) -> int | None:
    """用上次真实输入量加上其后的 canonical 尾部估算当前上下文。"""

    input_tokens = current["last_input_tokens"]
    message_count = current["last_usage_message_count"]
    if (
        input_tokens <= 0
        or current["last_usage_revision"] != current["revision"]
        or message_count < 0
        or message_count > len(canonical)
    ):
        return None
    trailing = convert_to_llm_messages(canonical[message_count:])
    tool_delta = max(0, current_tool_tokens - current["last_usage_tool_tokens"])
    if not trailing:
        return input_tokens + tool_delta
    # 上一条 provider usage 已覆盖 system、tool schema、摘要和当时的 canonical；这里只
    # 估新追加的 assistant/tool/user 尾部。estimate_messages_tokens 带 4 token 请求固定
    # 开销，作为很小的保守余量保留，不再每轮重估整份历史。
    return input_tokens + tool_delta + budget.estimate_messages_tokens(trailing)


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
        ephemeral_suffix: str = "",
    ) -> None:
        self.gateway = gateway
        self.tools = tools
        self.system_prompt = system_prompt
        # 每轮重算的临时上下文。它挂在视图末尾而不是 system prompt 里，见
        # `build_outbound_messages` 的说明。
        self.ephemeral_suffix = ephemeral_suffix
        self.prompts = prompts
        self._summary_system_template = PromptTemplate(
            template_id="compaction.system",
            source=prompts.system_prompt,
        )
        unsupported = set(self._summary_system_template.parameters) - {"max_summary_chars"}
        if unsupported:
            raise PromptTemplateError(
                "compaction system prompt 只支持参数 max_summary_chars，实际还有: "
                + ", ".join(sorted(unsupported))
            )
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
        *,
        system_prompt: str | None = None,
        ephemeral_suffix: str | None = None,
    ) -> list[Message]:
        return build_outbound_messages(
            canonical,
            compaction,
            system_prompt=self.system_prompt if system_prompt is None else system_prompt,
            prompts=self.prompts,
            ephemeral_suffix=(
                self.ephemeral_suffix if ephemeral_suffix is None else ephemeral_suffix
            ),
        )

    async def prepare(
        self,
        canonical: list[dict[str, Any]],
        current: CompactionState,
        *,
        forced: bool,
        system_prompt: str | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        ephemeral_suffix: str | None = None,
        summary_attempt: SummaryAttemptInvoker | None = None,
    ) -> PreparedOutbound:
        budget = self.prompt_budget()
        active_tools = self.tools if tools is None else list(tools)
        before_view = self.build(
            canonical,
            current,
            system_prompt=system_prompt,
            ephemeral_suffix=ephemeral_suffix,
        )
        before_tokens = budget.estimate_messages_tokens(before_view, active_tools)
        current_tool_tokens = budget.estimate_messages_tokens([], active_tools)
        observed_tokens = _usage_based_context_tokens(
            canonical, current, budget, current_tool_tokens
        )
        trigger_basis = observed_tokens if observed_tokens is not None else before_tokens
        trigger_source: Literal["provider_usage", "estimate"] = (
            "provider_usage" if observed_tokens is not None else "estimate"
        )
        trigger_tokens = max(1, int(budget.max_input_tokens * self.trigger_ratio))
        if not self.enabled or (not forced and trigger_basis < trigger_tokens):
            return PreparedOutbound(
                before_view,
                current,
                False,
                0,
                before_tokens,
                before_tokens,
                "none",
                trigger_basis,
                trigger_source,
            )

        updated = cast("CompactionState", dict(current))
        previous_summary_upto = updated["summary_upto"]
        previous_prefix_upto = updated["turn_prefix_upto"]
        previous_boundary = max(previous_summary_upto, previous_prefix_upto)
        target = _target_complete_turn_boundary(
            canonical,
            after=updated["summary_upto"],
            keep_recent=self.keep_recent_tool_rounds,
            forced=forced or before_tokens > budget.max_input_tokens,
        )
        archived = 0
        mode: Literal["none", "summary", "summary_fallback", "trim"] = "none"
        if target is not None:
            start = updated["summary_upto"]
            source = canonical[start:target]
            summary, fallback = await self._summarize(
                updated["summary"],
                source,
                summary_attempt=summary_attempt,
            )
            updated["details"] = _merge_compaction_details(updated["details"], source)
            updated["summary"] = summary
            updated["summary_upto"] = target
            updated["turn_prefix_summary"] = ""
            updated["turn_prefix_upto"] = target
            updated["revision"] += 1
            archived = max(0, target - previous_boundary)
            mode = "summary_fallback" if fallback else "summary"
        else:
            target = _target_turn_prefix_boundary(
                canonical,
                after=previous_boundary,
                keep_recent=self.keep_recent_tool_rounds,
                forced=forced or before_tokens > budget.max_input_tokens,
            )
            if target is not None:
                source = canonical[previous_boundary:target]
                prefix_summary, fallback = await self._summarize(
                    updated["turn_prefix_summary"],
                    source,
                    summary_attempt=summary_attempt,
                )
                updated["details"] = _merge_compaction_details(updated["details"], source)
                updated["turn_prefix_summary"] = prefix_summary
                updated["turn_prefix_upto"] = target
                updated["revision"] += 1
                archived = target - previous_boundary
                mode = "summary_fallback" if fallback else "summary"

        after_view = self.build(
            canonical,
            updated,
            system_prompt=system_prompt,
            ephemeral_suffix=ephemeral_suffix,
        )
        after_tokens = budget.estimate_messages_tokens(after_view, active_tools)
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
                after_view = self.build(
                    canonical,
                    updated,
                    system_prompt=system_prompt,
                    ephemeral_suffix=ephemeral_suffix,
                )
                after_tokens = budget.estimate_messages_tokens(after_view, active_tools)
                mode = "trim"

        changed = (
            updated["summary_upto"] != previous_summary_upto
            or updated["turn_prefix_upto"] != previous_prefix_upto
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
            trigger_tokens=trigger_basis,
            trigger_source=trigger_source,
        )

    async def _summarize(
        self,
        previous_summary: str,
        source: list[dict[str, Any]],
        *,
        summary_attempt: SummaryAttemptInvoker | None,
    ) -> tuple[str, bool]:
        # UI-only custom records are valid canonical audit entries but must not leak through
        # the secondary summarizer model path after being filtered from the main provider view.
        source = [
            {key: value for key, value in item.items() if key != "content_blocks"}
            for item in source
            if item.get("role") != "custom" or item.get("llm_visible") is True
        ]
        rendered = _render_summary_input(previous_summary, source, max_chars=self.max_input_chars)
        session_id = f"compaction:{uuid4()}"
        last_error: Exception | None = None
        for attempt in range(2):
            if attempt == 1:
                rendered = _truncate_middle(rendered, max(1_000, self.max_input_chars // 2))
            try:

                async def invoke(rendered_prompt: str = rendered) -> CompletionResult:
                    template_arguments = {
                        name: self.max_summary_chars
                        for name in self._summary_system_template.parameters
                    }
                    return await self.gateway.complete(
                        [
                            Message(
                                role="system",
                                content=self._summary_system_template.render(template_arguments),
                            ),
                            Message(role="user", content=rendered_prompt),
                        ],
                        task_type=self.prompts.summary_task_type,
                        max_tokens=self.max_tokens,
                        temperature=0.0,
                        cache_retention="none",
                        session_id=session_id,
                    )

                completion = (
                    await invoke() if summary_attempt is None else await summary_attempt(invoke)
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
    ephemeral_suffix: str = "",
) -> list[Message]:
    """按 checkpoint 的压缩边界构造当前真实发送给模型的消息视图。

    `ephemeral_suffix` 是每轮重算的临时上下文（任务清单、当前目录、模式提醒）。它
    **只能挂在视图末尾**，不能进 system prompt：provider 的 prompt cache 按前缀命中，
    system 是第 0 条消息，改一个字就让整段前缀作废；挂在末尾则只有这一小块失效，
    前面所有轮次仍然复用。挂到"最后一条 user 消息"也不行——工具循环里那条通常在很
    靠前的位置，改它等于改掉后面所有内容。

    它同样不进 canonical：canonical 是审计与恢复用的事实记录，临时上下文属于渲染，
    和 outbound-only 的 tool result 截断是同一类处理。

    末尾追加一条 user 消息在两类 provider 上都合法：OpenAI 兼容接口接受 tool 之后
    直接跟 user；Anthropic 适配器会把相邻同角色消息合并进同一轮。
    """

    prefix_messages: list[AgentMessage] = []
    boundary = max(compaction["summary_upto"], compaction["turn_prefix_upto"])
    if boundary > 0 and canonical:
        if compaction["summary"]:
            prefix_messages.append(
                compaction_summary(
                    prompts.outbound_prefix + compaction["summary"] + prompts.outbound_suffix
                )
            )
        if compaction["turn_prefix_summary"]:
            prefix_messages.append(
                compaction_summary(
                    prompts.turn_prefix_outbound_prefix
                    + compaction["turn_prefix_summary"]
                    + prompts.turn_prefix_outbound_suffix
                )
            )
        details_block = _render_compaction_details(compaction["details"])
        if details_block:
            prefix_messages.append(compaction_summary(details_block))
        raw_suffix = canonical[boundary:]
        archived_for_user_track = canonical[:boundary]
        if not raw_suffix:
            # 强制溢出恢复允许归档到历史末尾，但当前用户请求必须仍作为原始消息
            # 锚定在 summary 之后，不能只依赖模型生成的摘要转述目标。
            current_user_index = next(
                (
                    index
                    for index in range(boundary - 1, -1, -1)
                    if canonical[index].get("role") == "user"
                ),
                None,
            )
            if current_user_index is not None:
                raw_suffix = [canonical[current_user_index]]
                # 当前问题已以原始 Message 锚定，不再在历史原话轨道里重复。
                archived_for_user_track = canonical[:current_user_index]
        archived_user_messages = _render_archived_user_messages(archived_for_user_track)
        if archived_user_messages:
            prefix_messages.append({"role": "user", "content": archived_user_messages})
    else:
        raw_suffix = canonical
    # 旧 checkpoint 曾把 citation repair 记成 canonical system。不能把它原位下发：许多
    # OpenAI-compatible 服务要求 system 只能出现在最前面。恢复时把所有遗留 system
    # 折叠进唯一的第 0 条；新 checkpoint 已不再产生这种形状。
    legacy_system = [
        str(item.get("content", "")).strip()
        for item in raw_suffix
        if item.get("role") == "system" and str(item.get("content", "")).strip()
    ]
    leading_system = "\n\n".join([system_prompt, *legacy_system])
    agent_view: list[Mapping[str, Any]] = [
        {"role": "system", "content": leading_system},
        *prefix_messages,
    ]
    raw_suffix = [item for item in raw_suffix if item.get("role") != "system"]
    agent_view.extend(raw_suffix)
    messages = convert_to_llm_messages(agent_view)
    limit = compaction["tool_content_max_chars"]
    view = _limit_tool_contents(messages, limit) if limit > 0 else messages
    if ephemeral_suffix.strip():
        view = [
            *view,
            *convert_to_llm_messages([{"role": "user", "content": ephemeral_suffix.strip()}]),
        ]
    return view


def _render_archived_user_messages(canonical: list[dict[str, Any]]) -> str:
    """机械保留最近的用户原话，避免摘要遗漏长期约束。

    canonical 仍是唯一事实源；这个块只是 outbound-only 视图。内容按时间顺序输出，
    但从最近消息向前选取，因此在总量超限时优先保留较新的指令。单条超长消息使用
    中间截断，同时保留开头目标和末尾追加约束。
    """

    selected: list[str] = []
    remaining = _ARCHIVED_USER_MESSAGES_MAX_CHARS
    for item in reversed(canonical):
        if item.get("role") != "user":
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        # 为 JSON 引号、逗号和包裹标签预留少量空间；内容上限至少为 1，确保不会
        # 因某条过长消息把所有更近的用户消息一并挤掉。
        content_limit = max(1, remaining - 64)
        selected.append(_truncate_middle(content, content_limit))
        remaining -= len(selected[-1])
        if len(selected) >= _ARCHIVED_USER_MESSAGES_MAX_COUNT or remaining <= 64:
            break
    if not selected:
        return ""
    selected.reverse()
    payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
    return (
        '<archived_user_messages data="canonical-excerpt">\n'
        "以下是被压缩历史中的用户原话，按原顺序排列；冲突时以较新的用户消息为准。\n"
        f"{payload}\n"
        "</archived_user_messages>"
    )


def _normalize_compaction_details(raw: object) -> CompactionDetails:
    source = raw if isinstance(raw, dict) else {}

    def values(key: str) -> list[str]:
        items = source.get(key, [])
        if not isinstance(items, list):
            return []
        normalized = [
            str(item).strip()[:_COMPACTION_DETAILS_MAX_ITEM_CHARS]
            for item in items
            if str(item).strip()
        ]
        return list(dict.fromkeys(normalized))[-_COMPACTION_DETAILS_MAX_ITEMS_PER_KIND:]

    return {
        "read_files": values("read_files"),
        "modified_files": values("modified_files"),
        "artifacts": values("artifacts"),
    }


def _merge_compaction_details(
    current: CompactionDetails,
    source: list[dict[str, Any]],
) -> CompactionDetails:
    read_files = list(current["read_files"])
    modified_files = list(current["modified_files"])
    artifacts = list(current["artifacts"])
    write_markers = ("write", "replace", "create", "edit", "delete", "move", "copy", "shell")

    def add(target: list[str], value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = value.strip()
        if not normalized or normalized in target:
            return
        target.append(normalized[:_COMPACTION_DETAILS_MAX_ITEM_CHARS])
        del target[:-_COMPACTION_DETAILS_MAX_ITEMS_PER_KIND]

    for message in source:
        if message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").casefold()
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(arguments, dict):
                    continue
                destination = (
                    modified_files if any(item in name for item in write_markers) else read_files
                )
                for key in ("path", "file", "file_path", "source_path", "target_path"):
                    add(destination, arguments.get(key))
        elif message.get("role") == "tool":
            try:
                payload = json.loads(str(message.get("content") or "{}"))
            except json.JSONDecodeError:
                continue
            _collect_artifact_refs(payload, artifacts)
    return {
        "read_files": read_files,
        "modified_files": modified_files,
        "artifacts": artifacts,
    }


def _collect_artifact_refs(value: object, output: list[str]) -> None:
    if isinstance(value, dict):
        artifact_id = value.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id.strip():
            title = value.get("title")
            rendered = artifact_id.strip()
            if isinstance(title, str) and title.strip():
                rendered = f"{rendered} ({title.strip()})"
            if rendered not in output:
                output.append(rendered[:_COMPACTION_DETAILS_MAX_ITEM_CHARS])
                del output[:-_COMPACTION_DETAILS_MAX_ITEMS_PER_KIND]
        for child in value.values():
            _collect_artifact_refs(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_artifact_refs(child, output)


def _render_compaction_details(details: CompactionDetails) -> str:
    if not any(details.values()):
        return ""
    rendered: dict[str, object] = {}
    omitted: dict[str, int] = {}
    for key, values in (
        ("read_files", details["read_files"]),
        ("modified_files", details["modified_files"]),
        ("artifacts", details["artifacts"]),
    ):
        selected: list[str] = []
        used = 0
        for value in reversed(values):
            cost = len(value) + 4
            if selected and used + cost > _COMPACTION_DETAILS_RENDER_CHARS_PER_KIND:
                break
            selected.append(value)
            used += cost
        selected.reverse()
        rendered[key] = selected
        if len(selected) < len(values):
            omitted[key] = len(values) - len(selected)
    if omitted:
        rendered["omitted_older_items"] = omitted
    payload = json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
    return (
        '<compaction_details source="deterministic-tool-ledger">\n'
        "以下文件与产物操作由运行时从已完成工具协议机械提取，不依赖摘要模型。\n"
        f"{payload}\n"
        "</compaction_details>"
    )


def _tool_protocol_boundaries(canonical: list[dict[str, Any]]) -> list[int]:
    """Return boundaries that never split assistant tool calls from their results."""

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


def _complete_turn_boundaries(canonical: list[dict[str, Any]]) -> list[int]:
    """Return boundaries after complete user turns, never merely after a tool round."""

    protocol = set(_tool_protocol_boundaries(canonical))
    boundaries: list[int] = []
    turn_started = False
    for index, message in enumerate(canonical):
        if message.get("role") not in {"user", "runtime_directive"}:
            continue
        if turn_started and index in protocol:
            boundaries.append(index)
        turn_started = True
    if turn_started and canonical:
        final = canonical[-1]
        calls = final.get("tool_calls")
        if (
            final.get("role") == "assistant"
            and (not isinstance(calls, list) or not calls)
            and final.get("stop_reason") not in {"length", "error"}
            and len(canonical) in protocol
        ):
            boundaries.append(len(canonical))
    return list(dict.fromkeys(boundaries))


def _target_complete_turn_boundary(
    canonical: list[dict[str, Any]],
    *,
    after: int,
    keep_recent: int,
    forced: bool,
) -> int | None:
    available = [item for item in _complete_turn_boundaries(canonical) if item > after]
    if not available:
        return None
    # ``keep_recent`` historically counted tool rounds.  At the coarser turn level retaining
    # one complete turn is sufficient; retaining N full turns would prevent compaction in the
    # common two-turn conversation while the prompt is already over threshold.
    retained_complete_turns = 1 if keep_recent > 0 else 0
    archive_count = max(0, len(available) - retained_complete_turns)
    if archive_count > 0:
        return available[archive_count - 1]
    return available[-1] if forced else None


def _target_turn_prefix_boundary(
    canonical: list[dict[str, Any]],
    *,
    after: int,
    keep_recent: int,
    forced: bool,
) -> int | None:
    complete_turns = _complete_turn_boundaries(canonical)
    current_turn_start = max((item for item in complete_turns if item <= after), default=0)
    available = [
        item
        for item in _tool_protocol_boundaries(canonical)
        if item > after
        # Never “compact” only the current user question.  It remains an exact anchor and such
        # a summary cannot reduce the outbound view anyway.
        and any(
            message.get("role") in {"assistant", "tool"}
            for message in canonical[current_turn_start:item]
        )
    ]
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


def deterministic_history_summary(
    source: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
    previous_summary: str = "",
) -> str:
    """Public, provider-free summary used by branch navigation and recovery views.

    Branch movement must never depend on provider availability.  This is the same deterministic
    fallback used by compaction, exposed through a bounded interface instead of importing a
    private helper or falling back to a handful of 240-character previews.
    """

    if max_chars < 1:
        raise ValueError("deterministic summary max_chars 必须大于 0")
    normalized = [dict(item) for item in source]
    return _deterministic_summary(previous_summary, normalized, max_chars)


def collect_history_details(
    source: Sequence[Mapping[str, Any]],
    *,
    current: Mapping[str, Any] | None = None,
) -> CompactionDetails:
    """Extract the bounded read/modified/artifact ledger without invoking a model."""

    baseline = _normalize_compaction_details(current)
    return _merge_compaction_details(baseline, [dict(item) for item in source])


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
