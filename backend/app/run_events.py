"""持久化 run event 的封闭名称，以及由名称判别的 payload union。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict, cast, get_args

RunEventType = Literal[
    "agent.start",
    "agent.end",
    "turn.start",
    "turn.end",
    "message.start",
    "message.delta",
    "message.snapshot",
    "message.reset",
    "message.reasoning",
    "citation",
    "citation.validation_failed",
    "message.done",
    "plan",
    "step.update",
    "tool.prepare",
    "tool.start",
    "tool.update",
    "tool.result",
    "tool.error",
    "context.compacted",
    "todo.update",
    "memory.saved",
    "conversation.title",
    "reading.goto",
    "reading.annotated",
    "subagent.progress",
    "team.created",
    "team.worker.started",
    "team.pause",
    "team.resume",
    "team.archive",
    "team.revoke_write_delegation",
    "board.task.created",
    "board.task.assigned",
    "board.task.review",
    "board.task.failed",
    "board.task.reviewed",
    "board.task.resolved",
    "team.summary",
    "steering.queued",
    "steering.applied",
    "queue.message.queued",
    "queue.message.applied",
    "queue.message.cancelled",
    "interrupt",
    "approval.waived",
    "approval.semantic_review",
    "cowork.persona.reselected",
    "run.sleeping",
    "interaction.resolved",
    "artifact",
    "run.done",
    "error",
]

MessageStreamEventType = Literal["message.delta", "message.reasoning"]
RUN_EVENT_TYPES: frozenset[str] = frozenset(get_args(RunEventType))


class EmptyPayload(TypedDict):
    pass


class AgentLifecyclePayload(TypedDict, total=False):
    workflow_type: str
    status: str
    worker_id: str


class TurnLifecyclePayload(TypedDict, total=False):
    turn_id: str
    iteration: int
    recovery: int
    status: Literal["running", "completed", "failed", "cancelled"]
    stop_reason: str
    model: str
    provider: str
    tool_call_count: int


class ToolUpdatePayload(TypedDict):
    step_id: str
    tool_call_id: str
    tool: str
    update_type: str
    update: dict[str, Any]


class ToolPreparePayload(TypedDict):
    tool_call_index: int
    tool_call_id: str | None
    tool: str | None
    arguments_received_chars: int


class MessageStartPayload(TypedDict):
    message_id: str


class MessageTextPayload(TypedDict):
    text: str


class MessageDonePayload(TypedDict, total=False):
    message_id: str
    status: str
    refused: bool
    refusal_reason: str | None
    grounded: bool
    latency_ms: int
    cost_usd: str


class CitationPayload(TypedDict, total=False):
    citation_id: str
    block_id: str
    version_id: str
    document_id: str
    doc_id: str
    title: str
    source_uri: str
    quote: str
    quote_sha256: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]
    material_id: str | None
    locator: int | None
    verified: bool
    tool_call_id: str


class CitationValidationFailedPayload(TypedDict):
    attempt: int
    errors: list[str]


class ConversationTitlePayload(TypedDict):
    conversation_id: str
    title: str


class PlanPayload(TypedDict, total=False):
    workflow_type: str
    mode: Literal["dynamic_tool_loop"]
    cowork_mode: Literal["plan", "execute"]
    work_capabilities: list[str]
    persona: str
    tools: list[dict[str, Any]]
    steps: list[dict[str, Any]]


class StepUpdatePayload(TypedDict, total=False):
    step_id: str
    step_idx: int
    tool: str | None
    status: str
    summary: str
    activity: dict[str, Any]
    error: str
    safe_fallback: bool
    recovery_count: int


class ToolEventPayload(TypedDict, total=False):
    step_id: str
    step_idx: int
    tool: str | None
    error: str
    phase: str
    reused: bool
    effect_ref: str | None
    authorization_receipt: dict[str, Any] | None
    activity: dict[str, Any]
    details: dict[str, Any]
    usage: dict[str, int]
    terminate: bool


class ContextCompactedPayload(TypedDict):
    reason: Literal["threshold", "provider_overflow"]
    mode: Literal["none", "summary", "summary_fallback", "trim"]
    revision: int
    summary_upto: int
    turn_prefix_upto: int
    archived_messages: int
    before_tokens: int
    after_tokens: int
    trigger_tokens: int
    trigger_source: Literal["provider_usage", "estimate"]


class TodoItemPayload(TypedDict):
    content: str
    status: Literal["pending", "in_progress", "done"]


class TodoUpdatePayload(TypedDict):
    todos: list[TodoItemPayload]
    total: int
    done: int
    in_progress: int
    pending: int


class MemorySavedPayload(TypedDict):
    action: Literal["saved", "updated", "forgotten"]
    memory: dict[str, Any]
    previous_memory_id: str | None


class ReadingGotoPayload(TypedDict):
    path: str
    material_id: str
    unit: Literal["page", "section"]
    locator: int
    quote: str
    locations: list[dict[str, Any]]


class ReadingAnnotatedPayload(ReadingGotoPayload):
    annotation_id: str
    note: str
    color: Literal["yellow", "green", "blue", "pink"]


class SubagentProgressPayload(TypedDict, total=False):
    step_id: str
    tool_call_id: str
    agent: Literal["explore"]
    phase: Literal["started", "round", "tool", "finished"]
    round: int
    max_rounds: int
    calls_used: int
    used_tokens: int
    question: str
    planned_tools: list[str]
    tool_name: str
    ok: bool
    error: str
    status: str
    answer_chars: int


class TeamPayload(TypedDict, total=False):
    team_id: str
    status: str
    workers: list[dict[str, Any]]
    pause_reason: str | None
    write_delegation_scope: list[dict[str, Any]]
    write_delegation_active: bool
    write_delegation_receipt_id: str | None
    budget: dict[str, Any]
    note: str
    action: str
    reason: str | None


class TeamWorkerStartedPayload(TypedDict):
    task_id: str
    worker: str
    session_id: str
    attempt_count: int
    retry_count: int
    limits: NotRequired[dict[str, Any]]


class BoardTaskPayload(TypedDict, total=False):
    task_id: str
    title: str
    description: str
    acceptance_criteria: str
    resource_scope: list[dict[str, Any]]
    scope_receipt: dict[str, Any] | None
    status: str
    completion_kind: str
    assignee: str | None
    attempt_count: int
    retry_count: int
    worker_report: str | None
    review_comment: str | None
    rejection_reason: str | None
    last_error: str | None
    worker_session_id: str
    wake_delivery: str
    assignment_state: str
    task_complete: bool
    next_signal: str


class TeamSummaryPayload(TypedDict):
    team_id: str
    completion_status: Literal["complete", "partial"]
    workers: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    counts: dict[str, int]


class SteeringQueuedPayload(TypedDict):
    message_id: str
    message: str


class SteeringAppliedPayload(TypedDict):
    message_ids: list[str]
    count: int


class QueueMessageQueuedPayload(TypedDict):
    message_id: str
    message: str
    requested_delivery: Literal["steer", "follow_up", "next_run"]
    delivery: Literal["steer", "follow_up", "next_run"]
    status: Literal["pending", "ready", "consumed", "cancelled"]


class QueueMessageAppliedPayload(TypedDict):
    message_ids: list[str]
    count: int
    delivery: Literal["steer", "follow_up", "next_run"]


class QueueMessageCancelledPayload(TypedDict):
    message_id: str


class InterruptPayload(TypedDict):
    inbox_id: str
    kind: str
    resume_token: str
    payload: dict[str, Any]


class ApprovalWaivedPayload(TypedDict, total=False):
    tool: str
    reason: str
    rule_id: str
    match_kind: str
    scope: str
    allowlist_entry: str
    command: str


class SemanticReviewPayload(TypedDict, total=False):
    schema: str
    tool: str
    action_sha256: str
    decision: str
    disposition: str
    receipt_id: str
    breaker_tripped: bool
    breaker_persisted: bool
    model: str | None
    provider: str | None


class PersonaReselectedPayload(TypedDict):
    schema_version: str
    persona_snapshot: dict[str, Any]


class RunSleepingPayload(TypedDict):
    wake_at: str
    reason: str | None


class InteractionResolvedPayload(TypedDict):
    inbox_id: str
    kind: str
    status: str


class ArtifactPayload(TypedDict, total=False):
    kind: str
    title: str
    artifact_id: str
    effect_ref: str
    content: str
    path: str
    content_sha256: str
    reused: bool


class RunDonePayload(TypedDict, total=False):
    workflow_type: str
    status: str
    effect_ref: str | None


class ErrorPayload(TypedDict, total=False):
    code: str
    retryable: bool
    user_message: str
    errors: list[str]
    recoveries: int
    dimension: str
    used: int
    limit: int


RunEventPayload = (
    EmptyPayload
    | AgentLifecyclePayload
    | TurnLifecyclePayload
    | ToolUpdatePayload
    | ToolPreparePayload
    | MessageStartPayload
    | MessageTextPayload
    | MessageDonePayload
    | CitationPayload
    | CitationValidationFailedPayload
    | ConversationTitlePayload
    | PlanPayload
    | StepUpdatePayload
    | ToolEventPayload
    | ContextCompactedPayload
    | TodoUpdatePayload
    | MemorySavedPayload
    | ReadingGotoPayload
    | ReadingAnnotatedPayload
    | SubagentProgressPayload
    | TeamPayload
    | TeamWorkerStartedPayload
    | BoardTaskPayload
    | TeamSummaryPayload
    | SteeringQueuedPayload
    | SteeringAppliedPayload
    | QueueMessageQueuedPayload
    | QueueMessageAppliedPayload
    | QueueMessageCancelledPayload
    | InterruptPayload
    | ApprovalWaivedPayload
    | SemanticReviewPayload
    | PersonaReselectedPayload
    | RunSleepingPayload
    | InteractionResolvedPayload
    | ArtifactPayload
    | RunDonePayload
    | ErrorPayload
)

# tuple 的第 0 项就是 discriminator；第 1 项不再是无约束 dict。
RunEventInput = (
    tuple[Literal["agent.start"], AgentLifecyclePayload]
    | tuple[Literal["agent.end"], AgentLifecyclePayload]
    | tuple[Literal["turn.start"], TurnLifecyclePayload]
    | tuple[Literal["turn.end"], TurnLifecyclePayload]
    | tuple[Literal["message.start"], MessageStartPayload]
    | tuple[Literal["message.delta"], MessageTextPayload]
    | tuple[Literal["message.snapshot"], MessageTextPayload]
    | tuple[Literal["message.reset"], EmptyPayload]
    | tuple[Literal["message.reasoning"], MessageTextPayload]
    | tuple[Literal["citation"], CitationPayload]
    | tuple[Literal["citation.validation_failed"], CitationValidationFailedPayload]
    | tuple[Literal["message.done"], MessageDonePayload]
    | tuple[Literal["plan"], PlanPayload]
    | tuple[Literal["step.update"], StepUpdatePayload]
    | tuple[Literal["tool.prepare"], ToolPreparePayload]
    | tuple[Literal["tool.start"], ToolEventPayload]
    | tuple[Literal["tool.update"], ToolUpdatePayload]
    | tuple[Literal["tool.result"], ToolEventPayload]
    | tuple[Literal["tool.error"], ToolEventPayload]
    | tuple[Literal["context.compacted"], ContextCompactedPayload]
    | tuple[Literal["todo.update"], TodoUpdatePayload]
    | tuple[Literal["memory.saved"], MemorySavedPayload]
    | tuple[Literal["conversation.title"], ConversationTitlePayload]
    | tuple[Literal["reading.goto"], ReadingGotoPayload]
    | tuple[Literal["reading.annotated"], ReadingAnnotatedPayload]
    | tuple[Literal["subagent.progress"], SubagentProgressPayload]
    | tuple[Literal["team.created"], TeamPayload]
    | tuple[Literal["team.worker.started"], TeamWorkerStartedPayload]
    | tuple[Literal["team.pause"], TeamPayload]
    | tuple[Literal["team.resume"], TeamPayload]
    | tuple[Literal["team.archive"], TeamPayload]
    | tuple[Literal["team.revoke_write_delegation"], TeamPayload]
    | tuple[Literal["board.task.created"], BoardTaskPayload]
    | tuple[Literal["board.task.assigned"], BoardTaskPayload]
    | tuple[Literal["board.task.review"], BoardTaskPayload]
    | tuple[Literal["board.task.failed"], BoardTaskPayload]
    | tuple[Literal["board.task.reviewed"], BoardTaskPayload]
    | tuple[Literal["board.task.resolved"], BoardTaskPayload]
    | tuple[Literal["team.summary"], TeamSummaryPayload]
    | tuple[Literal["steering.queued"], SteeringQueuedPayload]
    | tuple[Literal["steering.applied"], SteeringAppliedPayload]
    | tuple[Literal["queue.message.queued"], QueueMessageQueuedPayload]
    | tuple[Literal["queue.message.applied"], QueueMessageAppliedPayload]
    | tuple[Literal["queue.message.cancelled"], QueueMessageCancelledPayload]
    | tuple[Literal["interrupt"], InterruptPayload]
    | tuple[Literal["approval.waived"], ApprovalWaivedPayload]
    | tuple[Literal["approval.semantic_review"], SemanticReviewPayload]
    | tuple[Literal["cowork.persona.reselected"], PersonaReselectedPayload]
    | tuple[Literal["run.sleeping"], RunSleepingPayload]
    | tuple[Literal["interaction.resolved"], InteractionResolvedPayload]
    | tuple[Literal["artifact"], ArtifactPayload]
    | tuple[Literal["run.done"], RunDonePayload]
    | tuple[Literal["error"], ErrorPayload]
)


RunEventDraft = tuple[str, dict[str, Any]]


def run_event(event_type: str, payload: Mapping[str, Any]) -> RunEventInput:
    """动态 emitter 的唯一收口；静态 producer 应直接构造 ``RunEventInput``。

    动态工具进度回调在运行时才知道事件名，无法靠 overload 保持判别关系；这里至少保证
    payload 被复制成普通 JSON object，随后返回封闭 union。事件名本身仍由 Literal 限定。
    """

    if event_type not in RUN_EVENT_TYPES:
        raise ValueError(f"未知 RunEvent type: {event_type}")
    return cast("RunEventInput", (cast("RunEventType", event_type), dict(payload)))
