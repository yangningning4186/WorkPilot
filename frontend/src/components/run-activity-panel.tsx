"use client";

import { useEffect, useState } from "react";

import type {
  BoardTaskPayload,
  SubagentProgressPayload,
  TeamSummaryPayload,
  TodoItem,
} from "@/lib/run-protocol";
import type { CoworkProgressStep, CoworkRunPhase } from "@/lib/use-cowork-run";

const TOOL_LABELS: Record<string, string> = {
  ask_user: "等待你的答复",
  request_directory: "申请工作目录",
  request_capability: "申请运行能力",
  propose_plan: "提交执行计划",
  todo_write: "更新任务清单",
  run_shell: "执行 Shell 命令",
  list_files: "列出文件",
  read_text_file: "读取文本",
  write_text_file: "写入文本",
  replace_in_file: "修改文件",
  search_files: "搜索文件",
  read_pdf: "读取 PDF",
  web_search: "搜索网页",
  fetch_url: "读取网页",
  browser_open: "打开浏览器",
  browser_snapshot: "读取页面结构",
  browser_click: "点击网页控件",
  browser_back: "浏览器返回",
  browser_type: "填写网页输入",
  browser_select: "选择网页选项",
  browser_upload: "上传网页文件",
  browser_download: "下载网页文件",
  browser_screenshot: "保存网页截图",
  browser_find: "查找页面内容",
  browser_close: "关闭浏览器",
  explore: "委派只读调查",
  create_artifact: "生成交付物",
  load_tools: "加载扩展工具",
  list_skills: "查看可用技能",
  load_skill: "加载格式 Skill",
  propose_team: "提交团队编制",
  board_create_task: "创建团队任务",
  board_list_tasks: "查看团队任务",
  board_assign_task: "分配团队任务",
  board_review_task: "验收团队任务",
  board_resolve_task: "收束团队任务",
};

const SUBAGENT_STOP_LABELS: Record<string, string> = {
  answered: "已交付证据",
  call_limit: "已结束本轮调查",
  round_limit: "已结束本轮调查",
  cancelled: "已按停止中止",
};

function describeSubagent(progress: SubagentProgressPayload): string {
  const spent = `${progress.round} 轮 · ${progress.calls_used} 次调用`;
  switch (progress.phase) {
    case "started":
      return "只读子 Agent 已启动，正在独立上下文里查找证据";
    case "round":
      return `第 ${progress.round}/${progress.max_rounds} 轮 · 准备调用 ${(progress.planned_tools ?? []).map((name) => TOOL_LABELS[name] ?? name).join("、")}`;
    case "tool":
      return `第 ${progress.round}/${progress.max_rounds} 轮 · ${progress.ok === false ? "调用失败" : "已查完"} ${TOOL_LABELS[progress.tool_name ?? ""] ?? progress.tool_name}`;
    default:
      return `${SUBAGENT_STOP_LABELS[progress.status ?? ""] ?? "调查已结束"} · ${spent}`;
  }
}

function phaseLabel(phase: CoworkRunPhase): string {
  switch (phase) {
    case "waiting_human":
      return "等待你的确认";
    case "sleeping":
      return "等待后继续";
    case "done":
      return "已完成";
    case "partial":
      return "部分完成";
    case "budget_exceeded":
      return "未完成";
    case "cancelled":
      return "已停止";
    case "error":
      return "执行失败";
    case "connecting":
      return "正在连接";
    default:
      return "正在执行";
  }
}

function boardTaskStatus(task: BoardTaskPayload): { label: string; tone: string } {
  if (task.status === "done" && task.completion_kind === "partial") {
    return { label: "部分完成", tone: "partial" };
  }
  const labels: Record<BoardTaskPayload["status"], { label: string; tone: string }> = {
    open: { label: task.attempt_count > 0 ? "待返工" : "待分配", tone: "open" },
    in_progress: { label: "执行中", tone: "running" },
    blocked: { label: "已阻塞", tone: "blocked" },
    review: { label: "待验收", tone: "review" },
    done: { label: "已完成", tone: "done" },
    cancelled: { label: "已取消", tone: "cancelled" },
  };
  return labels[task.status];
}

