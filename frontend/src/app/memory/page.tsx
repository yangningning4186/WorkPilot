"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAdminSession } from "@/components/admin-session";
import { Topbar } from "@/components/topbar";
import {
  ApiError,
  createMemory,
  deleteMemory,
  fetchMemories,
  type MemoryCategory,
  type MemoryRecord,
  type MemoryView,
  restoreMemory,
  updateMemory,
} from "@/lib/api";

const CATEGORY_META: Record<MemoryCategory, { label: string; short: string }> = {
  preference: { label: "偏好", short: "P" },
  profile: { label: "个人资料", short: "人" },
  interest: { label: "兴趣", short: "趣" },
  fact: { label: "事实", short: "实" },
};

function formatTime(value: string | null): string {
  if (value === null) return "尚未使用";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString("zh-CN", {
        hour12: false,
        dateStyle: "medium",
        timeStyle: "short",
      });
}

function friendlyError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "owner 会话已过期，请重新登录。";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "这条记忆的状态刚刚发生了变化，请刷新后再试。";
  }
  return "操作没有完成，请稍后重试。";
}

interface MemoryEditorProps {
  initial?: MemoryRecord;
  pending: boolean;
  onCancel: () => void;
  onSave: (value: { category: MemoryCategory; fact: string; pinned: boolean }) => void;
}

function MemoryEditor({ initial, pending, onCancel, onSave }: MemoryEditorProps) {
  const [category, setCategory] = useState<MemoryCategory>(initial?.category ?? "preference");
  const [fact, setFact] = useState(initial?.fact ?? "");
  const [pinned, setPinned] = useState(initial?.pinned ?? false);

  return (
    <form
      className="memory-editor"
      onSubmit={(event) => {
        event.preventDefault();
        if (fact.trim() !== "") onSave({ category, fact: fact.trim(), pinned });
      }}
    >
      <div className="memory-editor-fields">
        <label>
          <span>类别</span>
          <select
            disabled={pending}
            onChange={(event) => setCategory(event.target.value as MemoryCategory)}
            value={category}
          >
            {Object.entries(CATEGORY_META).map(([value, meta]) => (
              <option key={value} value={value}>
                {meta.label}
              </option>
            ))}
          </select>
        </label>
        <label className="memory-fact-field">
          <span>记住这件事</span>
          <textarea
            autoFocus
            disabled={pending}
            maxLength={2000}
            onChange={(event) => setFact(event.target.value)}
            placeholder="例如：回答时先给结论，再补充依据。"
            rows={3}
            value={fact}
          />
        </label>
      </div>
      <div className="memory-editor-footer">
        <label className="memory-pin-check">
          <input
            checked={pinned}
            disabled={pending}
            onChange={(event) => setPinned(event.target.checked)}
            type="checkbox"
          />
          始终优先召回
        </label>
        <div>
          <button className="memory-button quiet" disabled={pending} onClick={onCancel} type="button">
            取消
          </button>
          <button className="memory-button primary" disabled={pending || fact.trim() === ""} type="submit">
            {pending ? "保存中…" : initial === undefined ? "加入记忆" : "保存新版本"}
          </button>
        </div>
      </div>
    </form>
  );
}

interface MemoryCardProps {
  memory: MemoryRecord;
  view: MemoryView;
  pending: boolean;
  editing: boolean;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSave: (value: { category: MemoryCategory; fact: string; pinned: boolean }) => void;
  onPin: () => void;
  onDelete: () => void;
  onRestore: () => void;
}

function MemoryCard(props: MemoryCardProps) {
  const { memory, view, pending, editing } = props;
  const category = CATEGORY_META[memory.category];
  if (editing) {
    return (
      <article className="memory-card editing">
        <MemoryEditor
          initial={memory}
          onCancel={props.onCancelEdit}
          onSave={props.onSave}
          pending={pending}
        />
      </article>
    );
  }

  return (
    <article className={`memory-card${memory.pinned ? " pinned" : ""}`}>
      <div className="memory-card-mark" aria-hidden="true">
        {category.short}
      </div>
      <div className="memory-card-main">
        <div className="memory-card-kicker">
          <span className={`memory-category ${memory.category}`}>{category.label}</span>
          {memory.pinned && <span className="memory-pinned">优先</span>}
          <span>{memory.source_type === "conversation" ? "从对话提取" : "手动记录"}</span>
        </div>
        <p className="memory-fact">{memory.fact}</p>
        <div className="memory-card-meta">
          {view === "current" ? (
            <>
              <span>使用 {memory.access_count} 次</span>
              <span>上次召回 {formatTime(memory.last_used_at)}</span>
              <span>记录于 {formatTime(memory.valid_from)}</span>
            </>
          ) : (
            <>
              <span>生效 {formatTime(memory.valid_from)}</span>
              <span>失效 {formatTime(memory.invalid_at)}</span>
              <span>{memory.superseded_by === null ? "已删除" : "已有新版本"}</span>
            </>
          )}
        </div>
      </div>
      <div className="memory-card-actions">
        {view === "current" ? (
          <>
            <button disabled={pending} onClick={props.onPin} type="button">
              {memory.pinned ? "取消优先" : "设为优先"}
            </button>
            <button disabled={pending} onClick={props.onEdit} type="button">
              编辑
            </button>
            <button className="danger" disabled={pending} onClick={props.onDelete} type="button">
              删除
            </button>
          </>
        ) : (
          <button className="restore" disabled={pending} onClick={props.onRestore} type="button">
            {pending ? "恢复中…" : "恢复此版本"}
          </button>
        )}
      </div>
    </article>
  );
}

