"""Skill 自动蒸馏与确定性晋升门禁。"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any
from uuid import UUID

import structlog

from app.core.config import Settings
from app.cowork.provider_profiles import build_conversation_gateway
from app.cowork.skills.candidate_store import (
    claim_skill_job,
    complete_skill_job,
    retry_or_fail_skill_job,
    set_candidate_status,
    upsert_skill_candidate,
)
from app.cowork.skills.distillation import DistilledSkill, distill_skill_candidate
from app.cowork.skills.lifecycle import (
    AUTO_DISTILLED_PROVENANCE_PURPOSE,
    install_auto_distilled_skill,
)
from app.cowork_store.routing import cowork_store
from app.security.secret_store import LocalSecretStore

logger = structlog.get_logger(__name__)


def skill_worker_identity() -> str:
    return f"skill:{socket.gethostname()}:{os.getpid()}"


async def skill_distillation_job(ctx: dict[str, Any], run_id_raw: str) -> None:
    """作业标识就是被蒸馏的 run：候选和队列都在目录里，不再有独立的作业主键。"""

    run_id = UUID(run_id_raw)
    settings: Settings = ctx["settings"]
    if not settings.skill_distillation_enabled:
        return
    root = settings.cowork_skill_candidates_path
    worker_id = skill_worker_identity()

    source = await asyncio.to_thread(
        claim_skill_job,
        root,
        run_id=run_id,
        worker_id=worker_id,
        lease_s=settings.skill_distillation_job_lease_s,
        max_attempts=settings.skill_distillation_job_max_attempts,
    )
    if source is None:
        return

    try:
        store = cowork_store()
        run = None if store is None else await store.get_run(run_id)
        if run is None:
            raise RuntimeError("Skill 蒸馏作业缺少来源运行，无法解析用户选择的 Provider")
        session_factory = ctx["session_factory"]
        async with session_factory() as session:
            gateway = await build_conversation_gateway(
                session,
                conversation_id=run.conversation_id,
                settings=settings,
                session_factory=session_factory,
                run_id=run_id,
            )
        try:
            distilled = await distill_skill_candidate(
                gateway,
                source=source,
                max_tokens=settings.skill_distillation_max_tokens,
            )
        finally:
            await gateway.aclose()

        if distilled is not None:
            await asyncio.to_thread(
                _record_and_gate,
                settings,
                run_id,
                distilled,
                review_required_tools=source.review_required_tools,
            )
        if not await asyncio.to_thread(
            complete_skill_job, root, run_id=run_id, worker_id=worker_id
        ):
            raise RuntimeError("Skill 蒸馏作业租约已丢失")
    except Exception as error:
        # 来源正文可能进入 provider 异常；日志与失败 tombstone 都只保留异常类型。
        logger.error(
            "Skill 自动蒸馏失败",
            run_id=str(run_id),
            error_type=type(error).__name__,
        )
        await asyncio.to_thread(
            retry_or_fail_skill_job,
            root,
            run_id=run_id,
            worker_id=worker_id,
            error=type(error).__name__,
            max_attempts=settings.skill_distillation_job_max_attempts,
        )


def _record_and_gate(
    settings: Settings,
    run_id: UUID,
    distilled: DistilledSkill,
    *,
    review_required_tools: tuple[str, ...],
) -> None:
    """落一条证据，再跑确定性晋升门禁。

    门禁不是模型自己决定的：证据条数和置信度都由服务端比对配置阈值。
    """

    root = settings.cowork_skill_candidates_path
    candidate = upsert_skill_candidate(
        root,
        run_id=run_id,
        capability_key=distilled.capability_key,
        suggested_name=distilled.name,
        description=distilled.description,
        skill_md=distilled.skill_md,
        tools=distilled.tools,
        confidence=distilled.confidence,
    )
    if candidate.status != "collecting":
        return
    referenced_review_tools = sorted(set(distilled.tools) & set(review_required_tools))
    if referenced_review_tools:
        set_candidate_status(
            root,
            capability_key=candidate.capability_key,
            status="needs_review",
            review_reason=(
                "流程引用了不能自动固化的副作用、动态授权或持久控制面工具："
                + "、".join(referenced_review_tools)
            ),
        )
        return
    if candidate.evidence_count < settings.skill_promotion_min_evidence:
        return
    if candidate.confidence < settings.skill_promotion_min_confidence:
        set_candidate_status(
            root,
            capability_key=candidate.capability_key,
            status="needs_review",
            review_reason=(
                f"置信度 {candidate.confidence:.2f} 低于自动晋升门槛 "
                f"{settings.skill_promotion_min_confidence:.2f}"
            ),
        )
        return
    if not settings.skill_auto_promotion_enabled:
        return
    try:
        install_auto_distilled_skill(
            settings.cowork_skills_path,
            name=candidate.suggested_name,
            capability_key=candidate.capability_key,
            skill_md=candidate.skill_md,
            max_bytes=settings.cowork_skill_max_bytes,
            provenance_signing_key=LocalSecretStore(
                settings.secret_store_key_path
            ).derive_signing_key(AUTO_DISTILLED_PROVENANCE_PURPOSE),
        )
    except FileExistsError as error:
        set_candidate_status(
            root,
            capability_key=candidate.capability_key,
            status="needs_review",
            review_reason=str(error),
        )
    else:
        set_candidate_status(
            root,
            capability_key=candidate.capability_key,
            status="promoted",
            promoted_name=candidate.suggested_name,
        )
