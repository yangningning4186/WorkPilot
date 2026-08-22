"""Tauri 桌面壳的单一后端可执行文件入口。

开发时可用 ``uv run python -m app.desktop_sidecar``；发布时将该模块打包为
``workpilot-sidecar``，Tauri 以 migrate / api 两个子命令启动。

原来还有第三个 ``worker`` 子命令启动 Arq worker。队列改成进程内之后 worker 与
API 同进程，那个子命令没有东西可启动了——Tauri 侧本来也只 spawn 前两个。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workpilot-sidecar")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("migrate", help="升级本地数据库 schema")
    api = subparsers.add_parser("api", help="启动本机 FastAPI")
    api.add_argument("--port", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "migrate":
        # PostgreSQL 退役后已经没有 schema 要升级了：SQLite 的建表与列升级在
        # `SqliteCoworkStore.initialize()` 里，API 启动时自己会做。这个子命令
        # 留着只是为了不改 Tauri 侧的启动序列。
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
    raise AssertionError(f"未知 sidecar 模式: {args.mode}")


if __name__ == "__main__":  # pragma: no cover - 真实进程入口
    raise SystemExit(main())
