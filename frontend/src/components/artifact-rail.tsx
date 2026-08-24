"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { WorkdeskIcon } from "@/components/workdesk-shell";
import {
  fetchArtifactDiff,
  fetchArtifactPreview,
  type ArtifactDiffPayload,
  type CoworkArtifact,
} from "@/lib/api";

type ArtifactTab = "preview" | "diff";

function fileMark(artifact: CoworkArtifact): { label: string; className: string } {
  const suffix = artifact.title.toLowerCase().split(".").pop() ?? "";
  if (suffix === "xlsx") return { label: "X", className: "excel" };
  if (suffix === "docx") return { label: "W", className: "word" };
  if (suffix === "pptx") return { label: "P", className: "slides" };
  if (suffix === "pdf") return { label: "P", className: "pdf" };
  return { label: "A", className: "text" };
}

function shortPath(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length <= 3 ? path : `…/${parts.slice(-3).join("/")}`;
}

function previewModeLabel(mode: string): string {
  if (mode === "quicklook") return "macOS 版面渲染";
  if (mode === "libreoffice") return "LibreOffice 分页渲染";
  if (mode === "native-pdf") return "原生 PDF";
  if (mode === "structure") return "结构预览";
  if (mode === "text") return "文本预览";
  return "安全预览";
}

function DiffView({ diff }: { diff: ArtifactDiffPayload }) {
  if (!diff.available) {
    return (
      <div className="artifact-rail-empty compact">
        <span><WorkdeskIcon name="shield" /></span>
        <strong>没有可比较的执行前快照</strong>
        <p>{diff.reason ?? "这份交付物来自旧版本，或文件超过差异快照上限。"}</p>
      </div>
    );
  }
  if (diff.text === "") {
    return (
      <div className="artifact-rail-empty compact">
        <span><WorkdeskIcon name="file" /></span>
        <strong>{diff.created ? "已创建空文件" : "语义内容没有变化"}</strong>
        <p>{diff.reason ?? "可能只有版式、元数据或文件结构发生变化。"}</p>
      </div>
    );
  }
  return (
    <pre className="artifact-diff" aria-label="交付物差异">
      {diff.text.split("\n").map((line, index) => (
        <span
          className={
            line.startsWith("+++") || line.startsWith("---")
              ? "file"
              : line.startsWith("+")
                ? "added"
                : line.startsWith("-")
                  ? "removed"
                  : line.startsWith("@@")
                    ? "hunk"
                    : "context"
          }
          key={`${index}:${line.slice(0, 40)}`}
        >
          {line || " "}
        </span>
      ))}
      {diff.truncated && <em>差异过长，右栏只显示前 500 行</em>}
    </pre>
  );
}

export function ArtifactRail({
  artifacts,
  onClose,
}: {
  artifacts: CoworkArtifact[];
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(artifacts[0]?.id ?? null);
  const [tab, setTab] = useState<ArtifactTab>("preview");
  const [preview, setPreview] = useState<{ mode: string; url: string } | null>(null);
  const [diff, setDiff] = useState<ArtifactDiffPayload | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const objectUrl = useRef<string | null>(null);

  const selected = useMemo(
    () => artifacts.find((artifact) => artifact.id === selectedId) ?? artifacts[0] ?? null,
    [artifacts, selectedId],
  );

  useEffect(() => {
    if (selected === null) return;
    const currentGeneration = ++generation.current;
    const artifactId = selected.id;
    void Promise.resolve().then(async () => {
      if (generation.current !== currentGeneration) return;
      setLoading(true);
      setPreviewError(null);
      setDiff(null);
      const [previewResult, diffResult] = await Promise.allSettled([
        fetchArtifactPreview(artifactId),
        fetchArtifactDiff(artifactId),
      ]);
      if (generation.current !== currentGeneration) return;
      if (objectUrl.current !== null) URL.revokeObjectURL(objectUrl.current);
      objectUrl.current = null;
      if (previewResult.status === "fulfilled") {
        const url = URL.createObjectURL(previewResult.value.blob);
        objectUrl.current = url;
        setPreview({ mode: previewResult.value.mode, url });
      } else {
        setPreview(null);
        setPreviewError(
          previewResult.reason instanceof Error ? previewResult.reason.message : "这个格式暂时无法预览",
        );
      }
      if (diffResult.status === "fulfilled") setDiff(diffResult.value);
      else setDiff(null);
      setLoading(false);
    });
  }, [selected]);

  useEffect(() => () => {
    generation.current += 1;
    if (objectUrl.current !== null) URL.revokeObjectURL(objectUrl.current);
  }, []);

  if (selected === null) return null;
  const mark = fileMark(selected);
  const sha = typeof selected.meta.sha256 === "string" ? selected.meta.sha256 : null;
  const summary = typeof selected.meta.summary === "string" ? selected.meta.summary : null;

  return (
    <aside className="workdesk-artifact-rail" aria-label="Artifact 交付物">
      <header className="artifact-rail-head">
        <div>
          <small>ARTIFACTS</small>
          <strong>交付物</strong>
          <span>{artifacts.length}</span>
        </div>
        <button aria-label="关闭交付物右栏" onClick={onClose} type="button">×</button>
      </header>

      <div className="artifact-rail-list" aria-label="交付物列表">
        {artifacts.map((artifact) => {
          const itemMark = fileMark(artifact);
          return (
            <button
              aria-pressed={artifact.id === selected.id}
              className={artifact.id === selected.id ? "active" : ""}
              key={artifact.id}
              onClick={() => {
                setSelectedId(artifact.id);
                setTab("preview");
              }}
              type="button"
            >
              <span className={itemMark.className}>{itemMark.label}</span>
              <div><strong>{artifact.title}</strong><small>{shortPath(artifact.uri)}</small></div>
              <time>{new Date(artifact.updated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time>
            </button>
          );
        })}
      </div>

      <section className="artifact-rail-detail">
        <header>
          <span className={mark.className}>{mark.label}</span>
          <div>
            <strong>{selected.title}</strong>
            <small title={selected.uri}>{shortPath(selected.uri)}</small>
            {summary !== null && <em>{summary}</em>}
          </div>
        </header>
        <nav aria-label="交付物视图">
          <button aria-selected={tab === "preview"} onClick={() => setTab("preview")} role="tab" type="button">预览</button>
          <button aria-selected={tab === "diff"} onClick={() => setTab("diff")} role="tab" type="button">
            变更
            {diff?.available && <span><b>+{diff.added_lines}</b><i>−{diff.removed_lines}</i></span>}
          </button>
        </nav>
        <div className="artifact-rail-viewport">
          {loading ? (
            <div className="artifact-rail-loading"><i /><span>正在准备安全视图…</span></div>
          ) : tab === "preview" ? (
            preview !== null
              ? <iframe referrerPolicy="no-referrer" sandbox="" src={preview.url} title={`${selected.title} 预览`} />
              : <div className="artifact-rail-empty compact"><span><WorkdeskIcon name="file" /></span><strong>无法预览</strong><p>{previewError ?? "这个格式暂时没有安全预览器。"}</p></div>
          ) : diff !== null ? (
            <DiffView diff={diff} />
          ) : (
            <div className="artifact-rail-empty compact"><span><WorkdeskIcon name="shield" /></span><strong>差异读取失败</strong><p>文件本身仍然可在“预览”中检查。</p></div>
          )}
        </div>
        <footer>
          <span>{preview !== null ? previewModeLabel(preview.mode) : "本机交付物"}</span>
          {sha !== null && <code title={sha}>{sha.slice(0, 8)}</code>}
        </footer>
      </section>
    </aside>
  );
}
