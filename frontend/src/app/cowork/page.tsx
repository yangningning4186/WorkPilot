"use client";

import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";

import { AdminSessionControl, useAdminSession } from "@/components/admin-session";
import { AnswerMarkdown } from "@/components/answer-markdown";
import { ArtifactRail } from "@/components/artifact-rail";
import { ReaderPane } from "@/components/reader-pane";
import { RunActivityPanel } from "@/components/run-activity-panel";
import {
  HistoricalRunStageHistory,
  RunStageHistory,
} from "@/components/run-stage-history";
import { WorkdeskIcon, WorkdeskNavigation } from "@/components/workdesk-shell";
import {
  ApiError,
  type CoworkWorkMode,
  addCoworkRoot,
  cancelRun,
  createConversation,
  createCoworkRun,
  deleteConversation,
  fetchConversationMessages,
  fetchConversationContextUsage,
  fetchConversations,
  fetchCoworkArtifacts,
  fetchKnowledgeBases,
  fetchPersonas,
  fetchSessionKnowledgeBase,
  setSessionKnowledgeBase,
  type KnowledgeBase,
  type Persona,
  fetchCoworkGrants,
  fetchCoworkMemories,
  fetchApprovalRules,
  fetchCoworkRoots,
  fetchWorkspaceTrust,
  forgetCoworkMemory,
  patchCoworkMemory,
  fetchProviders,
  revokeCoworkRoot,
  respondToCoworkInteraction,
  revokeApprovalRule,
  setWorkspaceTrust,
  setConversationArchived,
  steerCoworkRun,
  updateConversationRuntime,
  uploadCoworkAttachment,
  type ApprovalRule,
  type WorkspaceTrustEntry,
  type ConversationSummary,
  type ConversationContextUsage,
  type ConversationMessage,
  type CoworkArtifact,
  type CoworkMemory,
  type CoworkGrant,
  type CoworkRoot,
  type ProviderProfile,
} from "@/lib/api";
import { readingViewportFor } from "@/lib/reading-turn-state";
import {
  isTauriRuntime,
  pickCoworkDirectory,
  pickCoworkReadingFile,
} from "@/lib/desktop";
import { useCoworkAutoScroll } from "@/lib/use-cowork-auto-scroll";
import { useCoworkRun } from "@/lib/use-cowork-run";
import { useSmoothStreamText } from "@/lib/use-smooth-stream-text";
import type { MemorySavedPayload } from "@/lib/run-protocol";

const MEMORY_SCOPE_LABELS: Record<string, string> = {
  global: "所有会话",
  workspace: "当前工作目录",
  conversation: "仅本次会话",
};

const CAPABILITY_LABELS: Record<string, string> = {
  "knowledge.read": "读取个人资料库",
  "filesystem.read": "读取文件",
  "filesystem.write": "写入文件",
  "network.fetch": "访问指定网站",
  "browser.read": "读取浏览器页面",
  "browser.write": "填写浏览器页面",
  "browser.destructive": "提交或删除网页内容",
  "sandbox.execute": "在隔离容器中执行",
  "host.execute": "在宿主机执行 Shell",
  "external.read": "读取外部系统",
  "external.write": "写入外部系统",
  "external.destructive": "删除外部系统数据",
};

// 旧版本数据库可能还留有这两类 grant；后端保留读取兼容，但新产品面不再展示或申请。
const RETIRED_CAPABILITIES = new Set([
  "office.word.edit",
  "office.excel.edit",
  "network.read",
  "browser.control",
  "shell.execute",
  "external.action",
]);

interface TeamProposalMember {
  name: string;
  role: string;
  reason: string;
}

function teamProposalMembers(payload: Record<string, unknown>): TeamProposalMember[] {
  const args = payload.arguments;
  if (typeof args !== "object" || args === null || Array.isArray(args)) return [];
  const members = (args as Record<string, unknown>).members;
  if (!Array.isArray(members)) return [];
  return members.flatMap((member) => {
    if (typeof member !== "object" || member === null || Array.isArray(member)) return [];
    const record = member as Record<string, unknown>;
    if (typeof record.name !== "string" || typeof record.role !== "string") return [];
    return [{
      name: record.name,
      role: record.role,
      reason: typeof record.reason === "string" ? record.reason : "",
    }];
  });
}

const OFFICE_PROMPTS = [
  { label: "文档处理", prompt: "整理工作空间里的 Word 文档，统一格式并提炼一页摘要。" },
  { label: "表格分析", prompt: "分析工作空间里的 Excel 表格，检查异常数据并补齐必要公式。" },
  { label: "数据可视化", prompt: "读取工作空间中的 Excel 数据，生成管理层可读的分析结论。" },
  { label: "批量整理", prompt: "扫描工作空间里的 Word 和 Excel，按内容归类并给出整理方案。" },
];

/**
 * 论文阅读的快捷任务。
 *
 * 每一条都刻意要求"标出处"：这一档的产品承诺是每个论断都能落回原文的具体位置，
 * 快捷任务如果自己不提，用户第一次用到的就是一个没有出处的普通摘要。
 */
const READING_PROMPTS = [
  { label: "读懂全文", prompt: "通读这篇论文，讲清它要解决什么问题、方法是什么、结论有多强，每个论断标出处。" },
  { label: "方法细节", prompt: "这篇论文的方法部分具体怎么做的？按步骤讲，并引用原文标出处。" },
  { label: "结论与局限", prompt: "这篇论文的主要结论和作者自己承认的局限分别是什么？引用原文标出处。" },
  { label: "找一段", prompt: "帮我找到论文里讨论实验设置的那一段，定位过去并解释它。" },
];

const INSPECTOR_WIDTH_STORAGE_KEY = "workpilot:cowork-inspector-width";
const READER_SESSION_STORAGE_PREFIX = "workpilot:cowork-reader:";
const DEFAULT_INSPECTOR_WIDTH = 400;
const MIN_INSPECTOR_WIDTH = 300;
const MAX_INSPECTOR_WIDTH = 720;

interface StoredReaderSession {
  workMode: CoworkWorkMode;
  path: string;
  locator: number;
  open: boolean;
}

const DEFAULT_READER_SESSION: StoredReaderSession = {
  workMode: "office",
  path: "",
  locator: 1,
  open: true,
};

function readReaderSession(conversationId: string): StoredReaderSession {
  try {
    const raw = window.sessionStorage.getItem(`${READER_SESSION_STORAGE_PREFIX}${conversationId}`);
    if (raw === null) return DEFAULT_READER_SESSION;
    const parsed = JSON.parse(raw) as Partial<StoredReaderSession>;
    return {
      workMode: parsed.workMode === "reading" ? "reading" : "office",
      path: typeof parsed.path === "string" ? parsed.path : "",
      locator:
        typeof parsed.locator === "number" && Number.isFinite(parsed.locator)
          ? Math.max(1, Math.floor(parsed.locator))
          : 1,
      open: typeof parsed.open === "boolean" ? parsed.open : true,
    };
  } catch {
    // sessionStorage 被禁用或旧值损坏时按默认办公模式启动；不能让一条 UI 草稿挡住会话。
    return DEFAULT_READER_SESSION;
  }
}

function writeReaderSession(conversationId: string, state: StoredReaderSession): void {
  try {
    window.sessionStorage.setItem(
      `${READER_SESSION_STORAGE_PREFIX}${conversationId}`,
      JSON.stringify(state),
    );
  } catch {
    // 阅读器恢复只是本机体验增强，存储配额或隐私模式失败不能影响正常对话。
  }
}

function clampInspectorWidth(width: number): number {
  if (typeof window === "undefined") return Math.max(MIN_INSPECTOR_WIDTH, Math.min(MAX_INSPECTOR_WIDTH, width));
  const sidebarWidth = window.innerWidth <= 1080 ? 238 : 292;
  // 给主对话区至少留出 420px；再窄时右栏会切换成抽屉，不参与三栏计算。
  const viewportMaximum = window.innerWidth <= 960
    ? MAX_INSPECTOR_WIDTH
    : Math.max(MIN_INSPECTOR_WIDTH, window.innerWidth - sidebarWidth - 420);
  return Math.max(MIN_INSPECTOR_WIDTH, Math.min(MAX_INSPECTOR_WIDTH, viewportMaximum, width));
}

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

function pathName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

function parentDirectory(path: string): string {
  const slash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  if (slash < 1) return path;
  // Windows 卷根需要保留尾部反斜杠；POSIX /foo 的父目录则是 /。
  if (slash === 2 && path[1] === ":") return path.slice(0, 3);
  return slash === 0 ? "/" : path.slice(0, slash);
}

