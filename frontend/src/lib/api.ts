/** 后端 HTTP 客户端。字段保持 snake_case，与后端契约一致。 */

// 默认走 Next.js 同源 rewrite，浏览器不再直接跨域访问后端。
// NEXT_PUBLIC_API_BASE 仅保留给明确需要直连 API 的部署方式。
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");

/** grounded = 依据资料库回答；general = 用户在拒答后显式选择的通用知识回答。 */
export type AnswerMode = "grounded" | "general";
export type WorkflowType = "answer" | "literature_review";

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
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    // 安全层落地后会用 Cookie session 做对象级鉴权，这里先统一带上凭据，
    // 免得到时候每个调用点都要改一遍。
    credentials: "include",
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await response.text());
  }
  return (await response.json()) as T;
}

/** 204 之类没有响应体的接口；套用 request 会在 response.json() 上炸掉。 */
async function requestVoid(path: string, init?: RequestInit): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: { "content-type": "application/json", ...init?.headers },
  });
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
  const response = await fetch(`${API_BASE}/api/v1/auth/admin/session`, {
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

export function runEventsUrl(runId: string, afterSeq: bigint): string {
  return `${API_BASE}/api/v1/runs/${runId}/events?after_seq=${afterSeq.toString()}`;
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
