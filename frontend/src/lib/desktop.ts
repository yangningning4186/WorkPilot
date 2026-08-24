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
const DESKTOP_BOOT_PENDING = "WorkPilot sidecar 正在后台启动";

function isDesktopBootPending(reason: unknown): boolean {
  return String(reason).includes(DESKTOP_BOOT_PENDING);
}

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
        // 只有明确的“仍在启动”可以重试。锁冲突、迁移失败、子进程退出等终态错误必须
        // 立即交给界面，否则一个本可瞬间解释的问题会伪装成 90 秒冷启动。
        if (!isDesktopBootPending(reason)) throw reason;
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

/** 阅读模式只接受一份可定位的本机文档。 */
export async function pickCoworkReadingFile(): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: false,
    filters: [{ name: "阅读文档", extensions: ["pdf", "md", "txt"] }],
    multiple: false,
    title: "选择要阅读的文档",
  });
  return typeof selected === "string" ? selected : null;
}

/**
 * OAuth 必须在系统浏览器完成。桌面端通过一个只接受官方授权地址的 Tauri command
 * 打开；Web 端复用当前点击同步创建的空白页，避免异步拿到 URL 后被 popup blocker 拦截。
 */
export function prepareConnectorAuthorizationWindow(): Window | null {
  if (typeof window === "undefined" || isTauriRuntime()) return null;
  return window.open("", "_blank");
}

export async function openConnectorAuthorization(
  url: string,
  preparedWindow: Window | null = null,
): Promise<void> {
  if (isTauriRuntime()) {
    preparedWindow?.close();
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_connector_authorization", { url });
    return;
  }

  if (preparedWindow !== null) {
    preparedWindow.opener = null;
    preparedWindow.location.replace(url);
    return;
  }

  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened === null) {
    throw new Error("浏览器阻止了授权页，请允许 WorkPilot 打开新窗口后重试");
  }
}
