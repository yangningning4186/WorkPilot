"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { WorkdeskAppShell } from "@/components/workdesk-shell";
import {
  ApiError,
  createCoworkSchedule,
  deleteCoworkSchedule,
  fetchCoworkSchedules,
  fetchInboxBindings,
  fetchUnattendedInbox,
  fetchUnrouted,
  respondToCoworkInteraction,
  runCoworkSchedule,
  updateCoworkSchedule,
  upsertInboxBinding,
  deleteInboxBinding,
  type CoworkSchedule,
  type InboxBinding,
  type UnattendedInboxItem,
  type UnroutedEntry,
} from "@/lib/api";
import { pickCoworkDirectory } from "@/lib/desktop";

const CRON_PRESETS = [
  { label: "每天 09:00", value: "0 9 * * *" },
  { label: "工作日 09:00", value: "0 9 * * 1-5" },
  { label: "每周一 09:00", value: "0 9 * * 1" },
  { label: "每月 1 日 09:00", value: "0 9 1 * *" },
];
const DEFAULT_CRON = "0 9 * * 1-5";

function readableError(reason: unknown): string {
  if (reason instanceof ApiError) {
    try {
      const parsed = JSON.parse(reason.message) as { detail?: string };
      return parsed.detail ?? reason.message;
    } catch {
      return reason.message;
    }
  }
  return reason instanceof Error ? reason.message : "操作未完成";
}

