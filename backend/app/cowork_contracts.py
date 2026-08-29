"""Cowork 产品层与 Store adapter 共用的纯数据契约。

这里与 ``agent_core.contracts`` 的边界是：后者可被任何 Agent 复用；本模块只描述
Cowork 的目录授权、附件、交付物、HITL inbox 和自动化计划。两者都禁止依赖 service、
数据库实现或具体 Agent runtime。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    "network.fetch",
    "browser.read",
    "browser.write",
    "browser.destructive",
    "sandbox.execute",
    "host.execute",
    "external.read",
    "external.write",
    "external.destructive",
]
ActiveCapability = Literal[
    "knowledge.read",
    "filesystem.read",
    "filesystem.write",
    "network.fetch",
    "browser.read",
    "browser.write",
    "browser.destructive",
    "sandbox.execute",
    "host.execute",
    "external.read",
    "external.write",
    "external.destructive",
]
MemoryScope = Literal["global", "workspace", "conversation"]
MemoryCategory = Literal["preference", "profile", "interest", "fact"]
MEMORY_CATEGORIES: frozenset[str] = frozenset({"preference", "profile", "interest", "fact"})
MemoryPolicyMode = Literal["inherit", "on", "off"]
MEMORY_POLICY_MODES: frozenset[str] = frozenset({"inherit", "on", "off"})
MAX_STANDING_RULES_CHARS = 20_000
MEMORY_JOB_RESULT_SCHEMA = "memory_extraction_result.v1"
MEMORY_JOB_RESULT_MAX_BYTES = 16_384
MEMORY_JOB_RESULT_MAX_OPERATIONS = 12
MEMORY_JOB_RESULT_REASON_MAX_CHARS = 500
MEMORY_JOB_ERROR_MAX_CHARS = 500
ArtifactKind = Literal["file", "report", "diff", "table"]
AttachmentKind = Literal["image", "pdf", "text"]
SteeringSource = Literal["local_owner", "external_inbound", "runtime", "unknown"]
QueuedMessageDelivery = Literal["steer", "follow_up", "next_run"]
QueuedMessageStatus = Literal["pending", "ready", "consumed", "cancelled"]
InteractionKind = Literal[
    "ask_user",
    "directory_request",
    "capability_request",
    "shell_approval",
    "external_approval",
    "plan_approval",
]
InteractionStatus = Literal["pending", "answered", "approved", "rejected", "cancelled"]
TeamStatus = Literal["active", "paused", "archived"]
TeamWorkerSessionStatus = Literal["idle", "running", "failed"]
TeamBudgetDimension = Literal["model_calls", "tool_calls", "wall_ms", "assignments"]
TeamBudgetReservationStatus = Literal["active", "settled"]
TeamToolAttemptStatus = Literal["in_flight", "succeeded", "failed", "unknown"]
TeamWakeTargetKind = Literal["none", "lead", "worker"]
TeamWakeDeliveryStatus = Literal["pending", "claimed", "delivered"]
DEFAULT_TEAM_BUDGET_LIMITS: dict[str, int] = {
    "model_calls": 96,
    "tool_calls": 256,
    "wall_ms": 3_600_000,
    "assignments": 24,
}
BoardTaskStatus = Literal["open", "in_progress", "blocked", "review", "done", "cancelled"]
BoardCompletionKind = Literal["pending", "complete", "partial", "cancelled"]
ScheduleKind = Literal["once", "cron"]
# 常驻审批规则。`once` 不落库——它就是现在这套一次性 call-id 集合，留在这里只是为了让
# API 的取值是闭合的。
ApprovalRememberScope = Literal["once", "command", "target"]
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
# 旧值只为读取/撤销历史记录；匹配器 fail closed，不再让它们放行任何调用。
ApprovalMatchKind = Literal[
    "action_target",
    "argv_pattern",
    "tool",
    "target",
    "command_prefix",
]


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


class TeamEventIntegrityError(RuntimeError):
    """Team event sequence、parent linkage 或 hash chain 已损坏。"""


class TeamBudgetExceededError(RuntimeError):
    """Team 的跨 run 累计预算已经耗尽；不会自动扩大或重试。"""

    def __init__(self, dimension: TeamBudgetDimension, *, used: int, limit: int) -> None:
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(f"Team {dimension} 预算已用/预留 {used}，上限 {limit}")


class TeamUnsafeReplayError(RuntimeError):
    """Worker 写工具在崩溃点结果未知，必须交回 Lead，不能盲目重放。"""


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
class OwnerMemoryPolicy:
    save_enabled: bool = True
    recall_enabled: bool = True
    standing_rules: str = ""
    revision: int = 0


@dataclass(frozen=True)
class ConversationMemoryPolicy:
    conversation_id: UUID
    save_mode: MemoryPolicyMode = "inherit"
    recall_mode: MemoryPolicyMode = "inherit"
    revision: int = 0


@dataclass(frozen=True)
class MemoryPolicySnapshot:
    """一次已通过 save gate 的控制面版本，用于 SQLite 写事务内 CAS。"""

    owner_revision: int
    conversation_id: UUID | None
    conversation_revision: int | None


class MemoryPolicyConflictError(RuntimeError):
    """Memory policy 在预检与落库之间漂移或已经关闭。"""

    def __init__(self, reason: str = "memory_policy_revision_conflict") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CoworkMemoryMutation:
    """SQLite 原子 Memory mutation 的完整返回值。"""

    applied: bool
    current_changed: bool
    memory: CoworkMemoryRecord | None
    previous: CoworkMemoryRecord | None = None


def _memory_job_result_reason(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"memory job result {field} 必须是字符串或 null")
    normalized = value.strip()
    if len(normalized) > MEMORY_JOB_RESULT_REASON_MAX_CHARS:
        raise ValueError(
            f"memory job result {field} 不能超过 {MEMORY_JOB_RESULT_REASON_MAX_CHARS} 个字符"
        )
    return normalized or None


def _memory_job_result_uuid(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"memory job result {field} 必须是 UUID 字符串或 null")
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ValueError(f"memory job result {field} 不是合法 UUID") from error


def normalize_memory_job_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规范化可持久化的自动记忆审计结果。

    这是 Store 最终边界使用的严格 allowlist。模型候选的 ``fact``、原始 ``content``、
    confidence 或任意额外字段都会被拒绝，而不是“序列化时顺便带进去”。
    """

    if not isinstance(value, Mapping):
        raise ValueError("memory job result 必须是对象")

    allowed_envelope = {
        "schema_version",
        "status",
        "skipped_reason",
        "operations",
        "truncated_operations",
    }
    extras = set(value) - allowed_envelope
    if extras:
        raise ValueError(f"memory job result 包含未允许字段: {sorted(extras)}")
    if value.get("schema_version") != MEMORY_JOB_RESULT_SCHEMA:
        raise ValueError("memory job result schema_version 无效")
    status = value.get("status")
    if status not in {"completed", "skipped"}:
        raise ValueError("memory job result status 无效")
    skipped_reason = _memory_job_result_reason(value.get("skipped_reason"), field="skipped_reason")
    if status == "skipped" and skipped_reason is None:
        raise ValueError("skipped memory job result 必须提供 skipped_reason")
    raw_operations = value.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("memory job result operations 必须是数组")
    if len(raw_operations) > MEMORY_JOB_RESULT_MAX_OPERATIONS:
        raise ValueError(f"memory job result operations 最多 {MEMORY_JOB_RESULT_MAX_OPERATIONS} 条")
    raw_truncated = value.get("truncated_operations", 0)
    if (
        isinstance(raw_truncated, bool)
        or not isinstance(raw_truncated, int)
        or not 0 <= raw_truncated <= 1_000_000
    ):
        raise ValueError("memory job result truncated_operations 无效")

    allowed_operation = {
        "operation",
        "requested_operation",
        "status",
        "category",
        "scope",
        "reason",
        "skipped_reason",
        "target_memory_id",
        "memory_id",
    }
    normalized_operations: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_operations):
        if not isinstance(raw, Mapping):
            raise ValueError(f"memory job result operations[{index}] 必须是对象")
        extras = set(raw) - allowed_operation
        if extras:
            raise ValueError(
                f"memory job result operations[{index}] 包含未允许字段: {sorted(extras)}"
            )
        operation = raw.get("operation")
        if operation not in {"ADD", "UPDATE", "DELETE", "NOOP", "SKIP"}:
            raise ValueError(f"memory job result operations[{index}].operation 无效")
        requested = raw.get("requested_operation")
        if requested is not None and requested not in {"ADD", "UPDATE", "DELETE", "NOOP"}:
            raise ValueError(f"memory job result operations[{index}].requested_operation 无效")
        operation_status = raw.get("status")
        if operation_status not in {"applied", "skipped", "unchanged", "blocked"}:
            raise ValueError(f"memory job result operations[{index}].status 无效")
        category = raw.get("category")
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"memory job result operations[{index}].category 无效")
        scope = raw.get("scope")
        if scope not in {"global", "workspace", "conversation"}:
            raise ValueError(f"memory job result operations[{index}].scope 无效")
        operation_skipped_reason = _memory_job_result_reason(
            raw.get("skipped_reason"), field=f"operations[{index}].skipped_reason"
        )
        if operation_status == "skipped" and operation_skipped_reason is None:
            raise ValueError(
                f"memory job result operations[{index}] skipped 时必须提供 skipped_reason"
            )
        normalized_operations.append(
            {
                "operation": operation,
                "requested_operation": requested,
                "status": operation_status,
                "category": category,
                "scope": scope,
                "reason": _memory_job_result_reason(
                    raw.get("reason"), field=f"operations[{index}].reason"
                ),
                "skipped_reason": operation_skipped_reason,
                "target_memory_id": _memory_job_result_uuid(
                    raw.get("target_memory_id"),
                    field=f"operations[{index}].target_memory_id",
                ),
                "memory_id": _memory_job_result_uuid(
                    raw.get("memory_id"), field=f"operations[{index}].memory_id"
                ),
            }
        )
    normalized: dict[str, Any] = {
        "schema_version": MEMORY_JOB_RESULT_SCHEMA,
        "status": status,
        "skipped_reason": skipped_reason,
        "operations": normalized_operations,
        "truncated_operations": raw_truncated,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MEMORY_JOB_RESULT_MAX_BYTES:
        raise ValueError(f"memory job result 不能超过 {MEMORY_JOB_RESULT_MAX_BYTES} bytes")
    return normalized


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
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class CapabilityGrantRecord:
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    resource_scope: str | None
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


#: 批注的颜色只是展示语义，不参与匹配。收敛成枚举而不是自由字符串，是为了让面板
#: 有一组确定的样式，也免得模型发明出 "light-ish yellow" 这种前端无法渲染的值。
AnnotationColor = Literal["yellow", "green", "blue", "pink"]


@dataclass(frozen=True)
class ReadingAnnotationRecord:
    """一条持久化批注。

    **锚在 material_id（文件内容哈希）上，不锚路径。** 文件改名或移动之后批注还在；
    文件**内容**变了，批注就不再出现——locator 与字符区间都可能已经指向别的文字，
    此时把高亮照画出来比不画更糟（约束 8 的同一条理由）。旧版本的批注不删除，
    仍然按路径可数，界面据此说清楚"这份文件有 N 条批注属于它的旧版本"。

    与 ``reader_goto`` 的对称性是刻意的：跳转在引文对不上时降级成只翻页不高亮，
    批注则**直接拒绝**——它会留在磁盘上，下次打开还在，是比一次跳转强得多的承诺。
    """

    id: UUID
    material_id: str
    path: str
    locator: int
    quote: str
    note: str
    color: AnnotationColor
    # 约束 3 的完整几何，写入时从命中的 ParsedBlock 原样取。为空表示这条批注只落在
    # locator 上（非 PDF 材料没有 bbox），面板据此只滚动不画框。
    locations: tuple[dict[str, Any], ...]
    conversation_id: UUID | None
    run_id: UUID | None
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
    grant_id: UUID | None = None


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
    source: SteeringSource
    status: QueuedMessageStatus
    created_at: datetime
    consumed_at: datetime | None
    requested_delivery: QueuedMessageDelivery = "steer"
    delivery: QueuedMessageDelivery = "steer"
    cancelled_at: datetime | None = None


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
class TeamRecord:
    """Lead 会话批准后创建的一支持久 Agent Team。"""

    id: UUID
    lead_conversation_id: UUID
    proposal_call_id: str
    status: TeamStatus
    note: str
    # 只有 propose_team 的不可豁免人工审批能够铸造这份委派边界。Board 写任务必须
    # 证明自己是该边界的子集；普通 Lead 的 filesystem.write grant 本身不能替代委派。
    write_delegation_scope: list[dict[str, str]]
    write_delegation_receipt: dict[str, Any] | None
    pause_reason: str | None
    budget_limits: dict[str, int]
    budget_usage: dict[str, int]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TeamWorkerRecord:
    id: UUID
    team_id: UUID
    name: str
    role: str
    reason: str
    session_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class TeamWorkerSessionRecord:
    """Worker 自己的持久会话；state 只含 Worker 历史，不含 Lead messages。"""

    id: UUID
    team_id: UUID
    worker_id: UUID
    status: TeamWorkerSessionStatus
    active_task_id: UUID | None
    state: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class BoardTaskRecord:
    id: UUID
    team_id: UUID
    title: str
    description: str
    acceptance_criteria: str
    resource_scope: list[dict[str, str]]
    # 写任务在创建时由 Team 的人工审批 receipt 派生，分配时重新核验 scope、grant
    # identity 与 receipt hash。只读任务保持为 None，不增加审批或交互。
    scope_receipt: dict[str, Any] | None
    status: BoardTaskStatus
    assignee_worker_id: UUID | None
    assignment_call_id: str | None
    attempt_count: int
    completion_kind: BoardCompletionKind
    worker_report: str | None
    review_comment: str | None
    last_rejection_comment: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TeamEventRecord:
    """Team/Board 关键变更的不可变审计记录。sequence 与 hash chain 都按 Team 隔离。"""

    id: UUID
    team_id: UUID
    sequence: int
    event_type: str
    actor: str
    cause: str
    parent_event_id: UUID | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


@dataclass(frozen=True)
class TeamEventVerification:
    team_id: UUID
    valid: bool
    event_count: int
    head_sequence: int
    head_hash: str
    verified_at: datetime


@dataclass(frozen=True)
class TeamEventCursorRecord:
    team_id: UUID
    consumer: str
    last_sequence: int
    last_event_hash: str
    updated_at: datetime


@dataclass(frozen=True)
class TeamWakeDeliveryRecord:
    """Team event 的持久 feed/outbox 项；delivery id 是下游幂等键。"""

    id: UUID
    team_id: UUID
    event_id: UUID
    event_sequence: int
    event_hash: str
    event_type: str
    target_kind: TeamWakeTargetKind
    target_id: str | None
    payload: dict[str, Any]
    status: TeamWakeDeliveryStatus
    attempt_count: int
    claim_owner: str | None
    claim_until: datetime | None
    validation_outcome: Literal["deliver", "suppress"] | None
    validated_at: datetime | None
    delivery_receipt: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None


@dataclass(frozen=True)
class TeamProjectionSummaryRecord:
    """从事件重放得到的独立 projection；暂不替换现有 Team/Board 热表。"""

    team_id: UUID
    watermark: int
    head_hash: str
    summary: dict[str, Any]
    rebuilt_at: datetime


@dataclass(frozen=True)
class TeamBudgetReservationRecord:
    id: UUID
    team_id: UUID
    task_id: UUID
    assignment_call_id: str
    status: TeamBudgetReservationStatus
    reserved: dict[str, int]
    used: dict[str, int]
    created_at: datetime
    updated_at: datetime
    settled_at: datetime | None


@dataclass(frozen=True)
class TeamWorkerToolAttemptRecord:
    id: UUID
    team_id: UUID
    session_id: UUID
    task_id: UUID
    tool_call_id: str
    tool_name: str
    effect: str
    retry_safe: bool
    status: TeamToolAttemptStatus
    arguments_sha256: str
    attempt_count: int
    result: dict[str, Any] | None
    effect_ref: str | None
    authorization_receipt: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None
    updated_at: datetime


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
