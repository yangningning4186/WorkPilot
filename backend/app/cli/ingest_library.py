"""批量导入本地资料库。

200 篇量级的导入按实测约 166s/篇 PDF 计要跑数小时, 不能挂在 HTTP 连接上
(CLAUDE.md: worker 不依附 HTTP 连接), 也超过 Arq 的 job_timeout。
本入口直接调用 sync_local_dir, 复用同一套增量游标、失败隔离与版本激活逻辑,
中断后重跑会跳过已入库文件。
"""

import argparse
import asyncio
import time
from pathlib import Path

from sqlalchemy import text

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.local_dir import (
    LocalDirSyncResult,
    SyncProgress,
    register_local_dir,
    sync_local_dir,
)
from app.telemetry.llm_calls import SqlLlmCallAudit


async def ingest_library(
    *,
    root: Path | None,
    name: str | None,
    max_chunk_chars: int,
    settings: Settings | None = None,
) -> LocalDirSyncResult:
    settings = settings or Settings()
    allowed_root = Path(settings.local_library_path)
    async with session_factory() as session:
        source = await register_local_dir(
            session,
            requested_root=root or allowed_root,
            allowed_root=allowed_root,
            name=name,
        )
        await session.rollback()

    started = time.perf_counter()
    async with session_factory() as session:
        gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
        result = await sync_local_dir(
            session,
            gateway,
            source_id=source.id,
            allowed_root=allowed_root,
            settings=settings,
            max_chunk_chars=max_chunk_chars,
            on_progress=_print_progress,
        )
    print(
        f"\n完成: added={result.added} updated={result.updated} skipped={result.skipped} "
        f"deleted={result.deleted} failed={result.failed} "
        f"总耗时={time.perf_counter() - started:.1f}s"
    )
    for failure in result.failures:
        print(f"  失败 {failure.source_uri}: {failure.error[:200]}")
    await _print_corpus_summary()
    await close_database()
    return result


def _print_progress(progress: SyncProgress) -> None:
    suffix = f" ← {progress.error[:120]}" if progress.error else ""
    print(
        f"[{progress.index}/{progress.total}] {progress.action:8} "
        f"{progress.elapsed_s:7.1f}s  {progress.source_uri}{suffix}",
        flush=True,
    )


async def _print_corpus_summary() -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT count(DISTINCT d.id) AS documents,
                           count(*) FILTER (WHERE c.is_searchable) AS searchable_chunks
                    FROM documents d
                    JOIN document_versions v ON v.document_id = d.id
                      AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                    LEFT JOIN chunks c ON c.version_id = v.id
                    WHERE d.deleted_at IS NULL
                    """
                )
            )
        ).mappings()
        row = rows.one()
        print(
            f"当前语料: {row['documents']} 篇激活文档 / {row['searchable_chunks']} 个可检索 chunk"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量导入本地资料库(不依附 HTTP 连接)")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="要导入的目录, 必须位于 LOCAL_LIBRARY_PATH 内; 默认取 LOCAL_LIBRARY_PATH 本身",
    )
    parser.add_argument("--name", default=None, help="source 名称, 默认取目录名")
    parser.add_argument("--max-chunk-chars", type=int, default=2000)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        ingest_library(
            root=args.root,
            name=args.name,
            max_chunk_chars=args.max_chunk_chars,
        )
    )
    raise SystemExit(1 if result.failed else 0)


if __name__ == "__main__":
    main()
