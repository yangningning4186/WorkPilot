"""Skill 自动蒸馏与确定性晋升门禁。"""

from __future__ import annotations

import os
import socket
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings
from app.cowork.skills.distillation import distill_skill_candidate
from app.cowork.skills.distillation_store import (
    claim_skill_job,
    complete_skill_job,
    retry_or_fail_skill_job,
    set_candidate_status,
    upsert_skill_candidate,
)
from app.cowork.skills.lifecycle import install_auto_distilled_skill
from app.llm_bootstrap import build_model_gateway
from app.telemetry.llm_calls import SqlLlmCallAudit
from app.telemetry.model_budget import build_cost_guard

logger = structlog.get_logger(__name__)


def skill_worker_identity() -> str:
    return f"skill:{socket.gethostname()}:{os.getpid()}"


async def skill_distillation_job(ctx: dict[str, Any], job_id_raw: str) -> None:
    job_id = UUID(job_id_raw)
    settings: Settings = ctx["settings"]
    if not settings.skill_distillation_enabled:
        return
    session_factory = ctx["session_factory"]
    worker_id = skill_worker_identity()

    async with session_factory() as session:
        source = await claim_skill_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            lease_s=settings.skill_distillation_job_lease_s,
            max_attempts=settings.skill_distillation_job_max_attempts,
        )
        await session.commit()
    if source is None:
        return

    try:
        async with session_factory() as session:
            gateway = build_model_gateway(
                settings,
                audit_sink=SqlLlmCallAudit(session),
                budget_guard=build_cost_guard(settings, session_factory),
                run_id=source.job.run_id,
            )
            try:
                distilled = await distill_skill_candidate(
                    gateway,
                    source=source,
                    max_tokens=settings.skill_distillation_max_tokens,
                )
            finally:
                await gateway.aclose()

            candidate_id: UUID | None = None
            if distilled is not None:
                candidate = await upsert_skill_candidate(
                    session,
                    run_id=source.job.run_id,
                    capability_key=distilled.capability_key,
                    suggested_name=distilled.name,
                    description=distilled.description,
                    skill_md=distilled.skill_md,
                    tools=distilled.tools,
                    confidence=distilled.confidence,
                )
                candidate_id = candidate.id
                if (
                    candidate.status == "collecting"
                    and settings.skill_auto_promotion_enabled
                    and candidate.evidence_count >= settings.skill_promotion_min_evidence
                    and candidate.confidence >= settings.skill_promotion_min_confidence
                ):
                    try:
                        install_auto_distilled_skill(
                            settings.cowork_skills_path,
                            name=candidate.suggested_name,
                            capability_key=candidate.capability_key,
                            skill_md=candidate.skill_md,
                            max_bytes=settings.cowork_skill_max_bytes,
                        )
                    except FileExistsError as error:
                        await set_candidate_status(
                            session,
                            candidate_id=candidate.id,
                            status="needs_review",
                            review_reason=str(error),
                        )
                    else:
                        await set_candidate_status(
                            session,
                            candidate_id=candidate.id,
                            status="promoted",
                            promoted_name=candidate.suggested_name,
                        )
                elif (
                    candidate.status == "collecting"
                    and candidate.evidence_count >= settings.skill_promotion_min_evidence
                    and candidate.confidence < settings.skill_promotion_min_confidence
                ):
                    await set_candidate_status(
                        session,
                        candidate_id=candidate.id,
                        status="needs_review",
                        review_reason=(
                            f"置信度 {candidate.confidence:.2f} 低于自动晋升门槛 "
                            f"{settings.skill_promotion_min_confidence:.2f}"
                        ),
                    )
            completed = await complete_skill_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                candidate_id=candidate_id,
            )
            if not completed:
                raise RuntimeError("Skill 蒸馏作业租约已丢失")
            await session.commit()
    except Exception as error:
        logger.exception("Skill 自动蒸馏失败", job_id=str(job_id))
        async with session_factory() as session:
            await retry_or_fail_skill_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                error=str(error),
                max_attempts=settings.skill_distillation_job_max_attempts,
            )
            await session.commit()
