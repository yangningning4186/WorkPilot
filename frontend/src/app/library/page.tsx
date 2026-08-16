"use client";

import { useCallback, useEffect, useState } from "react";

import { useAdminSession } from "@/components/admin-session";
import { Topbar } from "@/components/topbar";
import {
  ApiError,
  type DocumentState,
  type LibraryDocument,
  type LibraryResponse,
  fetchLibrary,
  syncSource,
} from "@/lib/api";

/**
 * 状态文案直接对应版本激活规则（约束 10）。
 *
 * 尤其是 failed：它表示**最新一版没进去**，而不是这篇文档检索不了——旧版仍在服务。
 * 把这两件事混成一个"失败"，用户会以为资料丢了，实际是悄悄用着旧版本。
 */
const STATE_LABELS: Record<DocumentState, { label: string; hint: string }> = {
  ready: { label: "可检索", hint: "已激活版本解析成功，正常参与检索" },
  parsing: { label: "解析中", hint: "新版本正在解析，此期间旧版本继续服务" },
  failed: { label: "新版失败", hint: "最新一版解析失败，检索仍在用上一个成功版本" },
  stale: { label: "无可检索块", hint: "版本已激活但没有可检索 chunk，通常是向量化没跟上" },
};

function StateBadge({ state }: { state: DocumentState }) {
  const meta = STATE_LABELS[state];
  return (
    <span className={`state-badge ${state}`} title={meta.hint}>
      {meta.label}
    </span>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString("zh-CN", { hour12: false, dateStyle: "short", timeStyle: "short" });
}

function DocumentRow({ document }: { document: LibraryDocument }) {
  return (
    <tr>
      <td>
        <div className="doc-title">{document.title}</div>
        <div className="doc-uri" title={document.source_uri}>
          {document.source_uri}
        </div>
        {document.parse_error !== null && (
          <div className="doc-error" title={document.parse_error}>
            {document.parse_error}
          </div>
        )}
      </td>
      <td>
        <StateBadge state={document.state} />
      </td>
      <td>{document.doc_type}</td>
      <td>{document.parser ?? "—"}</td>
      <td className="numeric">{document.page_count ?? "—"}</td>
      <td className="numeric">{document.block_count}</td>
      <td className="numeric">
        {document.searchable_chunk_count}
        {document.searchable_chunk_count === document.chunk_count
          ? ""
          : ` / ${document.chunk_count}`}
      </td>
      <td>
        {/* 没有 bbox 就只能给文本引用，点开原文看不到高亮——这一列是为了让人预期正确。 */}
        <span className={document.locatable ? "locatable yes" : "locatable no"}>
          {document.locatable ? "可高亮" : "仅文本"}
        </span>
      </td>
      <td className="time">{formatTime(document.updated_at)}</td>
    </tr>
  );
}

export default function LibraryPage() {
  const { invalidate: invalidateAdmin } = useAdminSession();
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (nextQuery: string) => {
    try {
      setData(await fetchLibrary(nextQuery));
      setError(null);
    } catch (reason) {
      setError(reason instanceof ApiError ? `读取资料库失败（${reason.status}）` : "读取资料库失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // 搜索框每敲一个字都打一次接口没有必要，顺带避开"在 effect 里同步 setState"。
  useEffect(() => {
    const timer = setTimeout(() => void load(query), query.trim() === "" ? 0 : 250);
    return () => clearTimeout(timer);
  }, [load, query]);

  // 同步是后台任务，页面只能靠轮询看进度；没有任务在跑时不轮询，免得白占连接。
  const syncing = data?.sources.some((source) => source.sync_status === "syncing") ?? false;
  useEffect(() => {
    if (!syncing) return;
    const timer = setInterval(() => void load(query), 3000);
    return () => clearInterval(timer);
  }, [load, query, syncing]);

  const triggerSync = useCallback(
    async (sourceId: string) => {
      setNotice(null);
      try {
        await syncSource(sourceId);
        setNotice("同步已触发，解析状态会在下方刷新。");
        await load(query);
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 401) {
          // 顶栏可能还显示着已登录（session 刚过期），拉回未登录才对得上。
          invalidateAdmin();
          setNotice("触发同步需要 admin 登录；请在右上角登录后重试。");
        } else {
          setNotice("同步触发失败，请检查后端日志。");
        }
      }
    },
    [load, query, invalidateAdmin],
  );

  return (
    <main className="app-frame library-frame">
      <Topbar />
      <div className="library-body">
        <section className="library-header">
          <div>
            <span className="eyebrow">Library</span>
            <h1>资料库</h1>
            <p>回答只会引用这里的内容。解析失败的版本不会顶掉正在服务的旧版本。</p>
          </div>
          <input
            aria-label="搜索资料"
            className="library-search"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按标题或路径搜索"
            type="search"
            value={query}
          />
        </section>

        {error !== null && <div className="inline-error">{error}</div>}
        {notice !== null && <div className="inline-notice">{notice}</div>}

        {data !== null && (
          <>
            <section className="library-stats" aria-label="资料库概览">
              <div className="stat-tile">
                <span>文档</span>
                <strong>{data.totals.documents}</strong>
              </div>
              <div className="stat-tile">
                <span>可检索 chunk</span>
                <strong>{data.totals.searchable_chunks}</strong>
                <em>共 {data.totals.chunks}</em>
              </div>
              <div className="stat-tile">
                <span>解析中</span>
                <strong>{data.totals.parsing}</strong>
              </div>
              <div className={`stat-tile${data.totals.failed > 0 ? " warn" : ""}`}>
                <span>新版解析失败</span>
                <strong>{data.totals.failed}</strong>
              </div>
            </section>

            <section className="source-list" aria-label="资料来源">
              {data.sources.map((source) => (
                <div className="source-card" key={source.id}>
                  <div>
                    <strong>{source.name}</strong>
                    <span className="source-meta">
                      {source.kind} · {source.document_count} 篇 · 上次同步{" "}
                      {source.last_sync_at === null ? "从未" : formatTime(source.last_sync_at)}
                    </span>
                    {source.sync_error !== null && (
                      <span className="source-error">{source.sync_error}</span>
                    )}
                  </div>
                  <div className="source-actions">
                    <span className={`sync-status ${source.sync_status}`}>
                      {source.sync_status === "syncing" ? "同步中" : source.sync_status}
                    </span>
                    <button
                      disabled={source.sync_status === "syncing"}
                      onClick={() => void triggerSync(source.id)}
                      type="button"
                    >
                      导入 / 同步
                    </button>
                  </div>
                </div>
              ))}
            </section>

            <div className="library-table-wrap">
              <table className="library-table">
                <thead>
                  <tr>
                    <th>文档</th>
                    <th>状态</th>
                    <th>类型</th>
                    <th>解析器</th>
                    <th className="numeric">页</th>
                    <th className="numeric">块</th>
                    <th className="numeric">可检索 chunk</th>
                    <th>引用定位</th>
                    <th>更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {data.documents.map((document) => (
                    <DocumentRow document={document} key={document.document_id} />
                  ))}
                </tbody>
              </table>
              {data.documents.length === 0 && !loading && (
                <p className="library-empty">
                  {query.trim() === "" ? "资料库还是空的。" : "没有匹配的资料。"}
                </p>
              )}
            </div>
          </>
        )}
        {loading && data === null && <p className="library-empty">正在读取资料库…</p>}
      </div>
    </main>
  );
}
