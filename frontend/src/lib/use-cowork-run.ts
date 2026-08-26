"use client";

import { useEffect, useRef, useState } from "react";

import { fetchRunEventLog, fetchRunEventStream } from "./api";
import {
  type ApprovalWaivedPayload,
  type ArtifactPayload,
  type CoworkToolCatalogEntry,
  type ConversationTitlePayload,
  type ErrorPayload,
  type InteractionResolvedPayload,
  type InterruptPayload,
  type MemorySavedPayload,
  type MessageDeltaPayload,
  type MessageReasoningPayload,
  type MessageSnapshotPayload,
  type PlanPayload,
  type RunDonePayload,
  type RunSleepingPayload,
  type StepUpdatePayload,
  type StreamEnvelope,
  type SubagentProgressPayload,
  type BoardTaskPayload,
  type TeamCreatedPayload,
  type TeamSummaryPayload,
  type TeamWorkerStartedPayload,
  type ToolActivityPayload,
  type ReadingAnnotatedPayload,
  type ReadingGotoPayload,
  type TodoItem,
  type TodoUpdatePayload,
  type ToolEventPayload,
  envelopeSeq,
  parseEnvelope,
} from "./run-protocol";
import { parseSseFrame, takeSseFrame, waitForStreamRetry } from "./run-sse";

const SSE_RETRY_MS = 1_000;
const POLL_FALLBACK_MS = 650;
const LEADING_THINK_OPEN = /^\s*<think(?:ing)?\b[^>]*>/i;
const LEADING_THINK_CLOSE = /<\/think(?:ing)?>/i;
const ORPHAN_THINK_PREAMBLE_LIMIT = 8_192;

function extractedLeadingReasoning(text: string): { reasoning: string; visible: string } | null {
  const opening = LEADING_THINK_OPEN.exec(text);
  if (opening !== null) {
    const remainder = text.slice(opening[0].length);
    const closing = LEADING_THINK_CLOSE.exec(remainder);
    if (closing === null) return null;
    return {
      reasoning: remainder.slice(0, closing.index),
      visible: remainder.slice(closing.index + closing[0].length).trimStart(),
    };
  }
  const closing = LEADING_THINK_CLOSE.exec(text);
  if (closing === null || closing.index > ORPHAN_THINK_PREAMBLE_LIMIT) return null;
  const prefix = text.slice(0, closing.index);
  // 正文里的 Markdown 代码示例可能有同名标签；与 Provider 的终态清洗保持同一边界。
  if (prefix.split("```").length % 2 === 0 || prefix.split("`").length % 2 === 0) return null;
  return {
    reasoning: prefix,
    visible: text.slice(closing.index + closing[0].length).trimStart(),
  };
}

function appendExtractedReasoning(current: string, fragment: string): string {
  const normalized = fragment.trim();
  if (normalized === "") return current;
  const base = current.trimEnd();
  if (base === normalized || base.endsWith(normalized)) return base;
  return base === "" ? normalized : `${base}\n\n${normalized}`;
}

export type CoworkRunPhase =
  | "idle"
  | "connecting"
  | "executing"
  | "waiting_human"
  | "sleeping"
  | "done"
  | "partial"
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
  activity: ToolActivityPayload | null;
}

export interface CoworkModelStage {
  id: string;
  reasoning: string;
  text: string;
}

