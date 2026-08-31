"""子进程资源上限；只在受信启动边界调用。"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_WINDOWS_JOB_HANDLE: Any = None


@dataclass(frozen=True)
class ProcessTreeUsage:
    pids: int
    rss_bytes: int
    cpu_seconds: float


def _parse_ps_cpu_time(value: str) -> float:
    days = 0
    clock = value
    if "-" in value:
        raw_days, clock = value.split("-", 1)
        days = int(raw_days)
    parts = clock.split(":")
    if len(parts) == 2:
        hours_value = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        hours_value = int(hours)
    else:
        raise ValueError("ps CPU time 格式无效")
    return days * 86_400 + hours_value * 3_600 + int(minutes) * 60 + float(seconds)


def read_process_tree_usage(root_pid: int) -> ProcessTreeUsage:
    """从受信父进程聚合 POSIX 子进程树的 RSS、PID 数与 CPU 时间。"""

    if os.name != "posix":
        raise OSError("进程树资源监控只支持 POSIX")
    completed = subprocess.run(
        ("/bin/ps", "-axo", "pid=,ppid=,rss=,time="),
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    records: dict[int, tuple[int, int, float]] = {}
    children: dict[int, list[int]] = {}
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split()
        if len(fields) != 4:
            continue
        pid, parent, rss_kib = (int(value) for value in fields[:3])
        records[pid] = (parent, rss_kib, _parse_ps_cpu_time(fields[3]))
        children.setdefault(parent, []).append(pid)
    members: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in members:
            continue
        members.add(pid)
        pending.extend(children.get(pid, ()))
    present = members.intersection(records)
    return ProcessTreeUsage(
        pids=max(1, len(present)),
        rss_bytes=sum(records[pid][1] * 1024 for pid in present),
        cpu_seconds=sum(records[pid][2] for pid in present),
    )


def _apply_windows_job_limits(*, memory_mb: int, pids_limit: int, cpu_seconds: int) -> None:
    """把当前 Windows validator 放入 kill-on-close 的受限 Job Object。"""

    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_process_time = 0x00000002
    job_object_limit_job_time = 0x00000004
    job_object_limit_active_process = 0x00000008
    job_object_limit_process_memory = 0x00000100
    job_object_limit_job_memory = 0x00000200
    job_object_limit_kill_on_job_close = 0x00002000
    ctypes_api: Any = ctypes
    kernel32 = ctypes_api.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes_api.get_last_error(), "无法创建 Windows Job Object")
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.PerProcessUserTimeLimit = cpu_seconds * 10_000_000
    limits.BasicLimitInformation.PerJobUserTimeLimit = cpu_seconds * 10_000_000
    limits.BasicLimitInformation.ActiveProcessLimit = pids_limit
    limits.BasicLimitInformation.LimitFlags = (
        job_object_limit_process_time
        | job_object_limit_job_time
        | job_object_limit_active_process
        | job_object_limit_process_memory
        | job_object_limit_job_memory
        | job_object_limit_kill_on_job_close
    )
    limits.ProcessMemoryLimit = memory_mb * 1024 * 1024
    limits.JobMemoryLimit = memory_mb * 1024 * 1024
    if not kernel32.SetInformationJobObject(
        handle,
        job_object_extended_limit_information,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(handle)
        raise OSError(ctypes_api.get_last_error(), "无法设置 Windows Job Object 上限")
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(handle)
        raise OSError(ctypes_api.get_last_error(), "无法把校验进程加入 Windows Job Object")
    global _WINDOWS_JOB_HANDLE
    _WINDOWS_JOB_HANDLE = handle


def apply_process_limits(
    *,
    memory_mb: int,
    pids_limit: int,
    cpu_seconds: int,
    file_size_bytes: int | None = None,
    enforce_nproc: bool = True,
) -> None:
    """把 hard limit 施加到当前进程，后代会继承同一上限。"""

    if os.name == "nt":  # pragma: no cover - Windows CI 不在当前矩阵
        _apply_windows_job_limits(
            memory_mb=memory_mb,
            pids_limit=pids_limit,
            cpu_seconds=cpu_seconds,
        )
        return
    if os.name != "posix":
        return
    import resource

    def lower_limit(kind: int, requested: int) -> None:
        _, hard = resource.getrlimit(kind)
        bounded = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(kind, (bounded, bounded))

    lower_limit(resource.RLIMIT_CPU, max(1, cpu_seconds))
    if sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_AS"):
        lower_limit(resource.RLIMIT_AS, memory_mb * 1024 * 1024)
    elif sys.platform == "darwin" and hasattr(resource, "RLIMIT_RSS"):
        # Darwin exposes RLIMIT_RSS but some kernel/Python combinations reject lowering it.
        # CPU/NPROC remain hard limits; the parent wall timeout is the final kill boundary.
        try:
            lower_limit(resource.RLIMIT_RSS, memory_mb * 1024 * 1024)
        except (OSError, ValueError):
            pass
    # Darwin 的 RLIMIT_NPROC 是“该登录用户的全局进程数”，不是当前进程树；桌面用户
    # 往往早已超过 sandbox 的 128 上限，降低它会让 sandbox-exec 连第一个子进程都无法
    # 启动。Linux user namespace 可安全使用 hard rlimit；macOS 由可信父进程树监控限额。
    if enforce_nproc and sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_NPROC"):
        lower_limit(resource.RLIMIT_NPROC, pids_limit)
    if file_size_bytes is not None and hasattr(resource, "RLIMIT_FSIZE"):
        lower_limit(resource.RLIMIT_FSIZE, file_size_bytes)
    if hasattr(resource, "RLIMIT_NOFILE"):
        lower_limit(resource.RLIMIT_NOFILE, 256)


def process_limit_preexec(
    *,
    memory_mb: int,
    pids_limit: int,
    cpus: float,
    wall_timeout_s: float,
    file_size_bytes: int | None = None,
) -> Callable[[], None] | None:
    """构造只含 setrlimit 的 POSIX pre-exec hook。"""

    if os.name != "posix":
        return None
    cpu_seconds = max(1, math.ceil(wall_timeout_s * cpus))

    def apply() -> None:
        apply_process_limits(
            memory_mb=memory_mb,
            pids_limit=pids_limit,
            cpu_seconds=cpu_seconds,
            file_size_bytes=file_size_bytes,
            # 这里限制的是 bwrap/Seatbelt 启动器；宿主用户已存在的进程会被
            # RLIMIT_NPROC 一并计数，可能让启动器无法创建第一个隔离子进程。
            # 精确的 sandbox 进程树数量由可信父进程监控。
            enforce_nproc=False,
        )

    return apply


__all__ = [
    "ProcessTreeUsage",
    "apply_process_limits",
    "process_limit_preexec",
    "read_process_tree_usage",
]
