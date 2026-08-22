"""Cowork 产品层与 Store adapter 共用的纯数据契约。

这里与 ``agent_core.contracts`` 的边界是：后者可被任何 Agent 复用；本模块只描述
Cowork 的目录授权、附件、交付物、HITL inbox 和自动化计划。两者都禁止依赖 service、
数据库实现或具体 Agent runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

AccessMode = Literal["read_only", "read_write"]
Capability = Literal[
    "knowledge.read",
    "filesystem.read",
    "filesystem.write",
    "office.word.edit",
    "office.excel.edit",
    "network.read",
    "browser.control",
    "shell.execute",
    "external.action",
]
MemoryScope = Literal["global", "workspace", "conversation"]
MemoryCategory = Literal["preference", "profile", "interest", "fact"]
MEMORY_CATEGORIES: frozenset[str] = frozenset(
    {"preference", "profile", "interest", "fact"}
)
ArtifactKind = Literal["file", "report", "diff", "table"]
AttachmentKind = Literal["image", "pdf", "text"]
InteractionKind = Literal[
    "ask_user",
    "directory_request",
    "capability_request",
    "shell_approval",
    "external_approval",
    "plan_approval",
]
InteractionStatus = Literal["pending", "answered", "approved", "rejected", "cancelled"]
ScheduleKind = Literal["once", "cron"]
# 常驻审批规则。`once` 不落库——它就是现在这套一次性 call-id 集合，留在这里只是为了让
# API 的取值是闭合的。
ApprovalRememberScope = Literal["once", "tool", "command", "target"]
# 会话的自主权上限。`interactive` 是默认：写入与命令逐次问人。`auto` 由用户在会话设置里
# 显式打开，模型无权切换；它只免掉"再问一次"，capability 与目录边界照旧生效。
# 计划模式是第三档，但它是 run 级的开关（`CoworkState["mode"]`），不放在这里——
# 一次调研型的 run 结束就该回到常规，而自主权上限是跟着会话走的长期选择。
ApprovalMode = Literal["interactive", "auto"]
# 目前只接了飞书：它是本项目已有官方连接器、能发交互卡片、也能回推事件的那一个。
# 这一层保持传输无关（发送方是注入的），加一个平台只需要再写一个适配器。
MessagingPlatform = Literal["feishu"]
# 死信的两个来源：无处投递的入站消息，以及后台轮次自己失败。
UnroutedKind = Literal["inbound", "background_turn"]
# 规则归谁所有：会话级由用户在审批卡片上勾选；计划级在 create_schedule 被批准的那一刻
# 派生，随计划一起被删除。
ApprovalRuleScope = Literal["conversation", "schedule"]
# 怎么匹配一次调用：整只工具、精确目标、或 shell 的 argv 前缀。
ApprovalMatchKind = Literal["tool", "target", "command_prefix"]


class ConversationBusyError(RuntimeError):
    """会话仍有持有效租约的 worker，不能在其写入期间变更。"""


class CoworkPermissionError(RuntimeError):
    pass


class ConversationNotFoundError(LookupError):
    pass


class SessionRootNotFoundError(LookupError):
    pass


class CapabilityDeniedError(PermissionError):
    pass


class ArtifactRegistrationError(RuntimeError):
    pass


class CoworkAttachmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionRootRecord:
    id: UUID
    conversation_id: UUID
    requested_path: str
    canonical_path: str
    label: str
    access_mode: AccessMode
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CoworkMemoryRecord:
    """一条长期记忆——两套记忆合并后的唯一形态。

    上半部分（scope / key / content / source）是 openworker `coworker/memory/base.py`
    那套：按作用域分层、可选幂等键、软删除。下半部分是原来 RAG 侧独有的**时序有效性**
    （ADR-0005）：记忆不覆盖只失效，`invalid_at` 标记何时不再成立，`superseded_by`
    指向接替它的那条。两个参照物都没有这一层——openworker 直接改写，DeepTutor 靠 LLM
    重新合并文档——但记忆面板的「历史」视图和"模型改错了要能看见它改了什么"都靠它。

    `forgotten_at` 是软删除：客户端的「撤销」和模型误删后的恢复都要能拿回原文，
    硬删掉就没有第二次机会了。真正的清理由用户在记忆面板里显式做。

    **没有 embedding。** 原来 RAG 侧那张表带 1024 维向量和 HNSW 部分索引，用于
    `recall_memories` 的语义召回；pgvector 退役、RAG 主答路径删掉之后那个函数没有
    调用者了。留着一列永远不写的向量，只会让人以为召回还在工作。
    """

    id: UUID
    scope: MemoryScope
    conversation_id: UUID | None
    workspace_path: str | None
    key: str | None
    content: str
    source: Literal["agent", "user"]
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None
    category: MemoryCategory = "fact"
    confidence: float = 1.0
    pinned: bool = False
    valid_from: datetime | None = None
    invalid_at: datetime | None = None
    superseded_by: UUID | None = None
    access_count: int = 0
    last_used_at: datetime | None = None
    source_message_id: UUID | None = None
    run_id: UUID | None = None

    @property
    def active(self) -> bool:
        return self.forgotten_at is None and self.invalid_at is None


class MemoryNotFoundError(LookupError):
    pass


class MemoryScopeError(ValueError):
    pass


class PinnedMemoryError(ValueError):
    """置顶记忆是用户明确按住的那几条，模型不能在背后改写或失效它。"""


@dataclass(frozen=True)
class MemoryExtractionJob:
    """一次待提炼的对话来源。

    来源快照（content / conversation_id / source_created_at）随作业一起存，
    不在 claim 时回查会话——省掉"作业还在、来源消息已被删除"这一整类失败。
    """

    id: UUID
    run_id: UUID
    conversation_id: UUID | None
    source_message_id: UUID | None
    content: str
    source_created_at: datetime
    status: Literal["queued", "running", "done", "failed"]
    attempts: int
    error: str | None
    created_at: datetime


@dataclass(frozen=True)
class CapabilityGrantRecord:
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    grant_source: str
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > datetime.now(UTC)
        )


@dataclass(frozen=True)
class ApprovalRuleRecord:
    """一条常驻审批规则：命中它的调用不再逐次弹审批。

    它**不**替代 capability：能力回答"这个会话能不能碰这类资源"，规则回答"这一类具体
    调用还要不要再问一次人"。两道闸门串联，规则永远只能省掉后者。
    """

    id: UUID
    conversation_id: UUID
    scope: ApprovalRuleScope
    schedule_id: UUID | None
    tool: str
    match_kind: ApprovalMatchKind
    target: str | None
    created_by: str
    revoked_at: datetime | None
    created_at: datetime

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class InboxBindingRecord:
    """一个命名 Inbox 及其可选的投递绑定。

    Inbox 永远以应用内为准（store of record），绑定只是把同一条 item **镜像**到一个
    聊天频道。双向：投递时把 item id 编进卡片按钮，点击回来按同一个 id 解析。
    """

    id: UUID
    name: str
    platform: MessagingPlatform | None
    chat_id: str | None
    connector_account_id: UUID | None
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class ChannelSubscriptionRecord:
    """一个会话对某个频道的订阅：把频道消息**带进来**。

    与 Inbox 路由方向相反，别指到同一个频道上——那会把"我发出去的审批"和"我听进来的
    消息"搅在一起，形成自问自答的回路。
    """

    id: UUID
    conversation_id: UUID
    platform: MessagingPlatform
    chat_id: str
    connector_account_id: UUID | None
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class ThreadSessionRecord:
    """@机器人开出来的会话，以及它拥有的那条 thread。

    键是 thread 目标串本身，和发消息、和授权用的是同一个字符串——一份真相服务三处，
    重启后也能从这里把"可以直接回这条 thread"的授权重新推出来。
    """

    target: str
    conversation_id: UUID
    platform: MessagingPlatform
    chat_id: str
    thread_id: str
    created_at: datetime


@dataclass(frozen=True)
class UnroutedRecord:
    """死信：无处投递的入站消息、以及失败的后台轮次。

    这是可见性设施，不是队列——条目只在界面里被读到，不会被重投。没有它，这两类失败
    就是彻底静默的。
    """

    id: UUID
    kind: UnroutedKind
    platform: MessagingPlatform | None
    chat_id: str | None
    summary: str
    payload: dict[str, Any]
    created_at: datetime


# 用户在开场界面选的那一档玩法。与 `CoworkState.mode`（plan / execute 审批档位）正交：
# 这一个说"干哪一类活"，那一个说"写工具放不放行"。
#
# 只有两档是刻意的。曾经还有一档"知识研究"，它的快捷任务指向工作区文件、提示词却指向
# 资料库，两个语料在同一档里打架；而"读一份文档整理脉络"本来就是论文阅读在做的事，
# 只是那一档给的是 locator 可回溯的引用。删掉它不影响 `search_knowledge`——工具照常
# 注册，模型需要查资料库时仍然会用，只是不再有一个专属档位去暗示它该用。
CoworkWorkMode = Literal["office", "reading"]


@dataclass(frozen=True)
class PathAuthorization:
    conversation_id: UUID
    root_id: UUID
    root_path: Path
    target_path: Path
    access_mode: AccessMode
    capability: Capability


@dataclass(frozen=True)
class ArtifactRecord:
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    session_root_id: UUID | None
    kind: ArtifactKind
    title: str
    uri: str
    mime_type: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CoworkAttachmentRecord:
    id: UUID
    conversation_id: UUID
    message_id: UUID | None
    run_id: UUID | None
    kind: AttachmentKind
    filename: str
    media_type: str
    storage_path: str
    size_bytes: int
    sha256: str
    extracted_text: str


@dataclass(frozen=True)
class SteeringRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    content: str
    status: str
    created_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class InboxRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    kind: InteractionKind
    status: InteractionStatus
    resume_token: UUID
    tool_call_id: str
    plan_step_id: UUID
    request: dict[str, Any]
    response: dict[str, Any] | None
    created_at: datetime
    responded_at: datetime | None
    unattended: bool


@dataclass(frozen=True)
class UnattendedInboxRecord:
    item: InboxRecord
    run_goal: str
    run_status: str
    schedule_id: UUID | None
    schedule_title: str | None


@dataclass(frozen=True)
class ScheduleRecord:
    id: UUID
    conversation_id: UUID
    title: str
    goal: str
    schedule_kind: ScheduleKind
    cron_expression: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    run_count: int
    skipped_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ScheduleView:
    schedule: ScheduleRecord
    last_run_status: str | None
    pending_inbox_count: int
    workspace_label: str | None
    workspace_path: str | None
