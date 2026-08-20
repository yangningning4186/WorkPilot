/**
 * run 事件协议，与后端 app/schemas/runs.py 和 docs/08 §3.2 一一对应。
 *
 * 字段名保持 snake_case：后端契约就是 snake_case，前端不做转换（CLAUDE.md 命名约定）。
 */

export type RunEventType =
  | "message.start"
  | "message.delta"
  | "citation"
  | "message.done"
  | "plan"
  | "step.update"
  | "tool.start"
  | "tool.result"
  | "tool.error"
  | "context.compacted"
  | "todo.update"
  | "steering.queued"
  | "steering.applied"
  | "interrupt"
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

export interface MessageDeltaPayload {
  text: string;
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
}

export interface ToolEventPayload {
  step_id?: string;
  step_idx?: number;
  tool: string | null;
  error?: string;
  phase?: string;
  reused?: boolean;
  effect_ref?: string | null;
}

export interface ContextCompactedPayload {
  reason: "threshold" | "provider_overflow";
  mode: "summary" | "summary_fallback" | "trim";
  revision: number;
  summary_upto: number;
  archived_messages: number;
  before_tokens: number;
  after_tokens: number;
}

export interface InterruptPayload {
  inbox_id?: string;
  kind:
    | "write_confirm"
    | "ask_user"
    | "directory_request"
    | "capability_request"
    | "shell_approval"
    | "external_approval";
  resume_token: string;
  payload: Record<string, unknown>;
}

export interface InteractionResolvedPayload {
  inbox_id: string;
  kind: "ask_user" | "directory_request" | "capability_request" | "shell_approval" | "external_approval";
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
  status?: "done" | "failed" | "cancelled" | "budget_exceeded";
}

export type RunEventData =
  | MessageStartPayload
  | MessageDeltaPayload
  | CitationPayload
  | MessageDonePayload
  | PlanPayload
  | StepUpdatePayload
  | ToolEventPayload
  | ContextCompactedPayload
  | InterruptPayload
  | InteractionResolvedPayload
  | ArtifactPayload
  | TodoUpdatePayload
  | RunDonePayload
  | ErrorPayload;

/**
 * SSE data: 字段里的信封。
 *
 * seq 是字符串——后端是 BIGINT，直接当 number 用会在 2^53 之后丢精度。
 * 所有比较都要走 BigInt，不要 parseInt。
 */
export interface StreamEnvelope<T extends RunEventData = RunEventData> {
  id: string;
  run_id: string;
  seq: string;
  type: RunEventType;
  data: T;
}

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
