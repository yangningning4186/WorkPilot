"""长期记忆抽取作业。"""

from __future__ import annotations

import os
import socket
from typing import Any
from uuid import UUID

import structlog

from app.cowork.memory import (
    claim_memory_job,
    complete_memory_job,
    retry_or_fail_memory_job,
)
from app.cowork.memory_extraction import process_memory_job_source
from app.llm_bootstrap import build_model_gateway
from app.telemetry import default_telemetry_store
from app.telemetry.model_budget import build_cost_guard

logger = structlog.get_logger(__name__)


def memory_worker_identity() -> str:
    return f"memory:{socket.gethostname()}:{os.getpid()}"


async def memory_extraction_job(ctx: dict[str, Any], job_id_raw: str) -> None:
    job_id = UUID(job_id_raw)
    settings = ctx["settings"]
    if not settings.memory_extraction_enabled:
        return
    worker_id = memory_worker_identity()

    source = await claim_memory_job(
        job_id=job_id,
        worker_id=worker_id,
        lease_s=settings.memory_job_lease_s,
        max_attempts=settings.memory_job_max_attempts,
    )
    if source is None:
        return

    try:
        telemetry = default_telemetry_store()
        gateway = build_model_gateway(
            settings,
            audit_sink=telemetry,
            budget_guard=build_cost_guard(settings, telemetry),
            run_id=source.run_id,
        )
        try:
            await process_memory_job_source(gateway, source=source)
        finally:
            await gateway.aclose()
        if not await complete_memory_job(job_id=job_id, worker_id=worker_id):
            raise RuntimeError("记忆作业租约已丢失")
    except Exception as error:
        logger.exception("长期记忆抽取失败", job_id=str(job_id))
        await retry_or_fail_memory_job(
            job_id=job_id,
            worker_id=worker_id,
            error=str(error),
            max_attempts=settings.memory_job_max_attempts,
            retry_delay_s=settings.memory_job_retry_delay_s,
        )
