import type { NextConfig } from "next";

const backendOrigin = (process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const desktopBuild = process.env.TAURI_BUILD === "true";

const nextConfig: NextConfig = {
  // 验收构建用独立产物目录（NEXT_DIST_DIR=.next-e2e），免得覆盖开发者正在用的 .next。
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  // Tauri 开发窗口从 127.0.0.1 打开 dev server；Next 16 默认会拦截这个
  // WebView origin 的 chunks/HMR，表现为桌面窗口白屏或“应用未响应”。
  allowedDevOrigins: ["127.0.0.1"],
  ...(desktopBuild
    ? {
        output: "export" as const,
        images: { unoptimized: true },
        trailingSlash: true,
      }
    : {}),
  // static export 没有 Next server，桌面构建由 webview 直连当次 sidecar。
  // 属性本身也不能出现，否则 Next 会把空 rewrites 仍视为 custom route。
  ...(desktopBuild
    ? {}
    : {
        async rewrites() {
          return [
            {
              source: "/api/:path*",
              destination: `${backendOrigin}/api/:path*`,
            },
          ];
        },
      }),
};

export default nextConfig;
