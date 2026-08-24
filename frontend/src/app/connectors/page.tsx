"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  connectorOAuthCallbackUrl,
  createConnector,
  deleteConnector,
  fetchConnectorCatalog,
  fetchConnectors,
  fetchMcpStatus,
  startConnectorOAuth,
  updateConnector,
  type ConnectorAccount,
  type ConnectorDescriptor,
  type McpStatusResponse,
} from "@/lib/api";
import {
  openConnectorAuthorization,
  prepareConnectorAuthorizationWindow,
} from "@/lib/desktop";

type ConnectMode = "quick" | "manual";
type ManualAuthType = "oauth2" | "token";

const LOGO_TEXT: Record<string, string> = {
  feishu: "✦",
  github: "GH",
  tencent_docs: "T",
  wechat: "公",
  wecom: "企",
};

const CAPABILITY_LABELS: Record<string, string> = {
  approval: "审批",
  base: "多维表格",
  calendar: "日历",
  docs: "文档",
  drive: "云盘",
  messaging: "消息",
  openapi: "官方 API",
  tasks: "任务",
};

function message(reason: unknown): string {
  if (reason instanceof ApiError) {
    try {
      return (JSON.parse(reason.message) as { detail?: string }).detail
        ?? `请求失败（${reason.status}）`;
    } catch {
      return `请求失败（${reason.status}）`;
    }
  }
  if (reason instanceof Error && reason.message.trim()) return reason.message;
  return "连接器服务暂时不可用。";
}

function descriptorStyle(descriptor: ConnectorDescriptor): CSSProperties {
  return { "--connector-color": descriptor.brand_color } as CSSProperties;
}

function ConnectorMark({ descriptor }: { descriptor: ConnectorDescriptor }) {
  return (
    <span className={`connector-mark connector-mark-${descriptor.logo}`} style={descriptorStyle(descriptor)}>
      {LOGO_TEXT[descriptor.logo] ?? descriptor.label.slice(0, 1)}
    </span>
  );
}

function statusLabel(account: ConnectorAccount): string {
  if (!account.enabled) return "已停用";
  if (account.status === "connected") return "已连接";
  if (account.status === "authorizing") return "等待授权";
  if (account.status === "error") return "连接异常";
  if (account.status === "expired") return "授权过期";
  return "待授权";
}

function isOnline(account: ConnectorAccount): boolean {
  return account.enabled && account.status === "connected";
}

