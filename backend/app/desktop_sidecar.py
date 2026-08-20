"""Tauri 桌面壳的单一后端可执行文件入口。

开发时可用 ``uv run python -m app.desktop_sidecar``；发布时将该模块打包为
``workpilot-sidecar``，Tauri 以 migrate / api / worker 三个子命令启动。
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import uvicorn
from alembic import command
from alembic.config import Config
from arq.worker import run_worker

from app.worker.main import WorkerSettings


async def _migrate_cowork_control_plane() -> None:
    from app.core.config import Settings
    from app.core.db import close_database, session_factory
    from app.cowork_store.jsonl import JsonlConversationStore
    from app.cowork_store.migration import migrate_and_verify
    from app.cowork_store.sqlite import SqliteCoworkStore

    settings = Settings()
    root = settings.cowork_data_path.expanduser().resolve()
    ready = root / "sqlite-ready"
    if ready.exists():
        return
    async with session_factory() as session:
        report = await migrate_and_verify(
            session,
            sqlite_store=SqliteCoworkStore(root / "cowork.db"),
            jsonl_store=JsonlConversationStore(root / "conversations"),
            report_path=root / "migration-report.json",
        )
    ready.write_text(report.generated_at + "\n", encoding="utf-8")
    ready.chmod(0o600)
    await close_database()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workpilot-sidecar")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("migrate", help="升级本地数据库 schema")
    api = subparsers.add_parser("api", help="启动本机 FastAPI")
    api.add_argument("--port", type=int, required=True)
    subparsers.add_parser("worker", help="启动 Cowork Arq worker")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "migrate":
        command.upgrade(Config("alembic.ini"), "head")
        # 只有双读摘要完全一致才会生成 sqlite-ready；异常会令 sidecar 迁移
        # 非零退出，Tauri 不会以 SQLite 后端启动 API。
        asyncio.run(_migrate_cowork_control_plane())
        return 0
    if args.mode == "api":
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=args.port,
            # 桌面 sidecar 只监听 loopback；保留访问日志，便于区分 WebView、
            # IPC/CORS 与业务错误，避免把“进程已启动”误判成“界面已连通”。
            access_log=True,
        )
        return 0
    if args.mode == "worker":
        # arq 运行时支持这种类属性 settings，但它的 WorkerSettingsType
        # Protocol 要求类继 WorkerSettingsBase，与官方 CLI 实际接口不一致。
        run_worker(WorkerSettings)  # type: ignore[arg-type]
        return 0
    raise AssertionError(f"未知 sidecar 模式: {args.mode}")


if __name__ == "__main__":  # pragma: no cover - 真实进程入口
    raise SystemExit(main())
