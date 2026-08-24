"""从本机 Cowork 权威事件存储导出可验证的 Run replay bundle。

导出是只读操作，不恢复 checkpoint、不调用模型也不执行工具。真实事件可能含
回答、引文和产物元数据，因此 CLI 必须显式确认敏感输出，文件使用 0600
权限独占创建，且不允许覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from app.agent_core.contracts import RunEvent
from app.core.config import Settings
from app.cowork_store.factory import (
    close_local_cowork_stores,
    initialize_local_cowork_stores,
)
from eval.replay import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    EVENT_PROTOCOL,
    seal_bundle,
    verify_bundle,
)

_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "budget_exceeded"})


class RunReplayExportError(RuntimeError):
    """导出输入不完整或不安全。"""


class ReplayExportStore(Protocol):
    async def get_run(self, run_id: UUID) -> object | None: ...

    async def list_events(
        self, *, run_id: UUID, after_seq: int = 0, limit: int | None = None
    ) -> list[RunEvent]: ...


def build_run_bundle(
    *,
    run_id: UUID,
    run: object,
    events: Sequence[RunEvent],
    case_id: str,
) -> dict[str, Any]:
    """把一个已完成 run 投影成离线协议 bundle，并在返回前自验。"""

    normalized_case_id = case_id.strip()
    if not normalized_case_id:
        raise RunReplayExportError("case_id 不能为空")
    status = getattr(run, "status", None)
    if status not in _TERMINAL_STATUSES:
        raise RunReplayExportError(
            f"run {run_id} 尚未进入终态（status={status!r}），拒绝导出不完整事件流"
        )
    if not events:
        raise RunReplayExportError(f"run {run_id} 没有可导出事件")
    wrong_run = [str(event.run_id) for event in events if event.run_id != run_id]
    if wrong_run:
        raise RunReplayExportError(f"事件流串入其他 run: {wrong_run[:3]}")

    workflow_type = getattr(run, "workflow_type", None)
    raw: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "event_protocol": EVENT_PROTOCOL,
        "name": f"local-run-{run_id}",
        "origin": "local_production_export",
        "exported_at": datetime.now(UTC).isoformat(),
        "sensitive": True,
        "cases": [
            {
                "case_id": normalized_case_id,
                "run_id": str(run_id),
                "metadata": {
                    "run_status": status,
                    "workflow_type": workflow_type,
                },
                "events": [event.envelope() for event in events],
            }
        ],
    }
    bundle = seal_bundle(raw)
    report = verify_bundle(bundle, source=f"local-run:{run_id}")
    if not report.valid:
        issues = [
            issue.code
            for issue in (
                *report.issues,
                *(issue for case in report.cases for issue in case.issues),
            )
            if issue.severity == "error"
        ]
        raise RunReplayExportError(f"run {run_id} 的事件不满足 replay 协议: {issues[:8]}")
    return bundle


async def export_run(
    store: ReplayExportStore,
    *,
    run_id: UUID,
    case_id: str | None = None,
) -> dict[str, Any]:
    run = await store.get_run(run_id)
    if run is None:
        raise RunReplayExportError(f"run 不存在: {run_id}")
    events = await store.list_events(run_id=run_id, after_seq=0, limit=None)
    return build_run_bundle(
        run_id=run_id,
        run=run,
        events=events,
        case_id=case_id or f"run-{run_id}",
    )


def write_bundle(bundle: Mapping[str, Any], output: Path) -> Path:
    """以不可覆盖、0600 模式写入敏感 bundle。"""

    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RunReplayExportError(f"输出已存在，拒绝覆盖: {target}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def _async_main(args: argparse.Namespace) -> Path:
    settings = Settings()
    data_root = (args.data_root or settings.cowork_data_path).expanduser().resolve()
    if not (data_root / "cowork.db").is_file():
        raise RunReplayExportError(f"Cowork 事件库不存在: {data_root / 'cowork.db'}")
    settings = settings.model_copy(update={"cowork_data_path": data_root})
    await close_local_cowork_stores()
    stores = await initialize_local_cowork_stores(settings)
    try:
        bundle = await export_run(stores.state, run_id=args.run_id, case_id=args.case_id)
        return write_bundle(bundle, args.output)
    finally:
        await close_local_cowork_stores()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从本机 Cowork run_events 导出离线 replay bundle")
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--data-root", type=Path, help="可选的 Cowork 数据根目录")
    parser.add_argument(
        "--acknowledge-sensitive-output",
        action="store_true",
        help="确认输出可能含回答、引文与 artifact 元数据",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.acknowledge_sensitive_output:
        print(
            "拒绝导出：真实 Run 事件可能含敏感内容，"
            "请审核输出位置后显式传 --acknowledge-sensitive-output",
            file=sys.stderr,
        )
        return 2
    try:
        output = asyncio.run(_async_main(args))
    except (OSError, ValueError, RunReplayExportError) as error:
        print(f"Run replay 导出失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "mode": "sensitive_local_event_export",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RunReplayExportError",
    "build_run_bundle",
    "export_run",
    "main",
    "write_bundle",
]
