"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { AdminSessionControl, useAdminSession } from "@/components/admin-session";
import { AnswerMarkdown } from "@/components/answer-markdown";
import { WorkdeskIcon, WorkdeskNavigation } from "@/components/workdesk-shell";
import {
  ApiError,
  addCoworkRoot,
  cancelRun,
  createConversation,
  createCoworkRun,
  fetchConversationMessages,
  fetchConversations,
  fetchCoworkArtifacts,
  fetchCoworkGrants,
  fetchCoworkRoots,
  revokeCoworkRoot,
  respondToCoworkInteraction,
  steerCoworkRun,
  type ConversationSummary,
  type ConversationMessage,
  type CoworkArtifact,
  type CoworkGrant,
  type CoworkRoot,
} from "@/lib/api";
import { isTauriRuntime, pickCoworkDirectory } from "@/lib/desktop";
import { useCoworkRun } from "@/lib/use-cowork-run";

const TOOL_LABELS: Record<string, string> = {
  ask_user: "等待你的答复",
  request_directory: "申请工作目录",
  request_capability: "申请运行能力",
  run_shell: "执行 Shell 命令",
  list_workspace_roots: "确认工作目录",
  list_files: "列出文件",
  read_text_file: "读取文本",
  write_text_file: "写入文本",
  search_files: "搜索文件",
  read_pdf: "读取 PDF",
  fetch_url: "读取网页",
  create_artifact: "生成交付物",
  list_office_files: "扫描 Word / Excel",
  inspect_office_file: "读取文档结构",
  edit_word: "编辑 Word",
  edit_excel: "编辑 Excel",
};

const CAPABILITY_LABELS: Record<string, string> = {
  "filesystem.read": "读取文件",
  "filesystem.write": "写入文件",
  "office.word.edit": "编辑 Word",
  "office.excel.edit": "编辑 Excel",
  "network.read": "读取公开网页",
  "shell.execute": "执行 Shell 命令",
  "external.action": "执行外部操作",
};

const OFFICE_PROMPTS = [
  { label: "文档处理", prompt: "整理工作空间里的 Word 文档，统一格式并提炼一页摘要。" },
  { label: "表格分析", prompt: "分析工作空间里的 Excel 表格，检查异常数据并补齐必要公式。" },
  { label: "数据可视化", prompt: "读取工作空间中的 Excel 数据，生成管理层可读的分析结论。" },
  { label: "批量整理", prompt: "扫描工作空间里的 Word 和 Excel，按内容归类并给出整理方案。" },
];

const RESEARCH_PROMPTS = [
  { label: "资料综述", prompt: "阅读工作空间里的文档，整理主题脉络、关键结论和待验证问题。" },
  { label: "深度研究", prompt: "对工作空间中的材料做交叉分析，输出一份有依据的研究摘要。" },
  { label: "观点提取", prompt: "提取所有文档中的核心观点、证据与分歧，并按主题归纳。" },
];

function readableError(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 401) return "需要 owner 身份。桌面版会在启动时自动建立。";
    if (reason.status === 422) return "这个目录或任务无法安全执行，请查看权限范围。";
    if (reason.status === 503) return "Cowork 依赖服务尚未就绪，请检查 sidecar、PostgreSQL 与 Redis。";
  }
  return reason instanceof Error ? reason.message : "操作未完成。";
}

function shortPath(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length <= 3 ? path : `…/${parts.slice(-3).join("/")}`;
}

function artifactNote(artifact: CoworkArtifact): string {
  const summary = artifact.meta.summary;
  if (typeof summary === "string" && summary.trim() !== "") return summary;
  const count = artifact.meta.change_count;
  return typeof count === "number" ? `已写入 ${count} 处修改` : "已登记到交付物索引";
}

