import { defineConfig, devices } from "@playwright/test";

/**
 * 前端验收：提问 → SSE 回答 → 点击引用 → 原文高亮。
 *
 * 被测的是产线构建（next build + next start），不是 dev server：
 * dev 下 StrictMode 会把 effect 跑两遍，fetch SSE 也就开两条，
 * 那既不是用户看到的东西，也会让断线续传这类用例的时序断言失真。
 *
 * 后端是 tests/e2e/mock-backend.mjs 按剧本回放的假后端，理由见该文件头注释。
 * 想对着真后端跑冒烟，见 README「前端验收」一节。
 */

const MOCK_PORT = Number(process.env.MOCK_BACKEND_PORT ?? 8787);
const APP_PORT = Number(process.env.E2E_APP_PORT ?? 3100);
const APP_URL = `http://127.0.0.1:${APP_PORT}`;

// Playwright 会自行探测 webServer.url；某些开发机全局配置了 HTTP 代理，
// 如果没有本地地址豁免，健康检查会被错误转发到代理并误判服务未启动。
const loopbackNoProxy = ["127.0.0.1", "localhost"];
for (const key of ["NO_PROXY", "no_proxy"] as const) {
  const current = process.env[key]?.split(",").filter(Boolean) ?? [];
  process.env[key] = [...new Set([...current, ...loopbackNoProxy])].join(",");
}

export default defineConfig({
  testDir: "./tests/e2e",
  // SSE 剧本靠真实时序推进，并发跑会互相抢 CPU 造成假失败。用例总量很小，串行足够快。
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: APP_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    // 高亮几何断言按百分比换算成像素，视口固定才有可比性。
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // 默认用 Playwright 自带的 Chromium；下不动浏览器的机器可以
        // PLAYWRIGHT_CHANNEL=chrome 直接用系统装的 Chrome。
        ...(process.env.PLAYWRIGHT_CHANNEL === undefined
          ? {}
          : { channel: process.env.PLAYWRIGHT_CHANNEL }),
      },
    },
  ],
  webServer: [
    {
      command: "node tests/e2e/mock-backend.mjs",
      url: `http://127.0.0.1:${MOCK_PORT}/__health`,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // rewrites 在 build 时就被烘进 routes-manifest，所以 BACKEND_ORIGIN 必须在
      // build 和 start 两步都生效——这里让它作用于整条命令。
      // Turbopack 的 PostCSS worker 会绑定临时端口，在受限 CI/agent 环境中可能被拒绝；
      // E2E 只关心产线产物行为，显式走 Next 官方 webpack 构建路径更稳定。
      command: `npm run build:e2e && npx next start --port ${APP_PORT}`,
      url: APP_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 240_000,
      env: {
        BACKEND_ORIGIN: `http://127.0.0.1:${MOCK_PORT}`,
        // 独立产物目录：别让验收构建覆盖开发者正在用的 .next 缓存。
        NEXT_DIST_DIR: ".next-e2e",
      },
    },
  ],
});
