"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  createProvider,
  deleteProvider,
  fetchProviders,
  probeProvider,
  updateProvider,
  type ProviderKind,
  type ProviderProfile,
} from "@/lib/api";

const PROVIDER_OPTIONS: { value: ProviderKind; label: string; endpointHint: string }[] = [
  { value: "openai", label: "OpenAI", endpointHint: "例如：https://api.openai.com/v1" },
  { value: "anthropic", label: "Anthropic", endpointHint: "例如：https://api.anthropic.com/v1" },
  { value: "gemini", label: "Gemini", endpointHint: "例如：https://generativelanguage.googleapis.com/v1beta" },
  { value: "deepseek", label: "DeepSeek", endpointHint: "例如：https://api.deepseek.com/v1" },
  { value: "qwen", label: "Qwen", endpointHint: "例如：https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { value: "ollama", label: "Ollama", endpointHint: "例如：http://127.0.0.1:11434/v1" },
  { value: "openai_compatible", label: "OpenAI 兼容服务", endpointHint: "例如：http://127.0.0.1:8000/v1" },
];

function message(reason: unknown): string {
  if (reason instanceof ApiError) {
    try {
      const body = JSON.parse(reason.message) as { detail?: string };
      return body.detail ?? `请求失败（${reason.status}）`;
    } catch {
      return `请求失败（${reason.status}）`;
    }
  }
  return "模型服务暂时不可用。";
}

export default function ProvidersPage() {
  const [items, setItems] = useState<ProviderProfile[]>([]);
  const [kind, setKind] = useState<ProviderKind>("openai");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<Record<string, string>>({});

  const reload = useCallback(async () => {
    try {
      setItems((await fetchProviders()).items);
      setError(null);
    } catch (reason) {
      setError(message(reason));
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  const selectKind = (value: ProviderKind) => {
    setKind(value);
    setName("");
    setUrl("");
    setModel("");
    setApiKey("");
  };

  const create = async () => {
    setBusy("create");
    try {
      await createProvider({
        name: name.trim(),
        provider: kind,
        base_url: url.trim(),
        default_model: model.trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        enabled: true,
        metadata: {},
      });
      setName("");
      setUrl("");
      setModel("");
      setApiKey("");
      await reload();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  };

  const mutate = async (item: ProviderProfile, action: "probe" | "toggle" | "delete") => {
    if (action === "delete" && !window.confirm(`删除模型配置“${item.name}”？密钥密文也会一并删除。`)) return;
    setBusy(item.id);
    try {
      if (action === "delete") {
        await deleteProvider(item.id);
        await reload();
      } else if (action === "toggle") {
        await updateProvider(item.id, { enabled: !item.enabled });
        await reload();
      } else {
        const result = await probeProvider(item.id);
        setProbe((current) => ({ ...current, [item.id]: `${result.latency_ms} ms · ${result.models.length} 个模型` }));
      }
      setError(null);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setBusy(null);
    }
  };

  return (
    <WorkdeskAppShell icon="spark" sectionTitle="模型与密钥">
      <section className="integration-page workdesk-route-surface">
        <header className="integration-hero">
          <div className="integration-hero-mark"><WorkdeskIcon name="spark" /></div>
          <div><span>MODEL ROUTING</span><h1>模型与密钥</h1><p>模型服务完全由你配置；密钥只在本机加密保存，不提供内置默认模型。</p></div>
          <button onClick={() => void reload()} type="button">刷新</button>
        </header>

        {error !== null && <div className="integration-notice error">{error}</div>}
        <section className="integration-editor">
          <header><div><span>NEW PROVIDER</span><h2>添加模型服务</h2></div><small>OpenAI · Anthropic · Gemini · DeepSeek · Qwen · Ollama</small></header>
          <div className="integration-form-grid">
            <label><span>Provider</span><select onChange={(event) => selectKind(event.target.value as ProviderKind)} value={kind}>{PROVIDER_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>
            <label><span>显示名称</span><input onChange={(event) => setName(event.target.value)} placeholder="例如：工作账号" value={name} /></label>
            <label className="wide"><span>Base URL</span><input onChange={(event) => setUrl(event.target.value)} placeholder={PROVIDER_OPTIONS.find((option) => option.value === kind)?.endpointHint} spellCheck={false} value={url} /></label>
            <label className="wide"><span>模型 ID</span><input onChange={(event) => setModel(event.target.value)} placeholder="输入服务端实际提供的模型 ID" spellCheck={false} value={model} /></label>
            <label className="wide"><span>API Key {kind === "ollama" ? "· 可留空" : ""}</span><input autoComplete="off" onChange={(event) => setApiKey(event.target.value)} placeholder="只写入本机加密存储" type="password" value={apiKey} /></label>
          </div>
          <footer><span>上下文容量由系统管理；保存后可在会话中选择此模型服务。</span><button disabled={busy !== null || !name.trim() || !url.trim() || !model.trim() || (kind !== "ollama" && !apiKey.trim())} onClick={() => void create()} type="button">{busy === "create" ? "正在保存…" : "保存 Provider"}</button></footer>
        </section>

        <section className="integration-card-list">
          {items.map((item) => <article key={item.id}>
            <header><span className="integration-provider-badge">{item.provider.slice(0, 2).toUpperCase()}</span><div><h2>{item.name}</h2><p>{item.default_model}</p></div><i className={item.enabled ? "online" : ""}>{item.enabled ? "已启用" : "已停用"}</i></header>
            <dl><div><dt>Endpoint</dt><dd>{item.base_url}</dd></div><div><dt>密钥</dt><dd>{item.has_api_key ? "已加密保存" : "本地免密"}</dd></div></dl>
            {probe[item.id] !== undefined && <p className="integration-inline-success">✓ {probe[item.id]}</p>}
            <footer><button disabled={busy !== null} onClick={() => void mutate(item, "probe")} type="button">连接测试</button><button disabled={busy !== null} onClick={() => void mutate(item, "toggle")} type="button">{item.enabled ? "停用" : "启用"}</button><button className="danger" disabled={busy !== null} onClick={() => void mutate(item, "delete")} type="button">删除</button></footer>
          </article>)}
          {items.length === 0 && <div className="integration-empty"><span><WorkdeskIcon name="spark" /></span><h2>还没有模型配置</h2><p>请先填写 Provider、地址、模型 ID 和密钥。完成配置前，Cowork 不会调用任何内置或默认模型。</p></div>}
        </section>
      </section>
    </WorkdeskAppShell>
  );
}
