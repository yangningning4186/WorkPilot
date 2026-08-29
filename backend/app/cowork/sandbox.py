"""容器隔离执行后端；永不静默降级到宿主机 Shell。"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.cowork.shell import ShellCommand, ShellExecutionResult, execute_shell_command


class CoworkSandboxError(RuntimeError):
    pass


SandboxRuntime = Literal["auto", "disabled", "docker", "podman"]


@dataclass(frozen=True)
class SandboxLimits:
    runtime: SandboxRuntime
    image: str
    memory_mb: int
    pids_limit: int
    cpus: float


def build_sandbox_argv(*, command: str, cwd: Path, limits: SandboxLimits) -> tuple[str, ...]:
    """构造无网络、只读 rootfs、最小 Linux 权限的容器 argv。"""

    canonical_cwd = cwd.resolve(strict=True)
    if not canonical_cwd.is_dir():
        raise CoworkSandboxError("sandbox cwd 必须是已授权的现有目录")
    if any(token in str(canonical_cwd) for token in ("\x00", "\n", "\r", ",")):
        raise CoworkSandboxError("sandbox cwd 包含容器挂载不支持的字符")
    runtime = limits.runtime
    if runtime == "disabled":
        raise CoworkSandboxError("sandbox 后端已禁用，不能降级到 host.execute")
    executable = None
    if runtime == "auto":
        executable = shutil.which("docker") or shutil.which("podman")
    else:
        executable = shutil.which(runtime)
    if executable is None:
        raise CoworkSandboxError("未找到 Docker/Podman；sandbox.execute 不会退回宿主机执行")
    if not limits.image.strip() or any(char.isspace() for char in limits.image):
        raise CoworkSandboxError("sandbox image 配置无效")

    uid_gid: tuple[str, ...] = ()
    if os.name == "posix":
        uid_gid = ("--user", f"{os.getuid()}:{os.getgid()}")
    return (
        executable,
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        str(limits.pids_limit),
        "--memory",
        f"{limits.memory_mb}m",
        "--cpus",
        f"{limits.cpus:g}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=64m",
        *uid_gid,
        "--mount",
        # Docker/Podman 的 --mount 默认就是读写；`rw` 不是合法的裸字段（必须是
        # key=value），保留它会在真正启动容器前被 CLI 直接拒绝。
        f"type=bind,source={canonical_cwd},target=/workspace",
        "--workdir",
        "/workspace",
        limits.image,
        "/bin/sh",
        "-lc",
        command,
    )


async def execute_sandbox_command(
    command: str,
    *,
    cwd: Path,
    limits: SandboxLimits,
    cancel_event: asyncio.Event | None,
    timeout_s: float,
    terminate_grace_s: float,
    max_output_bytes: int,
    full_output_path: Path | None = None,
    full_output_max_bytes: int = 64 * 1024 * 1024,
) -> ShellExecutionResult:
    argv = build_sandbox_argv(command=command, cwd=cwd, limits=limits)
    return await execute_shell_command(
        ShellCommand(raw=command.strip(), argv=argv, has_operators=False),
        cwd=cwd,
        cancel_event=cancel_event,
        timeout_s=timeout_s,
        terminate_grace_s=terminate_grace_s,
        max_output_bytes=max_output_bytes,
        full_output_path=full_output_path,
        full_output_max_bytes=full_output_max_bytes,
    )


__all__ = [
    "CoworkSandboxError",
    "SandboxLimits",
    "SandboxRuntime",
    "build_sandbox_argv",
    "execute_sandbox_command",
]
