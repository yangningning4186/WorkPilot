/**
 * 把 run 事件流折叠成可渲染状态。
 *
 * 刻意做成纯函数：实时推送和刷新回放读的是同一份 run_events，前端也必须用同一套
 * 折叠逻辑，否则"实时看到的"和"刷新后看到的"又会分叉——那正是后端把 run_events
 * 定为唯一真相源要消灭的问题（ADR-0007）。
 */

import {
  type CitationPayload,
  type ErrorPayload,
  type MessageDeltaPayload,
  type MessageDonePayload,
  type MessageStartPayload,
  type StreamEnvelope,
  envelopeSeq,
} from "./run-protocol";

export type RunPhase = "connecting" | "streaming" | "done" | "refused" | "error";

export interface RunState {
  /** 已消费的最大 seq，续传游标。0 表示还没收到任何事件。 */
  cursor: bigint;
  phase: RunPhase;
  messageId: string | null;
  text: string;
  citations: CitationPayload[];
  error: ErrorPayload | null;
  refusalReason: string | null;
  latencyMs: number | null;
  costUsd: string | null;
}

export function initialRunState(): RunState {
  return {
    cursor: 0n,
    phase: "connecting",
    messageId: null,
    text: "",
    citations: [],
    error: null,
    refusalReason: null,
    latencyMs: null,
    costUsd: null,
  };
}

/**
 * 应用一个事件。
 *
 * seq 不大于游标的一律丢弃：断线重连时服务端从 Last-Event-ID 续发，但重连竞态下
 * 仍可能重放已看过的事件，去重必须在这里做，否则正文会出现重复片段。
 */
export function applyEnvelope(state: RunState, envelope: StreamEnvelope): RunState {
  const seq = envelopeSeq(envelope);
  if (seq <= state.cursor) {
    return state;
  }
  const next: RunState = { ...state, cursor: seq };

  switch (envelope.type) {
    case "message.start": {
      const data = envelope.data as MessageStartPayload;
      next.messageId = data.message_id;
      next.phase = "streaming";
      return next;
    }
    case "message.delta": {
      const data = envelope.data as MessageDeltaPayload;
      next.text = state.text + data.text;
      next.phase = "streaming";
      return next;
    }
    case "citation": {
      const data = envelope.data as CitationPayload;
      // 同一 citation_id 只保留一份：回放与实时流可能都投递过它。
      if (state.citations.some((item) => item.citation_id === data.citation_id)) {
        return next;
      }
      next.citations = [...state.citations, data];
      return next;
    }
    case "message.done": {
      const data = envelope.data as MessageDonePayload;
      next.messageId = data.message_id;
      next.phase = data.refused ? "refused" : "done";
      next.refusalReason = data.refusal_reason;
      next.latencyMs = data.latency_ms;
      next.costUsd = data.cost_usd;
      return next;
    }
    case "error": {
      next.error = envelope.data as ErrorPayload;
      next.phase = "error";
      return next;
    }
    default:
      return next;
  }
}

export function foldEnvelopes(envelopes: StreamEnvelope[]): RunState {
  return envelopes.reduce(applyEnvelope, initialRunState());
}

export function isRunFinished(state: RunState): boolean {
  return state.phase === "done" || state.phase === "refused" || state.phase === "error";
}
