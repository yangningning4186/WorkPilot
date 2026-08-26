"use client";

import { useEffect, useState } from "react";

import { fetchRunEventLog } from "@/lib/api";
import {
  applyCoworkEvent,
  createEmptyCoworkRunView,
  type CoworkModelStage,
  type CoworkRunView,
} from "@/lib/use-cowork-run";
import { TeamBoardPanel } from "@/components/run-activity-panel";

function StageList({ stages }: { stages: CoworkModelStage[] }) {
  return (
    <ol className="workdesk-stage-list">
      {stages.map((stage, index) => (
        <li key={stage.id}>
          <header>
            <strong>阶段 {String(index + 1).padStart(2, "0")}</strong>
            <span>{stage.text !== "" ? "阶段说明" : "思考记录"}</span>
          </header>
          {stage.reasoning !== "" && (
            <details className="workdesk-stage-thinking">
              <summary>思考过程</summary>
              <p>{stage.reasoning}</p>
            </details>
          )}
          {stage.text !== "" && <p className="workdesk-stage-output">{stage.text}</p>}
        </li>
      ))}
    </ol>
  );
}

export function RunStageHistory({ stages }: { stages: CoworkModelStage[] }) {
  if (stages.length === 0) return null;
  return (
    <details className="workdesk-stage-history">
      <summary>
        <span>阶段记录</span>
        <small>{stages.length} 个阶段</small>
      </summary>
      <StageList stages={stages} />
    </details>
  );
}

export function HistoricalRunStageHistory({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<CoworkRunView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || view !== null || error !== null) return;
    let cancelled = false;
    void Promise.resolve().then(async () => {
      try {
        let replay = createEmptyCoworkRunView("connecting");
        let cursor = 0n;
        while (!cancelled) {
          const response = await fetchRunEventLog(runId, cursor);
          if (response.items.length === 0) break;
          replay = response.items.reduce(applyCoworkEvent, replay);
          const nextCursor = replay.cursor;
          if (nextCursor <= cursor || response.items.length < 200) break;
          cursor = nextCursor;
        }
        if (!cancelled) setView(replay);
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "无法读取阶段记录");
        }
      }
    });
    return () => {
      cancelled = true;
    };
  }, [error, open, runId, view]);

  return (
    <section className="workdesk-historical-stages">
      <button aria-expanded={open} onClick={() => setOpen((value) => !value)} type="button">
        <span>阶段记录</span>
        <small>
          {view === null
            ? ""
            : `${view.modelStages.length} 个阶段${view.team === null ? "" : ` · ${view.team.tasks.length} 个团队任务`}`}
        </small>
        <i aria-hidden>⌄</i>
      </button>
      {open && (
        <div className="workdesk-historical-stages-body">
          {error !== null ? (
            <p className="error">{error}</p>
          ) : view === null ? (
            <p className="loading"><i />正在回放运行记录…</p>
          ) : view.modelStages.length === 0 && view.team === null ? (
            <p className="empty">这次运行没有阶段性说明或思考记录。</p>
          ) : (
            <>
              <TeamBoardPanel team={view.team} />
              <StageList stages={view.modelStages} />
            </>
          )}
        </div>
      )}
    </section>
  );
}
