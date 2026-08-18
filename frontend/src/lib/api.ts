/** 后端 HTTP 客户端。字段保持 snake_case，与后端契约一致。 */

import type { CitationPayload, StreamEnvelope } from "./run-protocol";
import { getDesktopContext } from "./desktop";

// 默认走 Next.js 同源 rewrite，浏览器不再直接跨域访问后端。
// NEXT_PUBLIC_API_BASE 仅保留给明确需要直连 API 的部署方式。
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");

/** grounded = 依据资料库回答；general = 用户在拒答后显式选择的通用知识回答。 */
export type AnswerMode = "grounded" | "general";
export type WorkflowType = "answer" | "literature_review" | "cowork";

export interface CreateRunRequest {
  query: string;
  conversation_id?: string;
  top_k?: number;
  mode?: AnswerMode;
}

export interface CreateReviewRunRequest {
  goal: string;
  document_ids: string[];
  output_path: string;
  conversation_id?: string;
}

export interface ResumeRunRequest {
  resume_token: string;
  approved: boolean;
}

export interface CreateRunResponse {
  run_id: string;
  conversation_id: string;
  status: string;
  workflow_type: WorkflowType;
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  message_count: number;
  latest_message: string | null;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationListResponse {
  items: ConversationSummary[];
  total: number;
}

export interface ConversationMessage {
  id: string;
  seq: number;
  role: "user" | "assistant";
  content: string;
  status: string;
  run_id: string | null;
  citations: CitationPayload[];
  answer_mode: AnswerMode | null;
  created_at: string;
}

export interface ConversationMessageListResponse {
  items: ConversationMessage[];
  total: number;
}

export interface RunStatusResponse {
  run_id: string;
  conversation_id: string;
  goal: string;
  answer_mode: AnswerMode;
  workflow_type: WorkflowType;
  status: string;
  cancel_requested: boolean;
  used_tokens: number;
  used_calls: number;
  next_seq: number;
  error: string | null;
  schedule_id: string | null;
  unattended: boolean;
  run_trigger: "manual" | "schedule" | "catchup";
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const desktop = await getDesktopContext();
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (desktop !== null) {
    headers.set("x-workpilot-launch-token", desktop.launch_token);
  }
  return fetch(`${desktop?.api_base ?? API_BASE}${path}`, {
    ...init,
    // 安全层落地后会用 Cookie session 做对象级鉴权，这里先统一带上凭据，
    // 免得到时候每个调用点都要改一遍。
    // 桌面身份由每次启动的 header 证明，不向 localhost 带 webview cookie。
    credentials: desktop === null ? "include" : "omit",
    headers,
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    throw new ApiError(response.status, await response.text());
  }
  return (await response.json()) as T;
}

/** 204 之类没有响应体的接口；套用 request 会在 response.json() 上炸掉。 */
async function requestVoid(path: string, init?: RequestInit): Promise<void> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    throw new ApiError(response.status, await response.text());
  }
}

/**
 * admin 会话状态。
 *
 * cookie 是 httpOnly 的，前端读不到，唯一可靠的判据就是问后端。
 * `unconfigured` 单独成一档：后端没配 `DEMO_ADMIN_PASSWORD_HASH` 时任何密码都登不进去，
 * 这时候提示"密码错误"会把人往错误方向带（约束 4 同样适用于面向人的错误）。
 */
export type AdminAuthState = "authenticated" | "anonymous" | "unconfigured";

export async function fetchAdminSession(): Promise<AdminAuthState> {
  const response = await apiFetch("/api/v1/auth/admin/session", {
    credentials: "include",
    // 会话状态绝不能吃缓存，否则登出后顶栏还显示已登录。
    cache: "no-store",
  });
  if (response.ok) {
    return "authenticated";
  }
  if (response.status === 401 || response.status === 403) {
    return "anonymous";
  }
  throw new ApiError(response.status, await response.text());
}

