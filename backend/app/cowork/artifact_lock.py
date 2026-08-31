"""Artifact 提交排他锁。"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.cowork.files import CoworkFileError

_LOCK_STALE_SECONDS = 15 * 60


def _lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.workpilot-commit.lock"


def _remove_stale_lock(path: Path, *, now: float) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return True
    except OSError as error:
        raise CoworkFileError("Artifact 提交锁不可读取，请检查目标目录") from error
    try:
        locked_stat = os.fstat(descriptor)
        if not stat.S_ISREG(locked_stat.st_mode) or locked_stat.st_size > 4096:
            raise CoworkFileError("Artifact 提交锁格式无效，请人工检查目标目录")
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
        claimed_at = float(payload["claimed_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CoworkFileError("Artifact 提交锁缺少可信 claimed_at，请人工检查目标目录") from error
    if now - claimed_at <= _LOCK_STALE_SECONDS:
        return False
    try:
        current = path.lstat()
    except FileNotFoundError:
        return True
    if (current.st_dev, current.st_ino) != (locked_stat.st_dev, locked_stat.st_ino):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


@contextmanager
def artifact_commit_lock(target: Path) -> Iterator[None]:
    """用 O_CREAT|O_EXCL 持有从 baseline 检查到最终替换的排他锁。"""

    path = _lock_path(target)
    descriptor = -1
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError as error:
            if not _remove_stale_lock(path, now=time.time()):
                raise CoworkFileError("另一个 Artifact 提交正在处理同一目标，请稍后重试") from error
    if descriptor < 0:
        raise CoworkFileError("无法取得 Artifact 提交排他锁")
    locked_stat = os.fstat(descriptor)
    try:
        payload = json.dumps(
            {"claimed_at": time.time(), "pid": os.getpid()},
            separators=(",", ":"),
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (locked_stat.st_dev, locked_stat.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


__all__ = ["artifact_commit_lock"]
