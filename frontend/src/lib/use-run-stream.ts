"use client";

import { useEffect, useState } from "react";

import { runEventsUrl } from "./api";
import { type RunEventType, isTerminalEvent, parseEnvelope } from "./run-protocol";
import { type RunState, applyEnvelope, initialRunState } from "./run-state";

const EVENT_TYPES: RunEventType[] = [
  "message.start",
  "message.delta",
  "citation",
  "message.done",
  "plan",
  "step.update",
  "interrupt",
  "artifact",
  "run.done",
  "error",
];

/**
 * 订阅一个 run 的事件流。
 *
 * B1 刷新恢复：从 after_seq=0 重新拉，服务端先补历史再续实时，折叠逻辑与实时路径同一套。
 * B2 断线续传：EventSource 自动重连时用的是**最初那个 URL**（after_seq=0），带回
 *    Last-Event-ID；服务端让 Last-Event-ID 优先于查询参数，重连才会从断点续发而不是
 *    从头重放。也正因如此，这里不需要自己维护游标。
 * B5 并发隔离：每个 run 一个 EventSource 与一份 state，不共用全局状态。
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

    const source = new EventSource(runEventsUrl(runId, 0n), { withCredentials: true });

    const handle = (event: MessageEvent<string>) => {
      const envelope = parseEnvelope(event.data);
      // 畸形事件直接丢弃，不能让它打断整条长连接。
      if (envelope === null || envelope.run_id !== runId) {
        return;
      }
      setState((previous) => applyEnvelope(previous, envelope));
      if (isTerminalEvent(envelope.type)) {
        // 终态之后不会再有事件；不关的话浏览器会一直重连。
        source.close();
      }
    };

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, handle as EventListener);
    }

    return () => {
      for (const type of EVENT_TYPES) {
        source.removeEventListener(type, handle as EventListener);
      }
      source.close();
    };
  }, [runId]);

  return state;
}
