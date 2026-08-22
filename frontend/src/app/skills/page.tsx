"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  deleteSkill,
  fetchSkillCandidates,
  fetchSkillsStatus,
  promoteSkillCandidate,
  rejectSkillCandidate,
  saveSkill,
  setSkillEnabled,
  type SkillsStatusResponse,
  type SkillCandidatesResponse,
} from "@/lib/api";

function skillError(reason: unknown): string {
  if (reason instanceof ApiError) return `Skill 目录读取失败（${reason.status}）`;
  return "暂时无法读取本地 Skill 目录。";
}

export default function SkillsPage() {
  const [status, setStatus] = useState<SkillsStatusResponse | null>(null);
  const [candidates, setCandidates] = useState<SkillCandidatesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [skillName, setSkillName] = useState("");
  const [skillMd, setSkillMd] = useState("---\nname: my-skill\ndescription: Describe when this workflow is useful\ntrigger:\n  - example task\nanti_trigger:\n  - unrelated task\ntools: []\n---\n\nWrite the procedure here.\n");
  const [busy, setBusy] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [skillStatus, candidateStatus] = await Promise.all([
        fetchSkillsStatus(),
        fetchSkillCandidates(),
      ]);
      setStatus(skillStatus);
      setCandidates(candidateStatus);
      setError(null);
    } catch (reason) {
      setError(skillError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  const submit = async () => {
    setBusy("save");
    try {
      await saveSkill(skillName.trim(), skillMd, status?.installed.some((item) => item.name === skillName.trim()) ?? false);
      setShowEditor(false);
      await reload();
    } catch (reason) {
      setError(skillError(reason));
    } finally {
      setBusy(null);
    }
  };

  const reviewCandidate = async (capabilityKey: string, action: "promote" | "reject") => {
    setBusy(capabilityKey);
    try {
      if (action === "promote") await promoteSkillCandidate(capabilityKey);
      else await rejectSkillCandidate(capabilityKey);
      await reload();
    } catch (reason) {
      setError(skillError(reason));
    } finally {
      setBusy(null);
    }
  };

  const mutate = async (name: string, action: "toggle" | "delete") => {
    const item = status?.installed.find((entry) => entry.name === name);
    if (item === undefined) return;
    if (action === "delete" && !window.confirm(`卸载 Skill“${name}”？其附带资源也会删除。`)) return;
    setBusy(name);
    try {
      if (action === "delete") await deleteSkill(name);
      else await setSkillEnabled(name, !item.enabled);
      await reload();
    } catch (reason) {
      setError(skillError(reason));
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  return (
    <WorkdeskAppShell icon="skill" sectionTitle="Skills">
      <section className="integration-page workdesk-route-surface">
        <header className="integration-hero">
          <div className="integration-hero-mark"><WorkdeskIcon name="skill" /></div>
          <div>
            <span>LOCAL PROCEDURES</span>
            <h1>Skills</h1>
            <p>按需加载的本地工作流程。Agent 只先看到摘要，命中任务后才读取完整步骤。</p>
          </div>
          <button disabled={loading} onClick={() => setShowEditor((value) => !value)} type="button">{showEditor ? "收起编辑器" : "＋ 新建 Skill"}</button>
        </header>

        {error !== null && <div className="integration-notice error">{error}</div>}
        {showEditor && <section className="integration-editor"><header><div><span>SKILL LIFECYCLE</span><h2>安装或更新 Skill</h2></div><small>保存前会校验 frontmatter 与目录名</small></header><div className="integration-form-grid"><label className="wide"><span>Skill 名称</span><input onChange={(event) => setSkillName(event.target.value)} placeholder="小写字母、数字、_、-" value={skillName} /></label><label className="wide"><span>SKILL.md</span><textarea onChange={(event) => setSkillMd(event.target.value)} value={skillMd} /></label></div><footer><span>运行时只加载启用 Skill 的摘要；正文与资源按需读取。</span><button disabled={busy !== null || !skillName.trim() || !skillMd.trim()} onClick={() => void submit()} type="button">{busy === "save" ? "正在校验…" : "安装 Skill"}</button></footer></section>}
        {status !== null && (
          <>
            <section className="integration-summary" aria-label="Skill 目录状态">
              <div><strong>{status.installed.length}</strong><span>已安装</span></div>
              <div><strong>{status.skills.length}</strong><span>已启用</span></div>
              <div><strong>{status.errors.length}</strong><span>目录错误</span></div>
              <div><strong>{candidates?.items.filter((item) => item.status === "collecting" || item.status === "needs_review").length ?? 0}</strong><span>蒸馏候选</span></div>
              <div className="wide"><span>来源目录</span><code>{status.source_path}</code></div>
            </section>

            {candidates !== null && (
              <section className="skill-distillation-panel">
                <header>
                  <div><span>AUTO DISTILLATION</span><h2>Skills 自动蒸馏与晋升</h2></div>
                  <small>{candidates.enabled ? `连续 ${candidates.min_evidence} 次独立成功 · 置信度 ≥ ${(candidates.min_confidence * 100).toFixed(0)}%` : "当前已关闭"}</small>
                </header>
                <p>候选只学习成功工具链，不读取文件正文；Shell、外部动作和 MCP 工具不会自动晋升。</p>
                {candidates.items.length === 0 ? <div className="skill-candidate-empty">完成可复用的 Cowork 流程后，候选会在这里积累证据。</div> : (
                  <div className="skill-candidate-list">
                    {candidates.items.map((candidate) => <article key={candidate.capability_key}>
                      <header><div><strong>{candidate.suggested_name}</strong><code>{candidate.capability_key}</code></div><span className={`status ${candidate.status}`}>{candidate.status === "collecting" ? "积累证据" : candidate.status === "promoted" ? "已晋升" : candidate.status === "needs_review" ? "需要复核" : "已拒绝"}</span></header>
                      <p>{candidate.description}</p>
                      <div className="skill-candidate-meter"><i style={{ width: `${Math.min(100, candidate.evidence_count / candidates.min_evidence * 100)}%` }} /><span>{candidate.evidence_count} / {candidates.min_evidence} 次成功证据 · {(candidate.confidence * 100).toFixed(0)}%</span></div>
                      {candidate.tools.length > 0 && <footer>{candidate.tools.map((tool) => <code key={tool}>{tool}</code>)}</footer>}
                      {candidate.review_reason !== null && <small>{candidate.review_reason}</small>}
                      {(candidate.status === "collecting" || candidate.status === "needs_review") && <div className="skill-candidate-actions"><button disabled={busy !== null} onClick={() => void reviewCandidate(candidate.capability_key, "promote")} type="button">立即晋升</button><button className="danger" disabled={busy !== null} onClick={() => void reviewCandidate(candidate.capability_key, "reject")} type="button">拒绝</button></div>}
                    </article>)}
                  </div>
                )}
              </section>
            )}

            {status.errors.length > 0 && (
              <section className="integration-notice error">
                <strong>有些 Skill 没有加载</strong>
                {status.errors.map((item) => <p key={item}>{item}</p>)}
              </section>
            )}

            {status.installed.length === 0 ? (
              <section className="integration-empty">
                <span><WorkdeskIcon name="skill" /></span>
                <h2>还没有启用的 Skill</h2>
                <p>在下方目录建立一个同名文件夹，并添加带 YAML frontmatter 的 SKILL.md。</p>
                <code>{status.source_path}/&lt;skill-name&gt;/SKILL.md</code>
              </section>
            ) : (
              <section className="skill-grid" aria-label="Skills 列表">
                {status.installed.map((managed) => {
                  return <article className={`skill-card${managed.enabled ? "" : " disabled"}`} key={managed.name}>
                    <header><span><WorkdeskIcon name="skill" /></span><div><h2>{managed.name}</h2><code>{managed.sha256?.slice(0, 10) ?? "invalid"}</code></div></header>
                    <p>{managed.description ?? managed.error ?? "Skill 配置无效"}</p>
                    <dl>
                      <div><dt>状态</dt><dd>{managed.enabled ? "运行时可用" : "已停用"}</dd></div>
                      <div><dt>资源</dt><dd>{managed.resources.length} 个随附文件</dd></div>
                    </dl>
                    <footer>
                      <button disabled={busy !== null || managed.error !== null} onClick={() => void mutate(managed.name, "toggle")} type="button">{managed.enabled ? "停用" : "启用"}</button><button className="danger" disabled={busy !== null} onClick={() => void mutate(managed.name, "delete")} type="button">卸载</button>
                    </footer>
                  </article>;
                })}
              </section>
            )}

            <p className="integration-snapshot">目录快照 · {status.snapshot_sha256}</p>
          </>
        )}
      </section>
    </WorkdeskAppShell>
  );
}
