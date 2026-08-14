"""为已解析版本离线构建 fixed / recursive / semantic chunk。"""

import argparse
import asyncio
from uuid import UUID

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.services.chunk_building import (
    CHUNK_STRATEGIES,
    ChunkStrategy,
    build_chunk_strategies,
    list_active_version_ids,
)


async def build_offline_chunks(
    *,
    version_ids: list[UUID] | None,
    strategies: list[ChunkStrategy],
    settings: Settings | None = None,
) -> int:
    settings = settings or Settings()
    failures = 0
    async with session_factory() as session:
        targets = version_ids or await list_active_version_ids(session)
        gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
        try:
            for index, version_id in enumerate(targets, start=1):
                try:
                    result = await build_chunk_strategies(
                        session,
                        gateway,
                        version_id=version_id,
                        strategies=strategies,
                    )
                except Exception as error:
                    await session.rollback()
                    failures += 1
                    print(f"[{index}/{len(targets)}] {version_id} failed: {error}", flush=True)
                    continue
                summary = ", ".join(
                    f"{item.strategy}={item.chunk_count}"
                    f"({'rebuilt' if item.rebuilt else 'unchanged'})"
                    for item in result.strategies
                )
                print(f"[{index}/{len(targets)}] {version_id} {summary}", flush=True)
        finally:
            await gateway.aclose()
    await close_database()
    return failures


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="离线构建三套实验 chunk; 默认处理所有已激活 document version"
    )
    parser.add_argument(
        "--version-id",
        action="append",
        type=UUID,
        default=None,
        help="只处理指定 version, 可重复传入",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=CHUNK_STRATEGIES,
        default=None,
        help="只构建指定策略, 可重复传入; 默认三套全部构建",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    strategies: list[ChunkStrategy] = args.strategy or list(CHUNK_STRATEGIES)
    failures = asyncio.run(
        build_offline_chunks(version_ids=args.version_id, strategies=strategies)
    )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
