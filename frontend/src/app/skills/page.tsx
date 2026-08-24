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
  type ManagedSkill,
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
      const conversationId = new URLSearchParams(window.location.search).get("conversation")
        ?? undefined;
      const [skillStatus, candidateStatus] = await Promise.all([
        fetchSkillsStatus(conversationId),
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

  // 两层之后同一个 name 可能有两条（出厂那份 + 盖住它的 fork），所以传整条记录，
  // 不再按 name 去列表里反查——反查会拿到排在前面的那条，也就是出厂那份，
  // 于是"停用"点在了实际没有生效的那一份上。
  const mutate = async (item: ManagedSkill, action: "toggle" | "delete") => {
    if (action === "delete" && !item.removable) return;
    if (action === "delete" && !window.confirm(`卸载 Skill“${item.name}”？其附带资源也会删除。`)) return;
    setBusy(`${item.origin}:${item.name}`);
    try {
      if (action === "delete") await deleteSkill(item.name);
      else await setSkillEnabled(item.name, !item.enabled);
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
              <div><strong>{status.installed.filter((item) => item.origin === "builtin").length}</strong><span>出厂自带</span></div>
              <div><strong>{status.installed.filter((item) => item.origin === "user").length}</strong><span>自己安装</span></div>
              <div><strong>{status.skills.filter((item) => item.origin === "project").length}</strong><span>项目随附</span></div>
              <div><strong>{status.skills.length}</strong><span>已启用</span></div>
              <div><strong>{status.errors.length}</strong><span>目录错误</span></div>
              <div><strong>{candidates?.items.filter((item) => item.status === "collecting" || item.status === "needs_review").length ?? 0}</strong><span>蒸馏候选</span></div>
              <div className="wide"><span>来源目录</span><code>{status.source_path}</code></div>
              <div className="wide"><span>出厂目录（只读）</span><code>{status.builtin_path}</code></div>
              {status.project_paths.map((path) => <div className="wide" key={path}><span>项目目录</span><code>{path}</code></div>)}
            </section>

            {status.skills.some((item) => item.origin === "project") && (
              <section className="integration-notice">
                <strong>当前会话的项目 Skills</strong>
                <p>{status.skills.filter((item) => item.origin === "project").map((item) => item.name).join(" · ")}</p>
                <small>它们来自已授权工作区的 .workpilot/skills，随仓库版本控制，并覆盖同名用户或出厂 Skill。</small>
              </section>
            )}

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
                  const key = `${managed.origin}:${managed.name}`;
                  const builtin = managed.origin === "builtin";
                  return <article className={`skill-card${managed.enabled && !managed.shadowed ? "" : " disabled"}`} key={key}>
                    <header><span><WorkdeskIcon name="skill" /></span><div><h2>{managed.name}</h2><code>{managed.sha256?.slice(0, 10) ?? "invalid"}</code></div><small className={`skill-origin ${managed.origin}`}>{builtin ? "出厂自带" : "自己安装"}</small></header>
                    <p>{managed.description ?? managed.error ?? "Skill 配置无效"}</p>
                    <dl>
                      <div><dt>状态</dt><dd>{managed.shadowed ? "已被同名 Skill 覆盖" : managed.enabled ? "运行时可用" : "已停用"}</dd></div>
                      <div><dt>资源</dt><dd>{managed.resources.length} 个随附文件</dd></div>
                    </dl>
                    {builtin && !managed.shadowed && <small className="skill-card-note">出厂 Skill 不能卸载。装一个同名 Skill 即可覆盖它的流程，删掉那份就会恢复。</small>}
                    <footer>
                      <button disabled={busy !== null || managed.error !== null || managed.shadowed} onClick={() => void mutate(managed, "toggle")} type="button">{managed.enabled ? "停用" : "启用"}</button>
                      {managed.removable && <button className="danger" disabled={busy !== null} onClick={() => void mutate(managed, "delete")} type="button">卸载</button>}
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
