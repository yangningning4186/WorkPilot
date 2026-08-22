"use client";

import { useEffect, useState } from "react";

import { fetchRunEventLog } from "./api";
import {
  type ApprovalWaivedPayload,
  type ArtifactPayload,
  type CoworkToolCatalogEntry,
  type ErrorPayload,
  type InteractionResolvedPayload,
  type InterruptPayload,
  type MemorySavedPayload,
  type MessageDeltaPayload,
  type PlanPayload,
  type RunDonePayload,
  type RunSleepingPayload,
  type StepUpdatePayload,
  type StreamEnvelope,
  type ReadingGotoPayload,
  type TodoItem,
  type TodoUpdatePayload,
  type ToolEventPayload,
  envelopeSeq,
} from "./run-protocol";

export type CoworkRunPhase =
  | "idle"
  | "connecting"
  | "executing"
  | "waiting_human"
  | "sleeping"
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
  /** 仅由 message.delta 累积的最终回答正文。 */
  answer: string;
  /** step.update / interaction.resolved 提供的运行进度说明。 */
  progressSummary: string;
  artifactEvents: ArtifactPayload[];
  /**
   * 模型自己维护的任务清单。和 `steps` 不同：steps 是每次 tool call 的事后日志，
   * todos 是模型对"这件事分几步"的主动声明，两者互相替代不了。
   */
  todos: TodoItem[];
  /**
   * 本次运行写过的记忆，供 UI 内联渲染「已记住 …［撤销］」。按写入顺序累积，
   * 同一条记忆被反复改写时只保留最后一次——撤销要还原到运行开始前的状态。
   */
  memoryWrites: MemorySavedPayload[];
  /**
   * 模型最近一次把阅读器带到哪里。只保留最后一次而不是累积成列表：面板同一时刻只能
   * 显示一个位置，攒一串历史只会让"现在该显示哪一个"变成一个需要额外规则的问题。
   * `seq` 让面板能区分"同一处又跳了一次"和"没有新跳转"——两次跳到同一页时对象内容
   * 完全相同，没有它 useEffect 不会重跑。
   */
  readerJump: (ReadingGotoPayload & { seq: number }) | null;
  interrupt: InterruptPayload | null;
  /**
   * 本次运行里被免审批放行的调用。要在时间线上看得见——否则用户只会看到一条命令
   * 凭空执行了，也无从判断该去撤销哪条规则。
   */
  waivedApprovals: ApprovalWaivedPayload[];
  /** 休眠到期时间；非 null 表示 run 已挂起，到点会自己继续，用户无需操作。 */
  sleepingUntil: string | null;
  error: string | null;
}

const EMPTY: CoworkRunView = {
  cursor: 0n,
  phase: "idle",
  tools: [],
  steps: [],
  answer: "",
  progressSummary: "",
  artifactEvents: [],
  todos: [],
  memoryWrites: [],
  readerJump: null,
  interrupt: null,
  waivedApprovals: [],
  sleepingUntil: null,
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
        return {
          ...next,
          phase: "executing",
          progressSummary: data.summary ?? state.progressSummary ?? "",
        };
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
        progressSummary:
          data.status === "rejected"
            ? "用户未批准这项请求，Cowork 正在调整方案。"
            : state.progressSummary ?? "",
      };
    }
    case "approval.waived": {
      const data = envelope.data as ApprovalWaivedPayload;
      return { ...next, waivedApprovals: [...state.waivedApprovals, data] };
    }
    case "run.sleeping": {
      const data = envelope.data as RunSleepingPayload;
      return { ...next, phase: "sleeping", sleepingUntil: data.wake_at };
    }
    case "reading.goto": {
      const data = envelope.data as ReadingGotoPayload;
      return {
        ...next,
        phase: "executing",
        readerJump: { ...data, seq: (state.readerJump?.seq ?? 0) + 1 },
      };
    }
    case "memory.saved": {
      const data = envelope.data as MemorySavedPayload;
      const seen = state.memoryWrites.find((item) => item.memory.id === data.memory.id);
      if (seen === undefined) return { ...next, memoryWrites: [...state.memoryWrites, data] };
      // 同一条被改了第二次：保留最早那次的 previous_content，撤销才回得到原点。
      const merged = { ...data, previous_content: seen.previous_content };
      return {
        ...next,
        memoryWrites: state.memoryWrites.map((item) =>
          item.memory.id === data.memory.id ? merged : item,
        ),
      };
    }
    case "todo.update": {
      const data = envelope.data as TodoUpdatePayload;
      // 整份替换：后端每次都发完整清单，前端不做合并。
      return { ...next, phase: "executing", todos: data.todos ?? [] };
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
      return { ...next, answer: (state.answer ?? "") + data.text };
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
        return { ...next, phase: "cancelled", answer: data.user_message, error: null };
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