function pathIsWithinDirectory(path: string, directory: string): boolean {
  const normalize = (value: string) => {
    const normalized = value.replaceAll("\\", "/").replace(/\/+$/, "");
    return /^[A-Za-z]:\//.test(normalized) ? normalized.toLowerCase() : normalized;
  };
  const target = normalize(path);
  const root = normalize(directory);
  return target === root || target.startsWith(`${root}/`);
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
  const thresholdPercent = usage.trigger_tokens / Math.max(1, usage.context_window_tokens) * 100;
  const hasStarted = usage.run_status !== null;
  const usageLabel = hasStarted ? "当前请求估算" : "提交后预计占用";
  const breakdown = [
    { key: "system", label: "系统提示词", value: usage.breakdown.system, color: "#19ad91" },
    { key: "manifest", label: "扩展工具目录", value: usage.breakdown.tool_manifest, color: "#c9a56b" },
    { key: "tools", label: "基础与模式工具", value: usage.breakdown.tools, color: "#ddb05e" },
    { key: "loaded-tools", label: "已加载扩展工具", value: usage.breakdown.loaded_tools, color: "#c9854d" },
    { key: "messages", label: "对话消息", value: usage.breakdown.messages, color: "#7658e8" },
    { key: "activity", label: "Tool 调用与结果", value: usage.breakdown.tool_activity, color: "#29b9ce" },
    { key: "draft", label: "当前输入", value: draftTokens, color: "#4d79e9" },
  ];
  return (
    <details className="workdesk-context-meter" name="composer-menu">
      <summary aria-label={`预计上下文占用 ${percent.toFixed(1)}%`} title={`${usageLabel} ${percent.toFixed(1)}% · ${formatTokenCount(usedTokens)} / ${formatTokenCount(usage.context_window_tokens)}`}>
        <svg aria-hidden="true" viewBox="0 0 36 36">
          <circle className="track" cx="18" cy="18" r="14" />
          <circle className={percent >= thresholdPercent ? "value warning" : "value"} cx="18" cy="18" pathLength="100" r="14" strokeDasharray={`${percent} ${100 - percent}`} />
        </svg>
        <span>{Math.round(percent)}%</span>
      </summary>
      <section className="workdesk-context-popover">
        <header><div><small>CONTEXT WINDOW</small><strong>上下文占用估算</strong></div><span>{usage.model}</span></header>
        <div className="workdesk-context-total"><b>{percent.toFixed(1)}%</b><span>{usageLabel} {formatTokenCount(usedTokens)} / {formatTokenCount(usage.context_window_tokens)}</span></div>
        {!hasStarted && <p className="workdesk-context-note">尚未调用模型；这里展示系统提示词、基础工具 Schema 与紧凑扩展目录的预计开销。</p>}
        <div className="workdesk-context-bar" aria-hidden="true">
          {breakdown.map((item) => <i key={item.key} style={{ background: item.color, width: `${Math.min(100, item.value / Math.max(1, usage.context_window_tokens) * 100)}%` }} />)}
          <em style={{ left: `${Math.min(100, thresholdPercent)}%` }} />
        </div>
        <div className="workdesk-context-breakdown">
          {breakdown.map((item) => <div key={item.key}><i style={{ background: item.color }} /><span>{item.label}</span><b>{(item.value / Math.max(1, usage.context_window_tokens) * 100).toFixed(1)}%</b></div>)}
        </div>
        <footer>
          <span>{usage.auto_compaction ? `约 ${formatTokenCount(usage.trigger_tokens)} 时自动压缩` : "自动压缩已关闭"}</span>
          <b>{usage.compaction_revision > 0 ? `已压缩 ${usage.compaction_revision} 次` : "尚未压缩"}</b>
        </footer>
      </section>
    </details>
  );
}