export interface CoworkRunView {
  cursor: bigint;
  phase: CoworkRunPhase;
  /** 首条和终态事件时间，用于显示可回放的真实运行耗时。 */
  startedAt: string | null;
  finishedAt: string | null;
  tools: CoworkToolCatalogEntry[];
  steps: CoworkProgressStep[];
  /**
   * 由 message.delta 累积、由 message.reset 清空、由 message.snapshot 原子对齐的正文。
   *
   * reset 是必需的：Cowork 一轮可能先写一段话再调工具，下一轮再写一段。只累积不清空
   * 的话，显示的既不是最终回答，也不等于落盘的那条消息——刷新一次页面内容就变了。
   * worker 在终态会发一次完整 snapshot，所以重放到最后必然与消息落盘内容一致。
   */
  answer: string;
  /**
   * 模型本次 run 的思考增量。它不进 canonical 消息和下一轮模型上下文，但 run_events
   * 会保留显示侧记录；内部工具轮 reset 只清正文草稿，不能把刚显示的思考抹掉。
   */
  reasoning: string;
  /** 已结束的模型工具轮；message.reset 只切阶段，不再丢弃上一轮的说明与思考。 */
  modelStages: CoworkModelStage[];
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
  /** 首轮任务完成前由轻量标题模型生成，用于同步刷新侧栏。 */
  conversationTitle: ConversationTitlePayload | null;
  /**
   * 模型最近一次把阅读器带到哪里。只保留最后一次而不是累积成列表：面板同一时刻只能
   * 显示一个位置，攒一串历史只会让"现在该显示哪一个"变成一个需要额外规则的问题。
   * `seq` 让面板能区分"同一处又跳了一次"和"没有新跳转"——两次跳到同一页时对象内容
   * 完全相同，没有它 useEffect 不会重跑。
   */
  readerJump: (ReadingGotoPayload & { seq: number }) | null;
  /**
   * 模型最近一次留下的批注。面板拿它只做一件事：`seq` 变了就去重新拉一次批注列表。
   * 事件本身**不是**批注的真相——store 才是；把事件里的那条直接 push 进列表，刷新
   * 一次页面就会和后端对不上，而且用户在别处删掉的那条也不会消失。
   */
  readerAnnotation: (ReadingAnnotatedPayload & { seq: number }) | null;
  interrupt: InterruptPayload | null;
  /**
   * 每次 explore 委派的最新一条进度，按 tool_call_id 归并——只留最新一条而不是攒成
   * 流水：面板要回答的是"它现在在干什么"，以及结束后"这次调查花了多少"，中间每一步
   * 的历史在事件流里本来就查得到。
   */
  subagentRuns: SubagentProgressPayload[];
  /** Agent Team 的 roster 与 Board 真相；终态由 team.summary 原子覆盖。 */
  team: TeamSummaryPayload | null;
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
  startedAt: null,
  finishedAt: null,
  tools: [],
  steps: [],
  answer: "",
  reasoning: "",
  modelStages: [],
  progressSummary: "",
  artifactEvents: [],
  todos: [],
  memoryWrites: [],
  conversationTitle: null,
  readerJump: null,
  readerAnnotation: null,
  interrupt: null,
  subagentRuns: [],
  team: null,
  waivedApprovals: [],
  sleepingUntil: null,
  error: null,
};

export function createEmptyCoworkRunView(phase: CoworkRunPhase = "idle"): CoworkRunView {
  return {
    ...EMPTY,
    phase,
    tools: [],
    steps: [],
    artifactEvents: [],
    modelStages: [],
    todos: [],
    memoryWrites: [],
    subagentRuns: [],
    team: null,
    waivedApprovals: [],
  };
}

function appendModelStage(
  stages: CoworkModelStage[],
  value: CoworkModelStage,
): CoworkModelStage[] {
  const stage = {
    ...value,
    reasoning: value.reasoning.trim(),
    text: value.text.trim(),
  };
  if (stage.reasoning === "" && stage.text === "") return stages;
  const previous = stages.at(-1);
  if (previous?.reasoning === stage.reasoning && previous.text === stage.text) return stages;
  return [...stages, stage];
}

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
    // tool.start / tool.result 不该用“正在执行 / 执行完成”抹掉 pending 事件里更有用的
    // 目的说明。状态由 status 单独呈现，detail 只记录真正新增的信息。
    detail: detail ?? current?.detail ?? null,
    effectRef: "effect_ref" in data ? (data.effect_ref ?? null) : (current?.effectRef ?? null),
    activity: data.activity ?? current?.activity ?? null,
  };
  return current === undefined
    ? [...steps, next].sort((left, right) => left.idx - right.idx)
    : steps.map((item) => (item.id === data.step_id ? next : item));
}

function upsertBoardTask(
  team: TeamSummaryPayload | null,
  task: BoardTaskPayload,
): TeamSummaryPayload {
  const current = team ?? {
    team_id: "",
    completion_status: "partial",
    workers: [],
    tasks: [],
    counts: { open: 0, in_progress: 0, blocked: 0, review: 0, done: 0, cancelled: 0 },
  };
  const seen = current.tasks.some((item) => item.task_id === task.task_id);
  const tasks = seen
    ? current.tasks.map((item) => (item.task_id === task.task_id ? task : item))
    : [...current.tasks, task];
  return { ...current, tasks };
}