function formatDate(value: string | null): string {
  if (value === null) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function kindLabel(kind: UnattendedInboxItem["kind"]): string {
  if (kind === "ask_user") return "需要补充信息";
  if (kind === "directory_request") return "申请工作目录";
  if (kind === "capability_request") return "申请运行能力";
  if (kind === "external_approval") return "审批外部动作";
  return "审批 Shell 命令";
}

function requestText(item: UnattendedInboxItem): string {
  const candidate =
    item.request.question ?? item.request.reason ?? item.request.warning ?? item.request.command ?? item.run_goal;
  return typeof candidate === "string" ? candidate : item.run_goal;
}

export default function AutomationsPage() {
  const [schedules, setSchedules] = useState<CoworkSchedule[]>([]);
  const [inbox, setInbox] = useState<UnattendedInboxItem[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [bindings, setBindings] = useState<InboxBinding[]>([]);
  const [unrouted, setUnrouted] = useState<UnroutedEntry[]>([]);
  const [chatId, setChatId] = useState("");
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  const [scheduleKind, setScheduleKind] = useState<"cron" | "once">("cron");
  const [cronExpression, setCronExpression] = useState(DEFAULT_CRON);
  const [runAt, setRunAt] = useState("");
  const timezone = useMemo(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    [],
  );
  const minimumRunAt = useMemo(() => {
    const now = new Date();
    return new Date(now.getTime() - now.getTimezoneOffset() * 60_000)
      .toISOString()
      .slice(0, 16);
  }, []);

  const reload = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [scheduleData, inboxData, bindingData, unroutedData] = await Promise.all([
        fetchCoworkSchedules(),
        fetchUnattendedInbox(),
        fetchInboxBindings().catch(() => ({ items: [] })),
        fetchUnrouted(20).catch(() => ({ items: [] })),
      ]);
      setSchedules(scheduleData.items);
      setInbox(inboxData.items);
      setBindings(bindingData.items);
      setUnrouted(unroutedData.items);
      setError(null);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void reload(), 0);
    const timer = window.setInterval(() => void reload(true), 15_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [reload]);

  const bindDefaultInbox = useCallback(async () => {
    const normalized = chatId.trim();
    if (normalized === "") return;
    setBusy("bind");
    try {
      await upsertInboxBinding("default", { platform: "feishu", chat_id: normalized });
      setChatId("");
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }, [chatId, reload]);

  const unbindInbox = useCallback(async (name: string) => {
    setBusy("bind");
    try {
      await deleteInboxBinding(name);
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }, [reload]);

  async function submitSchedule() {
    if (!title.trim() || !goal.trim()) return;
    if (scheduleKind === "once" && !runAt) return;
    if (scheduleKind === "cron" && !cronExpression.trim()) return;
    setBusy("create");
    try {
      await createCoworkSchedule({
        title: title.trim(),
        goal: goal.trim(),
        workspace_path: workspacePath ?? undefined,
        schedule_kind: scheduleKind,
        cron_expression: scheduleKind === "cron" ? cronExpression.trim() : undefined,
        run_at: scheduleKind === "once" ? new Date(runAt).toISOString() : undefined,
        timezone,
      });
      setTitle("");
      setGoal("");
      setWorkspacePath(null);
      setShowForm(false);
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  async function chooseWorkspace() {
    const selected = await pickCoworkDirectory();
    if (selected !== null) setWorkspacePath(selected);
  }

  async function mutateSchedule(id: string, action: "toggle" | "run" | "delete") {
    const schedule = schedules.find((item) => item.id === id);
    if (!schedule) return;
    if (action === "delete" && !window.confirm(`删除自动化“${schedule.title}”？`)) return;
    setBusy(`${action}:${id}`);
    try {
      if (action === "toggle") {
        await updateCoworkSchedule(id, { enabled: !schedule.enabled });
      } else if (action === "run") {
        await runCoworkSchedule(id);
      } else {
        await deleteCoworkSchedule(id);
      }
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  async function resolveInbox(item: UnattendedInboxItem, approved: boolean) {
    setBusy(`inbox:${item.id}`);
    try {
      if (item.kind === "ask_user") {
        if (approved) {
          const answer = answers[item.id]?.trim();
          if (!answer) return;
          await respondToCoworkInteraction(item.run_id, item.resume_token, {
            approved: true,
            answer,
          });
        } else {
          await respondToCoworkInteraction(item.run_id, item.resume_token, { approved: false });
        }
      } else if (item.kind === "directory_request" && approved) {
        const path = await pickCoworkDirectory();
        if (path === null) return;
        await respondToCoworkInteraction(item.run_id, item.resume_token, {
          approved: true,
          path,
        });
      } else {
        await respondToCoworkInteraction(item.run_id, item.resume_token, { approved });
      }
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
  }

  const pendingCount = inbox.filter((item) => item.status === "pending").length;

  return (
    <WorkdeskAppShell icon="automation" sectionTitle="自动化与收件箱">
      <div className="automation-page workdesk-route-surface">
      <section className="automation-hero">
        <div>
          <span className="automation-kicker">SCHEDULED COWORK</span>
          <h1>让任务准时开始，<br />让决定留给你。</h1>
          <p>自动化在本机运行。遇到提问、目录申请或高风险动作时会暂停，统一进入收件箱。</p>
        </div>
        <div className="automation-hero-actions">
          <div><strong>{schedules.filter((item) => item.enabled).length}</strong><span>运行中的计划</span></div>
          <div className={pendingCount > 0 ? "attention" : ""}><strong>{pendingCount}</strong><span>等待你处理</span></div>
          <button onClick={() => setShowForm((value) => !value)} type="button">
            {showForm ? "收起" : "＋ 新建自动化"}
          </button>
        </div>
      </section>

      {showForm && (
        <section className="automation-create-card">
          <header><div><span>NEW AUTOMATION</span><h2>安排一项无人值守任务</h2></div><small>不会自动批准新的权限或 Shell 命令</small></header>
          <div className="automation-form-grid">
            <label><span>名称</span><input maxLength={200} onChange={(event) => setTitle(event.target.value)} placeholder="例如：工作日晨报" value={title} /></label>
            <div className="automation-workspace-field"><span>工作区 <small>可选</small></span><div><button onClick={() => void chooseWorkspace()} title={workspacePath ?? "未指定时使用 ~/Documents/WorkPilot"} type="button">{workspacePath === null ? "WorkPilot 默认文件夹" : workspacePath.split(/[\\/]/).filter(Boolean).at(-1) ?? workspacePath}</button>{workspacePath !== null && <button aria-label="恢复默认工作区" className="clear" onClick={() => setWorkspacePath(null)} type="button">×</button>}</div><small>{workspacePath === null ? "~/Documents/WorkPilot；也可以为此自动化单独指定文件夹" : workspacePath}</small></div>
            <label className="wide"><span>任务说明</span><textarea maxLength={4000} onChange={(event) => setGoal(event.target.value)} placeholder="写清楚期望结果、输入范围和产物位置…" value={goal} /></label>
            <div className="automation-schedule-editor wide">
              <div className="automation-kind-tabs"><button className={scheduleKind === "cron" ? "active" : ""} onClick={() => setScheduleKind("cron")} type="button">周期执行</button><button className={scheduleKind === "once" ? "active" : ""} onClick={() => setScheduleKind("once")} type="button">单次执行</button></div>
              {scheduleKind === "cron" ? <><div className="automation-presets">{CRON_PRESETS.map((preset) => <button className={cronExpression === preset.value ? "active" : ""} key={preset.value} onClick={() => setCronExpression(preset.value)} type="button">{preset.label}</button>)}</div><label><span>五段 cron · {timezone}</span><input onChange={(event) => setCronExpression(event.target.value)} value={cronExpression} /></label></> : <label><span>执行时间 · {timezone}</span><input min={minimumRunAt} onChange={(event) => setRunAt(event.target.value)} type="datetime-local" value={runAt} /></label>}
            </div>
          </div>
          <footer><span>未指定时使用默认工作区；Shell 和外部动作仍不会自动批准。</span><button disabled={busy === "create" || !title.trim() || !goal.trim() || (scheduleKind === "once" && !runAt) || (scheduleKind === "cron" && !cronExpression.trim())} onClick={() => void submitSchedule()} type="button">{busy === "create" ? "正在创建…" : "创建计划"}</button></footer>
        </section>
      )}

      {error && <div className="automation-error"><span>!</span><p>{error}</p><button onClick={() => setError(null)} type="button">关闭</button></div>}

      <section className="automation-grid">
        <div className="automation-column">
          <header className="automation-section-title"><div><span>计划</span><h2>自动化任务</h2></div><small>{schedules.length} 项</small></header>
          {loading ? <div className="automation-empty">正在读取本机计划…</div> : schedules.length === 0 ? <div className="automation-empty"><strong>还没有自动化</strong><p>自动化可以使用 WorkPilot 默认工作区，也可以单独指定一个本地文件夹。</p><button onClick={() => setShowForm(true)} type="button">创建计划</button></div> : <div className="automation-list">{schedules.map((schedule) => <article className={`automation-task-card ${schedule.enabled ? "" : "paused"}`} key={schedule.id}><div className="automation-task-head"><span className="automation-clock">{schedule.schedule_kind === "cron" ? "↻" : "1×"}</span><div><h3>{schedule.title}</h3><p>{schedule.goal}</p></div><i className={schedule.enabled ? "online" : ""}>{schedule.enabled ? "运行中" : "已暂停"}</i></div><dl><div><dt>下次运行</dt><dd>{schedule.enabled ? formatDate(schedule.next_run_at) : "已暂停"}</dd></div><div><dt>工作区</dt><dd title={schedule.workspace_path ?? "默认工作区"}>{schedule.workspace_label ?? "默认工作区"}</dd></div><div><dt>上一轮</dt><dd>{schedule.last_run_status ?? "尚未运行"}</dd></div></dl>{schedule.pending_inbox_count > 0 && <div className="automation-task-alert">有 {schedule.pending_inbox_count} 项需要处理</div>}<footer><button disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "toggle")} type="button">{schedule.enabled ? "暂停" : "启用"}</button><button disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "run")} type="button">立即运行</button><button className="danger" disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "delete")} type="button">删除</button></footer></article>)}</div>}
        </div>

        <aside className="automation-inbox-column">
          <header className="automation-section-title"><div><span>UNATTENDED INBOX</span><h2>等待你的决定</h2></div><small className={pendingCount > 0 ? "badge" : ""}>{pendingCount}</small></header>
          {inbox.length === 0 ? <div className="automation-inbox-empty"><span>✓</span><strong>收件箱已清空</strong><p>无人值守任务需要你时，会安全暂停并出现在这里。</p></div> : <div className="automation-inbox-list">{inbox.map((item) => <article className="automation-inbox-item" key={item.id}><header><span>{kindLabel(item.kind)}</span><time>{formatDate(item.created_at)}</time></header><h3>{item.schedule_title ?? "无人值守任务"}</h3><p>{requestText(item)}</p>{item.kind === "shell_approval" && typeof item.request.command === "string" && <code>{item.request.command}</code>}{item.kind === "external_approval" && <code>{JSON.stringify(item.request.arguments ?? {})}</code>}{item.kind === "ask_user" && <textarea onChange={(event) => setAnswers((value) => ({ ...value, [item.id]: event.target.value }))} placeholder="输入答复后继续运行…" value={answers[item.id] ?? ""} />}<footer><button disabled={busy !== null} onClick={() => void resolveInbox(item, false)} type="button">{item.kind === "ask_user" ? "跳过，由 Cowork 判断" : "拒绝"}</button><button className={item.kind === "shell_approval" || item.kind === "external_approval" ? "approve danger" : "approve"} disabled={busy !== null || (item.kind === "ask_user" && !(answers[item.id] ?? "").trim())} onClick={() => void resolveInbox(item, true)} type="button">{item.kind === "ask_user" ? "回复并继续" : item.kind === "directory_request" ? "选择目录并允许" : "允许一次"}</button></footer></article>)}</div>}
          <section className="automation-messaging">
            <h3>把审批送到飞书</h3>
            <p>绑定之后，这些请求会同时以卡片形式发到指定群，点按钮就能批准或拒绝。应用内收件箱仍然是准的那一份——发不出去只是没镜像出去，请求不会丢。</p>
            {bindings.filter((item) => item.chat_id !== null).map((binding) => (
              <div className="automation-messaging-row" key={binding.id}>
                <div><strong>{binding.name}</strong><small>{binding.platform} · {binding.chat_id}</small></div>
                <button disabled={busy !== null} onClick={() => void unbindInbox(binding.name)} type="button">解绑</button>
              </div>
            ))}
            <div className="automation-messaging-form">
              <input onChange={(event) => setChatId(event.target.value)} placeholder="飞书群 chat_id，例如 oc_xxx" value={chatId} />
              <button disabled={busy !== null || chatId.trim() === ""} onClick={() => void bindDefaultInbox()} type="button">绑定</button>
            </div>
            <small>开放式提问不会做成按钮：那种要打字的答复请回应用里给。</small>
          </section>

          {unrouted.length > 0 && (
            <section className="automation-messaging">
              <h3>没能投递的消息</h3>
              <p>这些入站消息没有找到该由谁处理。留在这里是为了让「我说了一句什么都没发生」能查得到原因；它们不会被重投。</p>
              {unrouted.slice(0, 6).map((entry) => (
                <div className="automation-messaging-row" key={entry.id}>
                  <div><strong>{entry.summary}</strong><small>{entry.chat_id ?? entry.kind} · {formatDate(entry.created_at)}</small></div>
                </div>
              ))}
            </section>
          )}

          <Link className="automation-open-cowork" href="/cowork">打开 Cowork 运行记录 →</Link>
        </aside>
      </section>
      </div>
    </WorkdeskAppShell>
  );
}
