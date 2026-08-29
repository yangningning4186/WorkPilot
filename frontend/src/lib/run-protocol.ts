/**
 * run 事件协议，与后端 app/schemas/runs.py 和 docs/08 §3.2 一一对应。
 *
 * 字段名保持 snake_case：后端契约就是 snake_case，前端不做转换（CLAUDE.md 命名约定）。
 */

export type RunEventType =
  | "agent.start"
  | "agent.end"
  | "turn.start"
  | "turn.end"
  | "message.start"
  | "message.delta"
  // 终态正文的原子替换，用于对齐流式显示与落盘消息。
  | "message.snapshot"
  // 清掉此前 delta 累积出来的正文。Cowork 一轮可能先写一段话再调工具，下一轮再写一段；
  // 没有这条，把每轮正文首尾相接之后显示的既不是最终回答，也不等于落盘的那条消息——
  // 刷新一次页面内容就变了。
  | "message.reset"
  // 思考过程的增量。与 message.delta 分开：它不进消息、不落盘，由下一条 reset 清掉。
  | "message.reasoning"
  | "citation"
  | "citation.validation_failed"
  | "message.done"
  | "plan"
  | "step.update"
  // 模型仍在拼装 tool call；只携带安全元数据，不包含可能有正文/凭据的参数片段。
  | "tool.prepare"
  | "tool.start"
  | "tool.update"
  | "tool.result"
  | "tool.error"
  | "context.compacted"
  | "todo.update"
  | "memory.saved"
  | "conversation.title"
  | "reading.goto"
  | "reading.annotated"
  // 只读子 Agent 的调查进度，挂在发起它的那次 explore 工具调用上。
  | "subagent.progress"
  | "team.created"
  | "team.worker.started"
  | "team.pause"
  | "team.resume"
  | "team.archive"
  | "team.revoke_write_delegation"
  | "board.task.created"
  | "board.task.assigned"
  | "board.task.review"
  | "board.task.failed"
  | "board.task.reviewed"
  | "board.task.resolved"
  | "team.summary"
  | "steering.queued"
  | "steering.applied"
  | "queue.message.queued"
  | "queue.message.applied"
  | "queue.message.cancelled"
  | "interrupt"
  // 免审批放行：会话处于 auto 档、命中常驻规则、或仓库白名单 + 目录信任同时成立。
  // 必须在时间线上看得见，否则用户只会看到一条命令凭空执行了。
  | "approval.waived"
  | "approval.semantic_review"
  | "cowork.persona.reselected"
  | "run.sleeping"
  | "interaction.resolved"
  | "artifact"
  | "run.done"
  | "error";

/** 引用的完整定位元数据。只有 bbox 四个数不够，换个渲染器就会高亮错位（约束 3）。 */
export interface CitationLocation {
  page_no: number;
  bbox_norm: [number, number, number, number];
  page_width: number;
  page_height: number;
  rotation: number;
  coord_origin: string;
}

export interface CitationPayload {
  citation_id: string;
  block_id: string;
  version_id: string;
  doc_id: string;
  title: string;
  source_uri: string;
  quote: string;
  char_start: number;
  char_end: number;
  heading_path: string[];
  locations: CitationLocation[];
}

export interface MessageStartPayload {
  message_id: string;
}

export interface AgentLifecyclePayload {
  workflow_type?: string;
  status?: string;
  worker_id?: string;
}

export interface TurnLifecyclePayload {
  turn_id: string;
  iteration: number;
  recovery: number;
  status: "running" | "completed" | "failed" | "cancelled";
  stop_reason?: string;
  model?: string;
  provider?: string;
  tool_call_count?: number;
}

export interface ToolUpdatePayload {
  step_id: string;
  tool_call_id: string;
  tool: string;
  update_type: string;
  update: Record<string, unknown>;
}

export interface ToolPreparePayload {
  tool_call_index: number;
  tool_call_id: string | null;
  tool: string | null;
  arguments_received_chars: number;
}

export interface MessageDeltaPayload {
  text: string;
}

export interface MessageSnapshotPayload {
  text: string;
}

export interface MessageReasoningPayload {
  text: string;
}

export interface ConversationTitlePayload {
  conversation_id: string;
  title: string;
}

export interface MessageDonePayload {
  message_id: string;
  refused: boolean;
  refusal_reason: string | null;
  /** 这条回答是否基于资料库。false 时正文不可溯源，必须挂免责标识。 */
  grounded: boolean;
  latency_ms: number;
  cost_usd: string;
}

export interface ErrorPayload {
  user_message: string;
  retryable: boolean;
  code: string;
}

export interface CitationValidationFailedPayload {
  attempt: number;
  errors: string[];
}

export interface SteeringQueuedPayload {
  message_id: string;
  message: string;
}

