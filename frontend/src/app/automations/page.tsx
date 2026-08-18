"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { WorkdeskAppShell } from "@/components/workdesk-shell";
import {
  ApiError,
  createCoworkSchedule,
  deleteCoworkSchedule,
  fetchConversations,
  fetchCoworkSchedules,
  fetchUnattendedInbox,
  respondToCoworkInteraction,
  runCoworkSchedule,
  updateCoworkSchedule,
  type ConversationSummary,
  type CoworkSchedule,
  type UnattendedInboxItem,
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
  return "审批 Shell 命令";
}

function requestText(item: UnattendedInboxItem): string {
  const candidate =
    item.request.question ?? item.request.reason ?? item.request.command ?? item.run_goal;
  return typeof candidate === "string" ? candidate : item.run_goal;
}

export default function AutomationsPage() {
  const [schedules, setSchedules] = useState<CoworkSchedule[]>([]);
  const [inbox, setInbox] = useState<UnattendedInboxItem[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [conversationId, setConversationId] = useState("");
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
      const [scheduleData, inboxData, conversationData] = await Promise.all([
        fetchCoworkSchedules(),
        fetchUnattendedInbox(),
        fetchConversations(),
      ]);
      setSchedules(scheduleData.items);
      setInbox(inboxData.items);
      setConversations(conversationData.items);
      setConversationId((current) => current || conversationData.items[0]?.id || "");
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

  async function submitSchedule() {
    if (!title.trim() || !goal.trim() || !conversationId) return;
    if (scheduleKind === "once" && !runAt) return;
    if (scheduleKind === "cron" && !cronExpression.trim()) return;
    setBusy("create");
    try {
      await createCoworkSchedule({
        conversation_id: conversationId,
        title: title.trim(),
        goal: goal.trim(),
        schedule_kind: scheduleKind,
        cron_expression: scheduleKind === "cron" ? cronExpression.trim() : undefined,
        run_at: scheduleKind === "once" ? new Date(runAt).toISOString() : undefined,
        timezone,
      });
      setTitle("");
      setGoal("");
      setShowForm(false);
      await reload(true);
    } catch (reason) {
      setError(readableError(reason));
    } finally {
      setBusy(null);
    }
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
        const answer = answers[item.id]?.trim();
        if (!answer) return;
        await respondToCoworkInteraction(item.run_id, item.resume_token, { answer });
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
            <label><span>工作会话</span><select onChange={(event) => setConversationId(event.target.value)} value={conversationId}><option value="">选择已连接工作区的会话</option>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title ?? "Cowork 会话"}</option>)}</select></label>
            <label className="wide"><span>任务说明</span><textarea maxLength={4000} onChange={(event) => setGoal(event.target.value)} placeholder="写清楚期望结果、输入范围和产物位置…" value={goal} /></label>
            <div className="automation-schedule-editor wide">
              <div className="automation-kind-tabs"><button className={scheduleKind === "cron" ? "active" : ""} onClick={() => setScheduleKind("cron")} type="button">周期执行</button><button className={scheduleKind === "once" ? "active" : ""} onClick={() => setScheduleKind("once")} type="button">单次执行</button></div>
              {scheduleKind === "cron" ? <><div className="automation-presets">{CRON_PRESETS.map((preset) => <button className={cronExpression === preset.value ? "active" : ""} key={preset.value} onClick={() => setCronExpression(preset.value)} type="button">{preset.label}</button>)}</div><label><span>五段 cron · {timezone}</span><input onChange={(event) => setCronExpression(event.target.value)} value={cronExpression} /></label></> : <label><span>执行时间 · {timezone}</span><input min={minimumRunAt} onChange={(event) => setRunAt(event.target.value)} type="datetime-local" value={runAt} /></label>}
            </div>
          </div>
          <footer><span>计划使用所选会话现有的目录和 capability；过期授权不会被续期。</span><button disabled={busy === "create" || !title.trim() || !goal.trim() || !conversationId || (scheduleKind === "once" && !runAt) || (scheduleKind === "cron" && !cronExpression.trim())} onClick={() => void submitSchedule()} type="button">{busy === "create" ? "正在创建…" : "创建计划"}</button></footer>
        </section>
      )}

      {error && <div className="automation-error"><span>!</span><p>{error}</p><button onClick={() => setError(null)} type="button">关闭</button></div>}

      <section className="automation-grid">
        <div className="automation-column">
          <header className="automation-section-title"><div><span>计划</span><h2>自动化任务</h2></div><small>{schedules.length} 项</small></header>
          {loading ? <div className="automation-empty">正在读取本机计划…</div> : schedules.length === 0 ? <div className="automation-empty"><strong>还没有自动化</strong><p>先在 Cowork 连接工作目录，再安排第一次运行。</p><button onClick={() => setShowForm(true)} type="button">创建计划</button></div> : <div className="automation-list">{schedules.map((schedule) => <article className={`automation-task-card ${schedule.enabled ? "" : "paused"}`} key={schedule.id}><div className="automation-task-head"><span className="automation-clock">{schedule.schedule_kind === "cron" ? "↻" : "1×"}</span><div><h3>{schedule.title}</h3><p>{schedule.goal}</p></div><i className={schedule.enabled ? "online" : ""}>{schedule.enabled ? "运行中" : "已暂停"}</i></div><dl><div><dt>下次运行</dt><dd>{schedule.enabled ? formatDate(schedule.next_run_at) : "已暂停"}</dd></div><div><dt>运行 / 跳过</dt><dd>{schedule.run_count} / {schedule.skipped_count}</dd></div><div><dt>上一轮</dt><dd>{schedule.last_run_status ?? "尚未运行"}</dd></div></dl>{schedule.pending_inbox_count > 0 && <div className="automation-task-alert">有 {schedule.pending_inbox_count} 项需要处理</div>}<footer><button disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "toggle")} type="button">{schedule.enabled ? "暂停" : "启用"}</button><button disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "run")} type="button">立即运行</button><button className="danger" disabled={busy !== null} onClick={() => void mutateSchedule(schedule.id, "delete")} type="button">删除</button></footer></article>)}</div>}
        </div>

        <aside className="automation-inbox-column">
          <header className="automation-section-title"><div><span>UNATTENDED INBOX</span><h2>等待你的决定</h2></div><small className={pendingCount > 0 ? "badge" : ""}>{pendingCount}</small></header>
          {inbox.length === 0 ? <div className="automation-inbox-empty"><span>✓</span><strong>收件箱已清空</strong><p>无人值守任务需要你时，会安全暂停并出现在这里。</p></div> : <div className="automation-inbox-list">{inbox.map((item) => <article className="automation-inbox-item" key={item.id}><header><span>{kindLabel(item.kind)}</span><time>{formatDate(item.created_at)}</time></header><h3>{item.schedule_title ?? "无人值守任务"}</h3><p>{requestText(item)}</p>{item.kind === "shell_approval" && typeof item.request.command === "string" && <code>{item.request.command}</code>}{item.kind === "ask_user" && <textarea onChange={(event) => setAnswers((value) => ({ ...value, [item.id]: event.target.value }))} placeholder="输入答复后继续运行…" value={answers[item.id] ?? ""} />}<footer>{item.kind !== "ask_user" && <button disabled={busy !== null} onClick={() => void resolveInbox(item, false)} type="button">拒绝</button>}<button className={item.kind === "shell_approval" ? "approve danger" : "approve"} disabled={busy !== null || (item.kind === "ask_user" && !(answers[item.id] ?? "").trim())} onClick={() => void resolveInbox(item, true)} type="button">{item.kind === "ask_user" ? "回复并继续" : item.kind === "directory_request" ? "选择目录并允许" : "允许一次"}</button></footer></article>)}</div>}
          <Link className="automation-open-cowork" href="/cowork">打开 Cowork 运行记录 →</Link>
        </aside>
      </section>
      </div>
    </WorkdeskAppShell>
  );
}
