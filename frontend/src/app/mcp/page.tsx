"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  fetchMcpStatus,
  probeMcpServer,
  type McpProbeResponse,
  type McpStatusResponse,
} from "@/lib/api";

function mcpError(reason: unknown): string {
  if (reason instanceof ApiError) {
    try {
      const body = JSON.parse(reason.message) as { detail?: string };
      return body.detail ?? `MCP 请求失败（${reason.status}）`;
    } catch {
      return `MCP 请求失败（${reason.status}）`;
    }
  }
  return "暂时无法读取 MCP 配置。";
}

export default function McpPage() {
  const [status, setStatus] = useState<McpStatusResponse | null>(null);
  const [probes, setProbes] = useState<Record<string, McpProbeResponse>>({});
  const [loading, setLoading] = useState(true);
  const [probing, setProbing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await fetchMcpStatus());
      setError(null);
    } catch (reason) {
      setError(mcpError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  const probe = useCallback(async (name: string) => {
    setProbing(name);
    setError(null);
    try {
      const result = await probeMcpServer(name);
      setProbes((current) => ({ ...current, [name]: result }));
    } catch (reason) {
      setError(mcpError(reason));
    } finally {
      setProbing(null);
    }
  }, []);

  const enabledCount = status?.servers.filter((server) => server.enabled).length ?? 0;
  const eligibleCount = status?.servers.reduce((sum, server) => sum + server.eligible_read_tools, 0) ?? 0;

  return (
    <WorkdeskAppShell icon="mcp" sectionTitle="MCP">
      <section className="integration-page workdesk-route-surface">
        <header className="integration-hero">
          <div className="integration-hero-mark"><WorkdeskIcon name="mcp" /></div>
          <div>
            <span>MODEL CONTEXT PROTOCOL</span>
            <h1>MCP</h1>
            <p>连接受信任的本地或远程工具。只有显式启用、目录未漂移且满足数据边界的只读工具会交给 Agent。</p>
          </div>
          <button disabled={loading} onClick={() => void reload()} type="button">
            {loading ? "读取中…" : "刷新配置"}
          </button>
        </header>

        {error !== null && <div className="integration-notice error">{error}</div>}
        {status !== null && (
          <>
            <section className="integration-summary" aria-label="MCP 状态">
              <div><strong>{enabledCount}</strong><span>已启用服务</span></div>
              <div><strong>{eligibleCount}</strong><span>可交给 Agent 的工具</span></div>
              <div className="wide"><span>配置文件</span><code>{status.source_path ?? "尚未配置"}</code></div>
            </section>

            {status.servers.length === 0 ? (
              <section className="integration-empty">
                <span><WorkdeskIcon name="mcp" /></span>
                <h2>还没有 MCP 服务</h2>
                <p>复制示例配置为 config/mcp.yaml，逐个声明服务和工具策略后再刷新。</p>
                <code>config/mcp.yaml.example → config/mcp.yaml</code>
              </section>
            ) : (
              <section className="mcp-list" aria-label="MCP 服务列表">
                {status.servers.map((server) => {
                  const result = probes[server.name];
                  return (
                    <article className={`mcp-card${server.enabled ? " enabled" : ""}`} key={server.name}>
                      <header>
                        <span className="mcp-server-mark"><WorkdeskIcon name="mcp" /></span>
                        <div><h2>{server.name}</h2><p>{server.transport} · {server.trusted ? "已信任" : "未信任"}</p></div>
                        <i>{server.enabled ? "已启用" : "已停用"}</i>
                      </header>
                      <div className="mcp-metrics">
                        <span><strong>{server.configured_tools}</strong> 已配置</span>
                        <span><strong>{server.eligible_read_tools}</strong> 可用只读</span>
                        <span className={server.blocked_side_effect_tools > 0 ? "warn" : ""}><strong>{server.blocked_side_effect_tools}</strong> 副作用阻断</span>
                        <span className={server.blocked_data_scope_tools > 0 ? "warn" : ""}><strong>{server.blocked_data_scope_tools}</strong> 数据域阻断</span>
                      </div>
                      <footer>
                        <code>{server.catalog_sha256 === null ? "目录哈希未固定" : server.catalog_sha256}</code>
                        <button disabled={!server.enabled || probing !== null} onClick={() => void probe(server.name)} type="button">
                          {probing === server.name ? "正在探测…" : "探测工具目录"}
                        </button>
                      </footer>
                      {result !== undefined && (
                        <details className="mcp-probe-result" open>
                          <summary>发现 {result.tools.length} 个工具 · {result.catalog_sha256.slice(0, 12)}</summary>
                          <div>
                            {result.tools.map((tool) => (
                              <article key={tool.name}>
                                <div><strong>{tool.name}</strong><p>{tool.description || "没有工具说明"}</p></div>
                                <span className={tool.configured_policy?.enabled ? "ready" : "blocked"}>{tool.configured_policy?.enabled ? "已配置" : "未配置"}</span>
                              </article>
                            ))}
                          </div>
                        </details>
                      )}
                    </article>
                  );
                })}
              </section>
            )}
          </>
        )}
      </section>
    </WorkdeskAppShell>
  );
}