export interface SteeringAppliedPayload {
  message_ids: string[];
  count: number;
}

export type QueuedMessageDelivery = "steer" | "follow_up" | "next_run";
export type QueuedMessageStatus = "pending" | "ready" | "consumed" | "cancelled";

export interface QueuedMessageResponse {
  message_id: string;
  run_id: string;
  conversation_id: string;
  message: string;
  requested_delivery: QueuedMessageDelivery;
  delivery: QueuedMessageDelivery;
  status: QueuedMessageStatus;
}

export interface QueueMessageQueuedPayload {
  message_id: string;
  message: string;
  requested_delivery: QueuedMessageDelivery;
  delivery: QueuedMessageDelivery;
  status: QueuedMessageStatus;
}

export interface QueueMessageAppliedPayload {
  message_ids: string[];
  count: number;
  delivery: QueuedMessageDelivery;
}

export interface QueueMessageCancelledPayload {
  message_id: string;
}

export type AgentStepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface AgentPlanStepPayload {
  id: string;
  idx: number;
  description: string;
  tool: string | null;
  depends_on: number[];
  status: AgentStepStatus;
  /** 步骤完成/失败时的一句话说明，来自 step.update。 */
  summary?: string;
}

export interface PlanPayload {
  workflow_type: "literature_review" | "cowork";
  steps?: AgentPlanStepPayload[];
  mode?: "dynamic_tool_loop";
  tools?: CoworkToolCatalogEntry[];
}

export interface CoworkToolCatalogEntry {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  capability: string;
  risk: "read" | "write" | "external";
  effect: "none" | "filesystem" | "external";
  parallel_safe: boolean;
  execution?: "local" | "interaction";
}

/**
 * 工具调用在任务过程里的安全展示信息。
 *
 * 后端只从白名单字段提取并截断，不能用原始 arguments 代替：事件会持久化回放，参数里
 * 可能含文件正文、连接器请求体或凭据。
 */
export interface ToolActivityPayload {
  title: string;
  summary?: string;
  target?: string;
  target_kind?: "text" | "code" | "path" | "url";
}

/**
 * step_id / step_idx 可缺省：watchdog 恢复失联 run 时发的是**run 级**通知
 * （"正在从 checkpoint 恢复"），它不属于任何一个计划步骤。
 */
export interface StepUpdatePayload {
  step_id?: string;
  step_idx?: number;
  status: AgentStepStatus | "recovering";
  summary?: string;
  recovery_count?: number;
  tool?: string;
  activity?: ToolActivityPayload;
}

export interface ToolEventPayload {
  step_id?: string;
  step_idx?: number;
  tool: string | null;
  error?: string;
  phase?: string;
  reused?: boolean;
  effect_ref?: string | null;
  authorization_receipt?: Record<string, unknown> | null;
  activity?: ToolActivityPayload;
  usage?: { input_tokens: number; output_tokens: number };
  terminate?: boolean;
}

export interface ContextCompactedPayload {
  reason: "threshold" | "provider_overflow";
  mode: "summary" | "summary_fallback" | "trim";
  revision: number;
  summary_upto: number;
  turn_prefix_upto: number;
  archived_messages: number;
  before_tokens: number;
  after_tokens: number;
  /** 触发判定所用的上下文量；可能来自 provider usage，而非完整历史估算。 */
  trigger_tokens: number;
  trigger_source: "provider_usage" | "estimate";
}

export interface RuntimeAuditPayload {
  [key: string]: unknown;
}

export interface InterruptPayload {
  inbox_id?: string;
  kind:
    | "write_confirm"
    | "ask_user"
    | "directory_request"
    | "capability_request"
    | "shell_approval"
    | "external_approval"
    | "plan_approval";
  resume_token: string;
  payload: Record<string, unknown>;
}

/** 一次免审批放行。`reason` 说明是哪条来源，好让用户能顺着它找到要撤销的东西。 */
export interface ApprovalWaivedPayload {
  tool: string;
  reason: "approval_mode=auto" | "standing_rule" | "workspace_trust";
  rule_id?: string;
  match_kind?: "action_target" | "argv_pattern" | "tool" | "target" | "command_prefix";
  scope?: "conversation" | "schedule";
  allowlist_entry?: string;
  command?: string;
}

export interface InteractionResolvedPayload {
  inbox_id: string;
  kind:
    | "ask_user"
    | "directory_request"
    | "capability_request"
    | "shell_approval"
    | "external_approval"
    | "plan_approval";
  status: "answered" | "approved" | "rejected";
}

export type TodoStatus = "pending" | "in_progress" | "done";

export interface TodoItem {
  content: string;
  status: TodoStatus;
}

/** 模型通过 todo_write 主动声明的任务清单；整份替换，不是增量。 */
export interface TodoUpdatePayload {
  todos: TodoItem[];
  total: number;
  done: number;
  in_progress: number;
  pending: number;
}

