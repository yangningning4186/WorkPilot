"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { AdminSessionControl, useAdminSession } from "@/components/admin-session";
import { AnswerMarkdown } from "@/components/answer-markdown";
import { WorkdeskIcon, WorkdeskNavigation } from "@/components/workdesk-shell";
import {
  ApiError,
  cancelRun,
  createConversation,
  createCoworkRun,
  deleteConversation,
  fetchConversationMessages,
  fetchConversationContextUsage,
  fetchConversations,
  fetchCoworkArtifacts,
  fetchArtifactPreview,
  fetchCoworkGrants,
  fetchCoworkMemories,
  fetchCoworkRoots,
  forgetCoworkMemory,
  patchCoworkMemory,
  fetchProviders,
  revokeCoworkRoot,
  respondToCoworkInteraction,
  setConversationArchived,
  steerCoworkRun,
  updateConversationRuntime,
  uploadCoworkAttachment,
  type ConversationSummary,
  type ConversationContextUsage,
  type ConversationMessage,
  type CoworkArtifact,
  type CoworkMemory,
  type CoworkGrant,
  type CoworkRoot,
  type ProviderProfile,
} from "@/lib/api";
import { isTauriRuntime, pickCoworkDirectory } from "@/lib/desktop";
import { useCoworkRun } from "@/lib/use-cowork-run";
import type { MemorySavedPayload } from "@/lib/run-protocol";

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
  browser_open: "打开浏览器",
  browser_snapshot: "读取页面 DOM",
  browser_click: "点击网页控件",
  browser_back: "浏览器返回",
  browser_type: "填写网页输入",
  browser_select: "选择网页选项",
  browser_upload: "上传网页文件",
  browser_download: "下载网页文件",
  browser_screenshot: "保存网页截图",
  browser_find: "查找页面内容",
  browser_close: "关闭浏览器",
  explore: "只读子 Agent 调查",
  fetch_url: "读取网页",
  create_artifact: "生成交付物",
  list_office_files: "扫描 Word / Excel",
  inspect_office_file: "读取文档结构",
  edit_word: "编辑 Word",
  edit_excel: "编辑 Excel",
};

const MEMORY_SCOPE_LABELS: Record<string, string> = {
  global: "所有会话",
  workspace: "当前工作目录",
  conversation: "仅本次会话",
};

