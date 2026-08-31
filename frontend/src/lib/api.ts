/** 后端 HTTP 客户端。字段保持 snake_case，与后端契约一致。 */

import type {
  CitationPayload,
  QueuedMessageDelivery,
  QueuedMessageResponse,
  ReadingLocation,
  StreamEnvelope,
} from "./run-protocol";
import { getDesktopContext } from "./desktop";

// 默认走 Next.js 同源 rewrite，浏览器不再直接跨域访问后端。
// NEXT_PUBLIC_API_BASE 仅保留给明确需要直连 API 的部署方式。
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");

/** OAuth 控制台必须登记绝对回调地址；桌面端优先使用当前 sidecar 的真实 API 地址。 */
export async function connectorOAuthCallbackUrl(): Promise<string> {
  const desktop = await getDesktopContext();
  const configuredBase = desktop?.api_base ?? API_BASE;
  const browserOrigin = typeof window === "undefined" ? "" : window.location.origin;
  const base = configuredBase
    ? configuredBase.startsWith("http://") || configuredBase.startsWith("https://")
      ? configuredBase
      : `${browserOrigin}${configuredBase}`
    : browserOrigin;
  return `${base.replace(/\/$/, "")}/api/v1/connectors/oauth/callback`;
}

/** grounded = 依据资料库回答；general = 用户在拒答后显式选择的通用知识回答。 */
export type AnswerMode = "grounded" | "general";
export type WorkflowType = "answer" | "literature_review" | "cowork";

export interface CreateRunResponse {
  run_id: string;
  conversation_id: string;
  conversation_title: string | null;
  status: string;
  workflow_type: WorkflowType;
}

