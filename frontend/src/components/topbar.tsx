"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { AdminSessionControl } from "@/components/admin-session";

/** 两个页面共用的顶栏。当前页高亮由 pathname 决定，不用各自维护状态。 */
export function Topbar() {
  const pathname = usePathname();

  return (
    <header className="topbar">
      <Link className="brand" href="/">
        <span>W</span>
        <strong>WorkPilot</strong>
      </Link>
      <nav className="topbar-nav" aria-label="主导航">
        <Link aria-current={pathname === "/" ? "page" : undefined} href="/">
          问答
        </Link>
        <Link aria-current={pathname === "/review" ? "page" : undefined} href="/review">
          综述
        </Link>
        <Link aria-current={pathname === "/library" ? "page" : undefined} href="/library">
          资料库
        </Link>
        <Link
          aria-current={pathname.startsWith("/workspace") ? "page" : undefined}
          href="/workspace"
        >
          文件编辑
        </Link>
        <Link aria-current={pathname === "/cowork" ? "page" : undefined} href="/cowork">
          Cowork
        </Link>
        <Link aria-current={pathname === "/memory" ? "page" : undefined} href="/memory">
          记忆
        </Link>
        {/* 成本是运营页，后端强制 admin；这里照常显示，未登录时页面自己提示登录 */}
        <Link aria-current={pathname === "/cost" ? "page" : undefined} href="/cost">
          成本
        </Link>
      </nav>
      <div className="topbar-right">
        <div className="product-note">
          <span />
          本地资料库已连接
        </div>
        <AdminSessionControl />
      </div>
    </header>
  );
}
