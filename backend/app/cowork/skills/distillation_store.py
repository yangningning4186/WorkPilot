"""Skill 蒸馏候选与可靠作业的 PostgreSQL 投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

SkillCandidateStatus = Literal["collecting", "promoted", "needs_review", "rejected"]
SkillJobStatus = Literal["queued", "running", "done", "failed"]


@dataclass(frozen=True)
class SkillCandidateRecord:
    id: UUID
    capability_key: str
    suggested_name: str
    description: str
    skill_md: str
    tools: list[str]
    confidence: float
    status: SkillCandidateStatus
    evidence_count: int
    promoted_name: str | None
    last_run_id: UUID | None
    review_reason: str | None
    promoted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def public(self, *, include_skill_md: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(self.id),
            "capability_key": self.capability_key,
            "suggested_name": self.suggested_name,
            "description": self.description,
            "tools": self.tools,
            "confidence": self.confidence,
            "status": self.status,
            "evidence_count": self.evidence_count,
            "promoted_name": self.promoted_name,
            "review_reason": self.review_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_skill_md:
            payload["skill_md"] = self.skill_md
        return payload


@dataclass(frozen=True)
class SkillDistillationJob:
    id: UUID
    run_id: UUID
    status: SkillJobStatus
    attempts: int
    worker_id: str | None
    lease_until: datetime | None
    available_at: datetime
    candidate_id: UUID | None
    error: str | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source_is_local: bool = False
    source_goal: str | None = None
    source_final_message: str | None = None
    source_tools: list[str] | None = None


@dataclass(frozen=True)
class SkillJobSource:
    job: SkillDistillationJob
    goal: str
    final_message: str
    successful_tools: list[str]


_CANDIDATE_COLUMNS = """
    id, capability_key, suggested_name, description, skill_md, tools, confidence,
    status, evidence_count, promoted_name, last_run_id, review_reason, promoted_at,
    created_at, updated_at
"""
_JOB_COLUMNS = """
    id, run_id, status, attempts, worker_id, lease_until, available_at, candidate_id,
    error, finished_at, created_at, updated_at, source_is_local, source_goal,
    source_final_message, source_tools
