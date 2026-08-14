"use client";

import { Suspense, useCallback, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { ApiError, cancelRun, createRun } from "@/lib/api";
import { useRunStream } from "@/lib/use-run-stream";
import { isRunFinished } from "@/lib/run-state";

function AskForm({ onSubmit, busy }: { onSubmit: (query: string) => void; busy: boolean }) {
  const [query, setQuery] = useState("");
  return (
    <form
      className="flex gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (query.trim() !== "") {
          onSubmit(query.trim());
        }
      }}
    >
      <input
        className="flex-1 rounded border border-neutral-300 px-3 py-2 dark:border-neutral-700 dark:bg-neutral-900"
        value={query}
        placeholder="问一个资料库里的问题"
        onChange={(event) => setQuery(event.target.value)}
      />
      <button
        className="rounded bg-neutral-900 px-4 py-2 text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
        disabled={busy || query.trim() === ""}
        type="submit"
      >
        提问
      </button>
    </form>
  );
}

function Conversation() {
  const router = useRouter();
  const params = useSearchParams();
  // run_id 放 URL 里：刷新页面天然回到同一个 run（B1），不需要额外的本地存储。
  const runId = params.get("run");
  const conversationId = params.get("conversation");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const state = useRunStream(runId);

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
          error instanceof ApiError ? `创建 run 失败（${error.status}）` : "创建 run 失败",
        );
      }
    },
    [conversationId, router],
  );

  const busy = runId !== null && !isRunFinished(state);

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 p-8">
      <header>
        <h1 className="text-xl font-semibold">WorkPilot</h1>
        <p className="text-sm text-neutral-500">
          基于个人资料库的可溯源问答。答案只依据检索到的证据。
        </p>
      </header>

      <AskForm onSubmit={ask} busy={busy} />
      {submitError !== null && <p className="text-sm text-red-600">{submitError}</p>}

      {runId !== null && (
        <section className="flex flex-col gap-4">
          <div className="flex items-center gap-3 text-xs text-neutral-500">
            <span>run {runId.slice(0, 8)}</span>
            <span>状态 {state.phase}</span>
            {busy && (
              <button
                className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
                onClick={() => void cancelRun(runId)}
                type="button"
              >
                取消
              </button>
            )}
          </div>

          {/* 首 token 未到时给阶段提示，而不是空白（B8）。 */}
          {state.phase === "connecting" && (
            <p className="text-sm text-neutral-500">正在检索…</p>
          )}

          {state.text !== "" && (
            <article className="whitespace-pre-wrap leading-relaxed">{state.text}</article>
          )}

          {state.error !== null && (
            <p className="text-sm text-red-600">
              {state.error.user_message}
              {state.error.retryable && "（可以重试）"}
            </p>
          )}

          {state.citations.length > 0 && (
            <aside className="flex flex-col gap-2">
              <h2 className="text-sm font-medium">引用</h2>
              {state.citations.map((citation) => (
                <div
                  key={citation.citation_id}
                  className="rounded border border-neutral-200 p-3 text-sm dark:border-neutral-800"
                >
                  <div className="text-xs text-neutral-500">
                    {citation.citation_id} · {citation.title}
                    {citation.locations[0] !== undefined &&
                      ` · 第 ${citation.locations[0].page_no} 页`}
                  </div>
                  <p className="mt-1">{citation.quote}</p>
                </div>
              ))}
            </aside>
          )}

          {state.latencyMs !== null && (
            <p className="text-xs text-neutral-500">
              耗时 {state.latencyMs} ms · 花费 ${state.costUsd}
            </p>
          )}
        </section>
      )}
    </main>
  );
}

export default function Page() {
  return (
    <Suspense fallback={null}>
      <Conversation />
    </Suspense>
  );
}
