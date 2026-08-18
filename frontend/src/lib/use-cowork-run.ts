"use client";

import { useEffect, useState } from "react";

import { fetchRunEventLog } from "./api";
import {
  type ArtifactPayload,
  type CoworkToolCatalogEntry,
  type ErrorPayload,
  type InteractionResolvedPayload,
  type InterruptPayload,
  type MessageDeltaPayload,
  type PlanPayload,
  type RunDonePayload,
  type StepUpdatePayload,
  type StreamEnvelope,
  type ToolEventPayload,
  envelopeSeq,
} from "./run-protocol";

export type CoworkRunPhase =
  | "idle"
  | "connecting"
  | "executing"
  | "waiting_human"
  | "done"
  | "budget_exceeded"
  | "cancelled"
  | "error";

export interface CoworkProgressStep {
  id: string;
  idx: number;
  tool: string;
  status: "pending" | "running" | "done" | "failed";
  detail: string | null;
  effectRef: string | null;
}

export interface CoworkRunView {
  cursor: bigint;
  phase: CoworkRunPhase;
  tools: CoworkToolCatalogEntry[];
  steps: CoworkProgressStep[];
  summary: string;
  artifactEvents: ArtifactPayload[];
  interrupt: InterruptPayload | null;
  error: string | null;
}

const EMPTY: CoworkRunView = {
  cursor: 0n,
  phase: "idle",
  tools: [],
  steps: [],
  summary: "",
  artifactEvents: [],
  interrupt: null,
  error: null,
};

function upsertStep(
  steps: CoworkProgressStep[],
  data: StepUpdatePayload | ToolEventPayload,
  status: CoworkProgressStep["status"],
  detail: string | null,
): CoworkProgressStep[] {
  if (data.step_id === undefined) return steps;
  const current = steps.find((item) => item.id === data.step_id);
  const next: CoworkProgressStep = {
    id: data.step_id,
    idx: data.step_idx ?? current?.idx ?? steps.length,
    tool: data.tool ?? current?.tool ?? "cowork",
    status,
    detail,
    effectRef: "effect_ref" in data ? (data.effect_ref ?? null) : (current?.effectRef ?? null),
  };
  return current === undefined
    ? [...steps, next].sort((left, right) => left.idx - right.idx)
    : steps.map((item) => (item.id === data.step_id ? next : item));
}

function applyEvent(state: CoworkRunView, envelope: StreamEnvelope): CoworkRunView {
  const seq = envelopeSeq(envelope);
  if (seq <= state.cursor) return state;
  const next = { ...state, cursor: seq };
  switch (envelope.type) {
    case "plan": {
      const data = envelope.data as PlanPayload;
      return { ...next, phase: "executing", tools: data.tools ?? [] };
    }
    case "step.update": {
      const data = envelope.data as StepUpdatePayload;
      if (data.step_id === undefined) {
        return { ...next, phase: "executing", summary: data.summary ?? state.summary };
      }
      const status =
        data.status === "recovering"
          ? "running"
          : data.status === "skipped"
            ? "done"
            : data.status;
      return {
        ...next,
        phase: "executing",
        steps: upsertStep(state.steps, data, status, data.summary ?? null),
      };
    }
    case "tool.start": {
      const data = envelope.data as ToolEventPayload;
      return {
        ...next,
        phase: "executing",
        steps: upsertStep(state.steps, data, "running", "正在执行"),
      };
    }
    case "tool.result": {
      const data = envelope.data as ToolEventPayload;
      return {
        ...next,
        phase: "executing",
        steps: upsertStep(
          state.steps,
          data,
          "done",
          data.reused ? "复用了已完成的幂等结果" : "执行完成",
        ),
      };
    }
    case "tool.error": {
      const data = envelope.data as ToolEventPayload;
      return {
        ...next,
        phase: "executing",
        steps: upsertStep(state.steps, data, "failed", data.error ?? "执行失败，Agent 将尝试修正"),
      };
    }
    case "interrupt": {
      const data = envelope.data as InterruptPayload;
      return { ...next, phase: "waiting_human", interrupt: data };
    }
    case "interaction.resolved": {
      const data = envelope.data as InteractionResolvedPayload;
      return {
        ...next,
        phase: "executing",
        interrupt: null,
        summary:
          data.status === "rejected" ? "用户未批准这项请求，Cowork 正在调整方案。" : state.summary,
      };
    }
    case "artifact": {
      const data = envelope.data as ArtifactPayload;
      if (
        data.artifact_id !== undefined &&
        state.artifactEvents.some((item) => item.artifact_id === data.artifact_id)
      ) {
        return next;
      }
      return { ...next, artifactEvents: [...state.artifactEvents, data] };
    }
    case "message.delta": {
      const data = envelope.data as MessageDeltaPayload;
      return { ...next, summary: state.summary + data.text };
    }
    case "run.done": {
      const data = envelope.data as RunDonePayload;
      if (data.status === "cancelled") return { ...next, phase: "cancelled" };
      if (data.status === "budget_exceeded") return { ...next, phase: "budget_exceeded" };
      if (data.status === "failed") return { ...next, phase: "error" };
      return { ...next, phase: "done" };
    }
    case "error": {
      const data = envelope.data as ErrorPayload;
      if (data.code === "cancelled") {
        return { ...next, phase: "cancelled", summary: data.user_message, error: null };
      }
      return { ...next, phase: "error", error: data.user_message };
    }
    default:
      return next;
  }
}

export function useCoworkRun(runId: string | null): CoworkRunView {
  const [state, setState] = useState<CoworkRunView>(EMPTY);

  useEffect(() => {
    setState(runId === null ? EMPTY : { ...EMPTY, phase: "connecting" });
    if (runId === null) return;
    let stopped = false;
    let cursor = 0n;

    const poll = async () => {
      while (!stopped) {
        try {
          const response = await fetchRunEventLog(runId, cursor);
          if (stopped) return;
          if (response.items.length > 0) {
            cursor = response.items.reduce(
              (maximum, item) => (envelopeSeq(item) > maximum ? envelopeSeq(item) : maximum),
              cursor,
            );
            setState((previous) => response.items.reduce(applyEvent, previous));
            const terminal = response.items.some(
              (item) => item.type === "run.done" || item.type === "error",
            );
            if (terminal) return;
          }
        } catch (reason) {
          if (!stopped) {
            setState((previous) => ({
              ...previous,
              phase: "error",
              error: reason instanceof Error ? reason.message : "无法读取 Cowork 进度",
            }));
          }
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 650));
      }
    };
    void poll();
    return () => {
      stopped = true;
    };
  }, [runId]);

  return state;
}
