"""模型写入长期记忆的四个入口。

`remember` 存新事实；`memory_update` / `memory_forget` 按注入块里的 `[#id]` 改写或
retire 旧事实——**修正要替换而不是在旁边堆一条**，否则同一件事的新旧两个版本会同时
出现在上下文里，模型没有办法判断哪个还算数。`memory_read` 取被截断记忆的全文。

`remember` / `memory_update` 是受 Memory policy 约束的模型辅助写入；`memory_forget` 会删除
跨 run 的用户数据，必须由 owner 逐次人工批准，并进入 `tool_invocations` 幂等账本。模型不能
把“我认为它过时了”当成用户要求遗忘的证明。owner-only Memory API 始终保留直接清理入口。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cowork.memory import (
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_KEY_CHARS,
    default_workspace_path,
    forget_memory,
    memory_payload,
    remember,
    require_visible_memory,
    update_memory,
)
from app.cowork.memory_extraction import model_memory_write_skip_reason
from app.cowork.memory_policy import (
    MemoryPolicyDeniedError,
    get_effective_memory_policy,
    require_memory_recall_enabled,
    require_memory_save_enabled,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
)
from app.cowork_contracts import (
    CoworkMemoryRecord,
    MemoryNotFoundError,
    MemoryScope,
    MemoryScopeError,
)


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_CHARS)
    scope: MemoryScope = "global"
    key: str | None = Field(default=None, max_length=MAX_MEMORY_KEY_CHARS)


class MemoryUpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: UUID
    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_CHARS)


class MemoryIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: UUID


async def _require_save(context: CoworkToolContext) -> None:
    policy = await get_effective_memory_policy(
        context.settings, conversation_id=context.conversation_id
    )
    try:
        require_memory_save_enabled(policy)
    except MemoryPolicyDeniedError as error:
        # ``str(error)`` 固定包含 machine-readable reason 和诚实的人类提示，模型不会把
        # policy 拒绝误判成“已保存”或继续原样重试。
        raise CoworkToolError(str(error)) from error


async def _require_recall(context: CoworkToolContext) -> None:
    policy = await get_effective_memory_policy(
        context.settings, conversation_id=context.conversation_id
    )
    try:
        require_memory_recall_enabled(policy)
    except MemoryPolicyDeniedError as error:
        raise CoworkToolError(str(error)) from error


def _require_safe_model_memory_content(content: str) -> None:
    """拒绝模型工具无法证明获得当前消息同意的敏感写入。

    工具参数本身不是用户原文，也不能作为同意证明；否则模型只要把敏感事实和“请记住”
    一起塞进 ``content`` 就能自我授权。明确同意的高敏保存仍可由绑定原始消息的自动抽取
    流水线处理；凭据则只能进入 SecretStore。
    """

    reason = model_memory_write_skip_reason(content)
    if reason is not None:
        raise CoworkToolError(f"model_memory_write_rejected:{reason}")


def _memory_write_ref(record: CoworkMemoryRecord) -> dict[str, object]:
    """写工具只回传定位元数据；正文已在参数里，不再复制进 tool/result 与事件账本。"""

    return {
        "id": str(record.id),
        "scope": record.scope,
        "key": record.key,
        "workspace_path": record.workspace_path,
        "forgotten": record.forgotten_at is not None,
        "updated_at": record.updated_at.isoformat(),
    }


async def _remember(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = RememberArgs.model_validate(raw.model_dump())
    await _require_save(context)
    _require_safe_model_memory_content(args.content)
    workspace_path = (
        await default_workspace_path(context.session, conversation_id=context.conversation_id)
        if args.scope == "workspace"
        else None
    )
    try:
        await _require_save(context)
        record, previous = await remember(
            context.session,
            conversation_id=context.conversation_id,
            scope=args.scope,
            content=args.content,
            key=args.key,
            workspace_path=workspace_path,
            source="agent",
        )
    except MemoryScopeError as error:
        raise CoworkToolError(str(error)) from error
    return CoworkToolResult(
        content={
            "memory": _memory_write_ref(record),
            "replaced": previous is not None,
            "previous_memory_id": None if previous is None else str(previous.id),
        },
        effect_ref=f"memory:{record.id}:revision",
    )


async def _memory_update(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryUpdateArgs.model_validate(raw.model_dump())
    await _require_save(context)
    _require_safe_model_memory_content(args.content)
    try:
        await require_visible_memory(
            context.session,
            conversation_id=context.conversation_id,
            memory_id=args.memory_id,
        )
        await _require_save(context)
        record, previous = await update_memory(
            context.session,
            memory_id=args.memory_id,
            content=args.content,
            actor="model",
            conversation_id=context.conversation_id,
        )
    except MemoryNotFoundError as error:
        raise CoworkToolError(
            f"记忆 {args.memory_id} 不存在，请使用 known_memories 里给出的 [#id]"
        ) from error
    except MemoryScopeError as error:
        raise CoworkToolError(str(error)) from error
    return CoworkToolResult(
        content={
            "memory": _memory_write_ref(record),
            "previous_memory_id": str(previous.id),
        },
        effect_ref=f"memory:{record.id}:revision",
    )


async def _memory_forget(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryIdArgs.model_validate(raw.model_dump())
    try:
        visible = await require_visible_memory(
            context.session,
            conversation_id=context.conversation_id,
            memory_id=args.memory_id,
            require_active=False,
        )
    except MemoryNotFoundError as error:
        raise CoworkToolError(f"记忆 {args.memory_id} 不存在或当前会话不可见") from error
    record = (
        None
        if not visible.active
        else await forget_memory(context.session, memory_id=args.memory_id)
    )
    if record is None:
        # 已经删过或从来不存在，都不是需要模型补救的错误。
        return CoworkToolResult(
            content={"memory_id": str(args.memory_id), "already_forgotten": True},
            effect_ref=f"memory:{args.memory_id}:forgotten",
        )
    return CoworkToolResult(
        content={"memory": _memory_write_ref(record), "already_forgotten": False},
        effect_ref=f"memory:{args.memory_id}:forgotten",
    )


async def _memory_read(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryIdArgs.model_validate(raw.model_dump())
    await _require_recall(context)
    try:
        record = await require_visible_memory(
            context.session,
            conversation_id=context.conversation_id,
            memory_id=args.memory_id,
        )
    except MemoryNotFoundError as error:
        raise CoworkToolError(f"记忆 {args.memory_id} 不存在或当前会话不可见") from error
    return CoworkToolResult(content={"memory": memory_payload(record)})


def register_memory_tools(registry: CoworkToolRegistry) -> None:
    registry.register_deferred(
        CoworkToolSpec(
            name="remember",
            description=(
                "记住一条跨会话有效的长期事实，例如用户偏好或项目约定。"
                "scope 取 global（对所有会话有效）、workspace（只对当前工作目录有效）"
                "或 conversation（只对本次会话有效）。"
                "同一件事请带上稳定的 key，这样后续会更新同一条而不是再堆一条。"
                "只记会长期成立的结论，不要记一次性的中间结果。"
            ),
            args_model=RememberArgs,
            risk="write",
            effect="store",
            parallel_safe=False,
            handler=_remember,
            search_aliases=("memory", "记忆", "记住", "偏好"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_update",
            description=(
                "改写一条已有记忆。发现 known_memories 里的事实过时了要用它替换，"
                "不要用 remember 在旁边补一条新的。memory_id 用注入块里的 [#id]。"
            ),
            args_model=MemoryUpdateArgs,
            risk="write",
            effect="store",
            parallel_safe=False,
            handler=_memory_update,
            search_aliases=("memory", "记忆", "更正"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_forget",
            description=(
                "请求 owner 永久 retire 一条记忆。只有用户明确要求忘记/删除时才调用；"
                "执行前会展示 memory_id 并要求逐次人工批准，不能由 auto 或常驻规则豁免。"
            ),
            args_model=MemoryIdArgs,
            risk="write",
            effect="store",
            parallel_safe=False,
            handler=_memory_forget,
            approval_required=True,
            approval_can_be_waived=False,
            approval_target_fields=("memory_id",),
            search_aliases=("memory", "记忆", "忘记"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_read",
            description="取一条记忆的完整内容；注入块里标注已截断时用它补全。",
            args_model=MemoryIdArgs,
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_memory_read,
            search_aliases=("memory", "记忆"),
        ),
        group="记忆",
    )
    registry.add_system_instructions(
        "用户表达长期偏好、项目约定或纠正你的既有认知时，调用 remember 记下来；"
        "已有记忆过时用 memory_update 替换、用 memory_forget retire，不要新旧并存。"
        "一次性的中间结果不要写进记忆。"
    )