export default function MemoryPage() {
  const { state: authState, invalidate } = useAdminSession();
  const [view, setView] = useState<MemoryView>("current");
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  const load = useCallback(async (nextView: MemoryView) => {
    setLoading(true);
    try {
      const response = await fetchMemories(nextView);
      setItems(response.items);
      setError(null);
    } catch (reason) {
      setError(friendlyError(reason));
      if (reason instanceof ApiError && reason.status === 401) invalidate();
    } finally {
      setLoading(false);
    }
  }, [invalidate]);

  useEffect(() => {
    if (authState !== "authenticated") return;
    const timer = setTimeout(() => void load(view), 0);
    return () => clearTimeout(timer);
  }, [authState, load, view]);

  const stats = useMemo(
    () => ({
      total: items.length,
      pinned: items.filter((item) => item.pinned).length,
      used: items.reduce((sum, item) => sum + item.access_count, 0),
    }),
    [items],
  );

  const runMutation = useCallback(
    async (id: string, operation: () => Promise<unknown>) => {
      setPendingId(id);
      setError(null);
      try {
        await operation();
        setEditingId(null);
        setShowCreate(false);
        await load(view);
      } catch (reason) {
        setError(friendlyError(reason));
        if (reason instanceof ApiError && reason.status === 401) invalidate();
      } finally {
        setPendingId(null);
      }
    },
    [invalidate, load, view],
  );

  return (
    <main className="app-frame memory-frame">
      <Topbar />
      <div className="memory-body">
        <header className="memory-header">
          <div>
            <span className="eyebrow">Personal context</span>
            <h1>长期记忆</h1>
            <p>只属于已登录 owner。它们会在相关问题出现时作为背景被召回，不会被当作指令执行。</p>
          </div>
          {authState === "authenticated" && view === "current" && (
            <button className="memory-add" onClick={() => setShowCreate((value) => !value)} type="button">
              <span aria-hidden="true">＋</span>
              手动添加
            </button>
          )}
        </header>

        {authState === "unknown" && (
          <section className="memory-gate loading" aria-live="polite">
            <span className="memory-gate-mark">···</span>
            <div><h2>正在确认 owner 会话</h2><p>个人记忆不会在身份未确认时加载。</p></div>
          </section>
        )}

        {authState !== "unknown" && authState !== "authenticated" && (
          <section className="memory-gate">
            <span className="memory-gate-mark" aria-hidden="true">钥</span>
            <div>
              <span className="eyebrow">Private by default</span>
              <h2>登录后才会打开个人记忆</h2>
              <p>匿名 demo 不会抽取、读取或注入 owner 的任何记忆。请使用右上角的 owner 登录。</p>
            </div>
          </section>
        )}

        {authState === "authenticated" && (
          <>
            <section className="memory-toolbar">
              <div className="memory-tabs" role="tablist" aria-label="记忆视图">
                <button
                  aria-selected={view === "current"}
                  onClick={() => { setView("current"); setEditingId(null); }}
                  role="tab"
                  type="button"
                >
                  当前记忆
                </button>
                <button
                  aria-selected={view === "history"}
                  onClick={() => { setView("history"); setEditingId(null); setShowCreate(false); }}
                  role="tab"
                  type="button"
                >
                  历史版本
                </button>
              </div>
              <div className="memory-stats" aria-label="当前视图统计">
                <span><strong>{stats.total}</strong> 条</span>
                {view === "current" && <><span><strong>{stats.pinned}</strong> 条优先</span><span>累计使用 <strong>{stats.used}</strong> 次</span></>}
              </div>
            </section>

            {showCreate && (
              <MemoryEditor
                onCancel={() => setShowCreate(false)}
                onSave={(value) => void runMutation("new", () => createMemory(value))}
                pending={pendingId === "new"}
              />
            )}

            {error !== null && <div className="memory-notice error" role="alert">{error}</div>}
            {loading && items.length === 0 && <div className="memory-notice">正在整理记忆…</div>}

            {!loading && items.length === 0 && (
              <section className="memory-empty">
                <span aria-hidden="true">空</span>
                <h2>{view === "current" ? "还没有长期记忆" : "还没有历史版本"}</h2>
                <p>{view === "current" ? "对话中的稳定偏好会在回答完成后异步提取，你也可以手动添加。" : "编辑或删除记忆后，旧版本会保留在这里。"}</p>
              </section>
            )}

            <section className="memory-list" aria-busy={loading} aria-live="polite">
              {items.map((memory) => (
                <MemoryCard
                  editing={editingId === memory.id}
                  key={memory.id}
                  memory={memory}
                  onCancelEdit={() => setEditingId(null)}
                  onDelete={() => {
                    if (window.confirm("删除后会停止召回这条记忆，但历史版本仍可恢复。确定删除吗？")) {
                      void runMutation(memory.id, () => deleteMemory(memory.id));
                    }
                  }}
                  onEdit={() => setEditingId(memory.id)}
                  onPin={() => void runMutation(memory.id, () => updateMemory(memory.id, { pinned: !memory.pinned }))}
                  onRestore={() => void runMutation(memory.id, () => restoreMemory(memory.id))}
                  onSave={(value) => void runMutation(memory.id, () => updateMemory(memory.id, value))}
                  pending={pendingId === memory.id}
                  view={view}
                />
              ))}
            </section>
          </>
        )}
      </div>
    </main>
  );
}
