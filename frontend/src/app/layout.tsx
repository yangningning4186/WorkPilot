import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "WorkPilot",
  description: "基于个人资料库的可溯源问答",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
