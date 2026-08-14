/** 后端 HTTP 客户端。字段保持 snake_case，与后端契约一致。 */

// 默认走 Next.js 同源 rewrite，浏览器不再直接跨域访问后端。
// NEXT_PUBLIC_API_BASE 仅保留给明确需要直连 API 的部署方式。
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");

export interface CreateRunRequest {
  query: string;
  conversation_id?: string;
  top_k?: number;
}

export interface CreateRunResponse {
  run_id: string;
  conversation_id: string;
  status: string;
}

export interface RunStatusResponse {
  run_id: string;
  conversation_id: string;
  goal: string;
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

export function createRun(body: CreateRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/v1/runs", {
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

export function sourceFileUrl(versionId: string): string {
  return `${API_BASE}/api/v1/documents/${encodeURIComponent(versionId)}/file`;
}

export function sourcePageUrl(versionId: string, pageNo: number): string {
  return `${API_BASE}/api/v1/documents/${encodeURIComponent(versionId)}/pages/${pageNo}.png`;
}
