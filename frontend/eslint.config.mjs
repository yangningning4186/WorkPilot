import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // 验收产物：构建输出与 Playwright 报告不参与 lint。
    ".next-e2e/**",
    // 原生发布构建生成的 sidecar 与 Playwright 浏览器发行物不是前端源码。
    "src-tauri/binaries/**",
    "src-tauri/resources/ms-playwright/**",
    "src-tauri/target/**",
    "test-results/**",
    "playwright-report/**",
  ]),
]);

export default eslintConfig;