export function applyCoworkEvent(state: CoworkRunView, envelope: StreamEnvelope): CoworkRunView {
  const seq = envelopeSeq(envelope);
  if (seq <= state.cursor) return state;
  const eventTime = envelope.created_at ?? new Date().toISOString();
  const next = {
    ...state,
    cursor: seq,
    startedAt: state.startedAt ?? eventTime,
  };
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
        steps: upsertStep(state.steps, data, "running", null),
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
          data.reused ? "复用了已完成的幂等结果" : null,
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
    case "reading.annotated": {
      const data = envelope.data as ReadingAnnotatedPayload;
      // 刻意不动 readerJump：批注不移动视口。
      return {
        ...next,
        phase: "executing",
        readerAnnotation: { ...data, seq: (state.readerAnnotation?.seq ?? 0) + 1 },
      };
    }
    case "subagent.progress": {
      const data = envelope.data as SubagentProgressPayload;
      const seen = state.subagentRuns.some((item) => item.tool_call_id === data.tool_call_id);
      return {
        ...next,
        phase: "executing",
        subagentRuns: seen
          ? state.subagentRuns.map((item) =>
              item.tool_call_id === data.tool_call_id ? data : item,
            )
          : [...state.subagentRuns, data],
      };
    }
    case "team.created": {
      const data = envelope.data as TeamCreatedPayload;
      return {
        ...next,
        phase: "executing",
        team: {
          team_id: data.team_id,
          completion_status: "partial",
          workers: data.workers,
          tasks: state.team?.tasks ?? [],
          counts: state.team?.counts
            ?? { open: 0, in_progress: 0, blocked: 0, review: 0, done: 0, cancelled: 0 },
        },
      };
    }
    case "team.worker.started": {
      const data = envelope.data as TeamWorkerStartedPayload;
      const current = state.team?.tasks.find((item) => item.task_id === data.task_id);
      if (current === undefined) return next;
      return {
        ...next,
        phase: "executing",
        team: upsertBoardTask(state.team, {
          ...current,
          status: "in_progress",
          assignee: data.worker,
          attempt_count: data.attempt_count,
          retry_count: data.retry_count,
        }),
      };
    }
    case "board.task.created":
    case "board.task.review":
    case "board.task.failed":
    case "board.task.reviewed":
    case "board.task.resolved": {
      return {
        ...next,
        phase: "executing",
        team: upsertBoardTask(state.team, envelope.data as BoardTaskPayload),
      };
    }
    case "team.summary": {
      return { ...next, team: envelope.data as TeamSummaryPayload };
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
    case "conversation.title": {
      return { ...next, conversationTitle: envelope.data as ConversationTitlePayload };
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
      const answer = (state.answer ?? "") + data.text;
      // 兼容旧/不规范端点：它把 <think> 块甚至只有闭标签的思考混进 content。
      // 收到闭标签后原子地把前缀移到思考栏，终态前也不再把它当回答展示。
      const extracted = extractedLeadingReasoning(answer);
      if (extracted === null) return { ...next, answer };
      return {
        ...next,
        answer: extracted.visible,
        reasoning: appendExtractedReasoning(state.reasoning, extracted.reasoning),
      };
    }
    case "message.snapshot": {
      const data = envelope.data as MessageSnapshotPayload;
      const extracted = extractedLeadingReasoning(data.text);
      const answer = extracted?.visible ?? data.text;
      const currentReasoning = extracted === null
        ? state.reasoning
        : appendExtractedReasoning(state.reasoning, extracted.reasoning);
      const currentText = state.answer.trim();
      const finalText = answer.trim();
      // 最后一轮的流式正文通常只是 snapshot 的前缀（最后一批 delta 甚至可能停在半个词）。
      // 它不是一份独立阶段输出，重复保存只会制造一条看起来“被截断”的伪记录。
      const stageText = currentText !== ""
        && !finalText.startsWith(currentText)
        && !currentText.startsWith(finalText)
        ? currentText
        : "";
      return {
        ...next,
        answer,
        reasoning: "",
        modelStages: appendModelStage(state.modelStages, {
          id: `stage-${seq.toString()}`,
          reasoning: currentReasoning,
          text: stageText,
        }),
        progressSummary: "",
      };
    }
    case "message.reset": {
      // 新一轮开写前把刚结束的模型工具轮固化；reset 是阶段边界，不是删除指令。
      return {
        ...next,
        answer: "",
        reasoning: "",
        modelStages: appendModelStage(state.modelStages, {
          id: `stage-${seq.toString()}`,
          reasoning: state.reasoning,
          text: state.answer,
        }),
      };
    }
    case "message.reasoning": {
      const data = envelope.data as MessageReasoningPayload;
      return { ...next, reasoning: (state.reasoning ?? "") + data.text };
    }
    case "run.done": {
      const data = envelope.data as RunDonePayload;
      if (data.status === "cancelled") return { ...next, phase: "cancelled", finishedAt: eventTime };
      if (data.status === "budget_exceeded") return { ...next, phase: "budget_exceeded", finishedAt: eventTime };
      if (data.status === "failed") return { ...next, phase: "error", finishedAt: eventTime };
      if (data.status === "partial") return { ...next, phase: "partial", finishedAt: eventTime };
      return { ...next, phase: "done", finishedAt: eventTime };
    }
    case "error": {
      const data = envelope.data as ErrorPayload;
      const modelStages = appendModelStage(state.modelStages, {
        id: `stage-${seq.toString()}`,
        reasoning: state.reasoning,
        text: state.answer,
      });
      if (data.code === "cancelled") {
        return { ...next, phase: "cancelled", answer: data.user_message, reasoning: "", modelStages, error: null, finishedAt: eventTime };
      }
      return { ...next, phase: "error", reasoning: "", modelStages, error: data.user_message, finishedAt: eventTime };
    }
    default:
      return next;
  }
}