"""


def _candidate(row: Any) -> SkillCandidateRecord:
    values = dict(row)
    values["tools"] = [str(item) for item in values["tools"] or []]
    return SkillCandidateRecord(**values)


def _job(row: Any) -> SkillDistillationJob:
    values = dict(row)
    values["source_tools"] = [str(item) for item in values["source_tools"] or []]
    return SkillDistillationJob(**values)


async def schedule_skill_distillation(
    session: AsyncSession,
    *,
    run_id: UUID,
    local_goal: str | None = None,
    local_final_message: str | None = None,
    local_successful_tools: list[str] | None = None,
) -> SkillDistillationJob | None:
    local_mode = any(
        value is not None
        for value in (local_goal, local_final_message, local_successful_tools)
    )
    if local_mode and (
        local_goal is None or local_final_message is None or local_successful_tools is None
    ):
        raise ValueError("SQLite Skill 来源快照字段必须完整")
    if local_mode:
        row = (
            (
                await session.execute(
                    text(
                        f"""
                        INSERT INTO skill_distillation_jobs
                            (id, run_id, source_is_local, source_goal,
                             source_final_message, source_tools)
                        VALUES
                            (:id, :run_id, true, :goal, :final_message,
                             CAST(:tools AS jsonb))
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING {_JOB_COLUMNS}
                        """
                    ),
                    {
                        "id": uuid7(),
                        "run_id": run_id,
                        "goal": local_goal,
                        "final_message": local_final_message,
                        "tools": __import__("json").dumps(
                            local_successful_tools, ensure_ascii=False
                        ),
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
    else:
        row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO skill_distillation_jobs (id, run_id)
                    SELECT :id, ar.id
                    FROM agent_runs ar
                    JOIN conversations c ON c.id = ar.conversation_id
                    WHERE ar.id = :run_id
                      AND ar.status = 'done'
                      AND ar.workflow_type = 'cowork'
                      AND c.scope = 'local_owner'
                      AND c.demo_session_id IS NULL
                    ON CONFLICT (run_id) DO NOTHING
                    RETURNING {_JOB_COLUMNS}
                    """
                ),
                {"id": uuid7(), "run_id": run_id},
            )
        )
        .mappings()
        .one_or_none()
        )
    if row is not None:
        return _job(row)
    existing = (
        (
            await session.execute(
                text(f"SELECT {_JOB_COLUMNS} FROM skill_distillation_jobs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if existing is None else _job(existing)


async def claim_skill_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    lease_s: int,
    max_attempts: int,
) -> SkillJobSource | None:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE skill_distillation_jobs
                    SET status = 'running', worker_id = :worker_id,
                        lease_until = now() + make_interval(secs => :lease_s),
                        attempts = attempts + 1, error = NULL
                    WHERE id = :job_id
                      AND attempts < :max_attempts
                      AND available_at <= now()
                      AND (status = 'queued' OR (status = 'running' AND lease_until < now()))
                    RETURNING {_JOB_COLUMNS}
                    """
                ),
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_s": lease_s,
                    "max_attempts": max_attempts,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    job = _job(row)
    if job.source_is_local:
        if job.source_goal is None or job.source_final_message is None:
            raise LookupError("SQLite Skill 蒸馏来源快照不完整")
        return SkillJobSource(
            job=job,
            goal=job.source_goal,
            final_message=job.source_final_message[:4_000],
            successful_tools=job.source_tools or [],
        )
    source_row = (
        (
            await session.execute(
                text(
                    """
                    SELECT ar.goal, checkpoint.state
                    FROM agent_runs ar
                    JOIN LATERAL (
                        SELECT state FROM agent_checkpoints
                        WHERE run_id = ar.id
                        ORDER BY checkpoint_id DESC LIMIT 1
                    ) checkpoint ON true
                    WHERE ar.id = :run_id AND ar.status = 'done'
                    """
                ),
                {"run_id": job.run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if source_row is None:
        raise LookupError("Skill 蒸馏来源运行或 checkpoint 不存在")
    state = source_row["state"]
    if not isinstance(state, dict):
        raise ValueError("Skill 蒸馏 checkpoint 格式无效")
    return SkillJobSource(
        job=job,
        goal=str(source_row["goal"]),
        final_message=str(state.get("final_message") or "")[:4_000],
        successful_tools=successful_tool_names(state),
    )


def successful_tool_names(state: dict[str, Any]) -> list[str]:
    pending: dict[str, str] = {}
    successful: list[str] = []
    messages = state.get("messages")
    if not isinstance(messages, list):
        return successful
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    continue
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    pending[call["id"]] = function["name"]
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                continue
            try:
                payload = __import__("json").loads(str(message.get("content") or ""))
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("ok") is True:
                successful.append(pending[call_id])
    return list(dict.fromkeys(successful))


async def upsert_skill_candidate(
    session: AsyncSession,
    *,
    run_id: UUID,
    capability_key: str,
    suggested_name: str,
    description: str,
    skill_md: str,
    tools: list[str],
    confidence: float,
) -> SkillCandidateRecord:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO skill_candidates
                        (id, capability_key, suggested_name, description, skill_md, tools,
                         confidence, last_run_id)
                    VALUES
                        (:id, :capability_key, :suggested_name, :description, :skill_md,
                         CAST(:tools AS jsonb), :confidence, :run_id)
                    ON CONFLICT (capability_key) DO UPDATE SET
                        suggested_name = CASE WHEN skill_candidates.status = 'collecting'
                            THEN EXCLUDED.suggested_name ELSE skill_candidates.suggested_name END,
                        description = CASE WHEN skill_candidates.status = 'collecting'
                            THEN EXCLUDED.description ELSE skill_candidates.description END,
                        skill_md = CASE WHEN skill_candidates.status = 'collecting'
                            THEN EXCLUDED.skill_md ELSE skill_candidates.skill_md END,
                        tools = CASE WHEN skill_candidates.status = 'collecting'
                            THEN EXCLUDED.tools ELSE skill_candidates.tools END,
                        confidence = GREATEST(skill_candidates.confidence, EXCLUDED.confidence),
                        last_run_id = EXCLUDED.last_run_id
                    RETURNING {_CANDIDATE_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "capability_key": capability_key,
                    "suggested_name": suggested_name,
                    "description": description,
                    "skill_md": skill_md,
                    "tools": __import__("json").dumps(tools, ensure_ascii=False),
                    "confidence": confidence,
                    "run_id": run_id,
                },
            )
        )
        .mappings()
        .one()
    )
    candidate = _candidate(row)
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO skill_candidate_evidence (candidate_id, run_id)
                VALUES (:candidate_id, :run_id)
                ON CONFLICT DO NOTHING
                RETURNING run_id
                """
            ),
            {"candidate_id": candidate.id, "run_id": run_id},
        )
    ).scalar_one_or_none()
    if inserted is not None:
        refreshed = (
            (
                await session.execute(
                    text(
                        f"""
                        UPDATE skill_candidates
                        SET evidence_count = (
                            SELECT count(*) FROM skill_candidate_evidence
                            WHERE candidate_id = skill_candidates.id
                        )
                        WHERE id = :candidate_id
                        RETURNING {_CANDIDATE_COLUMNS}
                        """
                    ),
                    {"candidate_id": candidate.id},
                )
            )
            .mappings()
            .one()
        )
        candidate = _candidate(refreshed)
    return candidate