export default function ConnectorsPage() {
  const [items, setItems] = useState<ConnectorAccount[]>([]);
  const [catalog, setCatalog] = useState<ConnectorDescriptor[]>([]);
  const [mcpStatus, setMcpStatus] = useState<McpStatusResponse | null>(null);
  const [selected, setSelected] = useState<ConnectorDescriptor | null>(null);
  const [mode, setMode] = useState<ConnectMode>("quick");
  const [authType, setAuthType] = useState<ManualAuthType>("oauth2");
  const [name, setName] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const [scopes, setScopes] = useState("");
  const [agentId, setAgentId] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [waitingAccountId, setWaitingAccountId] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [accounts, descriptors, mcp] = await Promise.all([
        fetchConnectors(),
        fetchConnectorCatalog(),
        fetchMcpStatus().catch(() => null),
      ]);
      setItems(accounts.items);
      setCatalog(descriptors.items);
      setMcpStatus(mcp);
      setError(null);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  useEffect(() => {
    if (selected === null) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSelected(null);
        setWaitingAccountId(null);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selected]);

  useEffect(() => {
    if (waitingAccountId === null) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const accounts = await fetchConnectors();
        if (cancelled) return;
        setItems(accounts.items);
        const account = accounts.items.find((item) => item.id === waitingAccountId);
        if (account !== undefined && account.status === "connected") {
          setWaitingAccountId(null);
          setSuccess(`${account.external_account_name ?? account.name} 已连接`);
          return;
        }
        if (account !== undefined && (account.status === "error" || account.status === "expired")) {
          setWaitingAccountId(null);
          setSuccess(null);
          setError(account.last_error ?? `${account.name} 授权未完成，请重新发起`);
          return;
        }
      } catch {
        // 授权页仍在进行时允许短暂请求失败；显式刷新会展示最终错误。
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 2000);
    };
    timer = window.setTimeout(() => void poll(), 1500);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [waitingAccountId]);

  useEffect(() => {
    const refreshAfterAuthorization = () => {
      if (waitingAccountId === null) return;
      void fetchConnectors().then((accounts) => setItems(accounts.items)).catch(() => undefined);
    };
    window.addEventListener("focus", refreshAfterAuthorization);
    return () => window.removeEventListener("focus", refreshAfterAuthorization);
  }, [waitingAccountId]);

  const descriptorByKind = useMemo(
    () => new Map(catalog.map((descriptor) => [descriptor.kind, descriptor])),
    [catalog],
  );
  const selectedAccounts = useMemo(
    () => selected === null ? [] : items.filter((item) => item.kind === selected.kind),
    [items, selected],
  );
  const filteredCatalog = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    const matching = normalized ? catalog.filter((descriptor) => (
      `${descriptor.label} ${descriptor.blurb} ${descriptor.capabilities.join(" ")}`
        .toLocaleLowerCase("zh-CN")
        .includes(normalized)
    )) : catalog;
    return matching.toSorted((left, right) => (
      Number(right.category === "china_office") - Number(left.category === "china_office")
    ));
  }, [catalog, query]);

  const resetForm = useCallback((descriptor: ConnectorDescriptor) => {
    const accountCount = items.filter((item) => item.kind === descriptor.kind).length;
    setName(accountCount === 0 ? descriptor.label : `${descriptor.label} ${accountCount + 1}`);
    setClientId("");
    setClientSecret("");
    setAccessToken("");
    setScopes(descriptor.default_scopes.join(" "));
    setAgentId("");
    const firstAuthType = descriptor.auth_types.includes("oauth2") ? "oauth2" : "token";
    setAuthType(firstAuthType);
    void connectorOAuthCallbackUrl()
      .then(setRedirectUri)
      .catch(() => setRedirectUri(""));
  }, [items]);

  const openConnector = useCallback((descriptor: ConnectorDescriptor, requestedMode?: ConnectMode) => {
    const accounts = items.filter((item) => item.kind === descriptor.kind);
    resetForm(descriptor);
    setSelected(descriptor);
    setMode(requestedMode ?? (accounts.length > 0 ? "quick" : "manual"));
    setWaitingAccountId(null);
    setSuccess(null);
    setError(null);
  }, [items, resetForm]);

  const closeModal = useCallback(() => {
    setSelected(null);
    setWaitingAccountId(null);
    setSuccess(null);
  }, []);

  const authorize = useCallback(async (account: ConnectorAccount) => {
    const popup = prepareConnectorAuthorizationWindow();
    setBusy(account.id);
    setError(null);
    setSuccess(null);
    try {
      const result = await startConnectorOAuth(account.id);
      await openConnectorAuthorization(result.authorization_url, popup);
      setWaitingAccountId(account.id);
      setSuccess(`已打开 ${account.name} 官方授权页，完成后这里会自动更新`);
    } catch (reason) {
      popup?.close();
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const createAndConnect = useCallback(async () => {
    if (selected === null) return;
    const popup = authType === "oauth2" ? prepareConnectorAuthorizationWindow() : null;
    setBusy("create");
    setError(null);
    setSuccess(null);
    try {
      const created = await createConnector({
        kind: selected.kind,
        name: name.trim(),
        auth_type: authType,
        ...(authType === "oauth2"
          ? {
              client_id: clientId.trim(),
              client_secret: clientSecret.trim(),
              redirect_uri: redirectUri.trim(),
            }
          : { access_token: accessToken.trim() }),
        scopes: scopes.split(/\s+/).filter(Boolean),
        config: selected.kind === "wecom" && agentId.trim() ? { agent_id: agentId.trim() } : {},
        enabled: true,
      });
      const accounts = await fetchConnectors();
      setItems(accounts.items);
      if (authType === "oauth2") {
        const result = await startConnectorOAuth(created.id);
        await openConnectorAuthorization(result.authorization_url, popup);
        setMode("quick");
        setWaitingAccountId(created.id);
        setSuccess(`已打开 ${created.name} 官方授权页，完成后这里会自动更新`);
      } else {
        setSuccess(`${created.name} 已连接`);
        setMode("quick");
      }
      setClientSecret("");
      setAccessToken("");
    } catch (reason) {
      popup?.close();
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }, [accessToken, agentId, authType, clientId, clientSecret, name, redirectUri, scopes, selected]);

  const connectFromCatalog = useCallback((descriptor: ConnectorDescriptor) => {
    const accounts = items.filter((item) => item.kind === descriptor.kind);
    const enabledOAuth = accounts.filter((item) => item.enabled && item.auth_type === "oauth2");
    const singleOAuth = enabledOAuth.length === 1 ? enabledOAuth[0] : undefined;
    const connected = accounts.some(isOnline);

    // 一个已经登记过应用、但尚未完成授权的账户，是目录卡片的真正快捷路径。
    // 已连接账户不做意外的重新授权；多个账户也必须先让用户明确选择。
    if (!connected && singleOAuth !== undefined) {
      void authorize(singleOAuth);
      return;
    }
    openConnector(descriptor, accounts.length > 0 ? "quick" : "manual");
  }, [authorize, items, openConnector]);

  const remove = useCallback(async (account: ConnectorAccount) => {
    if (!window.confirm(`删除连接器“${account.name}”及其 OAuth token？`)) return;
    setBusy(account.id);
    try {
      await deleteConnector(account.id);
      const accounts = await fetchConnectors();
      setItems(accounts.items);
      setSuccess(`${account.name} 已删除`);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const toggle = useCallback(async (account: ConnectorAccount) => {
    setBusy(account.id);
    try {
      await updateConnector(account.id, { enabled: !account.enabled });
      const accounts = await fetchConnectors();
      setItems(accounts.items);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  }, []);

  const modalBackdropClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) closeModal();
  };

  const enabledMcpCount = mcpStatus?.servers.filter((server) => server.enabled).length ?? 0;
  const connectedCount = items.filter(isOnline).length;
  const canSubmit = selected !== null
    && name.trim() !== ""
    && (authType === "oauth2"
      ? clientId.trim() !== "" && clientSecret.trim() !== "" && redirectUri.trim() !== ""
      : accessToken.trim() !== "");

  return (
    <WorkdeskAppShell icon="agent" sectionTitle="连接器">
      <section className="connector-page workdesk-route-surface">
        <header className="connector-page-head">
          <div>
            <span>APPS &amp; CONNECTORS</span>
            <h1>连接你的工作</h1>
            <p>用一个入口管理官方连接器与受控 MCP。凭据留在本机，外部写操作仍按次审批。</p>
          </div>
          <div className="connector-page-actions">
            <label className="connector-search">
              <WorkdeskIcon name="search" />
              <input
                aria-label="搜索连接器"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索连接器"
                value={query}
              />
            </label>
            <button
              disabled={catalog.length === 0}
              onClick={() => {
                const first = catalog.find((item) => item.category === "china_office") ?? catalog[0];
                if (first !== undefined) openConnector(first, "manual");
              }}
              type="button"
            >
              <WorkdeskIcon name="add" />手动配置
            </button>
          </div>
        </header>

        {error !== null && selected === null && <div className="integration-notice error">{error}</div>}
        {success !== null && selected === null && (
          <div className="connector-authorization-notice" role="status">
            <span className={waitingAccountId === null ? "complete" : "waiting"} />
            <div>
              <strong>{waitingAccountId === null ? "授权完成" : "等待浏览器授权"}</strong>
              <small>{success}</small>
            </div>
            {waitingAccountId !== null && <i>完成后可关闭浏览器页面</i>}
          </div>
        )}

        {items.length > 0 && (
          <section className="connector-group" aria-labelledby="configured-connectors-title">
            <header>
              <div><h2 id="configured-connectors-title">已配置</h2><span>{connectedCount}/{items.length} 个账户在线</span></div>
              <button disabled={loading} onClick={() => void reload()} type="button">
                {loading ? "刷新中…" : "刷新状态"}
              </button>
            </header>
            <div className="connector-account-list">
              {items.map((account) => {
                const descriptor = descriptorByKind.get(account.kind);
                if (descriptor === undefined) return null;
                return (
                  <button
                    className="connector-account-row"
                    key={account.id}
                    onClick={() => openConnector(descriptor, "quick")}
                    type="button"
                  >
                    <ConnectorMark descriptor={descriptor} />
                    <span><strong>{account.name}</strong><small>{account.external_account_name ?? descriptor.label}</small></span>
                    <i className={isOnline(account) ? "online" : ""}>{statusLabel(account)}</i>
                    <span className="connector-row-chevron" aria-hidden="true">›</span>
                  </button>
                );
              })}
            </div>
          </section>
        )}

        <section className="connector-group connector-mcp-group" aria-labelledby="custom-mcp-title">
          <header>
            <div><h2 id="custom-mcp-title">Custom · MCP</h2><span>独立信任边界</span></div>
          </header>
          <Link className="connector-mcp-row" href="/mcp">
            <span className="connector-mcp-mark"><WorkdeskIcon name="mcp" /></span>
            <span>
              <strong>自定义 MCP 服务</strong>
              <small>连接本地 stdio 或远程 HTTP 服务；探测后逐个确认工具权限。</small>
            </span>
            <i>{enabledMcpCount} 个已启用</i>
            <span className="connector-row-chevron" aria-hidden="true">›</span>
          </Link>
        </section>

        <section className="connector-group connector-catalog-group" aria-labelledby="available-connectors-title">
          <header>
            <div><h2 id="available-connectors-title">可用连接器</h2><span>{filteredCatalog.length} 个官方目录项</span></div>
          </header>
          {filteredCatalog.length > 0 ? (
            <div className="connector-catalog-grid">
              {filteredCatalog.map((descriptor) => {
                const accounts = items.filter((account) => account.kind === descriptor.kind);
                const online = accounts.some(isOnline);
                return (
                  <button
                    aria-label={`${online ? "管理" : "授权"} ${descriptor.label}`}
                    className="connector-catalog-card"
                    disabled={busy !== null}
                    key={descriptor.kind}
                    onClick={() => connectFromCatalog(descriptor)}
                    style={descriptorStyle(descriptor)}
                    type="button"
                  >
                    <header>
                      <ConnectorMark descriptor={descriptor} />
                      <div>
                        <h3>{descriptor.label}{online && <span className="connector-online-dot" title="已连接" />}</h3>
                        <small>{descriptor.category === "developer" ? "开发协作" : "中国办公栈"}</small>
                      </div>
                      <span className="connector-card-action">
                        {busy !== null && accounts.some((account) => account.id === busy)
                          ? "跳转中…"
                          : online
                            ? "管理"
                            : accounts.length > 0
                              ? "授权 ↗"
                              : "配置并授权"}
                      </span>
                    </header>
                    <p>{descriptor.blurb}</p>
                    <footer>
                      {descriptor.capabilities.filter((item) => item !== "openapi").slice(0, 3).map((capability) => (
                        <span key={capability}>{CAPABILITY_LABELS[capability] ?? capability}</span>
                      ))}
                      {accounts.length > 0 && <i>{accounts.length} 个账户</i>}
                    </footer>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="connector-catalog-empty">没有找到“{query}”相关的连接器</div>
          )}
        </section>
      </section>

      {selected !== null && (
        <div className="connector-modal-backdrop" onMouseDown={modalBackdropClick}>
          <section
            aria-labelledby="connector-modal-title"
            aria-modal="true"
            className="connector-modal"
            role="dialog"
            style={descriptorStyle(selected)}
          >
            <header className="connector-modal-head">
              <ConnectorMark descriptor={selected} />
              <div><span>连接器</span><h2 id="connector-modal-title">{selected.label}</h2><p>{selected.blurb}</p></div>
              <button aria-label="关闭" className="connector-modal-close" onClick={closeModal} type="button">×</button>
            </header>

            <div className="connector-mode-switch" role="tablist" aria-label="连接方式">
              <button aria-selected={mode === "quick"} className={mode === "quick" ? "active" : ""} onClick={() => setMode("quick")} role="tab" type="button">一键授权</button>
              <button aria-selected={mode === "manual"} className={mode === "manual" ? "active" : ""} onClick={() => setMode("manual")} role="tab" type="button">手动配置</button>
            </div>

            {error !== null && <div className="connector-modal-message error">{error}</div>}
            {success !== null && <div className="connector-modal-message success">{success}</div>}

            {mode === "quick" ? (
              <div className="connector-quick-pane">
                {selectedAccounts.length === 0 ? (
                  <div className="connector-first-setup">
                    <span><WorkdeskIcon name="shield" /></span>
                    <h3>首次连接需要登记应用</h3>
                    <p>WorkPilot 当前不托管厂商 Client Secret。先配置一次 OAuth 应用，之后重新授权就是一键完成。</p>
                    <button onClick={() => setMode("manual")} type="button">配置应用凭据</button>
                  </div>
                ) : (
                  <div className="connector-quick-accounts">
                    <p className="connector-pane-intro">选择一个本机账户前往官方授权页；WorkPilot 会自动等待授权结果。</p>
                    {selectedAccounts.map((account) => (
                      <article key={account.id}>
                        <span className={isOnline(account) ? "online" : ""} />
                        <div><strong>{account.name}</strong><small>{account.external_account_name ?? statusLabel(account)}</small></div>
                        {account.auth_type === "oauth2" ? (
                          <button disabled={busy !== null || !account.enabled} onClick={() => void authorize(account)} type="button">
                            {waitingAccountId === account.id ? "等待授权…" : isOnline(account) ? "重新授权" : "前往授权"}
                          </button>
                        ) : <i>{isOnline(account) ? "Token 已连接" : statusLabel(account)}</i>}
                        <button className="text" disabled={busy !== null} onClick={() => void toggle(account)} type="button">{account.enabled ? "停用" : "启用"}</button>
                        <button className="text danger" disabled={busy !== null} onClick={() => void remove(account)} type="button">删除</button>
                      </article>
                    ))}
                    <button className="connector-add-account" onClick={() => { resetForm(selected); setMode("manual"); }} type="button"><WorkdeskIcon name="add" />添加另一个账户</button>
                  </div>
                )}
              </div>
            ) : (
              <div className="connector-manual-pane">
                <div className="connector-auth-options">
                  {selected.auth_types.includes("oauth2") && <button className={authType === "oauth2" ? "active" : ""} onClick={() => setAuthType("oauth2")} type="button">OAuth 2.0</button>}
                  {selected.auth_types.includes("token") && <button className={authType === "token" ? "active" : ""} onClick={() => setAuthType("token")} type="button">Access Token</button>}
                </div>
                <div className="connector-form-grid">
                  <label><span>平台</span><select onChange={(event) => {
                    const descriptor = catalog.find((item) => item.kind === event.target.value);
                    if (descriptor !== undefined) {
                      resetForm(descriptor);
                      setSelected(descriptor);
                      setSuccess(null);
                      setError(null);
                    }
                  }} value={selected.kind}>{catalog.map((descriptor) => <option key={descriptor.kind} value={descriptor.kind}>{descriptor.label}</option>)}</select></label>
                  <label><span>显示名称</span><input autoFocus onChange={(event) => setName(event.target.value)} value={name} /></label>
                  {authType === "oauth2" ? (
                    <>
                      <label><span>Client / App ID</span><input autoComplete="off" onChange={(event) => setClientId(event.target.value)} value={clientId} /></label>
                      <label><span>Client / App Secret</span><input autoComplete="new-password" onChange={(event) => setClientSecret(event.target.value)} type="password" value={clientSecret} /></label>
                      {selected.kind === "wecom" && <label><span>Agent ID · 可选</span><input onChange={(event) => setAgentId(event.target.value)} value={agentId} /></label>}
                      <label className="wide"><span>Redirect URI</span><input onChange={(event) => setRedirectUri(event.target.value)} value={redirectUri} /></label>
                    </>
                  ) : (
                    <label className="wide"><span>Access Token</span><input autoComplete="new-password" onChange={(event) => setAccessToken(event.target.value)} type="password" value={accessToken} /></label>
                  )}
                  <label className="wide"><span>Scopes · 空格分隔</span><input onChange={(event) => setScopes(event.target.value)} value={scopes} /></label>
                </div>
                <div className="connector-scope-preview">
                  <strong>将启用</strong>
                  <span>{selected.capabilities.map((capability) => CAPABILITY_LABELS[capability] ?? capability).join(" · ")}</span>
                </div>
              </div>
            )}

            <footer className="connector-modal-footer">
              <span><WorkdeskIcon name="shield" />凭据只写入本机加密存储</span>
              <div>
                <button className="secondary" onClick={closeModal} type="button">取消</button>
                {mode === "manual" && (
                  <button disabled={busy !== null || !canSubmit} onClick={() => void createAndConnect()} type="button">
                    {busy === "create" ? "正在连接…" : authType === "oauth2" ? "保存并前往授权" : "保存并连接"}
                  </button>
                )}
              </div>
            </footer>
          </section>
        </div>
      )}
    </WorkdeskAppShell>
  );
}
