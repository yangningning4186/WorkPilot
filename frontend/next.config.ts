import type { NextConfig } from "next";

const backendOrigin = (process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  // 验收构建用独立产物目录（NEXT_DIST_DIR=.next-e2e），免得覆盖开发者正在用的 .next。
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
