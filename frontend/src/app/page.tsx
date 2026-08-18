"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { AnswerMarkdown } from "@/components/answer-markdown";
import { EvidencePreview } from "@/components/evidence-preview";
import { Topbar } from "@/components/topbar";
import {
  type AnswerMode,
  ApiError,
  cancelRun,
  type ConversationMessage,
  type ConversationSummary,
  createConversation,
  createRun,
  deleteConversation,
  fetchConversationMessages,
  fetchConversations,
  getRun,
} from "@/lib/api";
import type { CitationPayload } from "@/lib/run-protocol";
import { isRunFinished } from "@/lib/run-state";
import { useRunStream } from "@/lib/use-run-stream";

function ArrowIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <path d="M4 10h12M11 5l5 5-5 5" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <rect height="8" rx="1" width="8" x="6" y="6" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <path d="M4.5 6h11M8 3.5h4M6.5 6l.7 10h5.6l.7-10M8.5 8.5v5M11.5 8.5v5" />
    </svg>
  );
}

function AskForm({ onSubmit, busy }: { onSubmit: (query: string) => void; busy: boolean }) {
  const [query, setQuery] = useState("");

  return (
    <form
      className="ask-form"
      onSubmit={(event) => {
        event.preventDefault();
        const nextQuery = query.trim();
        if (nextQuery !== "") {
          onSubmit(nextQuery);
          setQuery("");
        }
      }}
    >
      <label htmlFor="knowledge-query">向资料库提问</label>
      <div className="ask-control">
        <textarea
          autoFocus
          disabled={busy}
          id="knowledge-query"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="例如：混合检索为什么比单路召回更稳定？"
          rows={2}
          value={query}
        />
        <button disabled={busy || query.trim() === ""} type="submit">
          <span>{busy ? "回答中" : "提问"}</span>
          <ArrowIcon />
        </button>
      </div>
      <p>Enter 发送 · Shift + Enter 换行 · 答案仅依据已入库资料</p>
    </form>
  );
}

