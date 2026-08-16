"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { useAdminSession } from "@/components/admin-session";
import { Topbar } from "@/components/topbar";
import {
  ApiError,
  type LibraryDocument,
  createReviewRun,
  fetchLibrary,
  resumeRun,
} from "@/lib/api";
import type { AgentPlanStepPayload, AgentStepStatus } from "@/lib/run-protocol";
import { useRunStream } from "@/lib/use-run-stream";

const STEP_LABELS: Record<AgentStepStatus, string> = {
  pending: "待执行",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

/**
 * 是否有激活版本可供固定综述读取。
 *
 * `failed` 只表示最新候选版本失败，旧的激活版本仍在服务；按 state === "ready"
 * 过滤会把这类可用文档误删。真正的能力边界是有激活 version 且有可检索块。
 */
function isReviewable(document: LibraryDocument): boolean {
  return document.version_id !== null && document.searchable_chunk_count > 0;
}

/** 写回路径只允许 AGENT_OUTPUT_PATH 内的相对 .md；这里先做同样的前置校验，少一次往返。 */
function outputPathError(value: string): string | null {
  const trimmed = value.trim();
  if (trimmed === "") {
    return "请填写写回路径";
  }
  if (!trimmed.endsWith(".md")) {
    return "只能写入 .md 文件";
  }
  if (trimmed.startsWith("/") || trimmed.includes("..")) {
    return "只能是输出目录内的相对路径，不能用绝对路径或 ..";
  }
  return null;
}

function StepRow({ step }: { step: AgentPlanStepPayload }) {
  return (
    <li className={`timeline-step ${step.status}`}>
      <span className="timeline-marker" aria-hidden />
      <div className="timeline-body">
        <div className="timeline-head">
          <span className="timeline-desc">{step.description}</span>
          <span className={`step-badge ${step.status}`}>{STEP_LABELS[step.status]}</span>
        </div>
        {step.tool !== null && <code className="timeline-tool">{step.tool}</code>}
        {step.summary !== undefined && step.summary !== "" && (
          <p className="timeline-summary">{step.summary}</p>
        )}
      </div>
    </li>
  );
}

function DocumentPicker({
  documents,
  selected,
  onToggle,
}: {
  documents: LibraryDocument[];
  selected: Set<string>;
  onToggle: (id: string) => void;
}) {
  if (documents.length === 0) {
    return <p className="empty-hint">没有可检索的文档。先去资料库同步。</p>;
  }
  return (
    <ul className="doc-picker">
      {documents.map((document) => (
        <li key={document.document_id}>
          <label>
            <input
              checked={selected.has(document.document_id)}
              onChange={() => onToggle(document.document_id)}
              type="checkbox"
            />
            <span className="doc-picker-title">{document.title}</span>
          </label>
        </li>
      ))}
    </ul>
  );
}

export default function ReviewPage() {
  // useSearchParams 会让上层组件树退回客户端渲染，用 Suspense 圈住它，
  // 顶栏这些不依赖查询串的部分仍然可以预渲染（Next 官方建议）。
  return (
    <Suspense fallback={null}>
      <ReviewWorkspace />
    </Suspense>
  );
}

function ReviewWorkspace() {
  /**
   * B1 刷新恢复：run_id 落在 URL 上，刷新后照样能接回同一条流。
   *
   * run_events 是唯一真相源，前端只要拿着 run_id 就能从 after_seq=0 重放；
   * 把它只存在组件内存里，等于自己把这条已经付过工程代价的性质丢掉。
   */
  const initialRunId = useSearchParams().get("run");
  const { state: adminState, invalidate: invalidateAdmin } = useAdminSession();
  const [documents, setDocuments] = useState<LibraryDocument[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [goal, setGoal] = useState("");
  const [outputPath, setOutputPath] = useState("reviews/综述.md");
  const [runId, setRunId] = useState<string | null>(initialRunId);
  const [submitting, setSubmitting] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const state = useRunStream(runId);

  useEffect(() => {
    let cancelled = false;
    fetchLibrary("")
      .then((library) => {
        if (!cancelled) {
          setDocuments(library.documents.filter(isReviewable));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setFormError("资料库读取失败，请确认后端已启动");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = useCallback((id: string) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const pathError = useMemo(() => outputPathError(outputPath), [outputPath]);
  // 创建和批准写回都要 admin session。与其让用户填完整张表再吃一个 401，
  // 不如一开始就说清楚缺什么——写回是有副作用的一步，不该靠试错才发现开不了工。
  const needsLogin = adminState !== "authenticated";
  const canSubmit =
    !submitting &&
    !needsLogin &&
    runId === null &&
    selected.size >= 2 &&
    goal.trim() !== "" &&
    pathError === null;

  const submit = useCallback(async () => {
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await createReviewRun({
        goal: goal.trim(),
        document_ids: [...selected],
        output_path: outputPath.trim(),
      });
      setRunId(created.run_id);
      window.history.replaceState(null, "", `?run=${created.run_id}`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        // session 可能是刚过期的，把顶栏拉回未登录，用户能就地重新登录。
        invalidateAdmin();
        setFormError("admin 会话已失效，请在右上角重新登录后重试。");
      } else {
        setFormError(
          error instanceof ApiError ? `创建失败（${error.status}）：${error.message}` : "创建失败",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }, [goal, outputPath, selected, invalidateAdmin]);

  const decide = useCallback(
    async (approved: boolean) => {
      if (runId === null || state.interrupt === null) {
        return;
      }
      setResuming(true);
      try {
        await resumeRun(runId, {
          resume_token: state.interrupt.resume_token,
          approved,
        });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          // 走到这一步说明只读部分已经跑完了，run 还停在 waiting_human。
          // 重新登录后原地再点一次即可，resume_token 仍然有效，不必从头再跑一遍。
          invalidateAdmin();
          setFormError("admin 会话已失效。请在右上角重新登录，run 仍停在确认点，可直接再点一次。");
        } else {
          setFormError(
            error instanceof ApiError
              ? `提交决定失败（${error.status}）：${error.message}`
              : "提交决定失败",
          );
        }
      } finally {
        setResuming(false);
      }
    },
    [runId, state.interrupt, invalidateAdmin],
  );

  const preview = state.artifacts.find((item) => item.kind === "review_preview");
  const written = state.artifacts.find((item) => item.kind === "written_note");

  return (
    <main className="app-frame review-frame">
      <Topbar />
      <div className="review-body">
        <section className="review-form" aria-label="创建固定综述">
          <h1>固定综述</h1>
          <p className="review-lede">
            按固定流程跑：筛选 → 抽卡 → 分组 → 对比 → 生成预览 → 你确认后才写回笔记。
            确认之前磁盘上不会出现任何文件。
          </p>

          <label className="field">
            <span>综述目标</span>
            <textarea
              disabled={runId !== null}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="例如：比较这几篇在记忆机制上的取舍"
              rows={3}
              value={goal}
            />
          </label>

          <label className="field">
            <span>写回路径</span>
            <input
              disabled={runId !== null}
              onChange={(event) => setOutputPath(event.target.value)}
              value={outputPath}
            />
            {pathError !== null && <em className="field-error">{pathError}</em>}
          </label>

          <div className="field">
            <span>
              选择文档（已选 {selected.size} 篇，至少 2 篇）
            </span>
            <DocumentPicker documents={documents} onToggle={toggle} selected={selected} />
          </div>

          {adminState === "anonymous" && (
            <p className="login-required">
              创建综述和批准写回属于写操作，需要先在右上角完成 admin 登录。
              浏览资料库和问答不受影响。
            </p>
          )}

          {formError !== null && <p className="form-error">{formError}</p>}

          <button className="primary-button" disabled={!canSubmit} onClick={submit} type="button">
            {submitting ? "创建中…" : "开始生成综述"}
          </button>
        </section>

        <section className="review-timeline" aria-label="执行时间线">
          {runId === null ? (
            <p className="empty-hint">还没有运行中的综述任务。</p>
          ) : (
            <>
              <h2>执行时间线</h2>
              {state.recoveryCount > 0 && (
                <p className="recovery-note">
                  worker 曾失联 {state.recoveryCount} 次，已从最近 checkpoint 自动恢复，
                  已完成的步骤不会重跑。
                  {state.notice !== null && ` ${state.notice}`}
                </p>
              )}
              {state.agentPlan.length === 0 ? (
                <p className="empty-hint">正在排队…</p>
              ) : (
                <ol className="timeline">
                  {state.agentPlan.map((step) => (
                    <StepRow key={step.id} step={step} />
                  ))}
                </ol>
              )}

              {preview !== undefined && (
                <article className="preview-card">
                  <h3>{preview.title}</h3>
                  <pre className="preview-body">{preview.content}</pre>
                </article>
              )}

              {state.interrupt !== null && (
                <div className="approval-card" role="group" aria-label="写回确认">
                  <h3>确认写入笔记</h3>
                  <p>
                    将写入 <code>{String(state.interrupt.payload.output_path ?? "")}</code>。
                    这是本次任务唯一有副作用的一步。
                  </p>
                  <div className="approval-actions">
                    <button
                      className="primary-button"
                      disabled={resuming}
                      onClick={() => decide(true)}
                      type="button"
                    >
                      批准写回
                    </button>
                    <button
                      className="ghost-button"
                      disabled={resuming}
                      onClick={() => decide(false)}
                      type="button"
                    >
                      拒绝
                    </button>
                  </div>
                </div>
              )}

              {written !== undefined && (
                <p className="written-note">
                  已写入 <code>{written.path}</code>
                  {written.reused === true && "（重复确认，未再次写入）"}
                </p>
              )}

              {state.error !== null && (
                <p className="form-error">
                  {state.error.user_message}
                  {!state.error.retryable && "（不可重试）"}
                </p>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}
