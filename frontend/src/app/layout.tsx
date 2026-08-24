import type { Metadata } from "next";
import "./globals.css";

import { AdminSessionProvider } from "@/components/admin-session";

export const metadata: Metadata = {
  title: "WorkPilot",
  description: "基于个人资料库的可溯源问答",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        {/* 顶栏和综述页要读同一份 admin 状态，提到 layout 里只问一次后端。 */}
        <AdminSessionProvider>{children}</AdminSessionProvider>
      </body>
    </html>
  );
}
