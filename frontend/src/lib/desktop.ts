/** Tauri 桌面桥；普通 Web 构建不会加载任何本地权限 API。 */

export interface DesktopContext {
  api_base: string;
  launch_token: string;
}

declare global {
  interface Window {
    isTauri?: boolean;
    __TAURI_INTERNALS__?: unknown;
  }
}

let contextPromise: Promise<DesktopContext | null> | null = null;

const DESKTOP_BOOT_TIMEOUT_MS = 90_000;
const DESKTOP_CONTEXT_RETRY_MS = 500;

export function isTauriRuntime(): boolean {
  if (typeof window === "undefined") return false;
  // Tauri 2 的公共运行时判据是 `window.isTauri`。内部对象不是稳定 API，
  // 某些 WebView 初始化方式下不可用于特性检测；保留它只兼容既有测试 mock。
  return window.isTauri === true || window.__TAURI_INTERNALS__ !== undefined;
}

export function getDesktopContext(): Promise<DesktopContext | null> {
  if (!isTauriRuntime()) return Promise.resolve(null);
  contextPromise ??= import("@tauri-apps/api/core").then(async ({ invoke }) => {
    const deadline = Date.now() + DESKTOP_BOOT_TIMEOUT_MS;
    let lastError: unknown = new Error("WorkPilot sidecar 尚未就绪");
    while (Date.now() < deadline) {
      try {
        return await invoke<DesktopContext>("desktop_context");
      } catch (reason) {
        lastError = reason;
        await new Promise((resolve) => window.setTimeout(resolve, DESKTOP_CONTEXT_RETRY_MS));
      }
    }
    throw lastError;
  });
  return contextPromise;
}

/** 只有系统目录选择器能建立 Cowork root，不接受网页自由输入绝对路径。 */
export async function pickCoworkDirectory(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    title: "选择 WorkPilot 可访问的目录",
  });
  return typeof selected === "string" ? selected : null;
}