const CAPABILITY_LABELS: Record<string, string> = {
  "knowledge.read": "读取个人资料库",
  "filesystem.read": "读取文件",
  "filesystem.write": "写入文件",
  "office.word.edit": "编辑 Word",
  "office.excel.edit": "编辑 Excel",
  "network.read": "读取公开网页",
  "browser.control": "控制浏览器",
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

function formatTokenCount(value: number): string {
  if (value < 1000) return `${Math.max(0, Math.round(value))}`;
  return `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}K`;
}

function ContextUsageMeter({ usage, draft }: { usage: ConversationContextUsage | null; draft: string }) {
  if (usage === null) {
    return <span className="workdesk-context-meter-loading" aria-label="正在计算上下文用量" />;
  }
  const draftTokens = draft.trim() === "" ? 0 : draft.length + 8;
  const usedTokens = usage.used_tokens + draftTokens;
  const ratio = Math.min(1, usedTokens / Math.max(1, usage.context_window_tokens));
  const percent = ratio * 100;
  const thresholdPercent = usage.trigger_ratio * 100;
  const breakdown = [
    { key: "system", label: "系统提示词", value: usage.breakdown.system, color: "#19ad91" },
    { key: "tools", label: "工具与子 Agent", value: usage.breakdown.tools, color: "#ddb05e" },
    { key: "messages", label: "对话消息", value: usage.breakdown.messages, color: "#7658e8" },
    { key: "activity", label: "Tool 调用与结果", value: usage.breakdown.tool_activity, color: "#29b9ce" },
    { key: "draft", label: "当前输入", value: draftTokens, color: "#4d79e9" },
  ];
  return (
    <details className="workdesk-context-meter">
      <summary aria-label={`上下文已使用 ${percent.toFixed(1)}%`} title={`${percent.toFixed(1)}% · ${formatTokenCount(usedTokens)} / ${formatTokenCount(usage.context_window_tokens)}`}>
        <svg aria-hidden="true" viewBox="0 0 36 36">
          <circle className="track" cx="18" cy="18" r="14" />
          <circle className={percent >= thresholdPercent ? "value warning" : "value"} cx="18" cy="18" pathLength="100" r="14" strokeDasharray={`${percent} ${100 - percent}`} />
        </svg>
        <span>{Math.round(percent)}%</span>
      </summary>
      <section className="workdesk-context-popover">
        <header><div><small>CONTEXT WINDOW</small><strong>上下文用量</strong></div><span>{usage.model}</span></header>
        <div className="workdesk-context-total"><b>{percent.toFixed(1)}%</b><span>已使用 {formatTokenCount(usedTokens)} / {formatTokenCount(usage.context_window_tokens)}</span></div>
        <div className="workdesk-context-bar" aria-hidden="true">
          {breakdown.map((item) => <i key={item.key} style={{ background: item.color, width: `${Math.min(100, item.value / Math.max(1, usage.context_window_tokens) * 100)}%` }} />)}
          <em style={{ left: `${Math.min(100, thresholdPercent)}%` }} />
        </div>
        <div className="workdesk-context-breakdown">
          {breakdown.map((item) => <div key={item.key}><i style={{ background: item.color }} /><span>{item.label}</span><b>{(item.value / Math.max(1, usage.context_window_tokens) * 100).toFixed(1)}%</b></div>)}
        </div>
        <footer>
          <span>{usage.auto_compaction ? `达到 ${thresholdPercent.toFixed(0)}% 自动压缩` : "自动压缩已关闭"}</span>
          <b>{usage.compaction_revision > 0 ? `已压缩 ${usage.compaction_revision} 次` : "尚未压缩"}</b>
        </footer>
      </section>
    </details>
  );
}

export default function CoworkPage() {
  const { state: authState } = useAdminSession();
  const desktopReady = useSyncExternalStore(
    () => () => undefined,
    isTauriRuntime,
    () => false,
  );
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [archivedConversations, setArchivedConversations] = useState<ConversationSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [managingConversationId, setManagingConversationId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ConversationSummary | null>(null);
  const [openConversationMenuId, setOpenConversationMenuId] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [roots, setRoots] = useState<CoworkRoot[]>([]);
  const [grants, setGrants] = useState<CoworkGrant[]>([]);
  const [artifacts, setArtifacts] = useState<CoworkArtifact[]>([]);
  const [memories, setMemories] = useState<CoworkMemory[]>([]);
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [memoryDraft, setMemoryDraft] = useState("");
  // 已经撤销过的记忆写入不再提示；memoryWrites 由事件流累积，页面不能直接改它。
  const [undoneMemories, setUndoneMemories] = useState<string[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [goal, setGoal] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const [activePrompt, setActivePrompt] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [responding, setResponding] = useState(false);
  const [interactionAnswer, setInteractionAnswer] = useState("");
  const [planMode, setPlanMode] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [workMode, setWorkMode] = useState<"office" | "research">("office");
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [contextUsage, setContextUsage] = useState<ConversationContextUsage | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<{ title: string; url: string; mode: string } | null>(null);
  const run = useCoworkRun(runId);

  useEffect(() => {
    if (openConversationMenuId === null) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || event.target.closest(".workdesk-task-menu") === null) {
        setOpenConversationMenuId(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenConversationMenuId(null);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [openConversationMenuId]);

  const loadSession = useCallback(async (id: string) => {
    const [rootResponse, grantResponse, artifactResponse, memoryResponse, messageResponse, contextResponse] = await Promise.all([
      fetchCoworkRoots(id),
      fetchCoworkGrants(id),
      fetchCoworkArtifacts(id),
      fetchCoworkMemories(id),
      fetchConversationMessages(id),
      fetchConversationContextUsage(id).catch(() => null),
    ]);
    setRoots(rootResponse.items);
    setGrants(grantResponse.items);
    setArtifacts(artifactResponse.items);
    setMemories(memoryResponse.items);
    setMessages(messageResponse.items);
    if (contextResponse !== null) setContextUsage(contextResponse);
  }, []);

  useEffect(() => () => {
    if (artifactPreview !== null) URL.revokeObjectURL(artifactPreview.url);
  }, [artifactPreview]);

  // 模型写过记忆就重拉面板：面板显示的必须和注入给模型的是同一份。
  useEffect(() => {
    if (conversationId === null || run.memoryWrites.length === 0) return;
    fetchCoworkMemories(conversationId)
      .then((response) => setMemories(response.items))
      .catch(() => undefined);
  }, [conversationId, run.memoryWrites.length]);

  const refreshMemories = useCallback(async () => {
    if (conversationId === null) return;
    const response = await fetchCoworkMemories(conversationId);
    setMemories(response.items);
  }, [conversationId]);

  const saveMemoryEdit = useCallback(async (memoryId: string) => {
    const content = memoryDraft.trim();
    if (content === "") return;
    setBusy(true);
    try {
      await patchCoworkMemory(memoryId, { content });
      setEditingMemoryId(null);
      await refreshMemories();
      setNotice("记忆已更新，下一轮起对模型生效。");
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [memoryDraft, refreshMemories]);

  const removeMemory = useCallback(async (memoryId: string) => {
    setBusy(true);
    try {
      await forgetCoworkMemory(memoryId);
      await refreshMemories();
      setNotice("已忘记这条记忆，模型下一轮不会再看到它。");
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [refreshMemories]);

  const undoMemoryWrite = useCallback(async (write: MemorySavedPayload) => {
    setBusy(true);
    try {
      if (write.action === "saved") {
        await forgetCoworkMemory(write.memory.id);
      } else if (write.action === "forgotten") {
        await patchCoworkMemory(write.memory.id, { restore: true });
      } else if (write.previous_content !== null) {
        await patchCoworkMemory(write.memory.id, { content: write.previous_content });
      }
      setUndoneMemories((current) => [...current, write.memory.id]);
      await refreshMemories();
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [refreshMemories]);

  const pendingMemoryWrites = run.memoryWrites.filter(
    (write) => !undoneMemories.includes(write.memory.id),
  );

  const previewArtifact = useCallback(async (artifact: CoworkArtifact) => {
    setBusy(true);
    try {
      const preview = await fetchArtifactPreview(artifact.id);
      setArtifactPreview((current) => {
        if (current !== null) URL.revokeObjectURL(current.url);
        return { title: artifact.title, url: URL.createObjectURL(preview.blob), mode: preview.mode };
      });
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (authState !== "authenticated") return;
    let cancelled = false;
    const load = async () => {
      try {
        const [response, archivedResponse, providerResponse] = await Promise.all([
          fetchConversations(),
          fetchConversations(true),
          fetchProviders(),
        ]);
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
        setArchivedConversations(archivedResponse.items);
        setProviders(providerResponse.items.filter((item) => item.enabled));
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
    if (conversationId === null || run.cursor === 0n) return;
    fetchConversationContextUsage(conversationId)
      .then(setContextUsage)
      .catch(() => undefined);
  }, [conversationId, run.cursor]);

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

  const steering = run.phase === "connecting" || run.phase === "executing";
  const running = steering || run.phase === "waiting_human";

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
      const created = await createConversation(
        `Cowork ${conversations.length + archivedConversations.length + 1}`,
      );
      setConversations((current) => [created, ...current]);
      setShowArchived(false);
      setConversationId(created.id);
      setRunId(null);
      setMessages([]);
      setContextUsage(null);
      setActivePrompt(null);
      setAttachments([]);
      setNotice(null);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [archivedConversations.length, conversations.length]);

  const openConversation = useCallback((item: ConversationSummary) => {
    setOpenConversationMenuId(null);
    setConversationId(item.id);
    setRunId(null);
    setActivePrompt(null);
    setAttachments([]);
    setNotice(null);
  }, []);

  const changeConversationArchive = useCallback(async (
    target: ConversationSummary,
    archived: boolean,
  ) => {
    if (running) return;
    setOpenConversationMenuId(null);
    setManagingConversationId(target.id);
    setNotice(null);
    try {
      const updated = await setConversationArchived(target.id, archived);
      if (archived) {
        setConversations((current) => current.filter((item) => item.id !== target.id));
        setArchivedConversations((current) => [
          updated,
          ...current.filter((item) => item.id !== target.id),
        ]);
        if (target.id === conversationId) setShowArchived(true);
        setNotice("会话已归档，可随时从归档列表恢复。");
      } else {
        setArchivedConversations((current) => current.filter((item) => item.id !== target.id));
        setConversations((current) => [
          updated,
          ...current.filter((item) => item.id !== target.id),
        ]);
        setShowArchived(false);
        setNotice("会话已恢复到任务列表。");
      }
    } catch (reason) {
      setNotice(
        reason instanceof ApiError && reason.status === 409
          ? "该会话的任务正在执行，请停止任务或稍后再操作。"
          : readableError(reason),
      );
    } finally {
      setManagingConversationId(null);
    }
  }, [conversationId, running]);

  const removeConversation = useCallback(async (target: ConversationSummary) => {
    if (running) return;
    setOpenConversationMenuId(null);
    setManagingConversationId(target.id);
    setNotice(null);
    try {
      await deleteConversation(target.id);
      const remainingActive = conversations.filter((item) => item.id !== target.id);
      const remainingArchived = archivedConversations.filter((item) => item.id !== target.id);
      setConversations(remainingActive);
      setArchivedConversations(remainingArchived);
      if (target.id === conversationId) {
        const next = remainingActive[0] ?? remainingArchived[0];
        if (next === undefined) {
          const created = await createConversation("Cowork 工作台");
          setConversations([created]);
          setShowArchived(false);
          openConversation(created);
        } else {
          setShowArchived(next.archived_at !== null);
          openConversation(next);
        }
      }
      setNotice("会话已永久删除。");
    } catch (reason) {
      setNotice(
        reason instanceof ApiError && reason.status === 409
          ? "该会话的任务正在执行，请停止任务或稍后再删除。"
          : readableError(reason),
      );
    } finally {
      setManagingConversationId(null);
      setPendingDelete(null);
    }
  }, [archivedConversations, conversationId, conversations, openConversation, running]);

  const execute = useCallback(async () => {
    if (
      conversationId === null
      || goal.trim() === ""
    ) return;
    const prompt = goal.trim();
    setBusy(true);
    setNotice(null);
    try {
      const uploaded = await Promise.all(
        attachments.map((file) => uploadCoworkAttachment(conversationId, file)),
      );
      const response = await createCoworkRun({
        conversation_id: conversationId,
        goal: prompt,
        attachment_ids: uploaded.map((item) => item.id),
        plan_mode: planMode,
      });
      setStopping(false);
      setActivePrompt(prompt);
      setRunId(response.run_id);
      setGoal("");
      setAttachments([]);
      await loadSession(conversationId);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [attachments, conversationId, goal, loadSession, planMode]);

  const addAttachments = useCallback((files: FileList | File[]) => {
    const accepted = Array.from(files).filter((file) => {
      const suffix = file.name.toLowerCase().split(".").pop() ?? "";
      return file.type.startsWith("image/")
        || file.type === "application/pdf"
        || ["txt", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml"].includes(suffix);
    });
    if (accepted.length === 0) {
      setNotice("只支持图片、PDF 和 UTF-8 文本附件。");
      return;
    }
    const tooLarge = accepted.find((file) => file.size > 10 * 1024 * 1024);
    if (tooLarge !== undefined) {
      setNotice(`${tooLarge.name} 超过 10 MB，未添加。`);
      return;
    }
    setAttachments((current) => {
      const merged = [...current, ...accepted].slice(0, 8);
      if (current.length + accepted.length > 8) setNotice("每条消息最多添加 8 个附件。");
      return merged;
    });
  }, []);

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

  const submitComposer = steering ? sendSteering : execute;
  const prompts = workMode === "office" ? OFFICE_PROMPTS : RESEARCH_PROMPTS;
  const listedConversations = showArchived ? archivedConversations : conversations;
  const activeConversation = [...conversations, ...archivedConversations].find(
    (item) => item.id === conversationId,
  );
  const conversationArchived = activeConversation?.archived_at !== null
    && activeConversation?.archived_at !== undefined;

  const selectProvider = useCallback(async (providerId: string) => {
    if (conversationId === null || running) return;
    setBusy(true);
    try {
      const selected = providers.find((item) => item.id === providerId);
      const updated = await updateConversationRuntime(conversationId, {
        provider_profile_id: providerId || null,
        model_override: selected?.default_model ?? null,
        unattended: activeConversation?.unattended ?? false,
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      fetchConversationContextUsage(conversationId).then(setContextUsage).catch(() => undefined);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [activeConversation, conversationId, providers, running]);

  const saveModelOverride = useCallback(async (modelValue: string) => {
    if (
      conversationId === null
      || activeConversation === undefined
      || activeConversation.provider_profile_id === null
      || running
    ) return;
    const normalized = modelValue.trim();
    if (!normalized || normalized === activeConversation.selected_model) return;
    setBusy(true);
    try {
      const updated = await updateConversationRuntime(conversationId, {
        provider_profile_id: activeConversation.provider_profile_id,
        model_override: normalized,
        unattended: activeConversation.unattended,
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      fetchConversationContextUsage(conversationId).then(setContextUsage).catch(() => undefined);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [activeConversation, conversationId, running]);
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
  const planSummary =
    typeof interactionPayload.summary === "string" ? interactionPayload.summary : "我打算这样做";
  const planSteps = Array.isArray(interactionPayload.steps)
    ? interactionPayload.steps.filter((item): item is string => typeof item === "string")
    : [];
  const planNotes = typeof interactionPayload.notes === "string" ? interactionPayload.notes : "";
  const runAnswer = run.answer || run.progressSummary || "";
  const hasConversation = messages.length > 0 || runId !== null;
  const visibleMessages =
    runId === null ? messages : messages.filter((message) => message.run_id !== runId);
  const currentPromptMessage = messages.find(
    (message) => message.run_id === runId && message.role === "user",
  );
  const currentPrompt = activePrompt ?? currentPromptMessage?.content ?? null;
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
          <header className="workdesk-task-heading">
            <span>{showArchived ? "已归档" : "任务"}</span>
            <small>{listedConversations.length}</small>
            <button
              aria-pressed={showArchived}
              className={showArchived ? "active" : ""}
              disabled={busy || running}
              onClick={() => setShowArchived((current) => !current)}
              type="button"
            >
              <WorkdeskIcon name={showArchived ? "restore" : "archive"} />
              {showArchived ? "返回任务" : "归档"}
            </button>
          </header>
          <div className="workdesk-task-list">
            {listedConversations.slice(0, 12).map((item) => (
              <div className={`workdesk-task-row${item.id === conversationId ? " active" : ""}`} key={item.id}>
                <button
                  className="workdesk-task-select"
                  disabled={running}
                  onClick={() => openConversation(item)}
                  type="button"
                >
                  <span>{item.title ?? "Cowork 任务"}</span>
                  <small>{item.id === conversationId ? "当前" : new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</small>
                </button>
                <div className={`workdesk-task-menu${openConversationMenuId === item.id ? " open" : ""}`}>
                  <button
                    aria-controls={`conversation-menu-${item.id}`}
                    aria-expanded={openConversationMenuId === item.id}
                    aria-haspopup="menu"
                    aria-label={`管理会话：${item.title ?? "Cowork 任务"}`}
                    className="workdesk-task-menu-trigger"
                    onClick={() => setOpenConversationMenuId((current) => current === item.id ? null : item.id)}
                    type="button"
                  >
                    <WorkdeskIcon name="dots" />
                  </button>
                  {openConversationMenuId === item.id && <div id={`conversation-menu-${item.id}`} role="menu">
                    <button
                      disabled={running || managingConversationId !== null}
                      onClick={() => void changeConversationArchive(item, !showArchived)}
                      role="menuitem"
                      type="button"
                    >
                      <WorkdeskIcon name={showArchived ? "restore" : "archive"} />
                      {showArchived ? "恢复会话" : "归档会话"}
                    </button>
                    <button
                      className="danger"
                      disabled={running || managingConversationId !== null}
                      onClick={() => {
                        setOpenConversationMenuId(null);
                        setPendingDelete(item);
                      }}
                      role="menuitem"
                      type="button"
                    >
                      <WorkdeskIcon name="trash" />永久删除
                    </button>
                  </div>}
                </div>
              </div>
            ))}
            {listedConversations.length === 0 && <p>{showArchived ? "还没有归档会话" : "连接后会在这里显示任务记录"}</p>}
          </div>
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
          <span className="workdesk-default-scope"><WorkdeskIcon name="shield" />默认权限</span>
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
                {conversationArchived && (
                  <div className="workdesk-archived-banner">
                    <WorkdeskIcon name="archive" />
                    <div><strong>此会话已归档</strong><span>内容保持只读，恢复后可以继续任务。</span></div>
                    <button disabled={managingConversationId !== null} onClick={() => activeConversation !== undefined && void changeConversationArchive(activeConversation, false)} type="button">恢复会话</button>
                  </div>
                )}
                {visibleMessages.filter((message) => message.content.trim() !== "" || message.attachments.length > 0).map((message) => (
                  <article className={`workdesk-message ${message.role}`} key={message.id}>
                    {message.role === "assistant" && <span className="workdesk-agent-avatar"><WorkdeskIcon name="spark" /></span>}
                    <div className="workdesk-message-body">
                      {message.role === "assistant" && <small>WorkPilot</small>}
                      {message.role === "assistant" ? <AnswerMarkdown text={message.content} /> : <p>{message.content}</p>}
                      {message.attachments.length > 0 && <div className="workdesk-message-attachments">{message.attachments.map((item) => <span key={item.id}><WorkdeskIcon name="file" /><b>{item.filename}</b><small>{item.kind === "image" ? "图片" : item.kind === "pdf" ? "PDF" : "文本"}</small></span>)}</div>}
                    </div>
                  </article>
                ))}

                {runId !== null && (
                  <>
                    {currentPrompt !== null && (
                      <article className="workdesk-message user current">
                        <div className="workdesk-message-body"><p>{currentPrompt}</p>{currentPromptMessage !== undefined && currentPromptMessage.attachments.length > 0 && <div className="workdesk-message-attachments">{currentPromptMessage.attachments.map((item) => <span key={item.id}><WorkdeskIcon name="file" /><b>{item.filename}</b><small>{item.kind === "image" ? "图片" : item.kind === "pdf" ? "PDF" : "文本"}</small></span>)}</div>}</div>
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

                        {pendingMemoryWrites.length > 0 && (
                          <section className="workdesk-memory-notices" aria-live="polite">
                            {pendingMemoryWrites.map((write) => (
                              <article key={write.memory.id}>
                                <WorkdeskIcon name="shield" />
                                <p>
                                  <strong>
                                    {write.action === "forgotten"
                                      ? "已忘记"
                                      : write.action === "updated"
                                        ? "已更新记忆"
                                        : "已记住"}
                                  </strong>
                                  <span>{write.memory.content}</span>
                                </p>
                                <button disabled={busy} onClick={() => void undoMemoryWrite(write)} type="button">撤销</button>
                              </article>
                            ))}
                          </section>
                        )}

                        {run.todos.length > 0 && (
                          <section className="workdesk-todos" aria-label="任务清单">
                            <header>
                              <span>任务清单</span>
                              <small>{run.todos.filter((todo) => todo.status === "done").length}/{run.todos.length}</small>
                            </header>
                            <ol>
                              {run.todos.map((todo, index) => (
                                <li className={todo.status} key={`${index}-${todo.content}`}>
                                  <span aria-hidden>{todo.status === "done" ? "✓" : todo.status === "in_progress" ? "▶" : ""}</span>
                                  <p>{todo.content}</p>
                                </li>
                              ))}
                            </ol>
                          </section>
                        )}

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
                                <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">跳过，由 Cowork 判断</button><button className="primary" disabled={responding || interactionAnswer.trim() === ""} onClick={() => void respondToInteraction({ approved: true, answer: interactionAnswer.trim() })} type="button">回复并继续</button></div>
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
                            ) : run.interrupt.kind === "plan_approval" ? (
                              <>
                                <h3>{planSummary}</h3>
                                <ol className="workdesk-plan-steps">
                                  {planSteps.map((step, index) => <li key={`${index}:${step}`}><span>{index + 1}</span><p>{step}</p></li>)}
                                </ol>
                                {planNotes !== "" && <p className="workdesk-plan-notes">{planNotes}</p>}
                                <small>批准后这些步骤会成为任务清单，写入类工具才会解锁。要改的话直接写在下面。</small>
                                <textarea aria-label="对这个计划的修改意见" disabled={responding} maxLength={4000} onChange={(event) => setInteractionAnswer(event.target.value)} placeholder="想改哪里？留空直接批准" rows={2} value={interactionAnswer} />
                                <div className="workdesk-inbox-actions"><button disabled={responding || interactionAnswer.trim() === ""} onClick={() => void respondToInteraction({ approved: false, answer: interactionAnswer.trim() })} type="button">按这些意见重做计划</button><button className="primary" disabled={responding} onClick={() => void respondToInteraction({ approved: true, answer: interactionAnswer.trim() || undefined })} type="button">批准并开始执行</button></div>
                              </>
                            ) : run.interrupt.kind === "external_approval" ? (
                              <>
                                <h3>允许执行这次外部动作？</h3>
                                <p>{typeof interactionPayload.warning === "string" ? interactionPayload.warning : "该工具会修改外部系统。"}</p>
                                <pre className="workdesk-shell-command"><code>{JSON.stringify(interactionPayload.arguments ?? {}, null, 2)}</code></pre>
                                <small>工具：{typeof interactionPayload.tool === "string" ? interactionPayload.tool : "外部工具"}</small>
                                <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">拒绝</button><button className="primary danger" disabled={responding} onClick={() => void respondToInteraction({ approved: true })} type="button">批准一次</button></div>
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
                              return <button className="workdesk-artifact-button" key={artifact.id} onClick={() => void previewArtifact(artifact)} type="button"><span className={excel ? "excel" : "word"}>{excel ? "X" : word ? "W" : artifact.mime_type === "application/pdf" ? "P" : "A"}</span><div><strong>{artifact.title}</strong><small>{artifactNote(artifact)}</small></div><time>{new Date(artifact.updated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></button>;
                            })}
                          </section>
                        )}
                      </div>
                    </article>
                  </>
                )}
              </section>
            )}

            <section className={`workdesk-composer${conversationArchived ? " is-archived" : ""}`} aria-label="创建 Cowork 任务">
              <input
                accept="image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown,text/csv,application/json,.txt,.md,.markdown,.csv,.tsv,.json,.xml,.yaml,.yml"
                aria-label="选择图片、PDF 或文本附件"
                hidden
                multiple
                onChange={(event) => {
                  if (event.target.files !== null) addAttachments(event.target.files);
                  event.target.value = "";
                }}
                ref={attachmentInput}
                type="file"
              />
              {attachments.length > 0 && (
                <div className="workdesk-attachment-tray" aria-label="待发送附件">
                  {attachments.map((file, index) => (
                    <span key={`${file.name}:${file.lastModified}:${index}`}>
                      <WorkdeskIcon name="file" />
                      <b title={file.name}>{file.name}</b>
                      <small>{file.size < 1024 * 1024 ? `${Math.max(1, Math.round(file.size / 1024))} KB` : `${(file.size / 1024 / 1024).toFixed(1)} MB`}</small>
                      <button aria-label={`移除 ${file.name}`} disabled={busy} onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))} type="button">×</button>
                    </span>
                  ))}
                </div>
              )}
              <textarea
                aria-label={steering ? "向运行中的 Cowork 追加指令" : "你想让 Cowork 完成什么？"}
                disabled={run.phase === "waiting_human" || responding || conversationArchived}
                id="cowork-goal"
                maxLength={4000}
                onChange={(event) => setGoal(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void submitComposer();
                }}
                onDrop={(event) => {
                  if (steering || event.dataTransfer.files.length === 0) return;
                  event.preventDefault();
                  addAttachments(event.dataTransfer.files);
                }}
                onDragOver={(event) => {
                  if (!steering && event.dataTransfer.types.includes("Files")) event.preventDefault();
                }}
                placeholder={conversationArchived ? "此会话已归档，恢复后可以继续" : run.phase === "waiting_human" ? "请先回复上方的问题" : steering ? "补充要求或调整方向…" : hasConversation ? "继续这段对话，或交代一个新任务…" : "今天帮你做些什么？可以直接提问、上传资料，或交代一项任务"}
                rows={hasConversation ? 2 : 4}
                value={goal}
              />
              <div className="workdesk-composer-actions">
                <button aria-label="添加图片、PDF 或文本附件" disabled={busy || running || conversationArchived} onClick={() => attachmentInput.current?.click()} title={conversationArchived ? "恢复会话后可添加附件" : running ? "运行期间暂不支持追加附件" : "添加图片、PDF 或文本（最多 8 个）"} type="button"><WorkdeskIcon name="add" /></button>
                <span>{conversationArchived ? "归档会话 · 只读" : run.phase === "waiting_human" ? "请先处理对话中的请求" : steering ? "发送后将在安全边界转向" : attachments.length > 0 ? `已添加 ${attachments.length} 个附件` : planMode ? "计划模式 · 先出方案等你批准" : "默认权限 · Agent 已就绪"}</span>
                <ContextUsageMeter draft={goal} usage={contextUsage} />
                <label className="workdesk-model-select" title="按会话切换 Provider">
                  <WorkdeskIcon name="spark" />
                  <select disabled={busy || running || conversationArchived} onChange={(event) => void selectProvider(event.target.value)} value={activeConversation?.provider_profile_id ?? ""}>
                    <option value="">系统默认</option>
                    {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
                  </select>
                </label>
                <button aria-checked={planMode} aria-label="先出计划再执行" className={`workdesk-plan-toggle${planMode ? " is-on" : ""}`} disabled={busy || running || conversationArchived || steering} onClick={() => setPlanMode((current) => !current)} role="switch" title="打开后 Cowork 会先调研并提交计划，等你批准再动手" type="button"><WorkdeskIcon name="shield" /><span>先出计划</span></button>
                <button aria-label={steering ? "追加运行指令" : "开始执行任务"} className="workdesk-send" disabled={busy || conversationArchived || run.phase === "waiting_human" || goal.trim() === ""} onClick={() => void submitComposer()} type="button"><WorkdeskIcon name="send" /></button>
              </div>
              <footer>
                <details className="workdesk-permission-menu">
                  <summary><WorkdeskIcon name="shield" /><span>默认权限</span><b>⌄</b></summary>
                  <div>
                    <h3>默认权限</h3>
                    <p>普通任务可以直接开始，新生成的文件默认保存在本机 ~/Documents/WorkPilot。读取其他本机目录、运行 Shell 或操作外部系统时，WorkPilot 会在需要的那一步单独向你确认。</p>
                    {roots.length > 0 && <h4>本次会话已授权目录</h4>}
                    {roots.map((root) => (
                      <article key={root.id}><div><strong>{root.label}</strong><small title={root.canonical_path}>{shortPath(root.canonical_path)} · {(capabilitiesByRoot.get(root.id) ?? []).join(" · ")}</small></div><button disabled={busy || running} onClick={() => void removeRoot(root.id)} type="button">收回</button></article>
                    ))}
                  </div>
                </details>
                <details className="workdesk-permission-menu workdesk-memory-menu">
                  <summary><WorkdeskIcon name="shield" /><span>记忆</span><b>{memories.length > 0 ? memories.length : "⌄"}</b></summary>
                  <div>
                    <h3>长期记忆</h3>
                    <p>这些事实会在每一轮注入给模型，面板里看到的就是它看到的。global 对所有会话有效，workspace 只对当前工作目录有效，conversation 只在本次会话有效。</p>
                    {memories.length === 0 && <h4>还没有记忆。模型在你表达长期偏好时会自己记下来。</h4>}
                    {memories.map((memory) => (
                      <article key={memory.id}>
                        {editingMemoryId === memory.id ? (
                          <>
                            <textarea aria-label="编辑记忆" maxLength={4000} onChange={(event) => setMemoryDraft(event.target.value)} rows={3} value={memoryDraft} />
                            <button disabled={busy || memoryDraft.trim() === ""} onClick={() => void saveMemoryEdit(memory.id)} type="button">保存</button>
                            <button disabled={busy} onClick={() => setEditingMemoryId(null)} type="button">取消</button>
                          </>
                        ) : (
                          <>
                            <div>
                              <strong>{memory.content}</strong>
                              <small>{MEMORY_SCOPE_LABELS[memory.scope]}{memory.source === "user" ? " · 你添加的" : ""}{memory.workspace_path !== null ? ` · ${shortPath(memory.workspace_path)}` : ""}</small>
                            </div>
                            <button disabled={busy} onClick={() => { setEditingMemoryId(memory.id); setMemoryDraft(memory.content); }} type="button">编辑</button>
                            <button disabled={busy} onClick={() => void removeMemory(memory.id)} type="button">忘记</button>
                          </>
                        )}
                      </article>
                    ))}
                  </div>
                </details>
                {activeConversation?.provider_profile_id !== null && activeConversation?.provider_profile_id !== undefined && (
                  <label className="workdesk-model-override"><span>模型</span><input defaultValue={activeConversation.selected_model ?? ""} disabled={busy || running} key={`${activeConversation.id}:${activeConversation.selected_model ?? ""}`} onBlur={(event) => void saveModelOverride(event.target.value)} /></label>
                )}
                <span>{steering ? "⌘ Enter 追加指令" : "⌘ Enter 发送"}</span>
              </footer>
            </section>
            {artifactPreview !== null && <div className="workdesk-preview-backdrop"><section className="workdesk-preview-dialog"><header><div><strong>{artifactPreview.title}</strong><span>{artifactPreview.mode === "quicklook" ? "macOS 系统版面渲染" : artifactPreview.mode === "libreoffice" ? "LibreOffice 分页渲染" : artifactPreview.mode === "native-pdf" ? "原生 PDF" : artifactPreview.mode === "structure" ? "结构预览 · 未检测到版面渲染器" : "安全预览"}</span></div><button onClick={() => setArtifactPreview(null)} type="button">关闭</button></header><iframe referrerPolicy="no-referrer" sandbox="" src={artifactPreview.url} title={`${artifactPreview.title} 预览`} /></section></div>}
          </div>
        )}
      </section>
      {pendingDelete !== null && (
        <div className="workdesk-conversation-dialog-backdrop" onMouseDown={(event) => {
          if (event.currentTarget === event.target && managingConversationId === null) setPendingDelete(null);
        }}>
          <section aria-describedby="workdesk-delete-description" aria-labelledby="workdesk-delete-title" aria-modal="true" className="workdesk-conversation-dialog" role="alertdialog">
            <span className="workdesk-dialog-icon danger"><WorkdeskIcon name="trash" /></span>
            <div><small>永久删除</small><h2 id="workdesk-delete-title">{pendingDelete.title ?? "Cowork 任务"}</h2></div>
            <p id="workdesk-delete-description">会话、消息、运行记录和交付物索引会从本机永久删除；等待回复或尚未开始的任务会一并取消。已经生成的文件不会被删除。</p>
            <footer>
              <button disabled={managingConversationId !== null} onClick={() => setPendingDelete(null)} type="button">取消</button>
              <button className="danger" disabled={managingConversationId !== null} onClick={() => void removeConversation(pendingDelete)} type="button">{managingConversationId === pendingDelete.id ? "正在删除…" : "确认永久删除"}</button>
            </footer>
          </section>
        </div>
      )}
      {notice !== null && <div className="cowork-toast" role="status">{notice}<button aria-label="关闭提示" onClick={() => setNotice(null)} type="button">×</button></div>}
    </main>
  );
}
