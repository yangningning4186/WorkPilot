"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import {
  ApiError,
  deleteSkill,
  fetchSkillsStatus,
  saveSkill,
  setSkillEnabled,
  type SkillsStatusResponse,
} from "@/lib/api";

function skillError(reason: unknown): string {
  if (reason instanceof ApiError) return `Skill 目录读取失败（${reason.status}）`;
  return "暂时无法读取本地 Skill 目录。";
}

export default function SkillsPage() {
  const [status, setStatus] = useState<SkillsStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [skillName, setSkillName] = useState("");
  const [skillMd, setSkillMd] = useState("---\nname: my-skill\ndescription: Describe when this workflow is useful\ntrigger:\n  - example task\nanti_trigger:\n  - unrelated task\ntools: []\n---\n\nWrite the procedure here.\n");
  const [busy, setBusy] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await fetchSkillsStatus());
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
              <div className="wide"><span>来源目录</span><code>{status.source_path}</code></div>
            </section>

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