export interface ConversationSummary {
  id: string;
  title: string | null;
  /** 非终态 run。切换会话仅断开当前订阅，切回后用它继续回放。 */
  active_run_id: string | null;
  message_count: number;
  latest_message: string | null;
  last_message_at: string | null;
  provider_profile_id: string | null;
  provider_name: string | null;
  provider: string | null;
  selected_model: string | null;
  unattended: boolean;
  approval_mode: "interactive" | "auto";
  persona_name: string;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationRuntimeUpdate {
  provider_profile_id: string | null;
  model_override: string | null;
  unattended: boolean;
  /** 自主权上限。默认必须是 interactive：漏传这个字段不该把会话悄悄升级成免审批。 */
  approval_mode: "interactive" | "auto";
  persona_name: string;
}

export interface ExpertTeamMember {
  profile: string;
  label: string;
  role: string;
  reason: string;
  tool_patterns: string[];
}

export interface Persona {
  name: string;
  label: string;
  description: string;
  tool_patterns: string[];
  default_approval_mode: "interactive" | "auto";
  recommended_connectors: ConnectorKind[];
  recommended_work_mode: CoworkWorkMode;
  expert_type: "agent" | "team";
  team_members: ExpertTeamMember[];
  origin: "builtin" | "user" | "project";
}

export interface PersonaListResponse {
  items: Persona[];
  errors: string[];
  project_paths: string[];
}

/** 一条常驻审批规则。只能在审批卡片上产生，没有创建接口。 */
export interface ApprovalRule {
  id: string;
  conversation_id: string;
  scope: "conversation" | "schedule";
  schedule_id: string | null;
  tool: string;
  match_kind: "action_target" | "argv_pattern" | "tool" | "target" | "command_prefix";
  target: string | null;
  created_by: string;
  revoked_at: string | null;
  active: boolean;
  created_at: string;
}

export interface WorkspaceTrustEntry {
  canonical_path: string;
  trusted: boolean;
  declared: string[];
  rejected: string[];
  config_error: string | null;
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
  attachments: CoworkAttachment[];
  created_at: string;
}

export interface ConversationMessageListResponse {
  items: ConversationMessage[];
  total: number;
}

export interface ConversationLaneNavigation {
  lane: string;
  previous_head_entry_id: string | null;
  current_head_entry_id: string | null;
  abandoned_lane: string | null;
  branch_summary_entry_id: string | null;
}

export type ConversationForkPosition = "before" | "after";

export interface ConversationContextUsage {
  used_tokens: number;
  context_window_tokens: number;
  max_input_tokens: number;
  trigger_tokens: number;
  trigger_ratio: number;
  auto_compaction: boolean;
  compaction_revision: number;
  compaction_mode: "none" | "summary" | "summary_fallback" | "trim";
  model: string;
  run_status: string | null;
  estimated: boolean;
  breakdown: {
    system: number;
    tool_manifest: number;
    tools: number;
    loaded_tools: number;
    messages: number;
    tool_activity: number;
  };
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
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  if (init?.body !== undefined && !isFormData && !headers.has("content-type")) {
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

export function fetchConversations(archived = false): Promise<ConversationListResponse> {
  return request<ConversationListResponse>(
    `/api/v1/conversations?archived=${archived ? "true" : "false"}`,
  );
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

export function setConversationArchived(
  conversationId: string,
  archived: boolean,
): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/v1/conversations/${conversationId}/archive`, {
    method: "PUT",
    body: JSON.stringify({ archived }),
  });
}

export function updateConversationRuntime(
  conversationId: string,
  body: ConversationRuntimeUpdate,
): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/v1/conversations/${conversationId}/runtime`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function fetchPersonas(conversationId?: string): Promise<PersonaListResponse> {
  const query = conversationId === undefined
    ? ""
    : `?conversation_id=${encodeURIComponent(conversationId)}`;
  return request<PersonaListResponse>(`/api/v1/personas${query}`);
}

export function fetchConversationMessages(
  conversationId: string,
): Promise<ConversationMessageListResponse> {
  return request<ConversationMessageListResponse>(
    `/api/v1/conversations/${conversationId}/messages`,
  );
}

export function forkConversation(
  conversationId: string,
  messageId: string,
  position: ConversationForkPosition = "after",
): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/v1/conversations/${conversationId}/fork`, {
    method: "POST",
    body: JSON.stringify({ message_id: messageId, position }),
  });
}

export function navigateConversationLane(
  conversationId: string,
  targetEntryId: string,
  position: ConversationForkPosition = "after",
): Promise<ConversationLaneNavigation> {
  return request<ConversationLaneNavigation>(
    `/api/v1/conversations/${conversationId}/lanes/main/navigate`,
    {
      method: "POST",
      body: JSON.stringify({
        target_entry_id: targetEntryId,
        position,
        summarize: true,
      }),
    },
  );
}

export function fetchConversationContextUsage(
  conversationId: string,
): Promise<ConversationContextUsage> {
  return request<ConversationContextUsage>(
    `/api/v1/conversations/${conversationId}/context-usage`,
    { cache: "no-store" },
  );
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

export function queueCoworkMessage(
  runId: string,
  message: string,
  delivery: QueuedMessageDelivery,
): Promise<QueuedMessageResponse> {
  return request<QueuedMessageResponse>(`/api/v1/runs/${runId}/queued-messages`, {
    method: "POST",
    body: JSON.stringify({ message, delivery }),
  });
}

export function cancelQueuedCoworkMessage(runId: string, messageId: string): Promise<void> {
  return requestVoid(`/api/v1/runs/${runId}/queued-messages/${messageId}`, {
    method: "DELETE",
  });
}

export interface CoworkInteractionResponse {
  approved?: boolean;
  answer?: string;
  path?: string;
  /**
   * 记住这次批准的粒度。默认 once：漏传这个字段只授权这一次，不会留下常驻规则。
   * command 只对没有 shell 操作符的命令有效；target 只对声明了目标字段的工具有效。
   */
  remember?: "once" | "command" | "target";
}

export function fetchApprovalRules(conversationId: string): Promise<{ items: ApprovalRule[] }> {
  return request<{ items: ApprovalRule[] }>(
    `/api/v1/cowork/sessions/${conversationId}/approval-rules`,
  );
}

export function revokeApprovalRule(conversationId: string, ruleId: string): Promise<void> {
  return request<void>(
    `/api/v1/cowork/sessions/${conversationId}/approval-rules/${ruleId}`,
    { method: "DELETE" },
  );
}

export function fetchWorkspaceTrust(
  conversationId: string,
): Promise<{ items: WorkspaceTrustEntry[] }> {
  return request<{ items: WorkspaceTrustEntry[] }>(
    `/api/v1/cowork/sessions/${conversationId}/workspace-trust`,
  );
}

export function setWorkspaceTrust(
  conversationId: string,
  canonicalPath: string,
  trusted: boolean,
): Promise<{ items: WorkspaceTrustEntry[] }> {
  return request<{ items: WorkspaceTrustEntry[] }>(
    `/api/v1/cowork/sessions/${conversationId}/workspace-trust`,
    { method: "PUT", body: JSON.stringify({ canonical_path: canonicalPath, trusted }) },
  );
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
  attachment_ids?: string[];
  /** 系统选择器点名的本机原文件；所在目录须已明确授予当前会话。 */
  workspace_files?: string[];
  /** 计划模式：只放行只读工具，先出方案等你批准再动手。 */
  plan_mode?: boolean;
  /** 开场界面选的玩法。与 plan_mode 正交：论文阅读也可以先出计划。 */
  work_mode?: CoworkWorkMode;
  /** 论文阅读模式下打开的文档路径；边界仍由每次工具调用的目录授权把关。 */
  reading_path?: string | null;
  /**
   * 发送这一刻阅读器停在哪、用户划着哪一句。阅读器 → 模型的反向通道：没有它，
   * "这段是什么意思"在模型那里无法解析，它只能猜最近提过的那一处。
   */
  reading_viewport?: ReadingViewportPayload | null;
}

export interface ReadingViewportPayload {
  locator?: number;
  selection?: string;
  unit?: "page" | "section";
}

/** 用户在开场界面选的那一档。后端 `app/cowork_contracts.py` 是同一份定义。 */
export type CoworkWorkMode = "office" | "reading";

export interface CoworkAttachment {
  id: string;
  conversation_id: string;
  message_id: string | null;
  run_id: string | null;
  kind: "image" | "pdf" | "text";
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
}

export type CoworkAccessMode = "read_only" | "read_write";
export type CoworkCapability =
  | "knowledge.read"
  | "filesystem.read"
  | "filesystem.write"
  | "office.word.edit"
  | "office.excel.edit"
  | "network.read"
  | "browser.control"
  | "shell.execute"
  | "external.action"
  | "network.fetch"
  | "browser.read"
  | "browser.write"
  | "browser.destructive"
  | "sandbox.execute"
  | "host.execute"
  | "external.read"
  | "external.write"
  | "external.destructive";

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
  resource_scope: string | null;
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

export type ArtifactValidationStatus = "passed" | "warning" | "failed" | "not_run";

export interface ArtifactValidationCheck {
  name: string;
  status: ArtifactValidationStatus;
  message: string;
  value: number | string | boolean | null;
}

export interface ArtifactValidationDimension {
  status: ArtifactValidationStatus;
  checks: ArtifactValidationCheck[];
}

export interface ArtifactEvidenceRef {
  citation_id: string;
  kind: string | null;
  title: string | null;
  source_uri: string | null;
  quote: string | null;
  locator: number | null;
  locations: Array<Record<string, unknown>>;
}

export interface ArtifactClaimBinding {
  claim_id: string;
  claim: string;
  target_type: string;
  target_id: string;
  evidence: ArtifactEvidenceRef[];
  missing_evidence_ids: string[];
}

export interface ArtifactManifestPayload {
  schema_version: 1;
  status: "candidate" | "validated" | "failed";
  artifact_type: "docx" | "xlsx" | "pptx" | "pdf" | "html";
  skill: {
    name: string;
    origin: "builtin" | "user" | "project" | "unknown";
    sha256: string | null;
    kind: "planning" | "artifact" | "workflow" | "action" | "unknown";
  };
  runtime: {
    profile: string;
    sandboxed_generated_code: boolean;
    renderer: string;
  };
  validation: Record<string, ArtifactValidationStatus>;
  validation_report: {
    structural: ArtifactValidationDimension;
    semantic: ArtifactValidationDimension;
    visual: ArtifactValidationDimension;
    evidence: ArtifactValidationDimension;
    security: ArtifactValidationDimension;
  };
  quality: { score: number; warnings: string[] };
  evidence_bindings: ArtifactClaimBinding[];
}

export function createCoworkRun(body: CreateCoworkRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/v1/runs/cowork", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function uploadCoworkAttachment(
  conversationId: string,
  file: File,
): Promise<CoworkAttachment> {
  const body = new FormData();
  body.append("upload", file, file.name);
  return request<CoworkAttachment>(
    `/api/v1/cowork/sessions/${conversationId}/attachments`,
    { method: "POST", body },
  );
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

export function revokeCoworkGrant(conversationId: string, grantId: string): Promise<void> {
  return requestVoid(`/api/v1/cowork/sessions/${conversationId}/grants/${grantId}`, {
    method: "DELETE",
  });
}

export function fetchCoworkArtifacts(
  conversationId: string,
): Promise<{ items: CoworkArtifact[] }> {
  return request<{ items: CoworkArtifact[] }>(
    `/api/v1/cowork/sessions/${conversationId}/artifacts`,
  );
}

export type CoworkMemoryScope = "global" | "workspace" | "conversation";

export interface CoworkMemory {
  id: string;
  scope: CoworkMemoryScope;
  conversation_id: string | null;
  workspace_path: string | null;
  key: string | null;
  content: string;
  source: "agent" | "user";
  created_at: string;
  updated_at: string;
  forgotten_at: string | null;
}

export function fetchCoworkMemories(
  conversationId: string,
  options: { includeForgotten?: boolean } = {},
): Promise<{ items: CoworkMemory[] }> {
  const query = options.includeForgotten ? "?include_forgotten=true" : "";
  return request<{ items: CoworkMemory[] }>(
    `/api/v1/cowork/sessions/${conversationId}/memories${query}`,
  );
}

export function createCoworkMemory(
  conversationId: string,
  body: { content: string; scope: CoworkMemoryScope; key?: string | null },
): Promise<CoworkMemory> {
  return request<CoworkMemory>(`/api/v1/cowork/sessions/${conversationId}/memories`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** 改写或恢复一条记忆；客户端的「撤销」也走这里。 */
export function patchCoworkMemory(
  memoryId: string,
  body: { content?: string; restore?: boolean },
): Promise<CoworkMemory> {
  return request<CoworkMemory>(`/api/v1/cowork/memories/${memoryId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function forgetCoworkMemory(memoryId: string): Promise<void> {
  return requestVoid(`/api/v1/cowork/memories/${memoryId}`, { method: "DELETE" });
}

export function undoCoworkMemoryUpdate(
  memoryId: string,
  previousMemoryId: string,
): Promise<CoworkMemory> {
  return request<CoworkMemory>(`/api/v1/cowork/memories/${memoryId}/undo-update`, {
    method: "POST",
    body: JSON.stringify({ previous_memory_id: previousMemoryId }),
  });
}

export interface ArtifactPreviewPayload {
  blob: Blob;
  mode: "quicklook" | "libreoffice" | "native-pdf" | "structure" | "text" | "offline-html" | "unknown";
}

export interface ArtifactDiffPayload {
  schema_version: 1;
  available: boolean;
  format: "unified";
  view: "text" | "semantic" | "unavailable";
  created: boolean;
  before_sha256: string | null;
  after_sha256: string | null;
  added_lines: number;
  removed_lines: number;
  truncated: boolean;
  text: string;
  reason: string | null;
}

export async function fetchArtifactPreview(artifactId: string): Promise<ArtifactPreviewPayload> {
  const response = await apiFetch(`/api/v1/cowork/artifacts/${artifactId}/preview`);
  if (!response.ok) throw new ApiError(response.status, await response.text());
  const rawMode = response.headers.get("x-workpilot-preview-mode") ?? "unknown";
  const modes = new Set(["quicklook", "libreoffice", "native-pdf", "structure", "text", "offline-html"]);
  const mode = modes.has(rawMode) ? rawMode as ArtifactPreviewPayload["mode"] : "unknown";
  return { blob: await response.blob(), mode };
}

export function fetchArtifactDiff(artifactId: string): Promise<ArtifactDiffPayload> {
  return request<ArtifactDiffPayload>(`/api/v1/cowork/artifacts/${artifactId}/diff`);
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
  workspace_label: string | null;
  workspace_path: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateCoworkScheduleRequest {
  conversation_id?: string;
  workspace_path?: string;
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
  kind: "ask_user" | "directory_request" | "capability_request" | "shell_approval" | "external_approval";
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
  origin: SkillOrigin;
  kind: SkillKind;
  runtime_profile: string;
  compatibility: string[];
}

export interface SkillsStatusResponse {
  source_path: string;
  /** 出厂 Skill 的安装位；随应用发布，只读。 */
  builtin_path: string;
  /** 当前会话已授权工作区里的 project Skill 目录。 */
  project_paths: string[];
  /** 传入 conversation_id 时由后端回显；未选择会话时为 null。 */
  conversation_id: string | null;
  /** 仅对当前会话生效的 deny 集合，不改变 Skill 的全局启用状态。 */
  muted_names: string[];
  snapshot_sha256: string;
  /** 已应用当前会话静音策略、实际会注入运行时的 Skill。 */
  skills: SkillSummary[];
  /** 应用会话静音前的有效目录，用于展示和恢复本会话 Skill。 */
  available_skills: SkillSummary[];
  errors: string[];
  /** 被同名用户 Skill 盖住的出厂 Skill 名字。 */
  shadowed: string[];
  installed: ManagedSkill[];
}

export type SkillOrigin = "builtin" | "user" | "project";
export type SkillKind = "planning" | "artifact" | "workflow" | "action";

export interface ManagedSkill {
  name: string;
  enabled: boolean;
  description: string | null;
  sha256: string | null;
  resources: string[];
  error: string | null;
  /**
   * `builtin` 随产品出厂。名字在两层里可以重复，所以列表的 key 必须是
   * `origin + name`，而不是 name。
   */
  origin: SkillOrigin;
  /** 出厂 Skill 被同名用户 Skill 盖住；仍然列出来，否则 fork 过这件事看不出来。 */
  shadowed: boolean;
  /** 出厂 Skill 不可卸载，只能停用或 fork。 */
  removable: boolean;
  kind: SkillKind;
  runtime_profile: string;
  compatibility: string[];
  resource_counts: {
    references: number;
    scripts: number;
    assets: number;
    evals: number;
    other: number;
  };
}

export interface SkillCandidate {
  // 候选的身份就是它的目录名；后端不再发 UUID。
  capability_key: string;
  suggested_name: string;
  description: string;
  skill_md: string;
  tools: string[];
  confidence: number;
  status: "collecting" | "promoted" | "needs_review" | "rejected";
  evidence_count: number;
  promoted_name: string | null;
  review_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface SkillCandidatesResponse {
  enabled: boolean;
  auto_promotion_enabled: boolean;
  min_evidence: number;
  min_confidence: number;
  source_path: string;
  items: SkillCandidate[];
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  trusted: boolean;
  transport: "stdio" | "streamable_http" | "http";
  configured_tools: number;
  eligible_read_tools: number;
  eligible_action_tools: number;
  blocked_side_effect_tools: number;
  blocked_data_scope_tools: number;
  catalog_sha256: string | null;
  oauth_connector_id: string | null;
  command: string | null;
  args: string[];
  cwd: string | null;
  url: string | null;
  env_names: string[];
  header_names: string[];
  tools: Record<string, McpToolPolicy>;
}

export interface McpToolPolicy {
  enabled: boolean;
  side_effect: boolean;
  approval: "always" | "never";
  data_scope: "deny" | "corpus_allowed";
  when_to_use: string;
  when_not_to_use: string;
}

export interface McpServerInput {
  enabled: boolean;
  trusted: boolean;
  transport: "stdio" | "streamable_http" | "http";
  command?: string;
  args?: string[];
  cwd?: string;
  url?: string;
  oauth_connector_id?: string;
  tools?: Record<string, McpToolPolicy>;
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
    approval: "always" | "never";
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

export function fetchSkillsStatus(conversationId?: string): Promise<SkillsStatusResponse> {
  const query = conversationId === undefined
    ? ""
    : `?conversation_id=${encodeURIComponent(conversationId)}`;
  return request<SkillsStatusResponse>(`/api/v1/integrations/skills${query}`);
}

export function setSessionSkillMuted(
  conversationId: string,
  skillName: string,
  muted: boolean,
): Promise<SkillsStatusResponse> {
  return request<SkillsStatusResponse>(
    `/api/v1/integrations/skills/session/${encodeURIComponent(conversationId)}/${encodeURIComponent(skillName)}`,
    { method: "PUT", body: JSON.stringify({ muted }) },
  );
}

export function fetchSkillCandidates(): Promise<SkillCandidatesResponse> {
  return request<SkillCandidatesResponse>("/api/v1/integrations/skills/candidates");
}

export function promoteSkillCandidate(capabilityKey: string): Promise<SkillCandidate> {
  return request<SkillCandidate>(
    `/api/v1/integrations/skills/candidates/${encodeURIComponent(capabilityKey)}/promote`,
    { method: "POST" },
  );
}

export function rejectSkillCandidate(capabilityKey: string): Promise<SkillCandidate> {
  return request<SkillCandidate>(
    `/api/v1/integrations/skills/candidates/${encodeURIComponent(capabilityKey)}/reject`,
    { method: "POST" },
  );
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

export function saveMcpServer(name: string, input: McpServerInput): Promise<McpStatusResponse> {
  return request<McpStatusResponse>(
    `/api/v1/integrations/mcp/servers/${encodeURIComponent(name)}`,
    { method: "PUT", body: JSON.stringify(input) },
  );
}

export function deleteMcpServer(name: string): Promise<void> {
  return requestVoid(`/api/v1/integrations/mcp/servers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export function saveMcpToolPolicy(
  serverName: string,
  toolName: string,
  policy: McpToolPolicy,
): Promise<McpStatusResponse> {
  return request<McpStatusResponse>(
    `/api/v1/integrations/mcp/servers/${encodeURIComponent(serverName)}/tools/${encodeURIComponent(toolName)}`,
    { method: "PUT", body: JSON.stringify(policy) },
  );
}

export function pinMcpCatalog(serverName: string): Promise<{ catalog_sha256: string }> {
  return request<{ catalog_sha256: string }>(
    `/api/v1/integrations/mcp/${encodeURIComponent(serverName)}/pin`,
    { method: "POST" },
  );
}

export function saveSkill(
  name: string,
  skillMd: string,
  replace: boolean,
): Promise<ManagedSkill> {
  return request<ManagedSkill>(`/api/v1/integrations/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ skill_md: skillMd, enabled: true, replace }),
  });
}

export function setSkillEnabled(name: string, enabled: boolean): Promise<ManagedSkill> {
  return request<ManagedSkill>(
    `/api/v1/integrations/skills/${encodeURIComponent(name)}/enabled`,
    { method: "PATCH", body: JSON.stringify({ enabled }) },
  );
}

export function deleteSkill(name: string): Promise<void> {
  return requestVoid(`/api/v1/integrations/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export type ProviderKind =
  | "openai"
  | "anthropic"
  | "gemini"
  | "deepseek"
  | "qwen"
  | "ollama"
  | "openai_compatible";

export interface ProviderProfile {
  id: string;
  name: string;
  provider: ProviderKind;
  base_url: string;
  default_model: string;
  context_window_tokens: number;
  enabled: boolean;
  has_api_key: boolean;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ProviderInput {
  name: string;
  provider: ProviderKind;
  base_url: string;
  default_model: string;
  api_key?: string;
  enabled: boolean;
  metadata: Record<string, unknown>;
}

export function fetchProviders(): Promise<{ items: ProviderProfile[] }> {
  return request<{ items: ProviderProfile[] }>("/api/v1/providers");
}

export function createProvider(body: ProviderInput): Promise<ProviderProfile> {
  return request<ProviderProfile>("/api/v1/providers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateProvider(
  id: string,
  body: Partial<Omit<ProviderInput, "provider">>,
): Promise<ProviderProfile> {
  return request<ProviderProfile>(`/api/v1/providers/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteProvider(id: string): Promise<void> {
  return requestVoid(`/api/v1/providers/${id}`, { method: "DELETE" });
}

export function probeProvider(
  id: string,
): Promise<{ ok: boolean; models: string[]; latency_ms: number; message: string }> {
  return request(`/api/v1/providers/${id}/probe`, { method: "POST" });
}

/** 平台集合由后端 Connector Descriptor catalog 决定，不在客户端维护第二份 union。 */
export type ConnectorKind = string;

export interface ConnectorAccount {
  id: string;
  kind: ConnectorKind;
  name: string;
  auth_type: "oauth2" | "token" | "app_credentials";
  status: "configured" | "authorizing" | "connected" | "expired" | "error";
  config: Record<string, unknown>;
  scopes: string[];
  capabilities: string[];
  external_account_id: string | null;
  external_account_name: string | null;
  expires_at: string | null;
  last_checked_at: string | null;
  last_error: string | null;
  enabled: boolean;
  has_secrets: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConnectorDescriptor {
  kind: ConnectorKind;
  label: string;
  blurb: string;
  logo: string;
  brand_color: string;
  category: "china_office" | "developer";
  auth_types: Array<"oauth2" | "token" | "app_credentials">;
  default_scopes: string[];
  capabilities: string[];
}

export interface ConnectorInput {
  kind: ConnectorKind;
  name: string;
  auth_type: "oauth2" | "token" | "app_credentials";
  client_id?: string;
  client_secret?: string;
  access_token?: string;
  redirect_uri?: string;
  scopes: string[];
  config: Record<string, unknown>;
  enabled: boolean;
}

export function fetchConnectors(): Promise<{ items: ConnectorAccount[] }> {
  return request<{ items: ConnectorAccount[] }>("/api/v1/connectors");
}

export function fetchConnectorCatalog(): Promise<{ items: ConnectorDescriptor[] }> {
  return request<{ items: ConnectorDescriptor[] }>("/api/v1/connectors/catalog");
}

export function createConnector(body: ConnectorInput): Promise<ConnectorAccount> {
  return request<ConnectorAccount>("/api/v1/connectors", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateConnector(
  id: string,
  body: Partial<Omit<ConnectorInput, "kind" | "auth_type">> & { clear_secrets?: boolean },
): Promise<ConnectorAccount> {
  return request<ConnectorAccount>(`/api/v1/connectors/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteConnector(id: string): Promise<void> {
  return requestVoid(`/api/v1/connectors/${id}`, { method: "DELETE" });
}

export function startConnectorOAuth(
  id: string,
): Promise<{ authorization_url: string; state: string; expires_at: string }> {
  return request(`/api/v1/connectors/${id}/oauth/start`, {
    method: "POST",
    body: JSON.stringify({}),
  });
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
  prompt_cache_read_tokens: number;
  prompt_cache_write_tokens: number;
  prompt_cache_read_rate: number;
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
  prompt_cache_read_tokens: number;
  prompt_cache_write_tokens: number;
}

export interface CostOverviewResponse {
  totals: {
    call_count: number;
    cached_count: number;
    cache_hit_rate: number;
    prompt_cache_read_tokens: number;
    prompt_cache_write_tokens: number;
    prompt_cache_read_rate: number;
    total_tokens: number;
    failed_count: number;
    fallback_count: number;
    /** 有单价的调用条数与没单价的条数分列，避免把"缺价"读成"免费"。 */
    priced_count: number;
    unpriced_count: number;
    cost_usd: string | null;
    cost_status: string;
    window_from: string | null;
    window_to: string | null;
  };
  by_tier: CostTierUsage[];
  by_task_type: CostTaskTypeUsage[];
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

/* ---- 阅读器面板 ---------------------------------------------------------- */

export interface ReadingOutlineEntry {
  locator: number;
  title: string;
  level: number;
  /** 用每个 unit 首行凑的，不是文档自带的章节结构——只能当线索。 */
  synthesised: boolean;
}

export interface ReadingMaterial {
  path: string;
  material_id: string;
  filename: string;
  title: string;
  unit: "page" | "section";
  unit_count: number;
  parser: string;
  /** 只有 PDF 能忠实渲染原页；其余格式显示抽取出来的文本。 */
  has_page_image: boolean;
  outline: ReadingOutlineEntry[];
}

export interface ReadingUnit {
  locator: number;
  unit: "page" | "section";
  text: string;
}

export function fetchReadingMaterial(
  conversationId: string,
  path: string,
): Promise<ReadingMaterial> {
  return request<ReadingMaterial>(
    `/api/v1/cowork/sessions/${conversationId}/reading/material?path=${encodeURIComponent(path)}`,
  );
}

export function fetchReadingUnit(
  conversationId: string,
  path: string,
  locator: number,
): Promise<ReadingUnit> {
  return request<ReadingUnit>(
    `/api/v1/cowork/sessions/${conversationId}/reading/units/${locator}`
      + `?path=${encodeURIComponent(path)}`,
  );
}

export interface ReadingAnnotation {
  id: string;
  locator: number;
  quote: string;
  note: string;
  color: "yellow" | "green" | "blue" | "pink";
  /** 约束 3 的完整几何。空数组表示这条批注只锚在 locator 上（非 PDF 材料）。 */
  locations: ReadingLocation[];
  created_at: string;
}

export interface ReadingAnnotations {
  material_id: string;
  items: ReadingAnnotation[];
  /**
   * 同一路径上属于**别的**内容版本的批注条数。它们不显示——几何可能已经指向别的
   * 文字。但也不能静默消失：改了一版 PDF 之后批注全没了却查不到为什么，
   * 是最糟的那种失败。
   */
  stale_count: number;
}

export function fetchReadingAnnotations(
  conversationId: string,
  path: string,
): Promise<ReadingAnnotations> {
  return request<ReadingAnnotations>(
    `/api/v1/cowork/sessions/${conversationId}/reading/annotations`
      + `?path=${encodeURIComponent(path)}`,
  );
}

/** 删除只开放给用户；模型没有对应的工具，它不知道哪一条是用户自己留的。 */
export function deleteReadingAnnotation(
  conversationId: string,
  path: string,
  annotationId: string,
): Promise<void> {
  return requestVoid(
    `/api/v1/cowork/sessions/${conversationId}/reading/annotations/${annotationId}`
      + `?path=${encodeURIComponent(path)}`,
    { method: "DELETE" },
  );
}

/**
 * 用户自己划出来的批注。
 *
 * 只送 `quote` 和 `locator`，**不送浏览器量出来的矩形**：几何由后端从解析结果里取
 * （约束 3），这样同一份文件上模型留的高亮和用户划的高亮在同一套坐标里。返回的
 * `verified` 说明这段引文有没有逐字对上——没对上照样存，只是画不出框。
 */
export function createReadingAnnotation(
  conversationId: string,
  body: {
    path: string;
    locator: number;
    quote: string;
    note?: string;
    color?: ReadingAnnotation["color"];
  },
): Promise<{ annotation: ReadingAnnotation; verified: boolean }> {
  return request<{ annotation: ReadingAnnotation; verified: boolean }>(
    `/api/v1/cowork/sessions/${conversationId}/reading/annotations`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/**
 * 取原始 PDF 字节，交给 pdf.js 在浏览器里渲染。
 *
 * 取代了"每页一张 PNG"：图片没有文本层，用户选不中、复制不了，也没法把"这一段"
 * 交给模型——阅读器因此只能是单向的。授权是同一条，后端每次都重跑目录检查。
 */
export async function fetchReadingFile(
  conversationId: string,
  path: string,
  materialId: string,
): Promise<ArrayBuffer> {
  const response = await apiFetch(
    `/api/v1/cowork/sessions/${conversationId}/reading/file`
      + `?path=${encodeURIComponent(path)}&v=${encodeURIComponent(materialId)}`,
  );
  if (!response.ok) throw new ApiError(response.status, await response.text());
  return response.arrayBuffer();
}

/**
 * 读取 PDF 原页图片。
 *
 * 不能把 URL 直接交给 img：桌面端 sidecar 端口每次随机，而且所有请求都必须带启动令牌，
 * img 无法添加这个 header。统一经过 apiFetch 取 Blob，Web 端仍会自然携带 owner Cookie。
 * material_id 是内容哈希，只用于让浏览器缓存随文件版本失效。
 */
export async function fetchReadingPage(
  conversationId: string,
  path: string,
  locator: number,
  materialId: string,
): Promise<Blob> {
  const response = await apiFetch(
    `/api/v1/cowork/sessions/${conversationId}/reading/pages/${locator}.png`
      + `?path=${encodeURIComponent(path)}&v=${encodeURIComponent(materialId)}`,
  );
  if (!response.ok) throw new ApiError(response.status, await response.text());
  const contentType = response.headers.get("content-type")?.split(";", 1).at(0)?.trim();
  if (contentType !== "image/png") {
    throw new Error(`阅读器页面返回了意外格式：${contentType ?? "未知"}`);
  }
  return response.blob();
}


/* ---- 消息面 ------------------------------------------------------------- */

export interface InboxBinding {
  id: string;
  name: string;
  platform: "feishu" | null;
  chat_id: string | null;
  connector_account_id: string | null;
  enabled: boolean;
  created_at: string;
}

export interface ChannelSubscription {
  id: string;
  conversation_id: string;
  platform: "feishu";
  chat_id: string;
  connector_account_id: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface UnroutedEntry {
  id: string;
  kind: "inbound" | "background_turn";
  platform: "feishu" | null;
  chat_id: string | null;
  summary: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export function fetchInboxBindings(): Promise<{ items: InboxBinding[] }> {
  return request<{ items: InboxBinding[] }>("/api/v1/messaging/inboxes");
}

export function upsertInboxBinding(
  name: string,
  body: {
    platform: "feishu" | null;
    chat_id: string | null;
    connector_account_id?: string | null;
    enabled?: boolean;
  },
): Promise<InboxBinding> {
  return request<InboxBinding>(`/api/v1/messaging/inboxes/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify({ enabled: true, connector_account_id: null, ...body }),
  });
}

export function deleteInboxBinding(name: string): Promise<void> {
  return request<void>(`/api/v1/messaging/inboxes/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export function fetchChannelSubscriptions(
  conversationId: string,
): Promise<{ items: ChannelSubscription[] }> {
  return request<{ items: ChannelSubscription[] }>(
    `/api/v1/messaging/sessions/${conversationId}/subscriptions`,
  );
}

export function subscribeChannel(
  conversationId: string,
  body: { platform: "feishu"; chat_id: string },
): Promise<ChannelSubscription> {
  return request<ChannelSubscription>(
    `/api/v1/messaging/sessions/${conversationId}/subscriptions`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function unsubscribeChannel(conversationId: string, subscriptionId: string): Promise<void> {
  return request<void>(
    `/api/v1/messaging/sessions/${conversationId}/subscriptions/${subscriptionId}`,
    { method: "DELETE" },
  );
}

export function fetchUnrouted(limit = 50): Promise<{ items: UnroutedEntry[] }> {
  return request<{ items: UnroutedEntry[] }>(`/api/v1/messaging/unrouted?limit=${limit}`);
}

/* ── 本地知识库 ─────────────────────────────────────────────────────────── */

export interface KnowledgeBaseDocument {
  doc_id: string;
  filename: string;
  title: string;
  parser: string;
  char_count: number;
  content_hash: string;
  snapshot_path: string | null;
  snapshot_available: boolean;
}

export interface KnowledgeBaseVersion {
  version_id: string;
  label: string;
  embedding: string;
  engine: "hybrid" | "dense" | "bm25";
  retrieval: string;
  node_count: number;
  stale: boolean;
  is_active: boolean;
}

export interface KnowledgeBase {
  slug: string;
  name: string;
  description: string;
  document_count: number;
  /** 没建过索引、或建到一半失败的库都是 false。挂载允许，但检索会要求先重建。 */
  is_indexed: boolean;
  /** 用哪个 embedding 建的。换模型后会和当前配置对不上，检索随即拒绝服务。 */
  embedding: string | null;
  active_version: string | null;
  versions: KnowledgeBaseVersion[];
  /** 旧的单索引布局只可重建迁移，不会被静默认领成一个缺配置的版本。 */
  needs_migration: boolean;
  documents: KnowledgeBaseDocument[];
}

export interface KnowledgeBaseIndexingJob {
  slug: string;
  status: "running" | "done" | "failed";
  /** 正在做什么：「解析 attention.pdf」「建立索引」。 */
  stage: string;
  done: number;
  total: number;
  added: number;
  error: string | null;
  skipped: Array<{ filename: string; reason: string }>;
}

export function fetchKnowledgeBases(): Promise<{ items: KnowledgeBase[] }> {
  return request<{ items: KnowledgeBase[] }>("/api/v1/cowork/knowledge-bases");
}

export function createKnowledgeBase(body: {
  slug: string;
  name: string;
  description: string;
}): Promise<KnowledgeBase> {
  return request<KnowledgeBase>("/api/v1/cowork/knowledge-bases", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteKnowledgeBase(slug: string): Promise<void> {
  return requestVoid(`/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
}

/** 立刻返回作业状态；解析与 embedding 在后台跑，用 fetchKnowledgeBaseIndexing 轮询。 */
export function addKnowledgeBaseDocuments(
  slug: string,
  paths: string[],
): Promise<KnowledgeBaseIndexingJob> {
  return request<KnowledgeBaseIndexingJob>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/documents`,
    { method: "POST", body: JSON.stringify({ paths }) },
  );
}

export function rebuildKnowledgeBase(slug: string): Promise<KnowledgeBaseIndexingJob> {
  return request<KnowledgeBaseIndexingJob>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/rebuild`,
    { method: "POST" },
  );
}

export function createKnowledgeBaseVersion(
  slug: string,
  body: {
    version_id: string | null;
    label: string;
    engine: "hybrid" | "dense" | "bm25";
    activate: boolean;
  },
): Promise<KnowledgeBaseIndexingJob> {
  return request<KnowledgeBaseIndexingJob>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/versions`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function activateKnowledgeBaseVersion(
  slug: string,
  versionId: string,
): Promise<KnowledgeBase> {
  return request<KnowledgeBase>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/versions/${encodeURIComponent(versionId)}/activate`,
    { method: "POST" },
  );
}

export function deleteKnowledgeBaseVersion(
  slug: string,
  versionId: string,
): Promise<KnowledgeBase> {
  return request<KnowledgeBase>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/versions/${encodeURIComponent(versionId)}`,
    { method: "DELETE" },
  );
}

export function fetchKnowledgeBaseIndexing(
  slug: string,
): Promise<KnowledgeBaseIndexingJob | null> {
  return request<KnowledgeBaseIndexingJob | null>(
    `/api/v1/cowork/knowledge-bases/${encodeURIComponent(slug)}/indexing`,
  );
}

export function fetchSessionKnowledgeBase(
  conversationId: string,
): Promise<{ slug: string | null }> {
  return request<{ slug: string | null }>(
    `/api/v1/cowork/sessions/${conversationId}/knowledge-base`,
  );
}

/** `slug: null` 卸载。 */
export function setSessionKnowledgeBase(
  conversationId: string,
  slug: string | null,
): Promise<{ slug: string | null }> {
  return request<{ slug: string | null }>(
    `/api/v1/cowork/sessions/${conversationId}/knowledge-base`,
    { method: "PUT", body: JSON.stringify({ slug }) },
  );
}
