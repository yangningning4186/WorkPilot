"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { AdminSessionControl, useAdminSession } from "@/components/admin-session";
import {
  fetchConversations,
  fetchCoworkRoots,
  type ConversationSummary,
  type CoworkRoot,
} from "@/lib/api";

export type WorkdeskIconName =
  | "add"
  | "agent"
  | "automation"
  | "archive"
  | "dots"
  | "file"
  | "folder"
  | "mcp"
  | "more"
  | "search"
  | "send"
  | "shield"
  | "skill"
  | "spark"
  | "stop"
  | "restore"
  | "trash";

export function WorkdeskIcon({ name }: { name: WorkdeskIconName }) {
  return (
    <svg aria-hidden="true" fill="none" viewBox="0 0 24 24">
      {name === "add" && <><circle cx="12" cy="12" r="8.5" /><path d="M12 8v8M8 12h8" /></>}
      {name === "agent" && <><path d="M5 7.5A3.5 3.5 0 0 1 8.5 4h7A3.5 3.5 0 0 1 19 7.5v5a3.5 3.5 0 0 1-3.5 3.5H11l-4.5 3v-3.7A3.5 3.5 0 0 1 5 12.5z" /><path d="M9 9.5h6M9 12.5h4" /></>}
      {name === "automation" && <><circle cx="12" cy="12" r="7.5" /><path d="M12 8v4l2.5 2M5.8 4.8 4 6.6M18.2 4.8 20 6.6" /></>}
      {name === "archive" && <><path d="M4 7.5h16v12H4zM3.5 4.5h17v3h-17z" /><path d="M9 11h6" /></>}
      {name === "dots" && <><circle cx="6" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="18" cy="12" r="1" /></>}
      {name === "file" && <><path d="M7 3.5h6l4 4v13H7z" /><path d="M13 3.5v4h4M9.5 12h5M9.5 15h5" /></>}
      {name === "folder" && <path d="M3.5 7.5A2.5 2.5 0 0 1 6 5h4l2 2h6a2.5 2.5 0 0 1 2.5 2.5v7A2.5 2.5 0 0 1 18 19H6a2.5 2.5 0 0 1-2.5-2.5z" />}
      {name === "mcp" && <><path d="M8 8.5V6a2 2 0 0 1 4 0v3M12 8.5V5a2 2 0 0 1 4 0v5" /><path d="M8 8.5a2 2 0 0 0-4 0v4.7c0 4.1 3.3 7.3 7.3 7.3h.7a6 6 0 0 0 6-6v-4a2 2 0 0 0-4 0v1" /></>}
      {name === "more" && <><circle cx="7" cy="7" r="2" /><circle cx="17" cy="7" r="2" /><circle cx="7" cy="17" r="2" /><path d="M17 14v6M14 17h6" /></>}
      {name === "search" && <><circle cx="10.5" cy="10.5" r="6" /><path d="m15 15 4.5 4.5" /></>}
      {name === "send" && <><path d="m5 12 13-7-4.5 14-2.5-5.5z" /><path d="m11 13.5 7-8.5" /></>}
      {name === "shield" && <><path d="M12 3.5 19 6v5.5c0 4.4-2.8 7.3-7 9-4.2-1.7-7-4.6-7-9V6z" /><path d="m9 12 2 2 4-4" /></>}
      {name === "skill" && <><path d="M6 4.5h9.5A2.5 2.5 0 0 1 18 7v12.5H8.5A2.5 2.5 0 0 1 6 17z" /><path d="M6 17a2.5 2.5 0 0 1 2.5-2.5H18M10 8h4M10 11h5" /></>}
      {name === "spark" && <><path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5z" /><path d="m18.5 16 .6 2.4 2.4.6-2.4.6-.6 2.4-.6-2.4-2.4-.6 2.4-.6z" /></>}
      {name === "stop" && <rect height="9" rx="2" width="9" x="7.5" y="7.5" />}
      {name === "restore" && <><path d="M5 8v5h5" /><path d="M6.5 12a6.5 6.5 0 1 0 1.4-4.1L5 10" /></>}
      {name === "trash" && <><path d="M5.5 7h13M9 7V4.5h6V7M7.5 7l.7 12h7.6l.7-12M10 10.5v5M14 10.5v5" /></>}
    </svg>
  );
}

interface WorkdeskNavigationProps {
  newTaskDisabled?: boolean;
  onNewTask?: () => void;
}

export function WorkdeskNavigation({ newTaskDisabled = false, onNewTask }: WorkdeskNavigationProps) {
  const pathname = usePathname();
  const items: Array<{ href: string; icon: WorkdeskIconName; label: string }> = [
    { href: "/library", icon: "file", label: "资料库" },
    { href: "/connectors", icon: "agent", label: "连接器与 OAuth" },
    { href: "/providers", icon: "spark", label: "模型与密钥" },
    { href: "/automations", icon: "automation", label: "自动化与收件箱" },
    { href: "/skills", icon: "skill", label: "Skills" },
    { href: "/mcp", icon: "mcp", label: "MCP" },
    { href: "/memory", icon: "more", label: "记忆与设置" },
  ];

  return (
    <nav className="workdesk-primary-nav" aria-label="工作台导航">
      {onNewTask === undefined ? (
        <Link className={pathname === "/cowork" ? "active" : ""} href="/cowork?new=1">
          <WorkdeskIcon name="add" /><span>新建任务</span>
        </Link>
      ) : (
        <button className="active" disabled={newTaskDisabled} onClick={onNewTask} type="button">
          <WorkdeskIcon name="add" /><span>新建任务</span>
        </button>
      )}
      {items.map((item) => (
        <Link
          aria-current={pathname === item.href ? "page" : undefined}
          className={pathname === item.href ? "active" : ""}
          href={item.href}
          key={item.href}
        >
          <WorkdeskIcon name={item.icon} /><span>{item.label}</span>
        </Link>
      ))}
    </nav>
  );
}

function shortPath(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length <= 3 ? path : `…/${parts.slice(-3).join("/")}`;
}

interface WorkdeskAppShellProps {
  children: ReactNode;
  icon: WorkdeskIconName;
  sectionTitle: string;
}

export function WorkdeskAppShell({ children, icon, sectionTitle }: WorkdeskAppShellProps) {
  const { state: authState } = useAdminSession();
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [roots, setRoots] = useState<CoworkRoot[]>([]);

  useEffect(() => {
    if (authState !== "authenticated") return;
    let cancelled = false;
    const load = async () => {
      try {
        const response = await fetchConversations();
        if (cancelled) return;
        setConversations(response.items);
        const first = response.items[0];
        if (first === undefined) return;
        try {
          const rootResponse = await fetchCoworkRoots(first.id);
          if (!cancelled) setRoots(rootResponse.items);
        } catch {
          if (!cancelled) setRoots([]);
        }
      } catch {
        if (!cancelled) {
          setConversations([]);
          setRoots([]);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [authState]);

  return (
    <main className="cowork-frame workdesk-shell workdesk-app-shell">
      <aside className="workdesk-sidebar">
        <div className="workdesk-sidebar-head">
          <Link className="workdesk-brand" href="/cowork">
            <span><WorkdeskIcon name="spark" /></span>
            <div><strong>WorkPilot</strong><small>Local Cowork</small></div>
          </Link>
          <Link aria-label="搜索资料" className="workdesk-icon-button" href="/library">
            <WorkdeskIcon name="search" />
          </Link>
        </div>

        <WorkdeskNavigation />

        <section className="workdesk-sidebar-group">
          <header><span>最近任务</span><small>{conversations.length}</small></header>
          <div className="workdesk-task-list">
            {conversations.slice(0, 6).map((item) => (
              <Link href={`/cowork?conversation=${item.id}`} key={item.id}>
                <span>{item.title ?? "Cowork 任务"}</span>
                <small>{new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</small>
              </Link>
            ))}
            {conversations.length === 0 && <p>任务记录会显示在这里</p>}
          </div>
        </section>

        <section className="workdesk-sidebar-group workdesk-spaces">
          <header><span>工作空间</span><small>{roots.length}</small></header>
          <Link className="workdesk-add-space" href="/cowork">
            <WorkdeskIcon name="folder" />
            <span>{roots.length === 0 ? "在 Cowork 中选择" : "管理工作空间"}</span><b>＋</b>
          </Link>
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
          <span className="workdesk-section-chip"><WorkdeskIcon name={icon} />{sectionTitle}</span>
          <p>WorkPilot Desktop</p>
        </header>
        <div className="workdesk-route-stage">{children}</div>
      </section>
    </main>
  );
}
