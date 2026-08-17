from __future__ import annotations

import os
import socket
from typing import Any
from uuid import UUID

import structlog

from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.memory.extraction import process_memory_job_source
from app.memory.store import (
    claim_memory_job,
    complete_memory_job,
    retry_or_fail_memory_job,
)
from app.services.model_budget import build_cost_guard

logger = structlog.get_logger(__name__)


def memory_worker_identity() -> str:
    return f"memory:{socket.gethostname()}:{os.getpid()}"


async def memory_extraction_job(ctx: dict[str, Any], job_id_raw: str) -> None:
    job_id = UUID(job_id_raw)
    settings = ctx["settings"]
    session_factory = ctx["session_factory"]
    worker_id = memory_worker_identity()

    async with session_factory() as session:
        source = await claim_memory_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            lease_s=settings.memory_job_lease_s,
            max_attempts=settings.memory_job_max_attempts,
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
                operations = await process_memory_job_source(
                    session,
                    gateway,
                    source=source,
                )
                completed = await complete_memory_job(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    operations=operations,
                )
                if not completed:
                    raise RuntimeError("记忆作业租约已丢失")
                await session.commit()
            finally:
                await gateway.aclose()
    except Exception as error:
        logger.exception("长期记忆抽取失败", job_id=str(job_id))
        async with session_factory() as session:
            await retry_or_fail_memory_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                error=str(error),
                max_attempts=settings.memory_job_max_attempts,
            )
            await session.commit()
