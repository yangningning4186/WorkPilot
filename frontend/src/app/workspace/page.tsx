"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAdminSession } from "@/components/admin-session";
import { Topbar } from "@/components/topbar";
import {
  ApiError,
  executeWorkspaceInstruction,
  fetchEditorPermission,
  fetchWorkspaceFile,
  fetchWorkspaceFiles,
  grantEditorPermission,
  revokeEditorPermission,
  type EditorPermission,
  type WorkspaceFile,
  type WorkspaceFileKind,
  type WorkspaceFileSummary,
  type WorkspaceInstructionResponse,
} from "@/lib/api";

const KIND_META: Record<
  WorkspaceFileKind,
  { mark: string; label: string; hint: string; examples: string[] }
> = {
  markdown: {
    mark: "M",
    label: "Markdown",
    hint: "可以先选中一段；没有选区时按全文执行。",
    examples: ["把选中段落改得更精炼", "整理全文结构并补充小标题", "把这段改成正式周报语气"],
  },
  word: {
    mark: "W",
    label: "Word",
    hint: "按段落和表格单元格执行，尽量保留原样式。",
    examples: ["把第一段标题改得更专业", "把结论段压缩到 100 字以内", "在末尾追加一段行动项"],
  },
  excel: {
    mark: "X",
    label: "Excel",
    hint: "按工作表和单元格执行，已有样式与公式不会被整表重建。",
    examples: ["把预算表 B2 改为 2000", "在 C 列补上 B 列乘以 1.06 的公式", "清空汇总表 D2"],
  },
};

function friendlyError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "owner 会话已过期，请重新登录。";
  }
  if (error instanceof ApiError && error.status === 403) {
    return "写权限尚未授予或已经过期，请重新授权。";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "文件刚刚被 Word、Excel 或其他程序修改。WorkPilot 已停止覆盖，请重新加载。";
  }
  if (error instanceof ApiError && error.status === 422) {
    return "这条指令无法安全应用。请缩小修改范围或把目标说得更具体。";
  }
  return "操作没有完成，请检查后端与模型服务。";
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatPermission(permission: EditorPermission | null): string {
  if (permission === null || !permission.granted) return "未授权";
  const minutes = Math.max(1, Math.ceil(permission.expires_in_s / 60));
  return `剩余约 ${minutes} 分钟`;
}

function codePointOffset(value: string, utf16Offset: number): number {
  return Array.from(value.slice(0, utf16Offset)).length;
}

function WorkspaceGate() {
  return (
    <section className="workspace-gate">
      <div className="workspace-gate-mark">⌘</div>
      <div>
        <span className="eyebrow">Private workspace</span>
        <h1>办公工作台只对 owner 开放</h1>
        <p>请先在右上角登录。文件内容、绝对路径与写权限不会暴露给匿名演示会话。</p>
      </div>
    </section>
  );
}

