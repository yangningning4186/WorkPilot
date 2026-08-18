"use client";

import { useCallback, useEffect, useState } from "react";

import { WorkdeskAppShell, WorkdeskIcon } from "@/components/workdesk-shell";
import { ApiError, fetchSkillsStatus, type SkillsStatusResponse } from "@/lib/api";

function skillError(reason: unknown): string {
  if (reason instanceof ApiError) return `Skill 目录读取失败（${reason.status}）`;
  return "暂时无法读取本地 Skill 目录。";
}

export default function SkillsPage() {
  const [status, setStatus] = useState<SkillsStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
          <button disabled={loading} onClick={() => void reload()} type="button">
            {loading ? "读取中…" : "刷新目录"}
          </button>
        </header>

        {error !== null && <div className="integration-notice error">{error}</div>}
        {status !== null && (
          <>
            <section className="integration-summary" aria-label="Skill 目录状态">
              <div><strong>{status.skills.length}</strong><span>可用 Skills</span></div>
              <div><strong>{status.errors.length}</strong><span>目录错误</span></div>
              <div className="wide"><span>来源目录</span><code>{status.source_path}</code></div>
            </section>

            {status.errors.length > 0 && (
              <section className="integration-notice error">
                <strong>有些 Skill 没有加载</strong>
                {status.errors.map((item) => <p key={item}>{item}</p>)}
              </section>
            )}

            {status.skills.length === 0 ? (
              <section className="integration-empty">
                <span><WorkdeskIcon name="skill" /></span>
                <h2>还没有启用的 Skill</h2>
                <p>在下方目录建立一个同名文件夹，并添加带 YAML frontmatter 的 SKILL.md。</p>
                <code>{status.source_path}/&lt;skill-name&gt;/SKILL.md</code>
              </section>
            ) : (
              <section className="skill-grid" aria-label="Skills 列表">
                {status.skills.map((skill) => (
                  <article className="skill-card" key={skill.name}>
                    <header><span><WorkdeskIcon name="skill" /></span><div><h2>{skill.name}</h2><code>{skill.sha256.slice(0, 10)}</code></div></header>
                    <p>{skill.description}</p>
                    <dl>
                      <div><dt>适用</dt><dd>{skill.trigger.length > 0 ? skill.trigger.join(" · ") : "由描述匹配"}</dd></div>
                      <div><dt>不适用</dt><dd>{skill.anti_trigger.length > 0 ? skill.anti_trigger.join(" · ") : "未声明"}</dd></div>
                    </dl>
                    <footer>
                      {skill.tools.length === 0 ? <span>不限定工具</span> : skill.tools.map((tool) => <span key={tool}>{tool}</span>)}
                    </footer>
                  </article>
                ))}
              </section>
            )}

            <p className="integration-snapshot">目录快照 · {status.snapshot_sha256}</p>
          </>
        )}
      </section>
    </WorkdeskAppShell>
  );
}