export default function CoworkPage() {
  const { state: authState } = useAdminSession();
  const desktopReady = useSyncExternalStore(
    () => () => undefined,
    isTauriRuntime,
    () => false,
  );
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [roots, setRoots] = useState<CoworkRoot[]>([]);
  const [grants, setGrants] = useState<CoworkGrant[]>([]);
  const [artifacts, setArtifacts] = useState<CoworkArtifact[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [goal, setGoal] = useState("");
  const [activePrompt, setActivePrompt] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [responding, setResponding] = useState(false);
  const [interactionAnswer, setInteractionAnswer] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [workMode, setWorkMode] = useState<"office" | "research">("office");
  const run = useCoworkRun(runId);

  const loadSession = useCallback(async (id: string) => {
    const [rootResponse, grantResponse, artifactResponse, messageResponse] = await Promise.all([
      fetchCoworkRoots(id),
      fetchCoworkGrants(id),
      fetchCoworkArtifacts(id),
      fetchConversationMessages(id),
    ]);
    setRoots(rootResponse.items);
    setGrants(grantResponse.items);
    setArtifacts(artifactResponse.items);
    setMessages(messageResponse.items);
  }, []);

  useEffect(() => {
    if (authState !== "authenticated") return;
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetchConversations();
        if (cancelled) return;
        let items = response.items;
        const query = new URLSearchParams(window.location.search);
        const requestedId = query.get("conversation");
        let selected: ConversationSummary | undefined;
        if (query.get("new") === "1") {
          window.history.replaceState(null, "", "/cowork");
          selected = await createConversation(`Cowork ${items.length + 1}`);
          items = [selected, ...items];
        } else {
          selected = items.find((item) => item.id === requestedId) ?? items[0];
        }
        if (selected === undefined) {
          selected = await createConversation("Cowork 工作台");
          items = [selected];
        }
        if (cancelled) return;
        setConversations(items);
        setConversationId(selected.id);
      } catch (reason) {
        if (!cancelled) setNotice(readableError(reason));
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [authState]);

  useEffect(() => {
    if (conversationId === null) return;
    let cancelled = false;
    const load = async () => {
      try {
        await loadSession(conversationId);
      } catch (reason) {
        if (!cancelled) setNotice(readableError(reason));
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [conversationId, loadSession]);

  useEffect(() => {
    if (conversationId === null || (run.phase !== "done" && run.artifactEvents.length === 0)) {
      return;
    }
    fetchCoworkArtifacts(conversationId)
      .then((response) => setArtifacts(response.items))
      .catch(() => undefined);
  }, [conversationId, run.artifactEvents.length, run.phase]);

  useEffect(() => {
    if (
      conversationId === null ||
      (run.phase !== "done" && run.phase !== "cancelled" && run.phase !== "error")
    ) {
      return;
    }
    fetchConversationMessages(conversationId)
      .then((response) => setMessages(response.items))
      .catch(() => undefined);
  }, [conversationId, run.phase]);

  const capabilitiesByRoot = useMemo(() => {
    const values = new Map<string, string[]>();
    for (const grant of grants) {
      if (!grant.active || grant.session_root_id === null) continue;
      const current = values.get(grant.session_root_id) ?? [];
      current.push(CAPABILITY_LABELS[grant.capability] ?? grant.capability);
      values.set(grant.session_root_id, current);
    }
    return values;
  }, [grants]);

  const addRoot = useCallback(async () => {
    if (conversationId === null) return;
    setBusy(true);
    setNotice(null);
    try {
      const selected = await pickCoworkDirectory();
      if (selected === null) {
        if (!isTauriRuntime()) setNotice("目录授权只在 Tauri 桌面版可用。");
        return;
      }
      await addCoworkRoot(conversationId, { path: selected, access_mode: "read_write" });
      await loadSession(conversationId);
      setNotice("目录已授权读写。后续 Word / Excel 任务会直接执行。");
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversationId, loadSession]);

  const removeRoot = useCallback(
    async (rootId: string) => {
      if (conversationId === null) return;
      setBusy(true);
      try {
        await revokeCoworkRoot(conversationId, rootId);
        await loadSession(conversationId);
        setNotice("目录权限已收回，关联编辑能力同步失效。");
      } catch (reason) {
        setNotice(readableError(reason));
      } finally {
        setBusy(false);
      }
    },
    [conversationId, loadSession],
  );

  const createSession = useCallback(async () => {
    setBusy(true);
    try {
      const created = await createConversation(`Cowork ${conversations.length + 1}`);
      setConversations((current) => [created, ...current]);
      setConversationId(created.id);
      setRunId(null);
      setMessages([]);
      setActivePrompt(null);
      setNotice(null);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversations.length]);

  const execute = useCallback(async () => {
    if (conversationId === null || goal.trim() === "" || roots.length === 0) return;
    const prompt = goal.trim();
    setBusy(true);
    setNotice(null);
    try {
      const response = await createCoworkRun({ conversation_id: conversationId, goal: prompt });
      setStopping(false);
      setActivePrompt(prompt);
      setRunId(response.run_id);
      setGoal("");
      await loadSession(conversationId);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversationId, goal, loadSession, roots.length]);

  const stopCowork = useCallback(async () => {
    if (runId === null || stopping) return;
    setStopping(true);
    setNotice(null);
    try {
      const response = await cancelRun(runId);
      setNotice(
        response.status === "cancelled"
          ? "Cowork 任务已停止。"
          : "已发送停止请求，当前安全步骤结束后会停止。",
      );
    } catch (reason) {
      setStopping(false);
      setNotice(readableError(reason));
    }
  }, [runId, stopping]);

  const sendSteering = useCallback(async () => {
    if (runId === null || goal.trim() === "") return;
    setBusy(true);
    setNotice(null);
    try {
      await steerCoworkRun(runId, goal.trim());
      setGoal("");
      setNotice("新指令已排队，会在当前工具步骤结束后生效。");
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [goal, runId]);

  const respondToInteraction = useCallback(
    async (body: { approved?: boolean; answer?: string; path?: string }) => {
      if (runId === null || run.interrupt === null) return;
      setResponding(true);
      setNotice(null);
      try {
        await respondToCoworkInteraction(runId, run.interrupt.resume_token, body);
        setInteractionAnswer("");
        if (conversationId !== null) await loadSession(conversationId);
        setNotice(body.approved === false ? "已拒绝请求，Cowork 会调整方案。" : "已提交，Cowork 正在继续。");
      } catch (reason) {
        setNotice(readableError(reason));
      } finally {
        setResponding(false);
      }
    },
    [conversationId, loadSession, run.interrupt, runId],
  );

  const approveDirectoryRequest = useCallback(async () => {
    const selected = await pickCoworkDirectory();
    if (selected === null) {
      if (!isTauriRuntime()) setNotice("目录授权只在 Tauri 桌面版可用。");
      return;
    }
    await respondToInteraction({ approved: true, path: selected });
  }, [respondToInteraction]);

  const steering = run.phase === "connecting" || run.phase === "executing";
  const running = steering || run.phase === "waiting_human";
  const submitComposer = steering ? sendSteering : execute;
  const prompts = workMode === "office" ? OFFICE_PROMPTS : RESEARCH_PROMPTS;
  const activeConversation = conversations.find((item) => item.id === conversationId);
  const interactionPayload = run.interrupt?.payload ?? {};
  const interactionQuestion =
    typeof interactionPayload.question === "string" ? interactionPayload.question : "Cowork 需要你的答复";
  const interactionReason =
    typeof interactionPayload.reason === "string" ? interactionPayload.reason : "完成当前任务需要额外授权。";
  const interactionChoices = Array.isArray(interactionPayload.choices)
    ? interactionPayload.choices.filter((item): item is string => typeof item === "string")
    : [];
  const requestedCapability =
    typeof interactionPayload.capability === "string" ? interactionPayload.capability : "未知能力";
  const shellCommand =
    typeof interactionPayload.command === "string" ? interactionPayload.command : "";
  const shellCwd = typeof interactionPayload.cwd === "string" ? interactionPayload.cwd : "";
  const runAnswer = run.answer || run.progressSummary || "";
  const hasConversation = messages.length > 0 || runId !== null;
  const visibleMessages =
    runId === null ? messages : messages.filter((message) => message.run_id !== runId);
  const currentPrompt =
    activePrompt ??
    messages.find((message) => message.run_id === runId && message.role === "user")?.content ??
    null;
  const runStatusLabel =
    run.phase === "waiting_human"
      ? "需要你的答复"
      : running
        ? stopping
          ? "正在安全停止"
          : "正在处理"
        : run.phase === "cancelled"
          ? "任务已停止"
          : run.phase === "budget_exceeded"
            ? "预算已用尽 · 任务未完成"
          : run.phase === "error"
            ? "这一步没有完成"
            : "已完成";

  return (
    <main className="cowork-frame workdesk-shell">
      <aside className="workdesk-sidebar">
        <div className="workdesk-sidebar-head">
          <Link className="workdesk-brand" href="/cowork">
            <span><WorkdeskIcon name="spark" /></span>
            <div><strong>WorkPilot</strong><small>Local Cowork</small></div>
          </Link>
          <button aria-label="搜索任务" className="workdesk-icon-button" type="button"><WorkdeskIcon name="search" /></button>
        </div>

        <WorkdeskNavigation
          newTaskDisabled={busy || running}
          onNewTask={() => void createSession()}
        />

        <section className="workdesk-sidebar-group">
          <header><span>任务</span><small>{conversations.length}</small></header>
          <div className="workdesk-task-list">
            {conversations.slice(0, 7).map((item) => (
              <button
                className={item.id === conversationId ? "active" : ""}
                disabled={running}
                key={item.id}
                onClick={() => {
                  setConversationId(item.id);
                  setRunId(null);
                  setActivePrompt(null);
                }}
                type="button"
              >
                <span>{item.title ?? "Cowork 任务"}</span>
                <small>{item.id === conversationId ? "当前" : new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</small>
              </button>
            ))}
            {conversations.length === 0 && <p>连接后会在这里显示任务记录</p>}
          </div>
        </section>

        <section className="workdesk-sidebar-group workdesk-spaces">
          <header><span>工作空间</span><small>{roots.length}</small></header>
          <button className="workdesk-add-space" disabled={busy || conversationId === null || !desktopReady} onClick={() => void addRoot()} type="button">
            <WorkdeskIcon name="folder" /><span>{roots.length === 0 ? "选择本地文件夹" : "添加工作空间"}</span><b>＋</b>
          </button>
          {roots.map((root) => (
            <div className="workdesk-space" key={root.id}>
              <span><WorkdeskIcon name="folder" /></span>
              <div><strong>{root.label}</strong><small title={root.canonical_path}>{shortPath(root.canonical_path)}</small></div>
              <i aria-label="已连接" />
            </div>
          ))}
        </section>

        <footer className="workdesk-account">
          <span className="workdesk-avatar">W</span>
          <div><strong>本机工作台</strong><AdminSessionControl /></div>
          <i className={authState === "authenticated" ? "online" : ""} />
        </footer>
      </aside>

      <section className="workdesk-main">
        <header className="workdesk-topline">
          <div><span className={authState === "authenticated" ? "online" : ""} />{authState === "authenticated" ? "本地 Agent 已连接" : "正在连接本地 Agent"}</div>
          <button
            aria-label={roots.length === 0 ? "选择工作目录" : "添加工作目录"}
            disabled={busy || conversationId === null || !desktopReady}
            onClick={() => void addRoot()}
            title={roots[0]?.canonical_path ?? "选择本机文件夹作为工作目录"}
            type="button"
          >
            <WorkdeskIcon name="folder" />
            <span>{roots.length === 0 ? "选择工作目录" : `${roots[0]?.label}${roots.length > 1 ? ` +${roots.length - 1}` : ""}`}</span>
          </button>
          <p>{activeConversation?.title ?? "新任务"}</p>
        </header>

        {authState !== "authenticated" ? (
          <section className="workdesk-connect-state">
            <span><WorkdeskIcon name="spark" /></span>
            <h1>{authState === "unknown" ? "正在唤醒 WorkPilot" : "等待 owner 身份"}</h1>
            <p>桌面端会自动连接本机 sidecar，并用本次启动令牌建立私有工作会话。</p>
          </section>
        ) : (
          <div className={`workdesk-stage${hasConversation ? " is-chat" : ""}`}>
            {!hasConversation ? (
              <>
                <section className="workdesk-welcome">
                  <div className="workdesk-orbit"><WorkdeskIcon name="spark" /></div>
                  <h1>WorkPilot，我帮你</h1>
                  <div className="workdesk-mode-switch" role="tablist" aria-label="任务模式">
                    <button aria-selected={workMode === "office"} onClick={() => setWorkMode("office")} role="tab" type="button">日常办公</button>
                    <button aria-selected={workMode === "research"} onClick={() => setWorkMode("research")} role="tab" type="button">知识研究</button>
                  </div>
                </section>
                <div className="workdesk-prompt-chips" aria-label="快捷任务">
                  {prompts.map((item) => <button key={item.label} onClick={() => setGoal(item.prompt)} type="button"><WorkdeskIcon name="file" />{item.label}</button>)}
                </div>
              </>
            ) : (
              <section aria-label="Cowork 对话" className="workdesk-chat-thread">
                {visibleMessages.filter((message) => message.content.trim() !== "").map((message) => (
                  <article className={`workdesk-message ${message.role}`} key={message.id}>
                    {message.role === "assistant" && <span className="workdesk-agent-avatar"><WorkdeskIcon name="spark" /></span>}
                    <div className="workdesk-message-body">
                      {message.role === "assistant" && <small>WorkPilot</small>}
                      {message.role === "assistant" ? <AnswerMarkdown text={message.content} /> : <p>{message.content}</p>}
                    </div>
                  </article>
                ))}

                {runId !== null && (
                  <>
                    {currentPrompt !== null && (
                      <article className="workdesk-message user current">
                        <div className="workdesk-message-body"><p>{currentPrompt}</p></div>
                      </article>
                    )}
                    <article className="workdesk-message assistant current" aria-live="polite">
                      <span className="workdesk-agent-avatar"><WorkdeskIcon name="spark" /></span>
                      <div className="workdesk-message-body workdesk-run-message">
                        <header className="workdesk-run-head">
                          <div><small>WorkPilot</small><strong>{runStatusLabel}</strong></div>
                          <div className="workdesk-progress-actions">
                            <code>{runId.slice(0, 8)}</code>
                            {running && (
                              <button
                                aria-label={stopping ? "正在停止 Cowork 任务" : "停止 Cowork 任务"}
                                className="workdesk-stop"
                                disabled={stopping}
                                onClick={() => void stopCowork()}
                                type="button"
                              >
                                <WorkdeskIcon name="stop" />
                                <span>{stopping ? "停止中" : "停止"}</span>
                              </button>
                            )}
                          </div>
                        </header>

                        <details className="workdesk-tool-trace" open={running || run.steps.length > 0}>
                          {run.steps.length === 0 ? (
                            <p className="workdesk-thinking"><i />正在理解任务并建立执行计划…</p>
                          ) : (
                            <>
                              <summary><span>执行过程</span><small>{run.steps.length} 个步骤</small></summary>
                              <ol>
                                {run.steps.map((step) => (
                                  <li className={step.status} key={step.id}>
                                    <span>{step.status === "done" ? "✓" : step.status === "failed" ? "!" : step.idx + 1}</span>
                                    <div><strong>{TOOL_LABELS[step.tool] ?? step.tool}</strong><small>{step.detail ?? "已加入计划"}</small></div>
                                  </li>
                                ))}
                              </ol>
                            </>
                          )}
                        </details>

                        {run.interrupt !== null && run.interrupt.kind !== "write_confirm" && (
                          <section className="workdesk-inbox-card" aria-live="polite">
                            <div className="workdesk-inbox-eyebrow"><WorkdeskIcon name="shield" /><span>需要你的确认</span></div>
                            {run.interrupt.kind === "ask_user" ? (
                              <>
                                <h3>{interactionQuestion}</h3>
                                {interactionChoices.length > 0 && (
                                  <div className="workdesk-inbox-choices">
                                    {interactionChoices.map((choice) => (
                                      <button disabled={responding} key={choice} onClick={() => setInteractionAnswer(choice)} type="button">{choice}</button>
                                    ))}
                                  </div>
                                )}
                                <textarea aria-label="回复 Cowork" disabled={responding} maxLength={4000} onChange={(event) => setInteractionAnswer(event.target.value)} placeholder="直接在这里回复" rows={3} value={interactionAnswer} />
                                <div className="workdesk-inbox-actions"><button className="primary" disabled={responding || interactionAnswer.trim() === ""} onClick={() => void respondToInteraction({ answer: interactionAnswer.trim() })} type="button">回复并继续</button></div>
                              </>
                            ) : run.interrupt.kind === "directory_request" ? (
                              <>
                                <h3>允许我使用另一个目录？</h3>
                                <p>{interactionReason}</p>
                                <small>范围：{interactionPayload.access_mode === "read_write" ? "读取与写入" : "仅读取"}。目录必须由你在系统选择器中明确选取。</small>
                                <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">不允许</button><button className="primary" disabled={responding || !desktopReady} onClick={() => void approveDirectoryRequest()} type="button">选择目录并允许</button></div>
                              </>
                            ) : run.interrupt.kind === "shell_approval" ? (
                              <>
                                <h3>允许我运行这条 Shell 命令？</h3>
                                <p>{interactionReason}</p>
                                <pre className="workdesk-shell-command"><code>{shellCommand}</code></pre>
                                <small>工作目录：{shellCwd}</small>
                                {interactionPayload.has_operators === true && <small className="risk">命令包含 shell 操作符，不能进入 allowlist，本次必须单独批准。</small>}
                                <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">拒绝</button><button className="primary danger" disabled={responding} onClick={() => void respondToInteraction({ approved: true })} type="button">批准并运行一次</button></div>
                              </>
                            ) : (
                              <>
                                <h3>授予“{CAPABILITY_LABELS[requestedCapability] ?? requestedCapability}”能力？</h3>
                                <p>{interactionReason}</p>
                                <small>授权只绑定当前 Cowork 会话，之后可以随时收回。</small>
                                <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">不允许</button><button className="primary" disabled={responding} onClick={() => void respondToInteraction({ approved: true })} type="button">允许并继续</button></div>
                              </>
                            )}
                          </section>
                        )}

                        {(runAnswer !== "" || run.error !== null) && (
                          <div className={`workdesk-run-answer${run.phase === "budget_exceeded" ? " budget" : run.error !== null ? " error" : run.phase === "cancelled" ? " cancelled" : ""}`}>
                            {run.error !== null ? <p>{run.error}</p> : <AnswerMarkdown text={runAnswer} />}
                          </div>
                        )}

                        {artifacts.length > 0 && (
                          <section className="workdesk-chat-artifacts">
                            <header><strong>交付物</strong><span>{artifacts.length}</span></header>
                            {artifacts.slice(0, 4).map((artifact) => {
                              const excel = artifact.mime_type?.includes("spreadsheet") ?? artifact.title.endsWith(".xlsx");
                              const word = artifact.mime_type?.includes("wordprocessingml") ?? artifact.title.endsWith(".docx");
                              return <article key={artifact.id}><span className={excel ? "excel" : "word"}>{excel ? "X" : word ? "W" : "A"}</span><div><strong>{artifact.title}</strong><small>{artifactNote(artifact)}</small></div><time>{new Date(artifact.updated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></article>;
                            })}
                          </section>
                        )}
                      </div>
                    </article>
                  </>
                )}
              </section>
            )}

            <section className="workdesk-composer" aria-label="创建 Cowork 任务">
              <textarea
                aria-label={steering ? "向运行中的 Cowork 追加指令" : "你想让 Cowork 完成什么？"}
                disabled={run.phase === "waiting_human" || responding}
                id="cowork-goal"
                maxLength={4000}
                onChange={(event) => setGoal(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void submitComposer();
                }}
                placeholder={run.phase === "waiting_human" ? "请先回复上方的问题" : steering ? "补充要求或调整方向…" : hasConversation ? "继续这段对话，或交代一个新任务…" : "今天帮你做些什么？输入指令修改 Word、Excel，或整理工作空间里的资料"}
                rows={hasConversation ? 2 : 4}
                value={goal}
              />
              <div className="workdesk-composer-actions">
                <button aria-label="添加工作空间" disabled={busy || !desktopReady} onClick={() => void addRoot()} type="button"><WorkdeskIcon name="add" /></button>
                <span>{run.phase === "waiting_human" ? "请先处理对话中的请求" : steering ? "发送后将在安全边界转向" : roots.length === 0 ? "需要先选择工作空间" : "Agent 已就绪"}</span>
                <button className="workdesk-speed" type="button"><WorkdeskIcon name="spark" />标准</button>
                <button aria-label={steering ? "追加运行指令" : "开始执行任务"} className="workdesk-send" disabled={busy || run.phase === "waiting_human" || roots.length === 0 || goal.trim() === ""} onClick={() => void submitComposer()} type="button"><WorkdeskIcon name="send" /></button>
              </div>
              <footer>
                <button disabled={busy || !desktopReady} onClick={() => void addRoot()} type="button"><WorkdeskIcon name="folder" /><span>{roots.length === 0 ? "选择工作空间" : `${roots[0]?.label}${roots.length > 1 ? ` +${roots.length - 1}` : ""}`}</span><b>⌄</b></button>
                <details className="workdesk-permission-menu">
                  <summary><WorkdeskIcon name="shield" /><span>读写与 Office 编辑</span><b>⌄</b></summary>
                  <div>
                    <h3>本次会话权限</h3>
                    {roots.length === 0 ? <p>选择目录后，将申请读取、写入、Word 和 Excel 编辑能力。</p> : roots.map((root) => (
                      <article key={root.id}><div><strong>{root.label}</strong><small>{(capabilitiesByRoot.get(root.id) ?? []).join(" · ")}</small></div><button disabled={busy || running} onClick={() => void removeRoot(root.id)} type="button">收回</button></article>
                    ))}
                  </div>
                </details>
                <span>{steering ? "⌘ Enter 追加指令" : "⌘ Enter 发送"}</span>
              </footer>
            </section>
          </div>
        )}
      </section>
      {notice !== null && <div className="cowork-toast" role="status">{notice}<button aria-label="关闭提示" onClick={() => setNotice(null)} type="button">×</button></div>}
    </main>
  );
}