export function loginAdmin(password: string): Promise<{ authenticated: boolean }> {
  return request<{ authenticated: boolean }>("/api/v1/auth/admin/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

export function logoutAdmin(): Promise<void> {
  return requestVoid("/api/v1/auth/admin/logout", { method: "POST" });
}

export function createRun(body: CreateRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/v1/runs", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchConversations(): Promise<ConversationListResponse> {
  return request<ConversationListResponse>("/api/v1/conversations");
}

export function createConversation(title = "新会话"): Promise<ConversationSummary> {
  return request<ConversationSummary>("/api/v1/conversations", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export function deleteConversation(conversationId: string): Promise<void> {
  return requestVoid(`/api/v1/conversations/${conversationId}`, { method: "DELETE" });
}

export function fetchConversationMessages(
  conversationId: string,
): Promise<ConversationMessageListResponse> {
  return request<ConversationMessageListResponse>(
    `/api/v1/conversations/${conversationId}/messages`,
  );
}

export function createReviewRun(body: CreateReviewRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/v1/runs/reviews", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function resumeRun(
  runId: string,
  body: ResumeRunRequest,
): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/v1/runs/${runId}/resume`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getRun(runId: string): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/v1/runs/${runId}`);
}

export function cancelRun(runId: string): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/v1/runs/${runId}/cancel`, { method: "POST" });
}

export function steerCoworkRun(runId: string, message: string): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/v1/runs/${runId}/steering`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export interface CoworkInteractionResponse {
  approved?: boolean;
  answer?: string;
  path?: string;
}

export function respondToCoworkInteraction(
  runId: string,
  resumeToken: string,
  body: CoworkInteractionResponse,
): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(
    `/api/v1/runs/${runId}/interactions/${resumeToken}/respond`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function fetchRunEventStream(
  runId: string,
  afterSeq: bigint,
  signal: AbortSignal,
): Promise<Response> {
  return apiFetch(`/api/v1/runs/${runId}/events?after_seq=${afterSeq.toString()}`, {
    cache: "no-store",
    headers: { accept: "text/event-stream" },
    signal,
  });
}

export interface CreateCoworkRunRequest {
  goal: string;
  conversation_id: string;
}

export type CoworkAccessMode = "read_only" | "read_write";
export type CoworkCapability =
  | "filesystem.read"
  | "filesystem.write"
  | "office.word.edit"
  | "office.excel.edit"
  | "network.read"
  | "shell.execute"
  | "external.action";

export interface CoworkRoot {
  id: string;
  conversation_id: string;
  requested_path: string;
  canonical_path: string;
  label: string;
  access_mode: CoworkAccessMode;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface CoworkGrant {
  id: string;
  conversation_id: string;
  session_root_id: string | null;
  capability: CoworkCapability;
  grant_source: string;
  expires_at: string | null;
  revoked_at: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CoworkArtifact {
  id: string;
  conversation_id: string;
  run_id: string | null;
  session_root_id: string | null;
  kind: "file" | "report" | "diff" | "table";
  title: string;
  uri: string;
  mime_type: string | null;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export function createCoworkRun(body: CreateCoworkRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/v1/runs/cowork", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchCoworkRoots(conversationId: string): Promise<{ items: CoworkRoot[] }> {
  return request<{ items: CoworkRoot[] }>(
    `/api/v1/cowork/sessions/${conversationId}/roots`,
  );
}

export function addCoworkRoot(
  conversationId: string,
  body: { path: string; access_mode: CoworkAccessMode; label?: string },
): Promise<CoworkRoot> {
  return request<CoworkRoot>(`/api/v1/cowork/sessions/${conversationId}/roots`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeCoworkRoot(conversationId: string, rootId: string): Promise<void> {
  return requestVoid(`/api/v1/cowork/sessions/${conversationId}/roots/${rootId}`, {
    method: "DELETE",
  });
}

export function fetchCoworkGrants(conversationId: string): Promise<{ items: CoworkGrant[] }> {
  return request<{ items: CoworkGrant[] }>(
    `/api/v1/cowork/sessions/${conversationId}/grants`,
  );
}

export function fetchCoworkArtifacts(
  conversationId: string,
): Promise<{ items: CoworkArtifact[] }> {
  return request<{ items: CoworkArtifact[] }>(
    `/api/v1/cowork/sessions/${conversationId}/artifacts`,
  );
}

export function fetchRunEventLog(
  runId: string,
  afterSeq: bigint,
): Promise<{ items: StreamEnvelope[] }> {
  return request<{ items: StreamEnvelope[] }>(
    `/api/v1/runs/${runId}/event-log?after_seq=${afterSeq.toString()}&limit=200`,
  );
}

export interface CoworkSchedule {
  id: string;
  conversation_id: string;
  title: string;
  goal: string;
  schedule_kind: "once" | "cron";
  cron_expression: string | null;
  run_at: string | null;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_run_status: string | null;
  run_count: number;
  skipped_count: number;
  pending_inbox_count: number;
  created_at: string;
  updated_at: string;
}

export interface CreateCoworkScheduleRequest {
  conversation_id: string;
  title: string;
  goal: string;
  schedule_kind: "once" | "cron";
  cron_expression?: string;
  run_at?: string;
  timezone: string;
}

export interface UnattendedInboxItem {
  id: string;
  run_id: string;
  conversation_id: string;
  schedule_id: string | null;
  schedule_title: string | null;
  run_goal: string;
  run_status: string;
  kind: "ask_user" | "directory_request" | "capability_request" | "shell_approval";
  status: "pending" | "answered" | "approved" | "rejected" | "cancelled";
  resume_token: string;
  request: Record<string, unknown>;
  response: Record<string, unknown> | null;
  created_at: string;
  responded_at: string | null;
}

export function fetchCoworkSchedules(): Promise<{ items: CoworkSchedule[]; total: number }> {
  return request<{ items: CoworkSchedule[]; total: number }>("/api/v1/automations");
}

export function createCoworkSchedule(
  body: CreateCoworkScheduleRequest,
): Promise<CoworkSchedule> {
  return request<CoworkSchedule>("/api/v1/automations", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateCoworkSchedule(
  scheduleId: string,
  body: Partial<Pick<CoworkSchedule, "title" | "goal" | "enabled" | "cron_expression" | "run_at" | "timezone">>,
): Promise<CoworkSchedule> {
  return request<CoworkSchedule>(`/api/v1/automations/${scheduleId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteCoworkSchedule(scheduleId: string): Promise<void> {
  return requestVoid(`/api/v1/automations/${scheduleId}`, { method: "DELETE" });
}

export function runCoworkSchedule(scheduleId: string): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/v1/automations/${scheduleId}/run`, {
    method: "POST",
  });
}

export function fetchUnattendedInbox(
  includeResolved = false,
): Promise<{ items: UnattendedInboxItem[]; total: number }> {
  return request<{ items: UnattendedInboxItem[]; total: number }>(
    `/api/v1/automations/inbox/items?include_resolved=${includeResolved ? "true" : "false"}`,
  );
}

export interface SkillSummary {
  name: string;
  description: string;
  trigger: string[];
  anti_trigger: string[];
  tools: string[];
  sha256: string;
}

export interface SkillsStatusResponse {
  source_path: string;
  snapshot_sha256: string;
  skills: SkillSummary[];
  errors: string[];
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  trusted: boolean;
  transport: "stdio" | "streamable_http" | "http";
  configured_tools: number;
  eligible_read_tools: number;
  blocked_side_effect_tools: number;
  blocked_data_scope_tools: number;
  catalog_sha256: string | null;
}

export interface McpStatusResponse {
  source_path: string | null;
  servers: McpServerStatus[];
}

export interface McpProbeTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  configured_policy: {
    enabled: boolean;
    side_effect: boolean;
    data_scope: "deny" | "corpus_allowed";
    when_to_use: string;
    when_not_to_use: string;
  } | null;
}

export interface McpProbeResponse {
  server: string;
  catalog_sha256: string;
  tools: McpProbeTool[];
}

export function fetchSkillsStatus(): Promise<SkillsStatusResponse> {
  return request<SkillsStatusResponse>("/api/v1/integrations/skills");
}

export function fetchMcpStatus(): Promise<McpStatusResponse> {
  return request<McpStatusResponse>("/api/v1/integrations/mcp");
}

export function probeMcpServer(serverName: string): Promise<McpProbeResponse> {
  return request<McpProbeResponse>(
    `/api/v1/integrations/mcp/${encodeURIComponent(serverName)}/probe`,
    { method: "POST" },
  );
}

/** 资料库读模型，字段与后端 app/schemas/library.py 一一对应。 */
export type DocumentState = "ready" | "parsing" | "failed" | "stale";

export interface LibraryDocument {
  document_id: string;
  version_id: string | null;
  title: string;
  source_uri: string;
  doc_type: string;
  source_name: string;
  source_kind: string;
  source_editable: boolean;
  state: DocumentState;
  parser: string | null;
  parse_error: string | null;
  page_count: number | null;
  block_count: number;
  chunk_count: number;
  searchable_chunk_count: number;
  locatable: boolean;
  version_no: number | null;
  updated_at: string;
}

export interface LibrarySource {
  id: string;
  name: string;
  kind: string;
  sync_status: string;
  sync_error: string | null;
  document_count: number;
  last_sync_at: string | null;
}

export interface LibraryResponse {
  sources: LibrarySource[];
  documents: LibraryDocument[];
  totals: {
    documents: number;
    chunks: number;
    searchable_chunks: number;
    parsing: number;
    failed: number;
  };
}

export function fetchLibrary(query: string): Promise<LibraryResponse> {
  const search = query.trim() === "" ? "" : `?query=${encodeURIComponent(query.trim())}`;
  return request<LibraryResponse>(`/api/v1/library${search}`);
}

/** 办公工作台：权限按 owner session 限时授予，文件范围固定在已注册本地资料目录。 */
export interface EditorPermission {
  granted: boolean;
  scope: "local_office_write";
  expires_in_s: number;
}

export type WorkspaceFileKind = "markdown" | "word" | "excel";

export interface WorkspaceFileSummary {
  file_id: string;
  name: string;
  source_name: string;
  source_uri: string;
  kind: WorkspaceFileKind;
  size_bytes: number;
  updated_at_ns: number;
}

export interface WorkspaceFile extends WorkspaceFileSummary {
  content: string;
  baseline_sha256: string;
  editable: boolean;
}

export interface WorkspaceInstructionResponse {
  file: WorkspaceFile;
  summary: string;
  change_count: number;
  model: string;
  provider: string;
  backup_uri: string | null;
}

export function fetchEditorPermission(): Promise<EditorPermission> {
  return request<EditorPermission>("/api/v1/editor/permission");
}

export function grantEditorPermission(): Promise<EditorPermission> {
  return request<EditorPermission>("/api/v1/editor/permission", { method: "POST" });
}

export function revokeEditorPermission(): Promise<void> {
  return requestVoid("/api/v1/editor/permission", { method: "DELETE" });
}

export function fetchWorkspaceFiles(): Promise<{ items: WorkspaceFileSummary[] }> {
  return request<{ items: WorkspaceFileSummary[] }>("/api/v1/editor/files");
}

export function fetchWorkspaceFile(fileId: string): Promise<WorkspaceFile> {
  return request<WorkspaceFile>(`/api/v1/editor/files/${encodeURIComponent(fileId)}`);
}

export function executeWorkspaceInstruction(
  fileId: string,
  body: {
    baseline_sha256: string;
    instruction: string;
    content?: string;
    selection_start?: number;
    selection_end?: number;
  },
): Promise<WorkspaceInstructionResponse> {
  return request<WorkspaceInstructionResponse>(
    `/api/v1/editor/files/${encodeURIComponent(fileId)}/execute`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/** owner 私有长期记忆；匿名 demo 永远不能读取这些字段。 */
export type MemoryCategory = "preference" | "profile" | "interest" | "fact";
export type MemoryView = "current" | "history";

export interface MemoryRecord {
  id: string;
  category: MemoryCategory;
  fact: string;
  valid_from: string;
  invalid_at: string | null;
  superseded_by: string | null;
  source_type: "conversation" | "manual";
  source_message_id: string | null;
  confidence: number;
  access_count: number;
  last_used_at: string | null;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface MemoryListResponse {
  items: MemoryRecord[];
  total: number;
}

export function fetchMemories(view: MemoryView): Promise<MemoryListResponse> {
  return request<MemoryListResponse>(`/api/v1/memories?view=${view}`);
}

export function createMemory(body: {
  category: MemoryCategory;
  fact: string;
  pinned: boolean;
}): Promise<MemoryRecord> {
  return request<MemoryRecord>("/api/v1/memories", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateMemory(
  memoryId: string,
  body: { category?: MemoryCategory; fact?: string; pinned?: boolean },
): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/api/v1/memories/${memoryId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteMemory(memoryId: string): Promise<void> {
  return requestVoid(`/api/v1/memories/${memoryId}`, { method: "DELETE" });
}

export function restoreMemory(memoryId: string): Promise<MemoryRecord> {
  return request<MemoryRecord>(`/api/v1/memories/${memoryId}/restore`, {
    method: "POST",
  });
}

/**
 * 成本看板。
 *
 * 金额一律是 `string | null` 而不是 number：本机自部署价格表为 0，
 * "没有可用价格"和"测过、就是不要钱"是两回事，缺价时给 null 并附 cost_status，
 * 绝不折成 0（docs/07 §7.4）。前端渲染时也必须保持这个区别。
 */
export interface CostTierUsage {
  tier: string;
  call_count: number;
  cached_count: number;
  failed_count: number;
  fallback_count: number;
  prompt_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  models: string[];
}

export interface CostTaskTypeUsage {
  task_type: string;
  tier: string;
  call_count: number;
  total_tokens: number;
  cache_hit_rate: number;
}

export interface CostBatchSummary {
  batch_id: string;
  label: string;
  tier: string;
  model: string;
  gpu_model: string | null;
  node_count: number;
  task_count: number;
  total_tokens: number;
  output_tokens: number;
  wall_s: string;
  gpu_s: string;
  gpu_s_per_task: string;
  tokens_per_task: number;
  tasks_per_s: string;
  tokens_per_s: string;
  mean_concurrency: string;
  client_occupancy: string;
  price_usd_per_hour: string | null;
  price_source: string | null;
  cost_usd: string | null;
  cost_per_task_usd: string | null;
  cost_per_ktok_usd: string | null;
  cost_status: string;
  cost_reason: string | null;
}

export interface CostOverviewResponse {
  totals: {
    call_count: number;
    cached_count: number;
    cache_hit_rate: number;
    total_tokens: number;
    failed_count: number;
    fallback_count: number;
    batch_count: number;
    priced_batch_count: number;
    unpriced_batch_count: number;
    cost_usd: string | null;
    cost_status: string;
    window_from: string | null;
    window_to: string | null;
  };
  by_tier: CostTierUsage[];
  by_task_type: CostTaskTypeUsage[];
  batches: CostBatchSummary[];
  undeployed_tiers: string[];
}

/** 需要 admin 登录；未登录时后端返回 401。 */
export function fetchCostOverview(days: number): Promise<CostOverviewResponse> {
  return request<CostOverviewResponse>(`/api/v1/cost/overview?days=${days}`);
}

/** 触发同步。需要 admin 登录，未登录时后端返回 401（前端据此提示）。 */
export function syncSource(sourceId: string): Promise<unknown> {
  return request<unknown>(`/api/v1/sources/${sourceId}/sync`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function sourceFileUrl(versionId: string): string {
  return `${API_BASE}/api/v1/documents/${encodeURIComponent(versionId)}/file`;
}

export function sourcePageUrl(versionId: string, pageNo: number): string {
  return `${API_BASE}/api/v1/documents/${encodeURIComponent(versionId)}/pages/${pageNo}.png`;
}