export default function CoworkPage() {
  const { state: authState, startupError } = useAdminSession();
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
  const [approvalRules, setApprovalRules] = useState<ApprovalRule[]>([]);
  const [workspaceTrust, setWorkspaceTrustState] = useState<WorkspaceTrustEntry[]>([]);
  /**
   * 这次批准要记多久。默认 once——一个漏改的界面不该悄悄留下常驻规则。
   *
   * 把 resume_token 一起存进 state，是为了让"换一条请求就重置"成为读取时的推导，
   * 而不是一个 effect：上一条勾过的"以后同类不用再问"绝不能静默套用到下一条
   * 完全不同的请求上，而 effect 至少要晚一帧才生效。
   */
  const [remember, setRemember] = useState<{
    token: string;
    scope: "once" | "command" | "target";
  }>({ token: "", scope: "once" });
  const [artifacts, setArtifacts] = useState<CoworkArtifact[]>([]);
  const [memories, setMemories] = useState<CoworkMemory[]>([]);
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [memoryDraft, setMemoryDraft] = useState("");
  // 已经撤销过的记忆写入不再提示；memoryWrites 由事件流累积，页面不能直接改它。
  const [undoneMemories, setUndoneMemories] = useState<string[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [goal, setGoal] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  // openworker 的工作空间是会话级选择，不是每条消息附带的一组原文件。空白页还没有
  // conversation_id，所以先保留为 draft；首轮发送严格按「建会话 → 挂工作区 → 建 run」
  // 提交。会话开始后从 roots 读取，工作空间不再随消息变化。
  const [workspaceDraftPath, setWorkspaceDraftPath] = useState<string | null>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const runSettingsMenu = useRef<HTMLDetailsElement>(null);
  const sessionLoadGeneration = useRef(0);
  const [activePrompt, setActivePrompt] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [responding, setResponding] = useState(false);
  const [interactionAnswer, setInteractionAnswer] = useState("");
  const [planMode, setPlanMode] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [workMode, setWorkMode] = useState<CoworkWorkMode>("office");
  // 论文阅读模式下打开的文档。只写进提示词告诉模型读哪一份；能不能读仍由每次工具调用
  // 上的 filesystem.read 授权决定，这里填一个未授权路径也越不过去。
  const [readingPath, setReadingPath] = useState("");
  const [readingPickerPath, setReadingPickerPath] = useState<string | null>(null);
  const [readingLocator, setReadingLocator] = useState(1);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [mountedKb, setMountedKb] = useState<string | null>(null);
  // 首屏初始化尚未拿到 conversation_id 时，知识库选择也必须是一个真实 draft，不能让
  // 下拉框看起来可操作、实际却把 onChange 丢掉。首次发送会按「建会话 → PUT 挂载 →
  // POST run」顺序提交它；已有会话仍然即时持久化选择。
  const [knowledgeBaseDraft, setKnowledgeBaseDraft] = useState<string | null>(null);
  const [knowledgeBaseDraftDirty, setKnowledgeBaseDraftDirty] = useState(false);
  // 新任务还没有 conversation_id，模型选择先留在浏览器 draft；首次发送时与新会话绑定，
  // 不允许未选择时回落到部署级默认模型。
  const [providerDraft, setProviderDraft] = useState<string | null>(null);
  const [knowledgeBaseLoadedFor, setKnowledgeBaseLoadedFor] = useState<string | null>(null);
  const conversationIdRef = useRef<string | null>(null);
  const [readerOpen, setReaderOpen] = useState(true);
  // 用户点回答里 `[p.12]` 的请求。nonce 让"再点一次同一页"也能被面板识别出来。
  const [locatorRequest, setLocatorRequest] = useState<{ locator: number; nonce: number } | null>(
    null,
  );
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [contextUsage, setContextUsage] = useState<ConversationContextUsage | null>(null);
  const [artifactRailOpen, setArtifactRailOpen] = useState(true);
  const [inspectorWidth, setInspectorWidth] = useState(DEFAULT_INSPECTOR_WIDTH);
  const [resizingInspector, setResizingInspector] = useState(false);
  const inspectorDrag = useRef<{
    currentWidth: number;
    pointerId: number;
    startWidth: number;
    startX: number;
  } | null>(null);
  const run = useCoworkRun(runId);
  // 阅读器当前打开的那份文档：模型跳过去的那份优先于输入框里填的那个路径。算在这里
  // 而不是等到渲染段，是因为发送逻辑也要用它（要带上视口），而 ref 读写在渲染期会被
  // React 的规则挡下——那条规则是对的，把值算早一步比绕过去干净。
  const readerPath = run.readerJump?.path ?? readingPath.trim();

  useEffect(() => {
    conversationIdRef.current = conversationId;
  }, [conversationId]);

  useEffect(() => {
    if (conversationId === null) return;
    writeReaderSession(conversationId, {
      workMode,
      path: readingPath,
      locator: readingLocator,
      open: readerOpen,
    });
  }, [conversationId, readerOpen, readingLocator, readingPath, workMode]);

  useEffect(() => {
    const savedWidth = Number.parseFloat(window.localStorage.getItem(INSPECTOR_WIDTH_STORAGE_KEY) ?? "");
    const restoreSavedWidth = window.requestAnimationFrame(() => {
      if (Number.isFinite(savedWidth)) setInspectorWidth(clampInspectorWidth(savedWidth));
    });
    const keepWidthInBounds = () => setInspectorWidth((current) => clampInspectorWidth(current));
    window.addEventListener("resize", keepWidthInBounds);
    return () => {
      window.cancelAnimationFrame(restoreSavedWidth);
      window.removeEventListener("resize", keepWidthInBounds);
    };
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("workdesk-pane-resizing", resizingInspector);
    return () => document.documentElement.classList.remove("workdesk-pane-resizing");
  }, [resizingInspector]);

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
    const generation = ++sessionLoadGeneration.current;
    const [rootResponse, grantResponse, artifactResponse, memoryResponse, messageResponse, contextResponse, ruleResponse, trustResponse, personaResponse] = await Promise.all([
      fetchCoworkRoots(id),
      fetchCoworkGrants(id),
      fetchCoworkArtifacts(id),
      fetchCoworkMemories(id),
      fetchConversationMessages(id),
      fetchConversationContextUsage(id).catch(() => null),
      fetchApprovalRules(id).catch(() => ({ items: [] })),
      fetchWorkspaceTrust(id).catch(() => ({ items: [] })),
      fetchPersonas(id).catch(() => ({ items: [], errors: [], project_paths: [] })),
    ]);
    // 快速切换会话时，旧请求可能后返回。它不能把新会话的消息/权限面板覆盖掉。
    if (generation !== sessionLoadGeneration.current) return;
    setRoots(rootResponse.items);
    setGrants(grantResponse.items);
    setApprovalRules(ruleResponse.items);
    setWorkspaceTrustState(trustResponse.items);
    setPersonas(personaResponse.items);
    setArtifacts(artifactResponse.items);
    setMemories(memoryResponse.items);
    setMessages(messageResponse.items);
    // 请求失败也要清空：否则从 A 切到 B 时，B 的 context 接口一旦失败就会继续显示 A 的用量。
    setContextUsage(contextResponse);
  }, []);

  // 模型写过记忆就重拉面板：面板显示的必须和注入给模型的是同一份。
  useEffect(() => {
    if (conversationId === null || run.memoryWrites.length === 0) return;
    fetchCoworkMemories(conversationId)
      .then((response) => setMemories(response.items))
      .catch(() => undefined);
  }, [conversationId, run.memoryWrites.length]);

  useEffect(() => {
    const generated = run.conversationTitle;
    if (generated === null) return;
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      setConversations((current) => current.map((item) =>
        item.id === generated.conversation_id ? { ...item, title: generated.title } : item
      ));
    });
    return () => {
      cancelled = true;
    };
  }, [run.conversationTitle]);

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
      setNotice("记忆已更新，从下一条消息起对模型生效。");
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
      setNotice("已忘记这条记忆，从下一条消息起模型不会再看到它。");
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
        const items = response.items;
        const query = new URLSearchParams(window.location.search);
        const requestedId = query.get("conversation");
        let selected: ConversationSummary | undefined;
        if (query.get("new") === "1") {
          window.history.replaceState(null, "", "/cowork");
          // 新任务先只存在浏览器里。等用户真正发送时再创建会话，避免空会话污染侧栏，
          // 也让尚无 conversation_id 的知识库选择可以作为 draft 一起原子提交。
          selected = undefined;
        } else {
          selected = items.find((item) => item.id === requestedId) ?? items[0];
        }
        if (cancelled) return;
        const restoredReader = selected === undefined
          ? DEFAULT_READER_SESSION
          : readReaderSession(selected.id);
        setConversations(items);
        setArchivedConversations(archivedResponse.items);
        setProviders(providerResponse.items.filter((item) => item.enabled));
        setWorkMode(restoredReader.workMode);
        setReadingPath(restoredReader.path);
        setReadingPickerPath(null);
        setReadingLocator(restoredReader.locator);
        setReaderOpen(restoredReader.open);
        setConversationId(selected?.id ?? null);
        setRunId(selected?.active_run_id ?? null);
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
    if (conversationId === null) {
      // 把切换前尚未返回的 loadSession 标成过期，否则旧会话内容可能覆盖本地空白页。
      sessionLoadGeneration.current += 1;
      return;
    }
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
    let cancelled = false;
    // cursor 会随每个 SSE 事件推进。直接请求会把一次流式回答放大成几十到上百个
    // context-usage API；短防抖只在事件暂歇时刷新，同时保留终态的最终读数。
    const refresh = window.setTimeout(() => {
      void fetchConversationContextUsage(conversationId)
        .then((response) => {
          if (!cancelled) setContextUsage(response);
        })
        .catch(() => undefined);
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(refresh);
    };
  }, [conversationId, run.cursor]);

  useEffect(() => {
    if (
      conversationId === null
      || (!["done", "partial"].includes(run.phase) && run.artifactEvents.length === 0)
    ) {
      return;
    }
    fetchCoworkArtifacts(conversationId)
      .then((response) => {
        setArtifacts(response.items);
        if (response.items.length > 0) setArtifactRailOpen(true);
      })
      .catch(() => undefined);
  }, [conversationId, run.artifactEvents.length, run.phase]);

  useEffect(() => {
    if (
      conversationId === null ||
      (!["done", "partial", "cancelled", "error"].includes(run.phase))
    ) {
      return;
    }
    fetchConversationMessages(conversationId)
      .then((response) => setMessages(response.items))
      .catch(() => undefined);
  }, [conversationId, run.phase]);

  useEffect(() => {
    if (
      runId === null
      || !["done", "partial", "cancelled", "budget_exceeded", "error"].includes(run.phase)
    ) return;
    const finishedRunId = runId;
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      setConversations((current) => current.map((item) =>
        item.active_run_id === finishedRunId ? { ...item, active_run_id: null } : item
      ));
      setArchivedConversations((current) => current.map((item) =>
        item.active_run_id === finishedRunId ? { ...item, active_run_id: null } : item
      ));
    });
    // run.done 先于可选标题后处理可见；稍后重拉列表，不让标题反向卡住终态。
    const refresh = window.setTimeout(() => {
      void Promise.all([fetchConversations(), fetchConversations(true)])
        .then(([active, archived]) => {
          setConversations(active.items);
          setArchivedConversations(archived.items);
        })
        .catch(() => undefined);
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(refresh);
    };
  }, [run.phase, runId]);

  const steering = run.phase === "connecting" || run.phase === "executing";
  const running = steering || run.phase === "waiting_human" || run.phase === "sleeping";

  const capabilitiesByRoot = useMemo(() => {
    const values = new Map<string, string[]>();
    for (const grant of grants) {
      if (
        !grant.active ||
        grant.session_root_id === null ||
        RETIRED_CAPABILITIES.has(grant.capability)
      ) continue;
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

  const createSession = useCallback(() => {
    sessionLoadGeneration.current += 1;
    window.history.replaceState(null, "", "/cowork?new=1");
    setShowArchived(false);
    setConversationId(null);
    setRunId(null);
    setRoots([]);
    setGrants([]);
    setApprovalRules([]);
    setWorkspaceTrustState([]);
    setArtifacts([]);
    setMemories([]);
    setMessages([]);
    setContextUsage(null);
    setActivePrompt(null);
    setAttachments([]);
    setWorkspaceDraftPath(null);
    // 新任务必须从产品默认模式开始。否则刚读过论文后点“新建任务”，一句 hello 也会
    // 携带旧 PDF 和阅读引用约束，表面看像模型或 citation 故障。
    setWorkMode("office");
    setReadingPath("");
    setReadingPickerPath(null);
    setReadingLocator(1);
    setReaderOpen(true);
    setLocatorRequest(null);
    setArtifactRailOpen(true);
    setMountedKb(null);
    setKnowledgeBaseLoadedFor(null);
    setKnowledgeBaseDraft(null);
    setKnowledgeBaseDraftDirty(false);
    setProviderDraft(null);
    setNotice(null);
  }, []);

  const openConversation = useCallback((item: ConversationSummary) => {
    // loadSession 是并发请求；切换当帧先清掉会话级状态，不能让旧消息/权限在新标题下闪现。
    sessionLoadGeneration.current += 1;
    window.history.replaceState(
      null,
      "",
      `/cowork?conversation=${encodeURIComponent(item.id)}`,
    );
    const restoredReader = readReaderSession(item.id);
    setOpenConversationMenuId(null);
    setConversationId(item.id);
    setRunId(item.active_run_id ?? null);
    setRoots([]);
    setGrants([]);
    setApprovalRules([]);
    setWorkspaceTrustState([]);
    setPersonas([]);
    setArtifacts([]);
    setMemories([]);
    setMessages([]);
    setContextUsage(null);
    setMountedKb(null);
    setKnowledgeBaseLoadedFor(null);
    setActivePrompt(null);
    setAttachments([]);
    setWorkspaceDraftPath(null);
    setWorkMode(restoredReader.workMode);
    setReadingPath(restoredReader.path);
    setReadingPickerPath(null);
    setReadingLocator(restoredReader.locator);
    setReaderOpen(restoredReader.open);
    setArtifactRailOpen(true);
    setKnowledgeBaseDraft(null);
    setKnowledgeBaseDraftDirty(false);
    setProviderDraft(null);
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
          const created = await createConversation("新会话");
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
    if (goal.trim() === "") return;
    if (conversationId !== null && knowledgeBaseLoadedFor !== conversationId) {
      setNotice("正在读取这个会话的知识库挂载状态，请稍后再试。");
      return;
    }
    const prompt = goal.trim();
    const requestedKb = knowledgeBaseDraftDirty ? knowledgeBaseDraft : mountedKb;
    const currentConversation = [...conversations, ...archivedConversations].find(
      (item) => item.id === conversationId,
    );
    const requestedProviderId = conversationId === null
      ? providerDraft
      : currentConversation?.provider_profile_id ?? null;
    const selectedProvider = providers.find((item) => item.id === requestedProviderId);
    if (selectedProvider === undefined) {
      setNotice(
        providers.length === 0
          ? "请先到“模型与密钥”添加模型服务，再开始任务。"
          : "请先在运行设置中选择模型服务。",
      );
      return;
    }
    let targetConversationId = conversationId;
    let createdConversation: ConversationSummary | null = null;
    setBusy(true);
    setNotice(null);
    try {
      if (targetConversationId === null) {
        createdConversation = await createConversation("新会话");
        targetConversationId = createdConversation.id;
        createdConversation = await updateConversationRuntime(targetConversationId, {
          provider_profile_id: selectedProvider.id,
          model_override: selectedProvider.default_model,
          unattended: false,
          approval_mode: "interactive",
          persona_name: "general",
        });
      }
      const runConversationId = targetConversationId;
      // 这次 PUT 不是多余请求：它既提交空白页 draft，也作为 run 前的顺序屏障，保证快速
      // 选择后立刻发送时不会让 POST /runs/cowork 抢在挂载完成之前读取旧值。
      const binding = await setSessionKnowledgeBase(runConversationId, requestedKb);
      if (createdConversation !== null) {
        const created = createdConversation;
        setConversations((current) => [
          created,
          ...current.filter((item) => item.id !== created.id),
        ]);
        setShowArchived(false);
        setConversationId(runConversationId);
        window.history.replaceState(
          null,
          "",
          `/cowork?conversation=${encodeURIComponent(runConversationId)}`,
        );
        setRunId(null);
        setMessages([]);
        setContextUsage(null);
        setActivePrompt(null);
        setProviderDraft(null);
      }
      setMountedKb(binding.slug);
      setKnowledgeBaseDraft(null);
      setKnowledgeBaseDraftDirty(false);
      setKnowledgeBaseLoadedFor(runConversationId);
      if (workspaceDraftPath !== null) {
        await addCoworkRoot(runConversationId, {
          path: workspaceDraftPath,
          access_mode: "read_write",
          label: pathName(workspaceDraftPath),
        });
      }
      if (
        workMode === "reading"
        && readingPickerPath !== null
        && readingPickerPath === readingPath.trim()
      ) {
        const path = parentDirectory(readingPickerPath);
        const alreadyAuthorized = (workspaceDraftPath !== null
          && pathIsWithinDirectory(readingPickerPath, workspaceDraftPath))
          || roots.some((root) => pathIsWithinDirectory(readingPickerPath, root.canonical_path));
        if (!alreadyAuthorized) {
          await addCoworkRoot(runConversationId, {
            path,
            access_mode: "read_only",
            label: pathName(path),
          });
        }
      }
      const uploaded = await Promise.all(
        attachments.map((file) => uploadCoworkAttachment(runConversationId, file)),
      );
      const response = await createCoworkRun({
        conversation_id: runConversationId,
        goal: prompt,
        attachment_ids: uploaded.map((item) => item.id),
        plan_mode: planMode,
        work_mode: workMode,
        reading_path: workMode === "reading" && readingPath.trim() !== "" ? readingPath.trim() : null,
        // 在发送的这一刻读一次阅读器的当前视口。读的是模块级的格子而不是 React state：
        // 视口每滚一下就变，订阅它会让滚动一个像素就重渲染整条消息列表。
        reading_viewport: readingViewportFor(workMode, readerPath) ?? null,
      });
      setStopping(false);
      setActivePrompt(prompt);
      setRunId(response.run_id);
      setConversations((current) => current.map((item) =>
        item.id === response.conversation_id
          ? { ...item, active_run_id: response.run_id }
          : item
      ));
      if (response.conversation_title !== null) {
        setConversations((current) => current.map((item) =>
          item.id === response.conversation_id
            ? { ...item, title: response.conversation_title }
            : item
        ));
      }
      setGoal("");
      setAttachments([]);
      setWorkspaceDraftPath(null);
      await loadSession(runConversationId);
    } catch (reason) {
      // 会话已经创建但后续挂载失败时也要把它留在界面上，不能在服务端留下一个用户看不见
      // 的孤儿会话。knowledgeBaseDraft 保留，用户修好 KB 后可直接重试。
      if (createdConversation !== null && targetConversationId !== null) {
        const created = createdConversation;
        setConversations((current) => [
          created,
          ...current.filter((item) => item.id !== created.id),
        ]);
        setConversationId(targetConversationId);
      }
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [
    attachments,
    archivedConversations,
    conversationId,
    conversations,
    goal,
    knowledgeBaseDraft,
    knowledgeBaseDraftDirty,
    knowledgeBaseLoadedFor,
    loadSession,
    mountedKb,
    planMode,
    providerDraft,
    providers,
    readerPath,
    readingPath,
    readingPickerPath,
    roots,
    workMode,
    workspaceDraftPath,
  ]);

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

  const selectWorkspace = useCallback(async () => {
    if (!isTauriRuntime()) {
      setNotice("工作空间选择只在 WorkPilot 桌面版可用；网页模式仍可使用默认工作区。");
      return;
    }
    try {
      const selected = await pickCoworkDirectory();
      if (selected === null) return;
      setWorkspaceDraftPath(selected);
      setNotice(null);
    } catch (reason) {
      setNotice(readableError(reason));
    }
  }, []);

  const selectReadingDocument = useCallback(async () => {
    if (!isTauriRuntime()) {
      setNotice("系统文档选择器只在 Tauri 桌面版可用，也可以手动填写已授权路径。");
      return;
    }
    try {
      const selected = await pickCoworkReadingFile();
      if (selected === null) return;
      setReadingPath(selected);
      setReadingPickerPath(selected);
      setReadingLocator(1);
    } catch (reason) {
      setNotice(readableError(reason));
    }
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
    async (body: {
      approved?: boolean;
      answer?: string;
      path?: string;
      remember?: "once" | "command" | "target";
    }) => {
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

  const dropApprovalRule = useCallback(async (ruleId: string) => {
    if (conversationId === null) return;
    setBusy(true);
    try {
      await revokeApprovalRule(conversationId, ruleId);
      setApprovalRules((current) => current.filter((item) => item.id !== ruleId));
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversationId]);

  const toggleWorkspaceTrust = useCallback(async (canonicalPath: string, trusted: boolean) => {
    if (conversationId === null) return;
    setBusy(true);
    try {
      const response = await setWorkspaceTrust(conversationId, canonicalPath, trusted);
      setWorkspaceTrustState(response.items);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversationId]);

  const approveDirectoryRequest = useCallback(async () => {
    const selected = await pickCoworkDirectory();
    if (selected === null) {
      if (!isTauriRuntime()) setNotice("目录授权只在 Tauri 桌面版可用。");
      return;
    }
    await respondToInteraction({ approved: true, path: selected });
  }, [respondToInteraction]);

  const submitComposer = steering ? sendSteering : execute;
  const submitFromComposer = () => {
    // 运行设置是编辑任务前的浮层。任务一旦发出就收起，避免它覆盖正在生成的
    // 正文和阶段记录；下一轮仍可从 footer 主动重新打开。
    if (runSettingsMenu.current !== null) runSettingsMenu.current.open = false;
    void submitComposer();
  };
  const requestLocator = useCallback((locator: number) => {
    setLocatorRequest((current) => ({ locator, nonce: (current?.nonce ?? 0) + 1 }));
    setReaderOpen(true);
  }, []);

  const prompts = workMode === "office" ? OFFICE_PROMPTS : READING_PROMPTS;
  const listedConversations = showArchived ? archivedConversations : conversations;
  const activeConversation = [...conversations, ...archivedConversations].find(
    (item) => item.id === conversationId,
  );
  const knowledgeBaseLoading = conversationId !== null
    && knowledgeBaseLoadedFor !== conversationId;
  const selectedKnowledgeBase = knowledgeBaseDraftDirty ? knowledgeBaseDraft : mountedKb;
  const activePersona = personas.find(
    (item) => item.name === (activeConversation?.persona_name ?? "general"),
  );
  const customizedRunSettings = [
    activeConversation?.provider_profile_id ?? providerDraft,
    activeConversation?.persona_name !== undefined && activeConversation.persona_name !== "general",
    workMode !== "office",
    selectedKnowledgeBase,
    planMode,
  ].filter(Boolean).length;
  const conversationArchived = activeConversation?.archived_at !== null
    && activeConversation?.archived_at !== undefined;
  const selectedProviderId = conversationId === null
    ? providerDraft
    : activeConversation?.provider_profile_id ?? null;
  const providerReady = providers.some((item) => item.id === selectedProviderId);

  const toggleApprovalMode = useCallback(async () => {
    if (conversationId === null || activeConversation === undefined || running) return;
    setBusy(true);
    try {
      const updated = await updateConversationRuntime(conversationId, {
        provider_profile_id: activeConversation.provider_profile_id,
        model_override: activeConversation.selected_model,
        unattended: activeConversation.unattended,
        approval_mode: activeConversation.approval_mode === "auto" ? "interactive" : "auto",
        persona_name: activeConversation.persona_name,
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [activeConversation, conversationId, running]);

  // 库列表与本会话的挂载分开取：列表全局共用，挂载跟着会话走。
  useEffect(() => {
    if (authState !== "authenticated") return;
    fetchKnowledgeBases()
      .then((response) => setKnowledgeBases(response.items))
      .catch(() => setKnowledgeBases([]));
  }, [authState]);

  useEffect(() => {
    if (conversationId === null) return;
    let cancelled = false;
    fetchSessionKnowledgeBase(conversationId)
      .then((response) => {
        if (!cancelled) setMountedKb(response.slug);
      })
      .catch(() => {
        if (!cancelled) setMountedKb(null);
      })
      .finally(() => {
        if (!cancelled) setKnowledgeBaseLoadedFor(conversationId);
      });
    return () => { cancelled = true; };
  }, [conversationId]);

  const selectKnowledgeBase = useCallback(async (slug: string) => {
    const next = slug === "" ? null : slug;
    if (conversationId === null) {
      setKnowledgeBaseDraft(next);
      setKnowledgeBaseDraftDirty(true);
      return;
    }
    if (knowledgeBaseLoadedFor !== conversationId) return;
    const previous = mountedKb;
    setMountedKb(next);
    setBusy(true);
    try {
      const updated = await setSessionKnowledgeBase(conversationId, next);
      if (conversationIdRef.current === conversationId) {
        setMountedKb(updated.slug);
        setKnowledgeBaseDraft(null);
        setKnowledgeBaseDraftDirty(false);
      }
    } catch (reason) {
      if (conversationIdRef.current === conversationId) setMountedKb(previous);
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [conversationId, knowledgeBaseLoadedFor, mountedKb]);

  const selectProvider = useCallback(async (providerId: string) => {
    if (running || providerId === "") return;
    if (conversationId === null) {
      setProviderDraft(providerId);
      setNotice(null);
      return;
    }
    setBusy(true);
    try {
      const selected = providers.find((item) => item.id === providerId);
      const updated = await updateConversationRuntime(conversationId, {
        provider_profile_id: providerId,
        model_override: selected?.default_model ?? null,
        unattended: activeConversation?.unattended ?? false,
        // 换 Provider 不该顺带改动自主权上限：这里必须原样带回当前值，
        // 漏掉它就等于每次换模型都把免审批悄悄关掉。
        approval_mode: activeConversation?.approval_mode ?? "interactive",
        persona_name: activeConversation?.persona_name ?? "general",
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      fetchConversationContextUsage(conversationId).then(setContextUsage).catch(() => undefined);
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [activeConversation, conversationId, providers, running]);

  const selectPersona = useCallback(async (personaName: string) => {
    if (conversationId === null || activeConversation === undefined || running) return;
    const selected = personas.find((item) => item.name === personaName);
    if (selected === undefined) return;
    setBusy(true);
    try {
      const updated = await updateConversationRuntime(conversationId, {
        provider_profile_id: activeConversation.provider_profile_id,
        model_override: activeConversation.selected_model,
        unattended: activeConversation.unattended,
        approval_mode: activeConversation.approval_mode,
        persona_name: selected.name,
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      setWorkMode(selected.recommended_work_mode);
      const connectors = selected.recommended_connectors.join("、");
      setNotice(
        `已切换为“${selected.label}”，默认审批档与工具面从下一轮生效。`
        + (connectors === "" ? "" : ` 推荐连接器：${connectors}。`),
      );
    } catch (reason) {
      setNotice(readableError(reason));
    } finally {
      setBusy(false);
    }
  }, [activeConversation, conversationId, personas, running]);

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
        approval_mode: activeConversation.approval_mode,
        persona_name: activeConversation.persona_name,
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
  // token 对不上就当作 once：换了一条请求，上一条的选择立刻失效，不需要等一帧。
  const rememberScope =
    remember.token === (run.interrupt?.resume_token ?? "") ? remember.scope : "once";
  const setRememberScope = (scope: "once" | "command" | "target") => {
    setRemember({ token: run.interrupt?.resume_token ?? "", scope });
  };
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
  const isTeamProposal =
    run.interrupt?.kind === "external_approval" && interactionPayload.tool === "propose_team";
  const proposedTeamMembers = isTeamProposal ? teamProposalMembers(interactionPayload) : [];
  const proposedTeamArgs =
    typeof interactionPayload.arguments === "object"
      && interactionPayload.arguments !== null
      && !Array.isArray(interactionPayload.arguments)
      ? interactionPayload.arguments as Record<string, unknown>
      : {};
  const teamProposalNote =
    typeof proposedTeamArgs.note === "string" ? proposedTeamArgs.note : "";
  const runAnswer = useSmoothStreamText(run.answer, steering);
  const hasConversation = messages.length > 0 || runId !== null;
  const hasComposerMaterials = attachments.length > 0;
  const activeWorkspace = roots[0] ?? null;
  const workspacePath = workspaceDraftPath ?? activeWorkspace?.canonical_path ?? null;
  const workspaceLabel = workspacePath === null
    ? "WorkPilot 默认文件夹"
    : workspaceDraftPath === null && activeWorkspace?.label === "WorkPilot 默认文件夹"
      ? activeWorkspace.label
      : pathName(workspacePath);
  const { containerRef: chatScrollRef, handleScroll: handleChatScroll } = useCoworkAutoScroll({
    scopeKey: conversationId,
    hasConversation,
    streaming: steering,
    contentKey: runAnswer,
    eventCount: run.steps.length + run.todos.length + run.artifactEvents.length,
  });
  useLayoutEffect(() => {
    const input = composerInput.current;
    if (input === null) return;
    // WKWebView 从系统文件选择器返回时会恢复焦点并重新做一次表单布局；如果这时沿用
    // 选择文件前的内联高度，空 textarea 偶尔会拿到 flex 容器的剩余高度，把 composer
    // 撑满整个窗口。每当素材托盘变化都从内容高度重新收敛，并用 CSS 的 min/max 作硬边界。
    input.style.height = "auto";
    const styles = window.getComputedStyle(input);
    const minimum = Number.parseFloat(styles.minHeight) || 0;
    const maximum = Number.parseFloat(styles.maxHeight) || (hasConversation ? 196 : 240);
    const nextHeight = Math.max(minimum, Math.min(input.scrollHeight, maximum));
    input.style.height = `${nextHeight}px`;
  }, [attachments.length, goal, hasConversation]);
  // 模型刚打开过的那份优先于输入框里填的：`reader_goto` 反映的是它此刻正在给你看什么。
  /**
   * 用户在阅读器里划了一段并点了"问这一段"。
   *
   * 只把引文放进输入框、把焦点交回去，**不替他发送**：他多半还要在后面接一句自己的
   * 问题。模型之所以知道"这段"指哪里，靠的是随请求一起走的 `reading_viewport`，
   * 不是这段文字本身——所以这里塞的是上下文，不是指令。
   */
  const askAboutSelection = useCallback((quote: string, locator: number) => {
    setGoal((current) => {
      const cited = `关于第 ${locator} 处的这段：“${quote}”\n`;
      return current.startsWith(cited) ? current : cited + current;
    });
  }, []);

  const saveInspectorWidth = useCallback((requestedWidth: number) => {
    const nextWidth = clampInspectorWidth(requestedWidth);
    setInspectorWidth(nextWidth);
    window.localStorage.setItem(INSPECTOR_WIDTH_STORAGE_KEY, String(Math.round(nextWidth)));
  }, []);

  const startInspectorResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    inspectorDrag.current = {
      currentWidth: inspectorWidth,
      pointerId: event.pointerId,
      startWidth: inspectorWidth,
      startX: event.clientX,
    };
    setResizingInspector(true);
  }, [inspectorWidth]);

  const moveInspectorResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = inspectorDrag.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    const nextWidth = clampInspectorWidth(drag.startWidth + drag.startX - event.clientX);
    drag.currentWidth = nextWidth;
    setInspectorWidth(nextWidth);
  }, []);

  const finishInspectorResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = inspectorDrag.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    inspectorDrag.current = null;
    setResizingInspector(false);
    saveInspectorWidth(drag.currentWidth);
  }, [saveInspectorWidth]);

  const resizeInspectorWithKeyboard = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    let requestedWidth: number | null = null;
    if (event.key === "ArrowLeft") requestedWidth = inspectorWidth + (event.shiftKey ? 80 : 24);
    if (event.key === "ArrowRight") requestedWidth = inspectorWidth - (event.shiftKey ? 80 : 24);
    if (event.key === "Home") requestedWidth = MIN_INSPECTOR_WIDTH;
    if (event.key === "End") requestedWidth = MAX_INSPECTOR_WIDTH;
    if (requestedWidth === null) return;
    event.preventDefault();
    saveInspectorWidth(requestedWidth);
  }, [inspectorWidth, saveInspectorWidth]);

  const readerVisible = readerOpen && hasConversation && workMode === "reading" && readerPath !== "";
  const artifactRailVisible = workMode === "office" && artifactRailOpen && artifacts.length > 0;
  const inspectorVisible = readerVisible || artifactRailVisible;
  const shellStyle = { "--workdesk-inspector-width": `${inspectorWidth}px` } as CSSProperties;
  const visibleMessages =
    runId === null ? messages : messages.filter((message) => message.run_id !== runId);
  const currentPromptMessage = messages.find(
    (message) => message.run_id === runId && message.role === "user",
  );
  const currentPrompt = activePrompt ?? currentPromptMessage?.content ?? null;

  return (
    <main className="cowork-frame workdesk-shell" style={shellStyle}>
      <aside className="workdesk-sidebar">
        <div className="workdesk-sidebar-head">
          <Link className="workdesk-brand" href="/cowork">
            <span><WorkdeskIcon name="spark" /></span>
            <div><strong>WorkPilot</strong><small>Local Cowork</small></div>
          </Link>
          <button aria-label="搜索任务" className="workdesk-icon-button" type="button"><WorkdeskIcon name="search" /></button>
        </div>

        <WorkdeskNavigation
          newTaskDisabled={busy}
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
              <div
                className={`workdesk-task-row${item.id === conversationId ? " active" : ""}`}
                data-conversation-id={item.id}
                key={item.id}
              >
                <button
                  className="workdesk-task-select"
                  onClick={() => openConversation(item)}
                  type="button"
                >
                  <span>{item.title ?? "Cowork 任务"}</span>
                  <small>{
                    item.id === conversationId
                      ? "当前"
                      : item.active_run_id !== null
                        ? "执行中"
                        : new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })
                  }</small>
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
        {inspectorVisible && (
          <div
            aria-label="调整右侧预览宽度"
            aria-orientation="vertical"
            aria-valuemax={MAX_INSPECTOR_WIDTH}
            aria-valuemin={MIN_INSPECTOR_WIDTH}
            aria-valuenow={Math.round(inspectorWidth)}
            className={`workdesk-pane-resizer${resizingInspector ? " is-dragging" : ""}`}
            onDoubleClick={() => saveInspectorWidth(DEFAULT_INSPECTOR_WIDTH)}
            onKeyDown={resizeInspectorWithKeyboard}
            onLostPointerCapture={finishInspectorResize}
            onPointerCancel={finishInspectorResize}
            onPointerDown={startInspectorResize}
            onPointerMove={moveInspectorResize}
            onPointerUp={finishInspectorResize}
            role="separator"
            tabIndex={0}
            title="拖动调整预览宽度，双击恢复默认"
          />
        )}
        <header className="workdesk-topline">
          <div><span className={authState === "authenticated" ? "online" : ""} />{authState === "authenticated" ? "本地 Agent 已连接" : "正在连接本地 Agent"}</div>
          <span className="workdesk-default-scope" title={workspacePath ?? "~/Documents/WorkPilot"}><WorkdeskIcon name="folder" /><span>{workspaceLabel}</span></span>
          <p>{activeConversation?.title ?? "新任务"}</p>
          {workMode === "reading" && hasConversation && !readerVisible && readerPath !== "" && (
            <button className="workdesk-reader-reopen" onClick={() => setReaderOpen(true)} type="button">
              <WorkdeskIcon name="file" />打开阅读器
            </button>
          )}
          {workMode === "office" && artifacts.length > 0 && !artifactRailOpen && (
            <button className="workdesk-artifact-reopen" onClick={() => setArtifactRailOpen(true)} type="button">
              <WorkdeskIcon name="file" />{artifacts.length} 个交付物
            </button>
          )}
        </header>

        {authState !== "authenticated" ? (
          <section className="workdesk-connect-state">
            <span><WorkdeskIcon name="spark" /></span>
            <h1>{startupError !== null ? "WorkPilot 启动失败" : authState === "unknown" ? "正在唤醒 WorkPilot" : "等待 owner 身份"}</h1>
            <p>{startupError ?? "桌面端会自动连接本机 sidecar，并用本次启动令牌建立私有工作会话。"}</p>
          </section>
        ) : (
          <div
            className={`workdesk-stage${hasConversation ? " is-chat" : ""}`}
            data-chat-scroll-root={hasConversation ? undefined : "true"}
            onScroll={hasConversation ? undefined : handleChatScroll}
            ref={hasConversation ? undefined : chatScrollRef}
          >
            {!hasConversation ? (
              <>
                <section className="workdesk-welcome">
                  <div className="workdesk-orbit"><WorkdeskIcon name="spark" /></div>
                  <h1>WorkPilot，我帮你</h1>
                  <div className="workdesk-mode-switch" role="tablist" aria-label="任务模式">
                    <button aria-selected={workMode === "office"} onClick={() => setWorkMode("office")} role="tab" type="button">日常办公</button>
                    <button aria-selected={workMode === "reading"} onClick={() => setWorkMode("reading")} role="tab" type="button">论文阅读</button>
                  </div>
                  {workMode === "reading" && (
                    <div className="workdesk-reading-picker">
                      <WorkdeskIcon name="file" />
                      <input
                        aria-label="要阅读的文档路径"
                        onChange={(event) => {
                          setReadingPath(event.target.value);
                          setReadingPickerPath(null);
                          setReadingLocator(1);
                        }}
                        placeholder="要读的文档，例如 papers/attention.pdf"
                        type="text"
                        value={readingPath}
                      />
                      <button disabled={!desktopReady} onClick={() => void selectReadingDocument()} type="button">选择文档</button>
                      {/* 不填也能提问：模型会明确说"还没打开文档"，而不是凭对同名论文的印象作答。 */}
                      <small>{readingPath.trim() === "" ? "还没选文档" : "按 locator 引用，答案可回溯到页"}</small>
                    </div>
                  )}
                </section>
                <div className="workdesk-prompt-chips" aria-label="快捷任务">
                  {prompts.map((item) => <button key={item.label} onClick={() => setGoal(item.prompt)} type="button"><WorkdeskIcon name="file" />{item.label}</button>)}
                </div>
              </>
            ) : (
              <section
                aria-label="Cowork 对话"
                className="workdesk-chat-thread"
                data-chat-scroll-root="true"
                onScroll={handleChatScroll}
                ref={chatScrollRef}
              >
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
                      {message.role === "assistant" ? (
                        <>
                          {message.run_id !== null && <HistoricalRunStageHistory runId={message.run_id} />}
                          <AnswerMarkdown onSelectLocator={workMode === "reading" ? requestLocator : undefined} text={message.content} />
                        </>
                      ) : <p>{message.content}</p>}
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
                          <div className="workdesk-run-identity"><strong>WorkPilot</strong></div>
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

                        <RunStageHistory stages={run.modelStages} />

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

                        <RunActivityPanel
                          finishedAt={run.finishedAt}
                          phase={run.phase}
                          progressSummary={run.progressSummary}
                          running={running}
                          startedAt={run.startedAt}
                          steps={run.steps}
                          subagentRuns={run.subagentRuns}
                          team={run.team}
                          todos={run.todos}
                        />

                        {run.sleepingUntil !== null && run.phase === "sleeping" && (
                          <section className="workdesk-inbox-card" aria-live="polite">
                            <div className="workdesk-inbox-eyebrow"><WorkdeskIcon name="spark" /><span>休眠中</span></div>
                            <h3>已挂起，{new Date(run.sleepingUntil).toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "numeric", day: "numeric" })} 自动继续</h3>
                            <p>不需要你操作，到点会从这里接着做，上下文原样保留。想提前结束就停止这次运行。</p>
                          </section>
                        )}

                        {run.waivedApprovals.map((waived, index) => (
                          <div className="workdesk-waived-note" key={`${waived.tool}:${index}`}>
                            <WorkdeskIcon name="shield" />
                            <span>
                              没有向你确认就执行了 <code>{waived.command ?? waived.tool}</code>：
                              {waived.reason === "approval_mode=auto"
                                ? "这个会话开着免审批。"
                                : waived.reason === "workspace_trust"
                                  ? `这个目录被你信任过，且它的白名单里有 ${waived.allowlist_entry ?? ""}。`
                                  : "命中了一条你之前留下的“不再询问”规则。"}
                              可以在“默认权限”里撤销。
                            </span>
                          </div>
                        ))}

                        {run.reasoning.trim() !== "" && (
                          // 当前阶段实时展开；阶段结束时 reducer 会把它固化进上方阶段记录。
                          <details className="workdesk-run-reasoning" open>
                            <summary>思考中…</summary>
                            <p>{run.reasoning.trim()}</p>
                          </details>
                        )}

                        {(runAnswer !== "" || run.error !== null) && (
                          <div className={`workdesk-run-answer${run.phase === "budget_exceeded" ? " budget" : run.error !== null ? " error" : run.phase === "cancelled" ? " cancelled" : run.phase === "partial" ? " partial" : ""}`}>
                            {run.error !== null && <p role="alert">{run.error}</p>}
                            {runAnswer !== "" && <AnswerMarkdown onSelectLocator={workMode === "reading" ? requestLocator : undefined} text={runAnswer} />}
                          </div>
                        )}

                      </div>
                    </article>
                  </>
                )}
              </section>
            )}

            <section className={`workdesk-composer${conversationArchived ? " is-archived" : ""}${hasComposerMaterials ? " has-materials" : ""}`} aria-label="创建 Cowork 任务">
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
              {!hasConversation && (
                <div className="workdesk-session-setup" aria-label="会话工作空间">
                  <span>工作空间</span>
                  <button
                    aria-label="选择工作空间"
                    disabled={busy || conversationArchived || !desktopReady}
                    onClick={() => void selectWorkspace()}
                    title={desktopReady ? workspacePath ?? "未选择时使用 ~/Documents/WorkPilot" : "请在 WorkPilot 桌面版中选择工作空间"}
                    type="button"
                  >
                    <WorkdeskIcon name="folder" />
                    <span>
                      <strong>{workspaceLabel}</strong>
                      <small>{workspacePath ?? "未选择时使用 ~/Documents/WorkPilot"}</small>
                    </span>
                    <b>{workspaceDraftPath === null ? "选择" : "更换"}</b>
                  </button>
                  {workspaceDraftPath !== null && (
                    <button
                      aria-label="恢复默认工作空间"
                      className="clear"
                      disabled={busy}
                      onClick={() => setWorkspaceDraftPath(null)}
                      title="恢复 WorkPilot 默认文件夹"
                      type="button"
                    >×</button>
                  )}
                </div>
              )}
              {run.interrupt !== null && run.interrupt.kind !== "write_confirm" && (
                <section className="workdesk-inbox-card workdesk-composer-interaction" aria-live="polite">
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
                      {typeof interactionPayload.standing_argv_pattern === "string" && (
                        <label className="workdesk-remember-toggle">
                          <input checked={rememberScope === "command"} disabled={responding} onChange={(event) => setRememberScope(event.target.checked ? "command" : "once")} type="checkbox" />
                          <span>以后仅相同完整 argv 与工作目录的 <code>{shellCommand}</code> 不用再问</span>
                        </label>
                      )}
                      <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">拒绝</button><button className="primary danger" disabled={responding} onClick={() => void respondToInteraction({ approved: true, remember: rememberScope })} type="button">{rememberScope === "command" ? "批准并记住" : "批准并运行一次"}</button></div>
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
                  ) : isTeamProposal ? (
                    <>
                      <div className="workdesk-team-proposal-heading">
                        <div>
                          <h3>组建一支 Agent Team？</h3>
                          <p>批准后会预创建 {proposedTeamMembers.length} 个独立持久 Worker Session。</p>
                        </div>
                        <span>{proposedTeamMembers.length}/{4} workers</span>
                      </div>
                      <div className="workdesk-team-roster" aria-label="拟议的 Worker roster">
                        {proposedTeamMembers.map((member, index) => (
                          <article key={`${member.name}:${index}`}>
                            <b>{member.name.slice(0, 1).toUpperCase()}</b>
                            <div>
                              <strong>{member.name}</strong>
                              <p>{member.role}</p>
                              {member.reason !== "" && <small>{member.reason}</small>}
                            </div>
                            <em>待创建</em>
                          </article>
                        ))}
                      </div>
                      {teamProposalNote !== "" && <p className="workdesk-team-note">{teamProposalNote}</p>}
                      <div className="workdesk-team-boundary">
                        <strong>隔离边界</strong>
                        <span>Worker 不继承当前对话历史，只通过 Board 接收任务描述、验收标准与资源范围。</span>
                      </div>
                      <small>创建 Session 本身不调用模型；Worker 首次收到 Board assignment 时才开始计费。团队编制不能被自动模式或常驻规则跳过。</small>
                      <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">暂不组建</button><button className="primary" disabled={responding || proposedTeamMembers.length === 0} onClick={() => void respondToInteraction({ approved: true, remember: "once" })} type="button">批准并创建团队</button></div>
                    </>
                  ) : run.interrupt.kind === "external_approval" ? (
                    <>
                      <h3>允许执行这次外部动作？</h3>
                      <p>{typeof interactionPayload.warning === "string" ? interactionPayload.warning : "该工具会修改外部系统。"}</p>
                      <pre className="workdesk-shell-command"><code>{JSON.stringify(interactionPayload.arguments ?? {}, null, 2)}</code></pre>
                      <small>工具：{typeof interactionPayload.tool === "string" ? interactionPayload.tool : "外部工具"}</small>
                      <div className="workdesk-remember-choices" role="radiogroup" aria-label="记住这次批准的范围">
                        <label><input checked={rememberScope === "once"} disabled={responding} name="remember" onChange={() => setRememberScope("once")} type="radio" /><span>只这一次</span></label>
                        {typeof interactionPayload.standing_action_target === "string" && (
                          <label><input checked={rememberScope === "target"} disabled={responding} name="remember" onChange={() => setRememberScope("target")} type="radio" /><span>相同动作与目标不用再问</span></label>
                        )}
                      </div>
                      <small>记住的范围只对当前会话有效，之后可以在“默认权限”里撤销。它省掉的是“再问一次”，不会放大这个会话已有的能力。</small>
                      <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">拒绝</button><button className="primary danger" disabled={responding} onClick={() => void respondToInteraction({ approved: true, remember: rememberScope })} type="button">{rememberScope === "once" ? "批准一次" : "批准并记住"}</button></div>
                    </>
                  ) : (
                    <>
                      <h3>授予“{CAPABILITY_LABELS[requestedCapability] ?? requestedCapability}”能力？</h3>
                      <p>{interactionReason}</p>
                      {typeof interactionPayload.resource_scope === "string" && <small>网络范围：{interactionPayload.resource_scope}</small>}
                      <small>授权只绑定当前 Cowork 会话，之后可以随时收回。</small>
                      <div className="workdesk-inbox-actions"><button disabled={responding} onClick={() => void respondToInteraction({ approved: false })} type="button">不允许</button><button className="primary" disabled={responding} onClick={() => void respondToInteraction({ approved: true })} type="button">允许并继续</button></div>
                    </>
                  )}
                </section>
              )}
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
                className="workdesk-goal-input"
                disabled={run.phase === "waiting_human" || responding || conversationArchived}
                id="cowork-goal"
                maxLength={4000}
                onChange={(event) => setGoal(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submitFromComposer();
                }}
                onDrop={(event) => {
                  if (steering || event.dataTransfer.files.length === 0) return;
                  event.preventDefault();
                  addAttachments(event.dataTransfer.files);
                }}
                onDragOver={(event) => {
                  if (!steering && event.dataTransfer.types.includes("Files")) event.preventDefault();
                }}
                placeholder={conversationArchived ? "此会话已归档，恢复后可以继续" : run.phase === "waiting_human" ? "请先处理输入框中的确认请求" : steering ? "补充要求或调整方向…" : hasConversation ? "继续这段对话，或交代一个新任务…" : "今天帮你做些什么？可以直接提问、上传资料，或交代一项任务"}
                ref={composerInput}
                rows={hasConversation ? 2 : 4}
                value={goal}
              />
              <div className="workdesk-composer-actions">
                <button aria-label="添加只读资料副本" disabled={busy || running || conversationArchived} onClick={() => attachmentInput.current?.click()} title={conversationArchived ? "恢复会话后可添加资料" : running ? "运行期间暂不支持追加资料" : "上传图片、PDF 或文本的私有只读副本"} type="button"><WorkdeskIcon name="add" /></button>
                <span className="workdesk-composer-status">{conversationArchived ? "归档会话 · 只读" : run.phase === "waiting_human" ? "请先处理输入框中的请求" : steering ? "发送后将在安全边界转向" : !providerReady ? providers.length === 0 ? "请先配置模型服务" : "请选择模型服务" : workspaceDraftPath !== null ? `将在 ${workspaceLabel} 中工作 · 可读写` : attachments.length > 0 ? `已添加 ${attachments.length} 份只读资料` : planMode ? "计划模式 · 先出方案等你批准" : "Agent 已就绪"}</span>
                <div className="workdesk-composer-primary-tools">
                  {conversationId !== null && <ContextUsageMeter draft={goal} usage={contextUsage} />}
                  <button aria-label={steering ? "追加运行指令" : "开始执行任务"} className="workdesk-send" disabled={busy || conversationArchived || run.phase === "waiting_human" || (!steering && (knowledgeBaseLoading || !providerReady)) || goal.trim() === ""} onClick={submitFromComposer} type="button"><WorkdeskIcon name="send" /></button>
                </div>
              </div>
              <footer>
                <details className="workdesk-run-settings" name="composer-menu" ref={runSettingsMenu}>
                  <summary>
                    <WorkdeskIcon name="spark" />
                    <span>运行设置</span>
                    <small>{activePersona?.label ?? "通用执行"}</small>
                    <b>{customizedRunSettings > 0 ? customizedRunSettings : "⌄"}</b>
                  </summary>
                  <div>
                    <header><span>RUN SETTINGS</span><h3>这一条任务怎么运行</h3><p>这里调整执行方式，不会扩大目录、能力或审批边界。</p></header>
                    <label className="workdesk-run-setting-field">
                      <span><strong>模型服务</strong><small>按会话选择 Provider</small></span>
                      <select aria-label="模型服务" disabled={busy || running || conversationArchived} onChange={(event) => void selectProvider(event.target.value)} value={selectedProviderId ?? ""}>
                        <option disabled value="">{providers.length === 0 ? "尚未配置模型服务" : "请选择模型服务"}</option>
                        {providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
                      </select>
                      {providers.length === 0 && <a className="workdesk-model-setup-link" href="/providers">前往“模型与密钥”配置</a>}
                    </label>
                    {activeConversation?.provider_profile_id !== null && activeConversation?.provider_profile_id !== undefined && (
                      <label className="workdesk-run-setting-field">
                        <span><strong>会话模型</strong><small>默认使用你在 Provider 中配置的模型 ID</small></span>
                        <input aria-label="具体模型" defaultValue={activeConversation.selected_model ?? ""} disabled={busy || running} key={`${activeConversation.id}:${activeConversation.selected_model ?? ""}`} onBlur={(event) => void saveModelOverride(event.target.value)} />
                      </label>
                    )}
                    <label className="workdesk-run-setting-field">
                      <span><strong>执行角色</strong><small>组合提示词与工具面</small></span>
                      <select aria-label="执行角色" disabled={busy || running || conversationArchived} onChange={(event) => void selectPersona(event.target.value)} value={activeConversation?.persona_name ?? "general"}>
                        {personas.map((persona) => (
                          <option key={`${persona.origin}:${persona.name}`} value={persona.name}>
                            {persona.label}{persona.origin === "project" ? "（项目）" : persona.origin === "user" ? "（自定义）" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="workdesk-run-setting-field">
                      <span><strong>工作模式</strong><small>日常办公或带定位的阅读流程</small></span>
                      <select aria-label="工作模式" disabled={busy || running || conversationArchived} onChange={(event) => setWorkMode(event.target.value as CoworkWorkMode)} value={workMode}>
                        <option value="office">日常办公</option>
                        <option value="reading">论文阅读</option>
                      </select>
                    </label>
                    {workMode === "reading" && (
                      <div className="workdesk-run-setting-field workdesk-reading-setting">
                        <span><strong>阅读文档</strong><small>相对默认工作区或已授权的绝对路径</small></span>
                        <div>
                          <input aria-label="要阅读的文档路径" disabled={busy || running || conversationArchived} onChange={(event) => { setReadingPath(event.target.value); setReadingPickerPath(null); setReadingLocator(1); }} placeholder="papers/attention.pdf" value={readingPath} />
                          <button disabled={busy || running || conversationArchived || !desktopReady} onClick={() => void selectReadingDocument()} type="button">选择</button>
                        </div>
                      </div>
                    )}
                    {/* 挂载是会话级的，运行中也可改；当前 run 已冻结，修改只影响下一轮。 */}
                    <label className="workdesk-run-setting-field">
                      <span><strong>知识库</strong><small>新任务开始时自动预检索</small></span>
                      <select aria-label="会话知识库" disabled={busy || knowledgeBaseLoading || conversationArchived} onChange={(event) => void selectKnowledgeBase(event.target.value)} value={knowledgeBaseLoading ? "" : selectedKnowledgeBase ?? ""}>
                        <option value="">不挂知识库</option>
                        {knowledgeBases.map((item) => (
                          <option key={item.slug} value={item.slug}>
                            {item.name}{item.is_indexed ? "" : "（未建索引）"}
                          </option>
                        ))}
                        {selectedKnowledgeBase !== null && !knowledgeBases.some((item) => item.slug === selectedKnowledgeBase) && (
                          <option value={selectedKnowledgeBase}>{selectedKnowledgeBase}（已失效）</option>
                        )}
                      </select>
                    </label>
                    <div className="workdesk-run-setting-toggle">
                      <span><strong>先出计划</strong><small>先调研并提交方案，批准后再执行</small></span>
                      <button aria-checked={planMode} aria-label="先出计划再执行" className={`workdesk-plan-toggle${planMode ? " is-on" : ""}`} disabled={busy || running || conversationArchived || steering} onClick={() => setPlanMode((current) => !current)} role="switch" type="button"><WorkdeskIcon name="shield" /><span>{planMode ? "已开启" : "关闭"}</span></button>
                    </div>
                  </div>
                </details>
                <details className="workdesk-permission-menu" name="composer-menu">
                  <summary><WorkdeskIcon name="shield" /><span>默认权限</span><b>⌄</b></summary>
                  <div>
                    <h3>默认权限</h3>
                    <p>普通任务可以直接开始，新生成的文件默认保存在本机 ~/Documents/WorkPilot。读取其他本机目录、运行 Shell 或操作外部系统时，WorkPilot 会在需要的那一步单独向你确认。</p>
                    {roots.length > 0 && <h4>本次会话已授权目录</h4>}
                    {roots.map((root) => (
                      <article key={root.id}><div><strong>{root.label}</strong><small title={root.canonical_path}>{shortPath(root.canonical_path)} · {(capabilitiesByRoot.get(root.id) ?? []).join(" · ")}</small></div><button disabled={busy || running} onClick={() => void removeRoot(root.id)} type="button">收回</button></article>
                    ))}

                    <h4>自主权上限</h4>
                    <p className="workdesk-permission-note">
                      {activeConversation?.approval_mode === "auto"
                        ? "这个会话当前不逐次询问写入与命令。目录与能力边界仍然生效——免审批省掉的是“再问一次”，不是权限本身。"
                        : "写入文件和运行命令时会逐次问你。改成免审批可以让无人值守任务不中断，但那意味着你事后才会看到发生了什么。"}
                    </p>
                    <button
                      aria-checked={activeConversation?.approval_mode === "auto"}
                      className={`workdesk-approval-toggle${activeConversation?.approval_mode === "auto" ? " is-on" : ""}`}
                      disabled={busy || running || conversationArchived}
                      onClick={() => void toggleApprovalMode()}
                      role="switch"
                      type="button"
                    >
                      <WorkdeskIcon name="shield" />
                      <span>{activeConversation?.approval_mode === "auto" ? "免审批已开启" : "逐次审批（推荐）"}</span>
                    </button>

                    {approvalRules.length > 0 && <h4>不再询问的动作</h4>}
                    {approvalRules.map((rule) => (
                      <article key={rule.id}>
                        <div>
                          <strong>
                            {rule.match_kind === "argv_pattern"
                              ? `${rule.tool} · 完整 argv`
                              : rule.match_kind === "action_target"
                                ? `${rule.tool} · 指定动作与目标`
                                : `${rule.tool} · 已停用的旧规则`}
                          </strong>
                          <small>{rule.scope === "schedule" ? "只在这条定时计划的运行里生效" : "本会话内生效"}</small>
                        </div>
                        <button disabled={busy} onClick={() => void dropApprovalRule(rule.id)} type="button">撤销</button>
                      </article>
                    ))}

                    {workspaceTrust.length > 0 && <h4>仓库自带的命令白名单</h4>}
                    {workspaceTrust.map((entry) => (
                      <article key={entry.canonical_path}>
                        <div>
                          <strong title={entry.canonical_path}>{shortPath(entry.canonical_path)}</strong>
                          <small>
                            {entry.config_error !== null
                              ? `配置有问题：${entry.config_error}`
                              : entry.declared.length === 0
                                ? "这个目录没有声明 .workpilot/config.toml"
                                : `声明了 ${entry.declared.join("、")}${entry.rejected.length > 0 ? `；已忽略 ${entry.rejected.length} 条` : ""}`}
                          </small>
                        </div>
                        <button
                          disabled={busy || entry.declared.length === 0}
                          onClick={() => void toggleWorkspaceTrust(entry.canonical_path, !entry.trusted)}
                          type="button"
                        >
                          {entry.trusted ? "撤销信任" : "信任这个目录"}
                        </button>
                      </article>
                    ))}
                  </div>
                </details>
                <details className="workdesk-permission-menu workdesk-memory-menu" name="composer-menu">
                  <summary><WorkdeskIcon name="more" /><span>记忆</span><b>{memories.length > 0 ? memories.length : "⌄"}</b></summary>
                  <div>
                    <h3>长期记忆</h3>
                    <p>这些事实会在每条消息开始时注入给模型，面板里看到的就是它看到的；改动从下一条消息起生效。global 对所有会话有效，workspace 只对当前工作目录有效，conversation 只在本次会话有效。</p>
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
                <span>{steering ? "⌘ Enter 追加指令" : "⌘ Enter 发送"}</span>
              </footer>
            </section>
          </div>
        )}
      </section>

      {artifactRailVisible && (
        <ArtifactRail artifacts={artifacts} onClose={() => setArtifactRailOpen(false)} />
      )}

      {readerVisible && conversationId !== null && (
        // key 绑路径：换文档直接重挂载，而不是在 effect 里逐个字段复位——漏一个就会出现
        // "新文档、旧高亮"这种没人会去测的组合。
        <ReaderPane
          annotated={run.readerAnnotation}
          conversationId={conversationId}
          jump={run.readerJump}
          key={readerPath}
          initialLocator={readingLocator}
          onAskSelection={askAboutSelection}
          onClose={() => setReaderOpen(false)}
          onLocatorChange={setReadingLocator}
          path={readerPath}
          requestedLocator={locatorRequest}
        />
      )}
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