/** 模型写入长期记忆后的通知，带旧文本以支持撤销。 */
/** 引用高亮的一块几何。字段与 `parsed_block_locations` 同口径（后端约束 3）。 */
export interface ReadingLocation {
  page_no: number;
  page_width: number;
  page_height: number;
  rotation: number;
  coord_origin: string;
  bbox_norm: [number, number, number, number];
}

/**
 * 模型调用 `reader_goto` 的结果：把阅读器带到某个 locator 并高亮一段。
 *
 * `locations` 为空是有意义的一档——引文没能逐字对上时翻页但不画高亮。跨语言问答里
 * 模型给的"引文"往往是它自己的译文，此时落在正确的页上远比原地不动有用，而在错误的
 * 位置涂一块颜色比不涂更糟。
 */
export interface ReadingGotoPayload {
  path: string;
  material_id: string;
  unit: "page" | "section";
  locator: number;
  quote: string;
  locations: ReadingLocation[];
}

export type AnnotationColor = "yellow" | "green" | "blue" | "pink";

/**
 * 模型调用 `reader_annotate` 的结果：在文档上留下一块**会持久保存**的高亮。
 *
 * 与 goto 分成两条事件而不是复用一条：面板对两者的反应不同。跳转要移动视口；批注只是
 * 多出一块永久高亮，视口不该被拽走——用户可能正在读别的地方。另一半不对称在后端：
 * 引文对不上时 goto 降级成只翻页，annotate 直接失败，因为它会留在磁盘上。
 */
export interface ReadingAnnotatedPayload extends ReadingGotoPayload {
  annotation_id: string;
  note: string;
  color: AnnotationColor;
}

export interface MemorySavedPayload {
  action: "saved" | "updated" | "forgotten";
  memory: {
    id: string;
    scope: "global" | "workspace" | "conversation";
    key: string | null;
    workspace_path: string | null;
    forgotten: boolean;
    updated_at: string;
  };
  previous_memory_id: string | null;
}

/**
 * 只读子 Agent（`explore`）的调查进度。
 *
 * 它一次要跑最多四轮模型调用加八次工具调用，期间时间线上只有一张不动的卡片——没有
 * 这条事件，用户既看不出它在干什么，事后也查不到这次委派花了多少。`used_tokens` 是
 * 子 Agent 自己那份账：花的仍是同一个 run 的预算，但要能单独看见。
 */
export interface SubagentProgressPayload {
  step_id: string;
  tool_call_id: string;
  agent: "explore";
  phase: "started" | "round" | "tool" | "finished";
  round: number;
  max_rounds: number;
  calls_used: number;
  used_tokens: number;
  /** phase=started */
  question?: string;
  /** phase=round：这一轮模型点名要调的工具。 */
  planned_tools?: string[];
  /** phase=tool */
  tool_name?: string;
  ok?: boolean;
  error?: string;
  /** phase=finished：为什么停下来的。 */
  status?: "answered" | "call_limit" | "round_limit" | "cancelled";
  answer_chars?: number;
}

export type BoardTaskStatus =
  | "open"
  | "in_progress"
  | "blocked"
  | "review"
  | "done"
  | "cancelled";

export type BoardCompletionKind = "pending" | "complete" | "partial" | "cancelled";

export interface TeamWorkerPayload {
  name: string;
  role: string;
  session_id: string;
}

export interface BoardTaskPayload {
  task_id: string;
  title: string;
  description: string;
  acceptance_criteria: string;
  resource_scope: Array<{ path: string; access_mode: "read_only" | "read_write" }>;
  status: BoardTaskStatus;
  completion_kind: BoardCompletionKind;
  assignee: string | null;
  attempt_count: number;
  retry_count: number;
  worker_report: string | null;
  review_comment: string | null;
  rejection_reason: string | null;
  last_error: string | null;
}

export interface TeamCreatedPayload {
  team_id: string;
  workers: TeamWorkerPayload[];
}

export interface TeamWorkerStartedPayload {
  task_id: string;
  worker: string;
  session_id: string;
  attempt_count: number;
  retry_count: number;
}

export interface TeamSummaryPayload extends TeamCreatedPayload {
  completion_status: "complete" | "partial";
  tasks: BoardTaskPayload[];
  counts: Record<BoardTaskStatus, number>;
}

/** run 自己挂起到某个时间点：在等时间，不是在等人，不需要用户操作。 */
export interface RunSleepingPayload {
  wake_at: string;
  reason: string | null;
}

export interface ArtifactPayload {
  kind: "review_preview" | "written_note" | "file";
  artifact_id?: string;
  title: string;
  content?: string;
  effect_ref?: string;
  path?: string;
  content_sha256?: string;
  reused?: boolean;
}

