"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  addKnowledgeBaseDocuments,
  createKnowledgeBase,
  deleteKnowledgeBase,
  fetchKnowledgeBaseIndexing,
  fetchKnowledgeBases,
  rebuildKnowledgeBase,
  type KnowledgeBase,
  type KnowledgeBaseIndexingJob,
} from "@/lib/api";

/**
 * 后端的错误消息按约束 4 已经写成了可执行指令（"确认本机推理服务已启动"、
 * "换成更具体的子目录"），所以 4xx 一律原样展示，不要用一句自己编的话盖掉它。
 * 只有说不出所以然的状态码才换成人话。
 */
function kbError(reason: unknown): string {
  if (reason instanceof ApiError) {
    const detail = reason.message.trim();
    if (detail !== "") {
      try {
        const parsed = JSON.parse(detail) as { detail?: unknown };
        if (typeof parsed.detail === "string") return parsed.detail;
      } catch {
        /* 不是 JSON 就按原文处理 */
      }
    }
    if (reason.status === 401 || reason.status === 403) return "请先登录本机 owner。";
    return `知识库接口失败（${reason.status}）`;
  }
  return "暂时无法连接本地服务。";
}

const POLL_INTERVAL_MS = 1500;

export default function KnowledgePage() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [jobs, setJobs] = useState<Record<string, KnowledgeBaseIndexingJob>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [pathDraft, setPathDraft] = useState<Record<string, string>>({});

  const reload = useCallback(async () => {
    try {
      const response = await fetchKnowledgeBases();
      setItems(response.items);
      setError(null);
      // 顺带把每个库的作业状态捞回来：刷新页面时正在跑的那个不该消失。
      const entries = await Promise.all(
        response.items.map(async (item) => {
          try {
            return [item.slug, await fetchKnowledgeBaseIndexing(item.slug)] as const;
          } catch {
            return [item.slug, null] as const;
          }
        }),
      );
      setJobs(
        Object.fromEntries(
          entries.filter((entry): entry is [string, KnowledgeBaseIndexingJob] =>
            entry[1] !== null,
          ),
        ),
      );
    } catch (reason) {
      setError(kbError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  // 有作业在跑就轮询。停在"没有 running 作业"上，而不是一直轮着——一个开着不动的
  // 页面不该每秒钟敲一次后端。
  const running = Object.values(jobs).some((job) => job.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void reload(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [running, reload]);

  const submitCreate = async () => {
    setBusy("create");
    try {
      await createKnowledgeBase({
        slug: slug.trim(),
        name: name.trim(),
        description: description.trim(),
      });
      setShowCreate(false);
      setSlug("");
      setName("");
      setDescription("");
      await reload();
    } catch (reason) {
      setError(kbError(reason));
    } finally {
      setBusy(null);
    }
  };

  const submitPaths = async (target: string) => {
    const raw = (pathDraft[target] ?? "").trim();
    if (raw === "") return;
    // 一行一个路径。粘贴多行比反复点"添加"快，而路径里本来就可能带空格和逗号。
    const paths = raw.split("\n").map((line) => line.trim()).filter((line) => line !== "");
    setBusy(target);
    try {
      const job = await addKnowledgeBaseDocuments(target, paths);
      setJobs((current) => ({ ...current, [target]: job }));
      setPathDraft((current) => ({ ...current, [target]: "" }));
    } catch (reason) {
      setError(kbError(reason));
    } finally {
      setBusy(null);
    }
  };

  const submitRebuild = async (target: string) => {
    setBusy(target);
    try {
      const job = await rebuildKnowledgeBase(target);
      setJobs((current) => ({ ...current, [target]: job }));
    } catch (reason) {
      setError(kbError(reason));
    } finally {
      setBusy(null);
    }
  };

  const submitDelete = async (target: string) => {
    if (!window.confirm(`删除知识库“${target}”？它的索引会一起删除，挂着它的会话将检索不到内容。`)) {
      return;
    }
    setBusy(target);
    try {
      await deleteKnowledgeBase(target);
      await reload();
    } catch (reason) {
      setError(kbError(reason));
    } finally {
      setBusy(null);
    }
  };

  const documentTotal = items.reduce((sum, item) => sum + item.document_count, 0);

  return (
    <WorkdeskAppShell icon="book" sectionTitle="知识库">
      <section className="integration-page workdesk-route-surface">
        <header className="integration-hero">
          <div className="integration-hero-mark"><WorkdeskIcon name="book" /></div>
          <div>
            <span>LOCAL KNOWLEDGE</span>
            <h1>知识库</h1>
            <p>
              按主题手建的本地知识库。资料留在你自己的目录里，这里只保存解析后的索引；
              在 Cowork 会话上挂一个，提问时会先检索它。
            </p>
          </div>
          <button disabled={loading} onClick={() => setShowCreate((value) => !value)} type="button">
            {showCreate ? "收起" : "＋ 新建知识库"}
          </button>
        </header>

        {error !== null && <div className="integration-notice error">{error}</div>}

        {showCreate && (
          <section className="integration-editor">
            <header>
              <div><span>NEW KNOWLEDGE BASE</span><h2>新建知识库</h2></div>
              <small>标识就是磁盘上的目录名，建好之后不能改</small>
            </header>
            <div className="integration-form-grid">
              <label>
                <span>标识（slug）</span>
                <input
                  onChange={(event) => setSlug(event.target.value)}
                  placeholder="papers"
                  value={slug}
                />
              </label>
              <label>
                <span>显示名</span>
                <input
                  onChange={(event) => setName(event.target.value)}
                  placeholder="我的论文库"
                  value={name}
                />
              </label>
              <label className="wide">
                <span>说明（可选）</span>
                <input
                  onChange={(event) => setDescription(event.target.value)}
                  placeholder="检索增强相关的论文与笔记"
                  value={description}
                />
              </label>
            </div>
            <footer>
              <span>标识只能用小写字母、数字和连字符；中文名请单独填在显示名里。</span>
              <button
                disabled={busy !== null || slug.trim() === ""}
                onClick={() => void submitCreate()}
                type="button"
              >
                {busy === "create" ? "正在创建…" : "创建"}
              </button>
            </footer>
          </section>
        )}

        <section className="integration-summary" aria-label="知识库概况">
          <div><strong>{items.length}</strong><span>知识库</span></div>
          <div><strong>{documentTotal}</strong><span>文档</span></div>
          <div><strong>{items.filter((item) => !item.is_indexed).length}</strong><span>未建索引</span></div>
        </section>

        {!loading && items.length === 0 ? (
          <section className="integration-empty">
            <span><WorkdeskIcon name="book" /></span>
            <h2>还没有知识库</h2>
            <p>新建一个，然后把论文所在的目录路径贴进去。目录会递归展开，支持 PDF 与 Markdown。</p>
            <code>uv run python -m app.cli.kb create papers --name &quot;我的论文库&quot;</code>
          </section>
        ) : (
          <section className="kb-grid" aria-label="知识库列表">
            {items.map((item) => {
              const job = jobs[item.slug];
              const indexing = job?.status === "running";
              return (
                <article className={`kb-card${item.is_indexed ? "" : " unindexed"}`} key={item.slug}>
                  <header>
                    <span><WorkdeskIcon name="book" /></span>
                    <div><h2>{item.name}</h2><code>{item.slug}</code></div>
                    <b className={item.is_indexed ? "ready" : "pending"}>
                      {item.is_indexed ? "可检索" : "未建索引"}
                    </b>
                  </header>
                  {item.description !== "" && <p>{item.description}</p>}
                  <dl>
                    <div><dt>文档</dt><dd>{item.document_count} 篇</dd></div>
                    <div>
                      <dt>Embedding</dt>
                      <dd title={item.embedding ?? undefined}>{item.embedding ?? "—"}</dd>
                    </div>
                  </dl>

                  {item.documents.length > 0 && (
                    <ul className="kb-doc-list">
                      {item.documents.slice(0, 6).map((doc) => (
                        <li key={doc.doc_id}>
                          <span>{doc.title || doc.filename}</span>
                          <small>{doc.parser} · {doc.char_count.toLocaleString("zh-CN")} 字</small>
                        </li>
                      ))}
                      {item.documents.length > 6 && (
                        <li className="kb-doc-more">还有 {item.documents.length - 6} 篇</li>
                      )}
                    </ul>
                  )}

                  {job !== undefined && (
                    <div className={`kb-job ${job.status}`}>
                      {job.status === "running" && (
                        <>
                          <i style={{ width: `${job.total === 0 ? 5 : Math.min(100, (job.done / job.total) * 100)}%` }} />
                          <span>{job.stage}{job.total > 0 && ` · ${job.done}/${job.total}`}</span>
                        </>
                      )}
                      {job.status === "done" && (
                        <span>本次新增 {job.added} 篇{job.skipped.length > 0 && `，跳过 ${job.skipped.length} 篇`}</span>
                      )}
                      {job.status === "failed" && <span>{job.error ?? "建索引失败"}</span>}
                      {job.skipped.length > 0 && (
                        <ul>
                          {job.skipped.map((skipped) => (
                            <li key={skipped.filename}>
                              <strong>{skipped.filename}</strong>{skipped.reason}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}

                  <label className="kb-add-paths">
                    <span>加入文档 · 一行一个本机路径，目录会递归展开</span>
                    <textarea
                      disabled={indexing}
                      onChange={(event) =>
                        setPathDraft((current) => ({ ...current, [item.slug]: event.target.value }))
                      }
                      placeholder={"~/papers\n~/notes/rag.md"}
                      rows={2}
                      value={pathDraft[item.slug] ?? ""}
                    />
                  </label>

                  <footer>
                    <button
                      disabled={busy !== null || indexing || (pathDraft[item.slug] ?? "").trim() === ""}
                      onClick={() => void submitPaths(item.slug)}
                      type="button"
                    >
                      {indexing ? "正在建索引…" : "加入并建索引"}
                    </button>
                    <button
                      disabled={busy !== null || indexing || item.document_count === 0}
                      onClick={() => void submitRebuild(item.slug)}
                      type="button"
                    >
                      重建索引
                    </button>
                    <button
                      className="danger"
                      disabled={busy !== null || indexing}
                      onClick={() => void submitDelete(item.slug)}
                      type="button"
                    >
                      删除
                    </button>
                  </footer>
                </article>
              );
            })}
          </section>
        )}

        <p className="integration-snapshot">
          换了 embedding 模型之后必须重建：旧向量和新查询不在同一个空间里，
          不重建的话检索会拒绝服务而不是悄悄给错结果。
        </p>
      </section>
    </WorkdeskAppShell>
  );
}
