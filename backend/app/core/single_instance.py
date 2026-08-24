"""桌面 sidecar 的数据目录级单实例锁。

SQLite 能串行化单次写入，但不能阻止两个不同版本的 worker 同时消费同一条任务。
因此锁的粒度必须是 ``cowork_data_path``，并由进程从启动一直持有到退出。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, cast


class SidecarAlreadyRunningError(RuntimeError):
    """同一数据目录已有一个仍持锁的 sidecar。"""


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - CI 运行在 Unix
        import msvcrt

        win_locking = cast("Any", msvcrt)
        handle.seek(0)
        try:
            win_locking.locking(handle.fileno(), win_locking.LK_NBLCK, 1)
        except OSError as error:
            raise SidecarAlreadyRunningError from error
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise SidecarAlreadyRunningError from error


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - CI 运行在 Unix
        import msvcrt

        win_locking = cast("Any", msvcrt)
        handle.seek(0)
        win_locking.locking(handle.fileno(), win_locking.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def desktop_sidecar_lock(data_path: Path) -> Iterator[Path]:
    """独占 ``data_path``；异常退出时由操作系统自动释放文件锁。"""

    root = data_path.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / ".sidecar.lock"
    handle = lock_path.open("a+b")
    locked = False
    try:
        os.chmod(lock_path, 0o600)
        try:
            _try_lock(handle)
            locked = True
        except SidecarAlreadyRunningError as error:
            handle.seek(0)
            owner = handle.read().decode("utf-8", errors="replace").strip() or "未知进程"
            raise SidecarAlreadyRunningError(
                f"数据目录 {root} 已由另一个 WorkPilot sidecar 占用（{owner}）。"
                "请先关闭旧客户端，避免不同版本 worker 竞争同一任务。"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}".encode())
        handle.flush()
        yield lock_path
    finally:
        try:
            if locked:
                _unlock(handle)
        finally:
            handle.close()
