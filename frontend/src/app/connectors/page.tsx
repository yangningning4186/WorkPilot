"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  createConnector,
  deleteConnector,
  fetchConnectors,
  startConnectorOAuth,
  updateConnector,
  type ConnectorAccount,
  type ConnectorKind,
} from "@/lib/api";

const LABELS: Record<ConnectorKind, string> = {
  github: "GitHub",
  feishu: "飞书",
  wecom: "企业微信",
  wechat_official: "微信公众号",
  tencent_docs: "腾讯文档",
};

function message(reason: unknown): string {
  if (reason instanceof ApiError) {
    try { return (JSON.parse(reason.message) as { detail?: string }).detail ?? `请求失败（${reason.status}）`; } catch { return `请求失败（${reason.status}）`; }
  }
  return "连接器服务暂时不可用。";
}

export default function ConnectorsPage() {
  const [items, setItems] = useState<ConnectorAccount[]>([]);
  const [kind, setKind] = useState<ConnectorKind>("github");
  const [name, setName] = useState("GitHub");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const [scopes, setScopes] = useState("read:user repo");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try { setItems((await fetchConnectors()).items); setError(null); } catch (reason) { setError(message(reason)); }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  const selectKind = (value: ConnectorKind) => {
    setKind(value); setName(LABELS[value]);
    setScopes(value === "github" ? "read:user repo" : value === "feishu" ? "offline_access" : value === "tencent_docs" ? "all" : "");
  };

  const create = async () => {
    setBusy("create");
    try {
      await createConnector({ kind, name: name.trim(), auth_type: "oauth2", client_id: clientId.trim(), client_secret: clientSecret.trim(), redirect_uri: redirectUri.trim(), scopes: scopes.split(/\s+/).filter(Boolean), config: {}, enabled: true });
      setClientSecret(""); await reload();
    } catch (reason) { setError(message(reason)); } finally { setBusy(null); }
  };

  const authorize = async (item: ConnectorAccount) => {
    setBusy(item.id);
    try {
      const result = await startConnectorOAuth(item.id);
      window.open(result.authorization_url, "_blank", "noopener,noreferrer");
      window.setTimeout(() => void reload(), 3000);
    } catch (reason) { setError(message(reason)); } finally { setBusy(null); }
  };

  const remove = async (item: ConnectorAccount) => {
    if (!window.confirm(`删除连接器“${item.name}”及其 OAuth token？`)) return;
    setBusy(item.id);
    try { await deleteConnector(item.id); await reload(); } catch (reason) { setError(message(reason)); } finally { setBusy(null); }
  };

  const toggle = async (item: ConnectorAccount) => {
    setBusy(item.id);
    try { await updateConnector(item.id, { enabled: !item.enabled }); await reload(); } catch (reason) { setError(message(reason)); } finally { setBusy(null); }
  };

  return (
    <WorkdeskAppShell icon="agent" sectionTitle="连接器与 OAuth">
      <section className="integration-page workdesk-route-surface">
        <header className="integration-hero"><div className="integration-hero-mark"><WorkdeskIcon name="agent" /></div><div><span>CONNECTED ACCOUNTS</span><h1>连接器与 OAuth</h1><p>连接外部账户，并把 token 保存在本机加密存储。写操作仍需逐次审批。</p></div><button onClick={() => void reload()} type="button">刷新状态</button></header>
        <div className="integration-notice"><strong>微信能力边界</strong><p>这里支持企业微信与微信公众号官方 API；不提供个人微信号模拟登录或非官方自动化。</p></div>
        {error !== null && <div className="integration-notice error">{error}</div>}
        <section className="integration-editor">
          <header><div><span>OAUTH 2.0</span><h2>添加连接器</h2></div><small>应用凭据只在交换 token 时解密</small></header>
          <div className="integration-form-grid">
            <label><span>平台</span><select onChange={(event) => selectKind(event.target.value as ConnectorKind)} value={kind}>{Object.entries(LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label><span>显示名称</span><input onChange={(event) => setName(event.target.value)} value={name} /></label>
            <label><span>Client / App ID</span><input onChange={(event) => setClientId(event.target.value)} value={clientId} /></label>
            <label><span>Client / App Secret</span><input autoComplete="off" onChange={(event) => setClientSecret(event.target.value)} type="password" value={clientSecret} /></label>
            <label className="wide"><span>Redirect URI</span><input onChange={(event) => setRedirectUri(event.target.value)} placeholder="在平台后台登记的回调地址" value={redirectUri} /></label>
            <label className="wide"><span>Scopes · 空格分隔</span><input onChange={(event) => setScopes(event.target.value)} value={scopes} /></label>
          </div>
          <footer><span>腾讯文档要求回调使用 HTTPS；桌面端需配置可达的 HTTPS 回调。</span><button disabled={busy !== null || !name.trim() || !clientId.trim() || !clientSecret.trim() || !redirectUri.trim()} onClick={() => void create()} type="button">{busy === "create" ? "正在保存…" : "保存连接器"}</button></footer>
        </section>
        <section className="integration-card-list">
          {items.map((item) => <article key={item.id}><header><span className="integration-provider-badge">{LABELS[item.kind].slice(0, 1)}</span><div><h2>{item.name}</h2><p>{LABELS[item.kind]} · {item.external_account_name ?? "尚未授权账户"}</p></div><i className={item.status === "connected" && item.enabled ? "online" : ""}>{!item.enabled ? "已停用" : item.status === "connected" ? "已连接" : item.status === "authorizing" ? "等待授权" : item.status === "error" ? "连接异常" : "待授权"}</i></header><dl><div><dt>权限</dt><dd>{item.scopes.join(" · ") || "平台默认"}</dd></div><div><dt>凭据</dt><dd>{item.has_secrets ? "已加密保存" : "尚未写入"}</dd></div><div><dt>连接器 ID</dt><dd>{item.id}</dd></div></dl>{item.last_error !== null && <p className="integration-inline-error">{item.last_error}</p>}<footer><button disabled={busy !== null || !item.enabled} onClick={() => void authorize(item)} type="button">{item.status === "connected" ? "重新授权" : "开始 OAuth"}</button><button disabled={busy !== null} onClick={() => void toggle(item)} type="button">{item.enabled ? "停用" : "启用"}</button><button className="danger" disabled={busy !== null} onClick={() => void remove(item)} type="button">删除</button></footer></article>)}
          {items.length === 0 && <div className="integration-empty"><span><WorkdeskIcon name="agent" /></span><h2>还没有连接外部账户</h2><p>先在目标平台创建 OAuth 应用，再把应用 ID、Secret 和回调地址填到上方。</p></div>}
        </section>
      </section>
    </WorkdeskAppShell>
  );
}