export function TeamBoardPanel({ team }: { team: TeamSummaryPayload | null }) {
  if (team === null || (team.workers.length === 0 && team.tasks.length === 0)) return null;
  const workerRoles = new Map(team.workers.map((worker) => [worker.name, worker.role]));
  const terminal = team.tasks.filter((task) =>
    task.status === "done" || task.status === "cancelled"
  ).length;
  return (
    <section className="workdesk-team-board" aria-label="Agent Team 状态">
      <header>
        <div>
          <span className="workdesk-team-signal" aria-hidden />
          <strong>Agent Team</strong>
        </div>
        <small>{terminal}/{team.tasks.length} 已收束</small>
      </header>
      {team.tasks.length === 0 ? (
        <p className="workdesk-team-empty">团队已创建，正在拆分任务。</p>
      ) : (
        <div className="workdesk-team-grid">
          {team.tasks.map((task) => {
            const status = boardTaskStatus(task);
            const worker = task.assignee ?? "待分配 Worker";
            return (
              <article className={`workdesk-team-task is-${status.tone}`} key={task.task_id}>
                <header>
                  <div>
                    <strong>{worker}</strong>
                    <span>{task.attempt_count > 0 ? `第 ${task.attempt_count} 次执行` : "尚未执行"}</span>
                  </div>
                  <em>{status.label}</em>
                </header>
                <h4>{task.title}</h4>
                {workerRoles.get(worker) !== undefined && (
                  <p className="workdesk-team-role">{workerRoles.get(worker)}</p>
                )}
                {task.retry_count > 0 && (
                  <p className="workdesk-team-retry">已返工 {task.retry_count} 次</p>
                )}
                {task.rejection_reason && (
                  <details className="workdesk-team-feedback">
                    <summary>最近拒绝原因</summary>
                    <p>{task.rejection_reason}</p>
                  </details>
                )}
                {task.last_error && <p className="workdesk-team-error">{task.last_error}</p>}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function stepTitle(step: CoworkProgressStep): string {
  return step.activity?.title || TOOL_LABELS[step.tool] || step.tool;
}

function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.round(milliseconds / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

function useElapsed(startedAt: string | null, finishedAt: string | null, running: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);

  if (startedAt === null) return null;
  const started = Date.parse(startedAt);
  const finished = finishedAt === null ? now : Date.parse(finishedAt);
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null;
  return formatElapsed(finished - started);
}

function StatusMark({ step }: { step: CoworkProgressStep }) {
  if (step.status === "running") return <i className="workdesk-activity-spinner" />;
  if (step.status === "done") return <span aria-hidden>✓</span>;
  if (step.status === "failed") return <span aria-hidden>!</span>;
  return <span className="pending" aria-hidden />;
}

function repetitionKey(step: CoworkProgressStep): string {
  return `${step.tool}\u0000${step.activity?.target ?? ""}`;
}

export function RunActivityPanel({
  phase,
  running,
  startedAt,
  finishedAt,
  steps,
  todos,
  subagentRuns,
  team,
  progressSummary,
}: {
  phase: CoworkRunPhase;
  running: boolean;
  startedAt: string | null;
  finishedAt: string | null;
  steps: CoworkProgressStep[];
  todos: TodoItem[];
  subagentRuns: SubagentProgressPayload[];
  team: TeamSummaryPayload | null;
  progressSummary: string;
}) {
  const [processOpen, setProcessOpen] = useState(true);
  const [traceOpen, setTraceOpen] = useState(true);
  const elapsed = useElapsed(startedAt, finishedAt, running);
  const doneTodos = todos.filter((todo) => todo.status === "done").length;
  const failedSteps = steps.filter((step) => step.status === "failed").length;
  const activeStep = [...steps]
    .reverse()
    .find((step) => step.status === "running" || step.status === "pending");
  const activeNarration =
    activeStep?.activity?.summary ||
    activeStep?.detail ||
    (running ? progressSummary : "");

  const repetitionTotals = new Map<string, number>();
  for (const step of steps) {
    const key = repetitionKey(step);
    repetitionTotals.set(key, (repetitionTotals.get(key) ?? 0) + 1);
  }
  const repetitionSeen = new Map<string, number>();

  return (
    <section
      className={`workdesk-run-process ${running ? "is-live" : `is-${phase}`}`}
      aria-label="任务进度"
    >
      <button
        aria-expanded={processOpen}
        className="workdesk-run-process-status"
        onClick={() => setProcessOpen((value) => !value)}
        type="button"
      >
        <span className="workdesk-run-process-state">
          {running && <i className="workdesk-activity-spinner" />}
          <strong>{phaseLabel(phase)}</strong>
          {elapsed !== null && <time>{elapsed}</time>}
        </span>
        <span className="workdesk-run-process-chevron" aria-hidden>⌄</span>
      </button>

      {processOpen && (
        <div className="workdesk-run-process-body">
          {activeNarration && (
            <p className="workdesk-run-process-narration" aria-live="polite">
              {activeNarration}
            </p>
          )}

          {todos.length > 0 && (
            <details className="workdesk-run-tasks">
              <summary>
                <span>任务清单</span>
                <small>{doneTodos}/{todos.length}</small>
              </summary>
              <ol>
                {todos.map((todo, index) => (
                  <li className={todo.status} key={`${index}-${todo.content}`}>
                    <span aria-hidden>{todo.status === "done" ? "✓" : todo.status === "in_progress" ? "→" : "·"}</span>
                    <p>{todo.content}</p>
                  </li>
                ))}
              </ol>
            </details>
          )}

          <TeamBoardPanel team={team} />

          <details className="workdesk-tool-trace" open={traceOpen}>
            <summary
              onClick={(event) => {
                event.preventDefault();
                setTraceOpen((value) => !value);
              }}
            >
              <span>执行过程</span>
              <small>
                {steps.length > 0 ? `${steps.length} 个操作` : "准备中"}
                {failedSteps > 0 ? ` · ${failedSteps} 次未成功` : ""}
              </small>
            </summary>

            {steps.length === 0 ? (
              <p className="workdesk-thinking"><i />正在理解任务并选择合适的工具…</p>
            ) : (
              <ol>
                {steps.map((step) => {
                  const key = repetitionKey(step);
                  const occurrence = (repetitionSeen.get(key) ?? 0) + 1;
                  repetitionSeen.set(key, occurrence);
                  const isRepeated = (repetitionTotals.get(key) ?? 0) > 1;
                  const subagent = subagentRuns.find((item) => item.step_id === step.id);
                  const summary = step.activity?.summary;
                  const extraDetail = step.detail && step.detail !== summary ? step.detail : null;
                  return (
                    <li className={step.status} key={step.id}>
                      <span className="workdesk-activity-step-mark"><StatusMark step={step} /></span>
                      <div className="workdesk-activity-step-body">
                        <header>
                          <strong>{stepTitle(step)}</strong>
                          {isRepeated && <small>第 {occurrence} 次</small>}
                          {step.status === "running" && <small className="running">进行中</small>}
                          {step.status === "failed" && <small className="failed">未成功</small>}
                        </header>
                        {summary && <p>{summary}</p>}
                        {step.activity?.target && (
                          <code className={step.activity.target_kind ?? "text"} title={step.activity.target}>
                            {step.activity.target}
                          </code>
                        )}
                        {extraDetail && (
                          <p className={step.status === "failed" ? "error" : "note"} title={extraDetail}>
                            {extraDetail}
                          </p>
                        )}
                        {subagent !== undefined && (
                          <p className="subagent"><i />{describeSubagent(subagent)}</p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </details>
        </div>
      )}
    </section>
  );
}
