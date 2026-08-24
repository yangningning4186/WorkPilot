"use client";

import { useEffect, useState } from "react";

import { fetchRunEventStream } from "./api";
import { envelopeSeq, isTerminalEvent, parseEnvelope } from "./run-protocol";
import { parseSseFrame, takeSseFrame, waitForStreamRetry } from "./run-sse";
import { type RunState, applyEnvelope, initialRunState } from "./run-state";

const DEFAULT_RETRY_MS = 1_000;

/**
 * 订阅一个 run 的事件流。
 *
 * B1 刷新恢复：从 after_seq=0 重新拉，服务端先补历史再续实时，折叠逻辑与实时路径同一套。
 * B2 断线续传：fetch 流断开后使用已消费的最大 seq 重新请求，不依赖浏览器自动维护
 *    Last-Event-ID。这样既能携带桌面启动 header，也不会从头重放正文。
 * B5 并发隔离：每个 run 一条 fetch 流、一个 AbortController 与一份 state，不共用游标。
 */
export function useRunStream(runId: string | null): RunState {
  const [state, setState] = useState<RunState>(initialRunState);
  const [activeRunId, setActiveRunId] = useState<string | null>(runId);

  // 换 run 时在渲染期重置，而不是在 effect 里 setState：后者会多渲染一帧，
  // 而那一帧里上一个 run 的正文会串到新 run 上。
  if (activeRunId !== runId) {
    setActiveRunId(runId);
    setState(initialRunState());
  }

  useEffect(() => {
    if (runId === null) {
      return;
    }
    const controller = new AbortController();
    let stopped = false;
    let cursor = 0n;
    let retryMs = DEFAULT_RETRY_MS;

    const fail = (message: string) => {
      setState((previous) => ({
        ...previous,
        phase: "error",
        error: { code: "stream_failed", retryable: true, user_message: message },
      }));
    };

    const consume = async () => {
      while (!stopped) {
        try {
          const response = await fetchRunEventStream(runId, cursor, controller.signal);
          if (!response.ok) {
            fail(`无法连接任务事件流（${response.status}）`);
            return;
          }
          if (response.body === null) {
            fail("任务事件流没有返回可读取的内容");
            return;
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!stopped) {
            const { done, value } = await reader.read();
            buffer += decoder.decode(value, { stream: !done });
            let next = takeSseFrame(buffer);
            while (next !== null) {
              const [rawFrame, remaining] = next;
              buffer = remaining;
              const frame = parseSseFrame(rawFrame);
              if (frame.retryMs !== null) retryMs = frame.retryMs;
              if (frame.data !== null) {
                const envelope = parseEnvelope(frame.data);
                // 畸形或串 run 的事件直接丢弃，不能让它打断整条长连接。
                if (envelope !== null && envelope.run_id === runId) {
                  const seq = envelopeSeq(envelope);
                  if (seq > cursor) cursor = seq;
                  setState((previous) => applyEnvelope(previous, envelope));
                  if (isTerminalEvent(envelope.type)) {
                    stopped = true;
                    await reader.cancel();
                    return;
                  }
                }
              }
              next = takeSseFrame(buffer);
            }
            if (done) break;
          }
        } catch (reason) {
          if (stopped || controller.signal.aborted) return;
          // 保留 SSE 的自动重连语义。HTTP 错误在上面明确结束，只有瞬时断网或
          // 服务端中断走这里。
          void reason;
        }
        if (!stopped) await waitForStreamRetry(retryMs, controller.signal);
      }
    };

    void consume();

    return () => {
      stopped = true;
      controller.abort();
    };
  }, [runId]);

  return state;
}
