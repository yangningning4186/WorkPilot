"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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
        <Link aria-current={pathname === "/library" ? "page" : undefined} href="/library">
          资料库
        </Link>
      </nav>
      <div className="product-note">
        <span />
        本地资料库已连接
      </div>
    </header>
  );
}
