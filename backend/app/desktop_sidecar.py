"""Tauri 桌面壳的单一后端可执行文件入口。

开发时可用 ``uv run python -m app.desktop_sidecar``；发布时将该模块打包为
``workpilot-sidecar``，Tauri 以 migrate / api 两个子命令启动。

原来还有第三个 ``worker`` 子命令启动 Arq worker。队列改成进程内之后 worker 与
API 同进程，那个子命令没有东西可启动了——Tauri 侧本来也只 spawn 前两个。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from app.core.config import get_settings
from app.core.single_instance import desktop_sidecar_lock


def _configure_packaged_paths() -> None:
    """让 frozen sidecar 不依赖安装目录或当前工作目录。

    PyInstaller onefile 会把只读资源展开到 ``sys._MEIPASS``。路由表随二进制发布；MCP、
    Skill 与所有可变数据仍落用户目录，升级应用不会覆盖用户内容。Finder 从 ``/`` 启动
    app，因此任何 ``../data`` 默认值都会错误地变成只读系统卷下的 ``/data``；发布态必须
    在 Settings 首次构造前把这些路径全部锚定到 Cowork 数据目录。
    """

    bundle_root_value = sys.__dict__.get("_MEIPASS")
    if not getattr(sys, "frozen", False) or not isinstance(bundle_root_value, str):
        return
    bundle_root = Path(bundle_root_value)
    data_root = Path(os.environ.get("COWORK_DATA_PATH", "~/.workpilot")).expanduser()
    os.environ.setdefault("ROUTING_CONFIG_PATH", str(bundle_root / "config" / "routing.yaml"))
    os.environ.setdefault("LOCAL_LIBRARY_PATH", str(data_root / "library"))
    os.environ.setdefault("AGENT_OUTPUT_PATH", str(data_root / "agent-output"))
    os.environ.setdefault("OFFICE_PREVIEW_CACHE_PATH", str(data_root / "preview-cache"))
    os.environ.setdefault("COWORK_ATTACHMENT_PATH", str(data_root / "cowork-attachments"))
    os.environ.setdefault("COWORK_MCP_CONFIG_PATH", str(data_root / "mcp.yaml"))
    os.environ.setdefault("COWORK_SKILLS_PATH", str(data_root / "skills"))
    os.environ.setdefault("SECRET_STORE_KEY_PATH", str(data_root / "secrets" / "master.key"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workpilot-sidecar")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("migrate", help="升级本地数据库 schema")
    api = subparsers.add_parser("api", help="启动本机 FastAPI")
    api.add_argument("--port", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_packaged_paths()
    args = _parser().parse_args(argv)
    if args.mode == "migrate":
        # PostgreSQL 退役后已经没有 schema 要升级了：SQLite 的建表与列升级在
        # `SqliteCoworkStore.initialize()` 里，API 启动时自己会做。这个子命令
        # 留着只是为了不改 Tauri 侧的启动序列。
        return 0
    if args.mode == "api":
        # 显式 import 让 frozen 构建能静态发现整棵 FastAPI 应用；字符串 "app.main:app"
        # 对 PyInstaller 是不可见的，会产出一个能启动但第一步就找不到 app.main 的假包。
        from app.main import app

        # 必须在导入 FastAPI app、启动 dispatcher 之前拿锁。SQLite 的事务只能保护
        # 单次写入，挡不住两个版本不同的 worker 轮询并接管同一条 queued run。
        with desktop_sidecar_lock(get_settings().cowork_data_path):
            uvicorn.run(
                app,
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