export function useCoworkRun(runId: string | null): CoworkRunView {
  const [state, setState] = useState<CoworkRunView>(
    () => createEmptyCoworkRunView(runId === null ? "idle" : "connecting"),
  );
  const [activeRunId, setActiveRunId] = useState<string | null>(runId);
  const requestedRunId = useRef(runId);
  requestedRunId.current = runId;

  // 切 run 的当帧就清掉上一条正文，避免 effect 晚一帧导致串对话。
  if (activeRunId !== runId) {
    setActiveRunId(runId);
    setState(createEmptyCoworkRunView(runId === null ? "idle" : "connecting"));
  }

  useEffect(() => {
    if (runId === null) return;
    const controller = new AbortController();
    let stopped = false;
    let cursor = 0n;
    let retryMs = SSE_RETRY_MS;
    let consecutiveFailures = 0;

    const applyBatch = (items: StreamEnvelope[]): boolean => {
      if (stopped || requestedRunId.current !== runId || items.length === 0) return false;
      const accepted = items.filter(
        (item) => item.run_id === runId && envelopeSeq(item) > cursor,
      );
      if (accepted.length === 0) return false;
      cursor = accepted.reduce(
        (maximum, item) => (envelopeSeq(item) > maximum ? envelopeSeq(item) : maximum),
        cursor,
      );
      // 一次 reader.read 里可能带多帧；一批只 setState 一次，避免 React
      // 在同一屏文字上反复解析 Markdown。
      setState((previous) => accepted.reduce(
        applyCoworkEvent,
        previous.phase === "error" ? { ...previous, phase: "connecting", error: null } : previous,
      ));
      // Cowork 的 message.done 只结束 assistant 消息；后面仍可能有标题、交付物
      // 等 run 级事件。只有 run.done/error 才能关闭订阅。
      return accepted.some((item) => item.type === "run.done" || item.type === "error");
    };

    const catchUpWithPolling = async (): Promise<boolean> => {
      const response = await fetchRunEventLog(runId, cursor);
      if (stopped) return true;
      return applyBatch(response.items);
    };

    const consume = async () => {
      while (!stopped) {
        try {
          const response = await fetchRunEventStream(runId, cursor, controller.signal);
          if (!response.ok) throw new Error(`SSE ${response.status}`);
          if (response.body === null) throw new Error("SSE body 为空");
          consecutiveFailures = 0;
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!stopped) {
            const { done, value } = await reader.read();
            buffer += decoder.decode(value, { stream: !done });
            const batch: StreamEnvelope[] = [];
            let next = takeSseFrame(buffer);
            while (next !== null) {
              const [rawFrame, remaining] = next;
              buffer = remaining;
              const frame = parseSseFrame(rawFrame);
              if (frame.retryMs !== null) retryMs = frame.retryMs;
              if (frame.data !== null) {
                const envelope = parseEnvelope(frame.data);
                if (envelope !== null) batch.push(envelope);
              }
              next = takeSseFrame(buffer);
            }
            if (applyBatch(batch)) {
              stopped = true;
              await reader.cancel();
              return;
            }
            if (done) break;
          }
        } catch {
          if (stopped || controller.signal.aborted) return;
        }

        // SSE 断开或不可用时用现有 event-log 补齐一次，再携带新游标
        // 重连长连接。固定 650ms 轮询不再是主路径。
        try {
          if (await catchUpWithPolling()) return;
          consecutiveFailures = 0;
        } catch (reason) {
          consecutiveFailures += 1;
          if (!stopped && consecutiveFailures >= 3) {
            setState((previous) => ({
              ...previous,
              phase: "error",
              error: reason instanceof Error ? reason.message : "无法读取 Cowork 进度",
            }));
          }
        }
        if (!stopped) {
          await waitForStreamRetry(
            Math.min(retryMs, POLL_FALLBACK_MS),
            controller.signal,
          );
        }
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
