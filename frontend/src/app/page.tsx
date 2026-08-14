"use client";

import { Suspense, useCallback, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { EvidencePreview } from "@/components/evidence-preview";
import { ApiError, cancelRun, createRun } from "@/lib/api";
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
  const [selectedCitationId, setSelectedCitationId] = useState<string | null>(null);
  const selectedCitation = state.citations.find(
    (citation) => citation.citation_id === selectedCitationId,
  );
  const busy = runId !== null && !isRunFinished(state);

  const ask = useCallback(
    async (query: string) => {
      setSubmitError(null);
      try {
        const created = await createRun({
          query,
          ...(conversationId === null ? {} : { conversation_id: conversationId }),
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

  return (
    <>
      <section className="conversation-panel">
        <AskForm busy={busy} onSubmit={(query) => void ask(query)} />
        {submitError !== null && <div className="inline-error">{submitError}</div>}

        {runId === null ? (
          <div className="empty-state">
            <span className="empty-mark">W</span>
            <div>
              <h1>从你的资料里，找到有出处的答案。</h1>
              <p>每条结论都可以回到原文核验；证据不足时，WorkPilot 会直接说明找不到。</p>
            </div>
          </div>
        ) : (
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

            {state.text !== "" && <article className="answer-copy">{state.text}</article>}

            {state.phase === "refused" && state.text === "" && (
              <div className="refusal-card">
                <strong>资料库中未找到足够证据</strong>
                <p>我没有用通用知识补全答案，以免把未经核验的信息混进来。</p>
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
                      active={citation.citation_id === selectedCitationId}
                      citation={citation}
                      onSelect={() => setSelectedCitationId(citation.citation_id)}
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
      </section>

      {selectedCitation !== undefined ? (
        <EvidencePreview
          key={selectedCitation.block_id}
          citation={selectedCitation}
          onClose={() => setSelectedCitationId(null)}
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
      <header className="topbar">
        <Link className="brand" href="/">
          <span>W</span>
          <strong>WorkPilot</strong>
        </Link>
        <div className="product-note">
          <span />
          本地资料库已连接
        </div>
      </header>
      <div className="workspace">
        <RunWorkspace
          key={runId ?? "new-run"}
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