function ConversationSidebar({
  activeId,
  conversations,
  deletingId,
  disabled,
  onCreate,
  onDelete,
  onSelect,
}: {
  activeId: string | null;
  conversations: ConversationSummary[];
  deletingId: string | null;
  disabled: boolean;
  onCreate: () => void;
  onDelete: (conversation: ConversationSummary) => Promise<void>;
  onSelect: (id: string) => void;
}) {
  const [pendingDelete, setPendingDelete] = useState<ConversationSummary | null>(null);
  const activeConversation = conversations.find((conversation) => conversation.id === activeId);

  return (
    <>
      <aside aria-label="会话列表" className="conversation-sidebar">
        <div className="conversation-sidebar-heading">
          <div>
            <span className="eyebrow">Conversations</span>
            <h1>会话</h1>
          </div>
          <span>{conversations.length}</span>
        </div>
        <button
          className="new-conversation-button"
          disabled={disabled || deletingId !== null}
          onClick={onCreate}
          type="button"
        >
          <span aria-hidden="true">＋</span>
          新建会话
        </button>

        <label className="conversation-mobile-select">
          <span className="sr-only">切换会话</span>
          <select
            aria-label="切换会话"
            disabled={disabled || deletingId !== null || conversations.length === 0}
            onChange={(event) => onSelect(event.target.value)}
            value={activeId ?? ""}
          >
            {activeId === null && <option value="">尚未选择会话</option>}
            {conversations.map((conversation) => (
              <option key={conversation.id} value={conversation.id}>
                {conversation.title ?? "未命名会话"}
              </option>
            ))}
          </select>
        </label>
        {activeConversation !== undefined && (
          <button
            aria-label={`删除会话：${activeConversation.title ?? "未命名会话"}`}
            className="conversation-mobile-delete"
            disabled={disabled || deletingId !== null}
            onClick={() => setPendingDelete(activeConversation)}
            type="button"
          >
            <TrashIcon />
          </button>
        )}

        {conversations.length === 0 ? (
          <div className="conversation-sidebar-empty">
            <span>还没有会话</span>
            <p>创建后，每段对话都会在这里独立保存。</p>
          </div>
        ) : (
          <ol className="conversation-list">
            {conversations.map((conversation) => {
              const active = conversation.id === activeId;
              const title = conversation.title ?? "未命名会话";
              return (
                <li className={active ? "active" : undefined} key={conversation.id}>
                  <button
                    aria-current={active ? "page" : undefined}
                    className="conversation-item-select"
                    data-conversation-id={conversation.id}
                    disabled={(disabled && !active) || deletingId !== null}
                    onClick={() => onSelect(conversation.id)}
                    type="button"
                  >
                    <span className="conversation-item-title">{title}</span>
                    <span className="conversation-item-preview">
                      {conversation.latest_message ?? "从一个新问题开始"}
                    </span>
                    <span className="conversation-item-meta">
                      {conversation.message_count === 0
                        ? "空会话"
                        : `${conversation.message_count} 条消息`}
                    </span>
                  </button>
                  <button
                    aria-label={`删除会话：${title}`}
                    className="conversation-delete-button"
                    disabled={disabled || deletingId !== null}
                    onClick={() => setPendingDelete(conversation)}
                    type="button"
                  >
                    <TrashIcon />
                  </button>
                </li>
              );
            })}
          </ol>
        )}

        <p className="conversation-isolation-note">
          <span aria-hidden="true">●</span>
          短期上下文按会话隔离
        </p>
      </aside>

      {pendingDelete !== null && (
        <div className="conversation-delete-backdrop">
          <section
            aria-describedby="delete-conversation-description"
            aria-labelledby="delete-conversation-title"
            aria-modal="true"
            className="conversation-delete-dialog"
            role="alertdialog"
          >
            <span className="eyebrow">删除会话</span>
            <h2 id="delete-conversation-title">
              {pendingDelete.title ?? "未命名会话"}
            </h2>
            <p id="delete-conversation-description">
              会话及消息会永久删除。已经抽取的长期记忆仍会保留，可前往记忆页单独管理。
            </p>
            <div>
              <button onClick={() => setPendingDelete(null)} type="button">
                取消
              </button>
              <button
                className="danger"
                disabled={deletingId !== null}
                onClick={() => void onDelete(pendingDelete).finally(() => setPendingDelete(null))}
                type="button"
              >
                {deletingId === pendingDelete.id ? "正在删除…" : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}

function ConversationHistory({
  currentRunId,
  messages,
  onSelectCitation,
}: {
  currentRunId: string | null;
  messages: ConversationMessage[];
  onSelectCitation: (citation: CitationPayload) => void;
}) {
  const visible = messages.filter(
    (message) =>
      message.content.trim() !== "" &&
      (message.run_id !== currentRunId || message.role === "user"),
  );
  if (visible.length === 0) return null;

  return (
    <section aria-label="会话记录" className="conversation-thread">
      {visible.map((message) =>
        message.role === "user" ? (
          <article className="chat-turn user" key={message.id}>
            <span className="chat-role">你</span>
            <p>{message.content}</p>
          </article>
        ) : (
          <article className="chat-turn assistant" key={message.id}>
            <div className="chat-role-line">
              <span className="chat-role">WorkPilot</span>
              {message.answer_mode === "general" && <span>通用知识 · 未溯源</span>}
            </div>
            <AnswerMarkdown
              activeCitationId={null}
              onSelectCitation={(id) => {
                const citation = message.citations.find((item) => item.citation_id === id);
                if (citation !== undefined) onSelectCitation(citation);
              }}
              text={message.content}
            />
          </article>
        ),
      )}
    </section>
  );
}

function CitationCard({
  citation,
  active,
  onSelect,
}: {
  citation: CitationPayload;
  active: boolean;
  onSelect: () => void;
}) {
  const page = citation.locations[0]?.page_no;
  return (
    <button
      aria-pressed={active}
      className={`citation-card${active ? " active" : ""}`}
      onClick={onSelect}
      type="button"
    >
      <span className="citation-index">{citation.citation_id}</span>
      <span className="citation-body">
        <strong>{citation.title}</strong>
        <span>
          {citation.heading_path.at(-1) ?? citation.source_uri}
          {page === undefined ? "" : ` · 第 ${page} 页`}
        </span>
        <q>{citation.quote}</q>
      </span>
      <span className="citation-open">
        查看原文
        <ArrowIcon />
      </span>
    </button>
  );
}

function RunWorkspace({
  runId,
  conversationId,
}: {
  runId: string | null;
  conversationId: string | null;
}) {
  const router = useRouter();
  const state = useRunStream(runId);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [citationSelection, setCitationSelection] = useState<
    | { source: "current"; citationId: string }
    | { source: "history"; citation: CitationPayload }
    | null
  >(null);
  const [switching, setSwitching] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loadedMessages, setLoadedMessages] = useState<{
    conversationId: string;
    items: ConversationMessage[];
  } | null>(null);
  const messages =
    loadedMessages?.conversationId === conversationId ? loadedMessages.items : [];
  const conversationLoading = conversationId !== null && loadedMessages?.conversationId !== conversationId;
  const selectedCitation =
    citationSelection?.source === "history"
      ? citationSelection.citation
      : state.citations.find(
          (citation) => citation.citation_id === citationSelection?.citationId,
        ) ?? null;
  const busy = runId !== null && !isRunFinished(state);

  useEffect(() => {
    let active = true;
    void fetchConversations()
      .then((response) => {
        if (active) setConversations(response.items);
      })
      .catch(() => {
        if (active) setSubmitError("暂时无法加载会话列表");
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (conversationId === null) return;
    let active = true;
    void fetchConversationMessages(conversationId)
      .then((response) => {
        if (active) setLoadedMessages({ conversationId, items: response.items });
      })
      .catch(() => {
        if (active) {
          setLoadedMessages({ conversationId, items: [] });
          setSubmitError("暂时无法加载当前会话");
        }
      });
    return () => {
      active = false;
    };
  }, [conversationId]);

  const startConversation = useCallback(async () => {
    setSubmitError(null);
    try {
      const created = await createConversation();
      router.push(`/?conversation=${created.id}`);
    } catch (error) {
      setSubmitError(
        error instanceof ApiError ? `创建会话失败（${error.status}）` : "暂时无法创建会话",
      );
    }
  }, [router]);

  const removeConversation = useCallback(
    async (target: ConversationSummary) => {
      setDeletingId(target.id);
      setSubmitError(null);
      try {
        await deleteConversation(target.id);
        const remaining = conversations.filter((conversation) => conversation.id !== target.id);
        setConversations(remaining);
        if (target.id === conversationId) {
          const next = remaining[0];
          router.replace(next === undefined ? "/" : `/?conversation=${next.id}`);
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          setSubmitError("当前会话仍有回答在运行，请先停止或等待完成后再删除");
        } else {
          setSubmitError(
            error instanceof ApiError ? `删除会话失败（${error.status}）` : "暂时无法删除会话",
          );
        }
      } finally {
        setDeletingId(null);
      }
    },
    [conversationId, conversations, router],
  );

  const ask = useCallback(
    async (query: string, mode: AnswerMode = "grounded") => {
      setSubmitError(null);
      try {
        const created = await createRun({
          query,
          ...(conversationId === null ? {} : { conversation_id: conversationId }),
          ...(mode === "grounded" ? {} : { mode }),
        });
        router.replace(`/?run=${created.run_id}&conversation=${created.conversation_id}`);
      } catch (error) {
        setSubmitError(
          error instanceof ApiError ? `创建回答失败（${error.status}）` : "暂时无法创建回答",
        );
      }
    },
    [conversationId, router],
  );

  // 切换到通用知识要重新提问，而刷新过的页面上原问题只存在于 run 记录里（run.goal）。
  // 点了才去取：拒答是少数情况，没必要每次都多打一次接口。
  const askWithGeneralKnowledge = useCallback(async () => {
    if (runId === null) return;
    setSwitching(true);
    setSubmitError(null);
    try {
      const run = await getRun(runId);
      await ask(run.goal, "general");
    } catch (error) {
      setSubmitError(
        error instanceof ApiError ? `切换失败（${error.status}）` : "暂时无法切换到通用知识回答",
      );
    } finally {
      setSwitching(false);
    }
  }, [ask, runId]);

  return (
    <>
      <ConversationSidebar
        activeId={conversationId}
        conversations={conversations}
        deletingId={deletingId}
        disabled={busy}
        onCreate={() => void startConversation()}
        onDelete={removeConversation}
        onSelect={(id) => router.push(`/?conversation=${id}`)}
      />
      <section className="conversation-panel">
        {submitError !== null && <div className="inline-error">{submitError}</div>}
        {conversationLoading && <div className="conversation-loading">正在加载会话记录…</div>}
        <ConversationHistory
          currentRunId={runId}
          messages={messages}
          onSelectCitation={(citation) =>
            setCitationSelection({ source: "history", citation })
          }
        />

        {runId === null && messages.length === 0 && !conversationLoading && (
          <div className="empty-state">
            <span className="empty-mark">W</span>
            <div>
              <h1>一段会话，只保留一条清晰的思路。</h1>
              <p>新建会话后继续追问；短期上下文不会串到其他会话，长期偏好仍可按需召回。</p>
            </div>
          </div>
        )}

        {runId !== null && (
          <div className="run-result">
            <div className="run-meta">
              <span className={`status-dot ${state.phase}`} />
              <span>{state.phase === "connecting" ? "正在检索证据" : state.phase}</span>
              <span className="run-id">run {runId.slice(0, 8)}</span>
              {busy && (
                <button
                  className="cancel-button"
                  onClick={() => void cancelRun(runId)}
                  type="button"
                >
                  <StopIcon />
                  停止
                </button>
              )}
            </div>

            {state.phase === "connecting" && (
              <div className="answer-skeleton" aria-label="正在生成回答">
                <span />
                <span />
                <span />
              </div>
            )}

            {!state.grounded && state.text !== "" && (
              <div className="ungrounded-banner" role="note">
                <strong>以下回答来自模型的通用知识</strong>
                <span>不基于你的资料库，没有引用可以核验，请自行判断准确性。</span>
              </div>
            )}

            {state.text !== "" && (
              <AnswerMarkdown
                activeCitationId={
                  citationSelection?.source === "current"
                    ? citationSelection.citationId
                    : null
                }
                // 正文里的 [S1] 与下方引用卡片指向同一份选中状态：点哪边都是打开这条原文。
                // 引用还没到达时点了也无妨——selectedCitation 查不到就还是占位态。
                onSelectCitation={(id) => {
                  setCitationSelection({ source: "current", citationId: id });
                }}
                text={state.text}
              />
            )}

            {state.phase === "refused" && state.text === "" && (
              <div className="refusal-card">
                <strong>资料库中未找到足够证据</strong>
                <p>我没有用通用知识补全答案，以免把未经核验的信息混进来。</p>
                <div className="refusal-actions">
                  <button
                    className="ghost-button"
                    disabled={switching}
                    onClick={() => void askWithGeneralKnowledge()}
                    type="button"
                  >
                    {switching ? "正在切换…" : "基于通用知识回答"}
                  </button>
                  <span>换来的答案不可溯源，只在你确认能自己判断时使用。</span>
                </div>
              </div>
            )}

            {state.error !== null && (
              <div className="inline-error">
                <strong>{state.error.user_message}</strong>
                {state.error.retryable && <span>稍后可以重新提问。</span>}
              </div>
            )}

            {state.citations.length > 0 && (
              <section className="citations-section">
                <div className="section-heading">
                  <div>
                    <span className="eyebrow">可核验来源</span>
                    <h2>引用证据</h2>
                  </div>
                  <span>{state.citations.length} 处</span>
                </div>
                <div className="citation-list">
                  {state.citations.map((citation) => (
                    <CitationCard
                      key={citation.citation_id}
                      active={
                        citationSelection?.source === "current" &&
                        citationSelection.citationId === citation.citation_id
                      }
                      citation={citation}
                      onSelect={() =>
                        setCitationSelection({
                          source: "current",
                          citationId: citation.citation_id,
                        })
                      }
                    />
                  ))}
                </div>
              </section>
            )}

            {state.latencyMs !== null && (
              <p className="run-footnote">
                本次回答耗时 {(state.latencyMs / 1000).toFixed(1)} 秒
                {state.costUsd === null ? "" : ` · 模型成本 $${state.costUsd}`}
              </p>
            )}
          </div>
        )}
        <div className="composer-dock">
          <AskForm busy={busy} onSubmit={(query) => void ask(query)} />
        </div>
      </section>

      {selectedCitation !== null ? (
        <EvidencePreview
          key={selectedCitation.block_id}
          citation={selectedCitation}
          onClose={() => setCitationSelection(null)}
        />
      ) : (
        <aside className="evidence-placeholder" aria-label="原文预览占位">
          <div className="preview-grid" />
          <div>
            <span className="eyebrow">Evidence viewer</span>
            <h2>原文会在这里打开</h2>
            <p>回答完成后，点击任意引用即可查看对应页面与精确高亮位置。</p>
          </div>
        </aside>
      )}
    </>
  );
}

function Conversation() {
  const params = useSearchParams();
  const runId = params.get("run");
  const conversationId = params.get("conversation");

  return (
    <main className="app-frame">
      <Topbar />
      <div className="workspace">
        <RunWorkspace
          key={`${conversationId ?? "no-conversation"}:${runId ?? "idle"}`}
          conversationId={conversationId}
          runId={runId}
        />
      </div>
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<div className="app-loading">正在打开工作台…</div>}>
      <Conversation />
    </Suspense>
  );
}