export default function WorkspacePage() {
  const { state: authState, invalidate } = useAdminSession();
  const [files, setFiles] = useState<WorkspaceFileSummary[]>([]);
  const [activeFile, setActiveFile] = useState<WorkspaceFile | null>(null);
  const [permission, setPermission] = useState<EditorPermission | null>(null);
  const [query, setQuery] = useState("");
  const [instruction, setInstruction] = useState("");
  const [selection, setSelection] = useState({ start: 0, end: 0 });
  const [result, setResult] = useState<WorkspaceInstructionResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [permissionPending, setPermissionPending] = useState(false);
  const previewRef = useRef<HTMLTextAreaElement>(null);

  const loadFile = useCallback(
    async (fileId: string) => {
      setLoading(true);
      setNotice(null);
      try {
        const loaded = await fetchWorkspaceFile(fileId);
        setActiveFile(loaded);
        setSelection({ start: 0, end: 0 });
        setResult(null);
      } catch (reason) {
        setNotice(friendlyError(reason));
        if (reason instanceof ApiError && reason.status === 401) invalidate();
      } finally {
        setLoading(false);
      }
    },
    [invalidate],
  );

  useEffect(() => {
    if (authState !== "authenticated") return;
    let cancelled = false;
    Promise.all([fetchWorkspaceFiles(), fetchEditorPermission()])
      .then(([fileResponse, permissionResponse]) => {
        if (cancelled) return;
        setFiles(fileResponse.items);
        setPermission(permissionResponse);
        if (fileResponse.items[0] !== undefined) void loadFile(fileResponse.items[0].file_id);
        else setLoading(false);
      })
      .catch((reason) => {
        if (cancelled) return;
        setNotice(friendlyError(reason));
        setLoading(false);
        if (reason instanceof ApiError && reason.status === 401) invalidate();
      });
    return () => {
      cancelled = true;
    };
  }, [authState, invalidate, loadFile]);

  const filteredFiles = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    if (keyword === "") return files;
    return files.filter(
      (file) =>
        file.name.toLocaleLowerCase().includes(keyword) ||
        file.source_uri.toLocaleLowerCase().includes(keyword),
    );
  }, [files, query]);

  const grant = useCallback(async () => {
    setPermissionPending(true);
    setNotice(null);
    try {
      setPermission(await grantEditorPermission());
      setNotice("已授予限时写权限。接下来的指令会直接写入文件，并在写前创建备份。");
    } catch (reason) {
      setNotice(friendlyError(reason));
      if (reason instanceof ApiError && reason.status === 401) invalidate();
    } finally {
      setPermissionPending(false);
    }
  }, [invalidate]);

  const revoke = useCallback(async () => {
    setPermissionPending(true);
    try {
      await revokeEditorPermission();
      setPermission({ granted: false, scope: "local_office_write", expires_in_s: 0 });
      setNotice("写权限已收回。文件仍可查看，但新指令不会执行。");
    } catch (reason) {
      setNotice(friendlyError(reason));
    } finally {
      setPermissionPending(false);
    }
  }, []);

  const execute = useCallback(async () => {
    if (activeFile === null || instruction.trim() === "" || !permission?.granted) return;
    setExecuting(true);
    setNotice(null);
    setResult(null);
    const element = previewRef.current;
    const rawStart = activeFile.kind === "markdown" ? (element?.selectionStart ?? 0) : 0;
    const rawEnd = activeFile.kind === "markdown" ? (element?.selectionEnd ?? 0) : 0;
    try {
      const response = await executeWorkspaceInstruction(activeFile.file_id, {
        baseline_sha256: activeFile.baseline_sha256,
        instruction: instruction.trim(),
        ...(activeFile.kind === "markdown"
          ? {
              content: activeFile.content,
              selection_start: codePointOffset(activeFile.content, rawStart),
              selection_end: codePointOffset(activeFile.content, rawEnd),
            }
          : {}),
      });
      setActiveFile(response.file);
      setResult(response);
      setInstruction("");
      setSelection({ start: 0, end: 0 });
      setNotice(
        response.change_count === 0
          ? "指令已执行，模型判断无需修改文件。"
          : `已直接写入 ${response.change_count} 处修改。`,
      );
    } catch (reason) {
      setNotice(friendlyError(reason));
      if (reason instanceof ApiError && reason.status === 401) invalidate();
      if (reason instanceof ApiError && reason.status === 403) {
        setPermission({ granted: false, scope: "local_office_write", expires_in_s: 0 });
      }
    } finally {
      setExecuting(false);
    }
  }, [activeFile, instruction, invalidate, permission]);

  const updateSelection = useCallback(() => {
    const element = previewRef.current;
    if (element === null) return;
    setSelection({ start: element.selectionStart, end: element.selectionEnd });
  }, []);

  const kind = activeFile === null ? null : KIND_META[activeFile.kind];
  const selectedCharacters = Math.max(0, selection.end - selection.start);

  return (
    <main className="app-frame office-frame">
      <Topbar />
      {authState !== "authenticated" ? (
        <WorkspaceGate />
      ) : (
        <div className="office-shell">
          <aside className="office-files" aria-label="办公文档">
            <div className="office-files-head">
              <div>
                <span className="eyebrow">Local files</span>
                <h1>工作台</h1>
              </div>
              <span className="office-file-count">{files.length}</span>
            </div>
            <input
              aria-label="搜索办公文档"
              className="office-search"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索文件"
              type="search"
              value={query}
            />
            <div className="office-file-list">
              {filteredFiles.map((file) => {
                const meta = KIND_META[file.kind];
                return (
                  <button
                    aria-current={activeFile?.file_id === file.file_id ? "true" : undefined}
                    className="office-file-item"
                    key={file.file_id}
                    onClick={() => void loadFile(file.file_id)}
                    type="button"
                  >
                    <span className={`office-kind ${file.kind}`}>{meta.mark}</span>
                    <span className="office-file-copy">
                      <strong>{file.name}</strong>
                      <small>{file.source_uri}</small>
                    </span>
                    <span className="office-file-size">{formatBytes(file.size_bytes)}</span>
                  </button>
                );
              })}
              {!loading && filteredFiles.length === 0 && (
                <div className="office-list-empty">没有找到 .md、.docx 或 .xlsx 文件。</div>
              )}
            </div>
          </aside>

          <section className="office-document" aria-label="文档内容">
            {activeFile === null ? (
              <div className="office-document-empty">
                <span>⌁</span>
                <h2>{loading ? "正在读取文档…" : "选择一个办公文档"}</h2>
                <p>工作台只扫描已注册本地资料目录，不接受任意绝对路径。</p>
              </div>
            ) : (
              <>
                <header className="office-document-head">
                  <div>
                    <div className="office-document-kicker">
                      <span className={`office-kind ${activeFile.kind}`}>{kind?.mark}</span>
                      <span>{kind?.label}</span>
                      <span>·</span>
                      <span>{activeFile.source_name}</span>
                    </div>
                    <h2>{activeFile.name}</h2>
                    <p>{activeFile.source_uri}</p>
                  </div>
                  <button
                    className="office-reload"
                    disabled={loading}
                    onClick={() => void loadFile(activeFile.file_id)}
                    type="button"
                  >
                    重新加载
                  </button>
                </header>
                <div className="office-preview-meta">
                  <span>{kind?.hint}</span>
                  {activeFile.kind === "markdown" && (
                    <strong>{selectedCharacters > 0 ? `已选 ${selectedCharacters} 字` : "全文"}</strong>
                  )}
                </div>
                <textarea
                  aria-label="办公文档内容"
                  className={`office-preview ${activeFile.kind}`}
                  onClick={updateSelection}
                  onKeyUp={updateSelection}
                  onSelect={updateSelection}
                  readOnly
                  ref={previewRef}
                  spellCheck={false}
                  value={activeFile.content}
                />
              </>
            )}
          </section>

          <aside className="office-command" aria-label="指令面板">
            <section className={`permission-card${permission?.granted ? " granted" : ""}`}>
              <div className="permission-card-head">
                <span className="permission-signal" />
                <div>
                  <strong>{permission?.granted ? "本地写权限已启用" : "需要本地写权限"}</strong>
                  <small>{formatPermission(permission)}</small>
                </div>
              </div>
              <p>
                范围固定为已注册目录中的 Markdown、Word 与 Excel。每次写入前会校验文件哈希并创建隐藏备份。
              </p>
              {permission?.granted ? (
                <button disabled={permissionPending} onClick={() => void revoke()} type="button">
                  收回权限
                </button>
              ) : (
                <button
                  className="grant-button"
                  disabled={permissionPending}
                  onClick={() => void grant()}
                  type="button"
                >
                  {permissionPending ? "正在授权…" : "授予限时写权限"}
                </button>
              )}
            </section>

            <section className="command-card">
              <div className="command-card-heading">
                <span className="eyebrow">Direct command</span>
                <h2>告诉 WorkPilot 怎么改</h2>
                <p>权限有效时，提交后直接写入，不再逐次确认。</p>
              </div>
              <label htmlFor="office-instruction">修改指令</label>
              <textarea
                disabled={activeFile === null || executing}
                id="office-instruction"
                maxLength={4000}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder="例如：把第二段改得更正式，并保留所有数字和引用。"
                rows={6}
                value={instruction}
              />
              {kind !== null && (
                <div className="command-examples" aria-label="指令示例">
                  {kind.examples.map((example) => (
                    <button key={example} onClick={() => setInstruction(example)} type="button">
                      {example}
                    </button>
                  ))}
                </div>
              )}
              <button
                className="execute-command"
                disabled={
                  activeFile === null ||
                  instruction.trim() === "" ||
                  executing ||
                  !permission?.granted
                }
                onClick={() => void execute()}
                type="button"
              >
                {executing ? "正在修改并校验…" : "执行并直接写入"}
              </button>
            </section>

            {notice !== null && (
              <div className={`office-notice${notice.includes("没有完成") ? " error" : ""}`} role="status">
                {notice}
              </div>
            )}
            {result !== null && (
              <section className="office-result">
                <span>Last operation</span>
                <h3>{result.summary}</h3>
                <dl>
                  <div>
                    <dt>修改</dt>
                    <dd>{result.change_count} 处</dd>
                  </div>
                  <div>
                    <dt>模型</dt>
                    <dd>{result.model}</dd>
                  </div>
                </dl>
                <p>
                  {result.backup_uri === null
                    ? "文件内容未变化，因此没有创建备份。"
                    : `备份：${result.backup_uri}`}
                </p>
              </section>
            )}
          </aside>
        </div>
      )}
    </main>
  );
}