async def set_candidate_status(
    session: AsyncSession,
    *,
    candidate_id: UUID,
    status: SkillCandidateStatus,
    promoted_name: str | None = None,
    review_reason: str | None = None,
) -> SkillCandidateRecord:
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE skill_candidates
                    SET status = :status, promoted_name = :promoted_name,
                        review_reason = :review_reason,
                        promoted_at = CASE WHEN :status = 'promoted' THEN now() ELSE promoted_at END
                    WHERE id = :candidate_id
                    RETURNING {_CANDIDATE_COLUMNS}
                    """
                ),
                {
                    "candidate_id": candidate_id,
                    "status": status,
                    "promoted_name": promoted_name,
                    "review_reason": review_reason,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError(str(candidate_id))
    return _candidate(row)


async def get_skill_candidate(
    session: AsyncSession, candidate_id: UUID
) -> SkillCandidateRecord | None:
    row = (
        (
            await session.execute(
                text(f"SELECT {_CANDIDATE_COLUMNS} FROM skill_candidates WHERE id = :id"),
                {"id": candidate_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _candidate(row)


async def list_skill_candidates(
    session: AsyncSession, *, limit: int = 100
) -> list[SkillCandidateRecord]:
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_CANDIDATE_COLUMNS} FROM skill_candidates
                    ORDER BY CASE status WHEN 'needs_review' THEN 0 WHEN 'collecting' THEN 1
                        WHEN 'promoted' THEN 2 ELSE 3 END, updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [_candidate(row) for row in rows]


async def complete_skill_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    candidate_id: UUID | None,
) -> bool:
    updated = (
        await session.execute(
            text(
                """
                UPDATE skill_distillation_jobs
                SET status = 'done', candidate_id = :candidate_id, worker_id = NULL,
                    lease_until = NULL, finished_at = now(), error = NULL
                WHERE id = :job_id AND status = 'running' AND worker_id = :worker_id
                RETURNING id
                """
            ),
            {"job_id": job_id, "worker_id": worker_id, "candidate_id": candidate_id},
        )
    ).scalar_one_or_none()
    return updated is not None


async def retry_or_fail_skill_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    worker_id: str,
    error: str,
    max_attempts: int,
) -> None:
    await session.execute(
        text(
            """
            UPDATE skill_distillation_jobs
            SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
                available_at = CASE WHEN attempts >= :max_attempts THEN available_at
                    ELSE now() + make_interval(secs => LEAST(300, 5 * attempts * attempts)) END,
                worker_id = NULL, lease_until = NULL, error = :error,
                finished_at = CASE WHEN attempts >= :max_attempts THEN now() ELSE NULL END
            WHERE id = :job_id AND status = 'running' AND worker_id = :worker_id
            """
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "error": error[:2_000],
            "max_attempts": max_attempts,
        },
    )


async def list_dispatchable_skill_jobs(
    session: AsyncSession, *, max_attempts: int, limit: int = 100
) -> list[tuple[UUID, int]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id, attempts FROM skill_distillation_jobs
                WHERE attempts < :max_attempts AND available_at <= now()
                  AND (status = 'queued' OR (status = 'running' AND lease_until < now()))
                ORDER BY available_at, id LIMIT :limit
                """
            ),
            {"max_attempts": max_attempts, "limit": limit},
        )
    ).all()
    return [(UUID(str(row.id)), int(row.attempts)) for row in rows]
