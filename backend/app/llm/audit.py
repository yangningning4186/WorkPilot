from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.llm.types import AuditRecord


class SqlLlmCallAudit:
    """写入调用事实；事务提交由调用方统一控制。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: AuditRecord) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO llm_calls
                    (id, trace_id, task_type, tier, model, provider,
                     prompt_tokens, output_tokens, latency_ms, success)
                VALUES
                    (:id, :trace_id, :task_type, :tier, :model, :provider,
                     :input_tokens, :output_tokens, :latency_ms, :success)
                """
            ),
            {"id": uuid7(), **vars(call)},
        )
