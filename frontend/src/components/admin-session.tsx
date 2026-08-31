"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { ApiError, type AdminAuthState, fetchAdminSession, loginAdmin, logoutAdmin } from "@/lib/api";
import { isTauriRuntime } from "@/lib/desktop";

interface AdminSessionValue {
  /** `unknown` 表示还没问过后端，用来区分"确认未登录"和"还不知道"。 */
  state: AdminAuthState | "unknown";
  /** 桌面 sidecar 的终态启动错误；null 表示仍在连接或连接正常。 */
  startupError: string | null;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** 收到 401 的调用点用它把顶栏拉回未登录，避免界面和后端各说各话。 */
  invalidate: () => void;
}

const AdminSessionContext = createContext<AdminSessionValue | null>(null);

export function AdminSessionProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AdminAuthState | "unknown">("unknown");
  const [startupError, setStartupError] = useState<string | null>(null);

  // cookie 是 httpOnly 的，挂载时只能问后端要一次当前状态。
  useEffect(() => {
    let cancelled = false;
    fetchAdminSession()
      .then((next) => {
        if (!cancelled) setState(next);
      })
      .catch((reason: unknown) => {
        // 后端没起来时不该谎称已登录，也不该谎称密码错。
        if (!cancelled) {
          setStartupError(String(reason));
          setState("anonymous");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (password: string) => {
    await loginAdmin(password);
    setState("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await logoutAdmin();
    } finally {
      // 登出失败也要回到未登录：cookie 可能已经被删，继续显示已登录只会让人以为还能写。
      setState("anonymous");
    }
  }, []);

  const invalidate = useCallback(() => setState("anonymous"), []);

  const value = useMemo<AdminSessionValue>(
    () => ({ state, startupError, login, logout, invalidate }),
    [state, startupError, login, logout, invalidate],
  );

  return <AdminSessionContext.Provider value={value}>{children}</AdminSessionContext.Provider>;
}

export function useAdminSession(): AdminSessionValue {
  const value = useContext(AdminSessionContext);
  if (value === null) {
    throw new Error("useAdminSession 必须在 AdminSessionProvider 内使用");
  }
  return value;
}

/** 把后端的失败翻译成"下一步该做什么"，而不是把 HTTP 状态码糊到脸上。 */
function loginErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "登录请求没发出去，确认后端已启动。";
  }
  if (error.status === 401) {
    return "密码错误。";
  }
  if (error.status === 503) {
    return "后端还没配 DEMO_ADMIN_PASSWORD_HASH。先跑 uv run python -m app.cli.hash_admin_password 生成并填进 .env，再重启后端。";
  }
  if (error.status === 429) {
    return "尝试过于频繁，稍后再试。";
  }
  return `登录失败（${error.status}）。`;
}

/**
 * 顶栏里的 admin 登录入口。
 *
 * 写操作（触发同步、创建综述、批准写回）全部要 admin session，但在此之前浏览器里
 * 根本没有拿到 session 的地方——只能靠 curl。这个控件就是补上那个缺口。
 */
export function AdminSessionControl() {
  const { state, login, logout } = useAdminSession();
  const desktop = useSyncExternalStore(
    () => () => undefined,
    isTauriRuntime,
    () => false,
  );
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setPassword("");
    setError(null);
  }, []);

  // Esc 关闭：这是个覆盖在页面上的小浮层，没有退路会让人下意识去点浏览器后退。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      if (password === "") return;
      setPending(true);
      setError(null);
      try {
        await login(password);
        setOpen(false);
        setPassword("");
      } catch (reason) {
        setError(loginErrorMessage(reason));
        // 密码留空重来，免得用户在一个已知错误的值上反复回车。
        setPassword("");
        inputRef.current?.focus();
      } finally {
        setPending(false);
      }
    },
    [login, password],
  );

  if (state === "authenticated") {
    return (
      <div className="admin-session" data-auth-state={state}>
        <span className="admin-badge" title="owner 私有会话：可使用个人记忆与管理功能">
          {desktop ? "desktop owner" : "owner"}
        </span>
        {!desktop && (
          <button className="link-button" onClick={() => void logout()} type="button">
            登出
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="admin-session" data-auth-state={state}>
      <button
        aria-expanded={open}
        aria-haspopup="dialog"
        className="link-button"
        disabled={state === "unknown"}
        onClick={() => (open ? close() : setOpen(true))}
        type="button"
      >
        owner 登录
      </button>
      {open && (
        <form aria-label="owner 登录" className="admin-login" onSubmit={submit} role="dialog">
          <label htmlFor={inputId}>owner 口令</label>
          <input
            autoComplete="current-password"
            autoFocus
            id={inputId}
            onChange={(event) => setPassword(event.target.value)}
            ref={inputRef}
            type="password"
            value={password}
          />
          {error !== null && <p className="form-error">{error}</p>}
          <div className="admin-login-actions">
            <button className="primary-button" disabled={pending || password === ""} type="submit">
              {pending ? "登录中…" : "登录"}
            </button>
            <button className="link-button" onClick={close} type="button">
              取消
            </button>
          </div>
          <p className="admin-login-hint">
            单用户 owner 口令。个人记忆只会在这个会话中抽取、召回和管理。
          </p>
        </form>
      )}
    </div>
  );
}