export interface RunDonePayload {
  workflow_type: "answer" | "literature_review" | "cowork";
  effect_ref?: string | null;
  status?: "done" | "partial" | "failed" | "cancelled" | "budget_exceeded";
}

export interface RunEventPayloadMap {
  "agent.start": AgentLifecyclePayload;
  "agent.end": AgentLifecyclePayload;
  "turn.start": TurnLifecyclePayload;
  "turn.end": TurnLifecyclePayload;
  "message.start": MessageStartPayload;
  "message.delta": MessageDeltaPayload;
  "message.snapshot": MessageSnapshotPayload;
  "message.reset": RuntimeAuditPayload;
  "message.reasoning": MessageReasoningPayload;
  citation: CitationPayload;
  "citation.validation_failed": CitationValidationFailedPayload;
  "message.done": MessageDonePayload;
  plan: PlanPayload;
  "step.update": StepUpdatePayload;
  "tool.prepare": ToolPreparePayload;
  "tool.start": ToolEventPayload;
  "tool.update": ToolUpdatePayload;
  "tool.result": ToolEventPayload;
  "tool.error": ToolEventPayload;
  "context.compacted": ContextCompactedPayload;
  "todo.update": TodoUpdatePayload;
  "memory.saved": MemorySavedPayload;
  "conversation.title": ConversationTitlePayload;
  "reading.goto": ReadingGotoPayload;
  "reading.annotated": ReadingAnnotatedPayload;
  "subagent.progress": SubagentProgressPayload;
  "team.created": TeamCreatedPayload;
  "team.worker.started": TeamWorkerStartedPayload;
  "team.pause": RuntimeAuditPayload;
  "team.resume": RuntimeAuditPayload;
  "team.archive": RuntimeAuditPayload;
  "team.revoke_write_delegation": RuntimeAuditPayload;
  "board.task.created": BoardTaskPayload;
  "board.task.assigned": BoardTaskPayload;
  "board.task.review": BoardTaskPayload;
  "board.task.failed": BoardTaskPayload;
  "board.task.reviewed": BoardTaskPayload;
  "board.task.resolved": BoardTaskPayload;
  "team.summary": TeamSummaryPayload;
  "steering.queued": SteeringQueuedPayload;
  "steering.applied": SteeringAppliedPayload;
  "queue.message.queued": QueueMessageQueuedPayload;
  "queue.message.applied": QueueMessageAppliedPayload;
  "queue.message.cancelled": QueueMessageCancelledPayload;
  interrupt: InterruptPayload;
  "approval.waived": ApprovalWaivedPayload;
  "approval.semantic_review": RuntimeAuditPayload;
  "cowork.persona.reselected": RuntimeAuditPayload;
  "run.sleeping": RunSleepingPayload;
  "interaction.resolved": InteractionResolvedPayload;
  artifact: ArtifactPayload;
  "run.done": RunDonePayload;
  error: ErrorPayload;
}

export type RunEventData = RunEventPayloadMap[RunEventType];

/**
 * SSE data: 字段里的信封。
 *
 * seq 是字符串——后端是 BIGINT，直接当 number 用会在 2^53 之后丢精度。
 * 所有比较都要走 BigInt，不要 parseInt。
 */
interface StreamEnvelopeBase {
  id: string;
  run_id: string;
  seq: string;
  /** 后端持久化事件时间；旧 sidecar 可能不带，因此消费端要保留兼容回退。 */
  created_at?: string;
}

export type StreamEnvelope = {
  [Type in RunEventType]: StreamEnvelopeBase & {
    type: Type;
    data: RunEventPayloadMap[Type];
  };
}[RunEventType];

export function envelopeSeq(envelope: StreamEnvelope): bigint {
  return BigInt(envelope.seq);
}

/** 事件已经在终态，SSE 不会再有后续。 */
export function isTerminalEvent(type: RunEventType): boolean {
  return type === "message.done" || type === "run.done" || type === "error";
}

function hasStringField(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === "string";
}

/**
 * 解析并校验一个信封。
 *
 * 宁可丢掉一个畸形事件也不能让它污染已经渲染出来的正文，因此解析失败返回 null
 * 而不是抛错——SSE 是长连接，一次异常会把整条流打断。
 */
export function parseEnvelope(raw: string): StreamEnvelope | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) {
    return null;
  }
  const value = parsed as Record<string, unknown>;
  if (
    !hasStringField(value, "id") ||
    !hasStringField(value, "run_id") ||
    !hasStringField(value, "seq") ||
    !hasStringField(value, "type")
  ) {
    return null;
  }
  if (typeof value.data !== "object" || value.data === null) {
    return null;
  }
  try {
    BigInt(value.seq as string);
  } catch {
    return null;
  }
  return value as unknown as StreamEnvelope;
}
