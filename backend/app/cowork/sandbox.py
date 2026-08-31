"""WorkPilot 原生隔离执行后端；永不静默降级到普通宿主 Shell。

发布版使用随桌面应用打包的 ``workpilot-artifact-python``。macOS 通过 Seatbelt
(``sandbox-exec``)，Linux 通过 bubblewrap 强制文件与网络边界。可信父进程只负责把验证
通过的候选输出提交回用户工作区，模型生成的代码始终留在原生沙箱子进程中。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.cowork.artifact_validation import validate_artifact_in_subprocess
from app.cowork.files import create_file_backup
from app.cowork.process_limits import process_limit_preexec
from app.cowork.shell import ShellCommand, execute_shell_command

_VALIDATED_OUTPUT_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".pdf", ".html"})
_FIXED_RENDERER_OUTPUT_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx", ".pdf"})
_TEXT_OUTPUT_SUFFIXES = frozenset({".txt", ".md", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml"})


class CoworkSandboxError(RuntimeError):
    pass


SandboxRuntime = Literal["auto", "disabled", "native", "docker", "podman"]


@dataclass(frozen=True)
class SandboxLimits:
    runtime: SandboxRuntime
    python_executable: Path | None = None
    profile: str = "artifact-python:1.0.0"
    image: str = "workpilot-artifact-python:1.0.0"
    memory_mb: int = 512
    pids_limit: int = 128
    cpus: float = 1.0


@dataclass(frozen=True)
class SandboxLaunch:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    engine: Literal["seatbelt", "bubblewrap", "docker", "podman"]
    python_executable: Path | None


@dataclass(frozen=True)
class SandboxExecutionResult:
    command_sha256: str
    exit_code: int
    stdout: str
    stderr: str
    output_truncated: bool
    execution_mode: Literal["native_sandbox"]
    sandbox_engine: Literal["seatbelt", "bubblewrap", "docker", "podman"]
    runtime_profile: str
    full_output_path: str | None
    full_output_truncated: bool
    full_output_size_bytes: int
    committed_outputs: tuple[str, ...]
    output_warnings: tuple[str, ...]


def build_sandbox_launch(
    *,
    command: str,
    inputs: Path,
    work: Path,
    outputs: Path,
    temporary: Path,
    runtime_bin: Path,
    skill_roots: tuple[Path, ...],
    limits: SandboxLimits,
) -> SandboxLaunch:
    """构造 inputs/skills RO、work/outputs/tmp RW、无网络的隔离后端。"""

    _require_managed_python_entrypoint(command)
    canonical_inputs = _sandbox_directory(inputs, label="inputs")
    canonical_work = _sandbox_directory(work, label="work")
    canonical_outputs = _sandbox_directory(outputs, label="outputs")
    canonical_temporary = _sandbox_directory(temporary, label="temporary")
    canonical_runtime_bin = _sandbox_directory(runtime_bin, label="runtime bin")
    canonical_skills = tuple(
        _sandbox_directory(path, label="skills") for path in skill_roots
    )
    if limits.runtime == "disabled":
        raise CoworkSandboxError("sandbox 已禁用，不能降级到 host.execute")
    backend = limits.runtime
    if backend == "auto":
        backend = "docker" if sys.platform == "win32" else "native"
        if backend == "docker" and shutil.which("docker") is None:
            backend = "podman"
    if backend in {"docker", "podman"}:
        return _container_launch(
            command=command,
            inputs=canonical_inputs,
            work=canonical_work,
            outputs=canonical_outputs,
            temporary=canonical_temporary,
            skill_roots=canonical_skills,
            limits=limits,
            runtime="docker" if backend == "docker" else "podman",
        )
    if backend != "native":  # pragma: no cover - SandboxRuntime 已穷尽
        raise CoworkSandboxError(f"未知 sandbox 后端：{backend}")
    python_executable = _resolve_python_executable(limits.python_executable)
    _write_runtime_shims(canonical_runtime_bin, python_executable)
    environment = {
        "HOME": str(canonical_work),
        "TMPDIR": str(canonical_temporary),
        "TMP": str(canonical_temporary),
        "TEMP": str(canonical_temporary),
        "PATH": os.pathsep.join((str(canonical_runtime_bin), "/usr/bin", "/bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "WORKPILOT_INPUTS": str(canonical_inputs),
        "WORKPILOT_WORK": str(canonical_work),
        "WORKPILOT_OUTPUTS": str(canonical_outputs),
        "WORKPILOT_SKILLS": os.pathsep.join(str(path) for path in canonical_skills),
        "WORKPILOT_PYTHON": str(python_executable),
        "WORKPILOT_RUNTIME_PROFILE": limits.profile,
    }

    if sys.platform == "darwin":
        executable = shutil.which("sandbox-exec")
        if executable is None:
            raise CoworkSandboxError(
                "macOS Seatbelt 后端不可用；run_sandbox 不会退回普通宿主 Shell"
            )
        read_paths = (
            *_macos_runtime_read_paths(python_executable),
            canonical_inputs,
            canonical_work,
            canonical_outputs,
            canonical_temporary,
            canonical_runtime_bin,
            *canonical_skills,
        )
        profile = _macos_profile(
            read_paths=read_paths,
            write_paths=(canonical_work, canonical_outputs, canonical_temporary),
        )
        return SandboxLaunch(
            # 登录 shell 会运行 macOS path_helper 并覆盖受控 PATH，导致 ``python3``
            # 错误命中 /usr/bin/xcrun。沙箱命令不需要 profile/rc 初始化。
            argv=(executable, "-p", profile, "/bin/sh", "-c", command),
            cwd=canonical_work,
            environment=environment,
            engine="seatbelt",
            python_executable=python_executable,
        )

    if sys.platform.startswith("linux"):
        executable = shutil.which("bwrap")
        if executable is None:
            raise CoworkSandboxError(
                "Linux bubblewrap 后端不可用；请安装 bwrap，run_sandbox 不会退回普通宿主 Shell"
            )
        read_paths = (
            *_linux_runtime_read_paths(python_executable),
            canonical_inputs,
            canonical_runtime_bin,
            *canonical_skills,
        )
        read_mounts = tuple(
            argument
            for path in read_paths
            for argument in ("--ro-bind", str(path), str(path))
        )
        return SandboxLaunch(
            argv=(
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--unshare-net",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                *read_mounts,
                "--bind",
                str(canonical_work),
                str(canonical_work),
                "--bind",
                str(canonical_outputs),
                str(canonical_outputs),
                "--bind",
                str(canonical_temporary),
                str(canonical_temporary),
                "--chdir",
                str(canonical_work),
                "/bin/sh",
                "-c",
                command,
            ),
            cwd=canonical_work,
            environment=environment,
            engine="bubblewrap",
            python_executable=python_executable,
        )

    raise CoworkSandboxError(
        f"当前平台 {sys.platform!r} 尚无原生 sandbox 后端；不会退回普通宿主 Shell"
    )


def _container_launch(
    *,
    command: str,
    inputs: Path,
    work: Path,
    outputs: Path,
    temporary: Path,
    skill_roots: tuple[Path, ...],
    limits: SandboxLimits,
    runtime: Literal["docker", "podman"],
) -> SandboxLaunch:
    executable = shutil.which(runtime)
    if executable is None:
        raise CoworkSandboxError(
            f"未找到 {runtime}；Windows run_sandbox 不会退回普通宿主 Shell"
        )
    if not limits.image.strip() or any(character.isspace() for character in limits.image):
        raise CoworkSandboxError("sandbox image 配置无效")
    paths = (inputs, work, outputs, temporary, *skill_roots)
    if any("," in str(path) for path in paths):
        raise CoworkSandboxError("sandbox 路径包含容器挂载不支持的逗号")
    skill_targets = tuple(f"/workpilot/skills/{index}" for index in range(len(skill_roots)))
    mounts = (
        "--mount",
        f"type=bind,source={inputs},target=/workpilot/inputs,readonly",
        "--mount",
        f"type=bind,source={work},target=/workpilot/work",
        "--mount",
        f"type=bind,source={outputs},target=/workpilot/outputs",
        "--mount",
        f"type=bind,source={temporary},target=/workpilot/tmp",
        *(
            argument
            for source, target in zip(skill_roots, skill_targets, strict=True)
            for argument in (
                "--mount",
                f"type=bind,source={source},target={target},readonly",
            )
        ),
    )
    uid_gid: tuple[str, ...] = ()
    if os.name == "posix":
        uid_gid = ("--user", f"{os.getuid()}:{os.getgid()}")
    container_environment = {
        "HOME": "/workpilot/work",
        "TMPDIR": "/workpilot/tmp",
        "TMP": "/workpilot/tmp",
        "TEMP": "/workpilot/tmp",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "WORKPILOT_INPUTS": "/workpilot/inputs",
        "WORKPILOT_WORK": "/workpilot/work",
        "WORKPILOT_OUTPUTS": "/workpilot/outputs",
        "WORKPILOT_SKILLS": ":".join(skill_targets),
        "WORKPILOT_PYTHON": "python3",
        "WORKPILOT_RUNTIME_PROFILE": limits.profile,
    }
    environment_arguments = tuple(
        argument
        for key, value in container_environment.items()
        for argument in ("--env", f"{key}={value}")
    )
    return SandboxLaunch(
        argv=(
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
            *uid_gid,
            *mounts,
            *environment_arguments,
            "--workdir",
            "/workpilot/work",
            limits.image,
            "/bin/sh",
            "-c",
            command,
        ),
        cwd=work,
        environment={},
        engine=runtime,
        python_executable=None,
    )


def _sandbox_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise CoworkSandboxError(f"sandbox {label} 不能是符号链接")
    canonical = path.resolve(strict=True)
    if not canonical.is_dir():
        raise CoworkSandboxError(f"sandbox {label} 必须是普通目录")
    if any(token in str(canonical) for token in ("\x00", "\n", "\r")):
        raise CoworkSandboxError(f"sandbox {label} 包含原生策略不支持的字符")
    return canonical


def _resolve_python_executable(configured: Path | None) -> Path:
    candidate = configured
    if candidate is None:
        if getattr(sys, "frozen", False):
            raise CoworkSandboxError(
                "安装包缺少随包 artifact Python；run_sandbox 不会使用系统 Python"
            )
        candidate = Path(sys.executable)
    expanded = candidate.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    absolute = Path(os.path.abspath(expanded))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise CoworkSandboxError(f"随包 artifact Python 不存在：{absolute}") from error
    if not resolved.is_file() or not os.access(absolute, os.X_OK):
        raise CoworkSandboxError(f"随包 artifact Python 不可执行：{absolute}")
    return absolute


def _require_managed_python_entrypoint(command: str) -> None:
    """禁止模型通过 PATH 或绝对宿主路径选择 Python；非 Python 命令不受影响。"""

    scrubbed = command.replace("${WORKPILOT_PYTHON}", "").replace("$WORKPILOT_PYTHON", "")
    for token in re.split(r"[\s;&|()]+", scrubbed):
        normalized = token.strip("\"'`").rsplit("/", 1)[-1]
        if re.fullmatch(r"python(?:3(?:\.\d+)*)?", normalized, flags=re.IGNORECASE):
            raise CoworkSandboxError(
                "run_sandbox 禁止通过 PATH 或宿主路径选择 Python；请直接调用 $WORKPILOT_PYTHON"
            )


def _write_runtime_shims(runtime_bin: Path, python_executable: Path) -> None:
    quoted = shlex.quote(str(python_executable))
    payload = f"#!/bin/sh\nexec {quoted} \"$@\"\n"
    for name in ("python", "python3"):
        target = runtime_bin / name
        target.write_text(payload, encoding="utf-8")
        target.chmod(0o500)


def _macos_runtime_read_paths(python_executable: Path) -> tuple[Path, ...]:
    paths = [
        Path("/System"),
        Path("/Library/Fonts"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/bin"),
        Path("/usr/bin"),
        Path("/private/etc"),
        python_executable,
    ]
    # 开发态通常传入 venv/bin/python；需要同时读取 venv site-packages 与解释器真实前缀。
    for executable in (python_executable, python_executable.resolve(strict=True)):
        root = executable.parent.parent
        if root.is_dir():
            paths.append(root)
    unique: dict[str, Path] = {}
    for path in paths:
        if path.exists():
            unique[str(path)] = path
    return tuple(unique.values())


def _linux_runtime_read_paths(python_executable: Path) -> tuple[Path, ...]:
    """只挂载解释器/动态库/字体运行时，不把宿主根目录暴露给模型代码。"""

    paths = [
        Path("/bin"),
        Path("/usr/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/usr/lib"),
        Path("/usr/lib64"),
        Path("/usr/share"),
        Path("/etc/ld.so.cache"),
        Path("/etc/ssl"),
        Path("/etc/ca-certificates"),
        Path("/etc/fonts"),
        python_executable,
    ]
    for executable in (python_executable, python_executable.resolve(strict=True)):
        runtime_root = executable.parent.parent
        if runtime_root.is_dir() and runtime_root not in {Path("/"), Path("/usr")}:
            paths.append(runtime_root)
    unique: dict[str, Path] = {}
    for path in paths:
        if path.exists():
            unique[str(path)] = path
    return tuple(unique.values())


def _sandbox_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos_profile(*, read_paths: tuple[Path, ...], write_paths: tuple[Path, ...]) -> str:
    read_rules = " ".join(f"(subpath {_sandbox_string(str(path))})" for path in read_paths)
    write_rules = " ".join(f"(subpath {_sandbox_string(str(path))})" for path in write_paths)
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            # PyInstaller one-file bootloader serializes extraction with a private SysV semaphore.
            "(allow ipc-sysv-sem)",
            "(allow file-read-metadata)",
            f"(allow file-read* {read_rules})",
            f'(allow file-write* {write_rules} (literal "/dev/null"))',
            "(deny network*)",
        )
    )


async def execute_sandbox_command(
    command: str,
    *,
    cwd: Path,
    output_root: Path,
    skill_roots: tuple[Path, ...] = (),
    limits: SandboxLimits,
    cancel_event: asyncio.Event | None,
    timeout_s: float,
    terminate_grace_s: float,
    max_output_bytes: int,
    full_output_path: Path | None = None,
    full_output_max_bytes: int = 64 * 1024 * 1024,
    output_max_files: int = 200,
    output_max_bytes: int = 64 * 1024 * 1024,
    office_edit_baselines: Mapping[str, str] | None = None,
    backup_versions: int = 10,
) -> SandboxExecutionResult:
    with tempfile.TemporaryDirectory(prefix="workpilot-sandbox-") as raw_workspace:
        workspace = Path(raw_workspace)
        work = workspace / "work"
        outputs = workspace / "outputs"
        temporary = workspace / "tmp"
        runtime_bin = workspace / "runtime" / "bin"
        work.mkdir(mode=0o700)
        outputs.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        runtime_bin.mkdir(parents=True, mode=0o700)
        launch = build_sandbox_launch(
            command=command,
            inputs=cwd,
            work=work,
            outputs=outputs,
            temporary=temporary,
            runtime_bin=runtime_bin,
            skill_roots=skill_roots,
            limits=limits,
        )
        result = await execute_shell_command(
            ShellCommand(raw=command.strip(), argv=launch.argv, has_operators=False),
            cwd=launch.cwd,
            cancel_event=cancel_event,
            timeout_s=timeout_s,
            terminate_grace_s=terminate_grace_s,
            max_output_bytes=max_output_bytes,
            full_output_path=full_output_path,
            full_output_max_bytes=full_output_max_bytes,
            environment_overrides=launch.environment,
            preexec_fn=(
                process_limit_preexec(
                    memory_mb=limits.memory_mb,
                    pids_limit=limits.pids_limit,
                    cpus=limits.cpus,
                    wall_timeout_s=timeout_s,
                    file_size_bytes=max(output_max_bytes * 2, full_output_max_bytes),
                )
                if launch.engine in {"seatbelt", "bubblewrap"}
                else None
            ),
            process_tree_memory_bytes=(
                limits.memory_mb * 1024 * 1024
                if launch.engine in {"seatbelt", "bubblewrap"}
                else None
            ),
            process_tree_pids_limit=(
                limits.pids_limit
                if launch.engine in {"seatbelt", "bubblewrap"}
                else None
            ),
            process_tree_cpu_seconds=(
                max(1, math.ceil(timeout_s * limits.cpus))
                if launch.engine in {"seatbelt", "bubblewrap"}
                else None
            ),
        )
        committed: tuple[str, ...] = ()
        warnings: tuple[str, ...] = ()
        if result.exit_code == 0:
            committed, warnings = await asyncio.to_thread(
                _commit_outputs,
                outputs,
                output_root,
                max_files=output_max_files,
                max_bytes=output_max_bytes,
                office_edit_baselines=office_edit_baselines or {},
                backup_versions=backup_versions,
            )
        return SandboxExecutionResult(
            command_sha256=result.command_sha256,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
            execution_mode="native_sandbox",
            sandbox_engine=launch.engine,
            runtime_profile=limits.profile,
            full_output_path=result.full_output_path,
            full_output_truncated=result.full_output_truncated,
            full_output_size_bytes=result.full_output_size_bytes,
            committed_outputs=committed,
            output_warnings=warnings,
        )


def _commit_outputs(
    outputs: Path,
    output_root: Path,
    *,
    max_files: int,
    max_bytes: int,
    office_edit_baselines: Mapping[str, str] | None = None,
    backup_versions: int = 10,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = output_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise CoworkSandboxError("sandbox output_root 必须是普通目录")
    files: list[tuple[Path, Path]] = []
    scanned = 0
    total_bytes = 0
    for current, directories, names in os.walk(outputs, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(current) / name).is_symlink()
        )
        for name in sorted(names):
            scanned += 1
            if scanned > max_files:
                raise CoworkSandboxError(f"sandbox outputs 文件数超过上限 {max_files}")
            source = Path(current) / name
            if source.is_symlink() or not source.is_file():
                continue
            size = source.stat().st_size
            total_bytes += size
            if total_bytes > max_bytes:
                raise CoworkSandboxError(f"sandbox outputs 总大小超过上限 {max_bytes} bytes")
            files.append((source, source.relative_to(outputs)))
    committed: list[str] = []
    warnings: list[str] = []
    baselines = office_edit_baselines or {}
    for source, relative in files:
        try:
            destination = _safe_output_destination(root, relative)
            suffix = destination.suffix.casefold()
            baseline = baselines.get(relative.as_posix())
            if suffix in _FIXED_RENDERER_OUTPUT_SUFFIXES and baseline is None:
                raise CoworkSandboxError(
                    "新建 DOCX/XLSX/PPTX/PDF 必须使用 render_artifact；"
                    "run_sandbox 的 Office 候选路径必须与声明的既有源文件完全一致"
                )
            if baseline is not None:
                if destination.is_symlink() or not destination.is_file():
                    raise CoworkSandboxError("声明的 Office 源文件已不存在或不再是普通文件")
                if _sha256(destination) != baseline:
                    raise CoworkSandboxError("声明的 Office 源文件在 sandbox 执行期间发生变化")
            elif destination.exists() or destination.is_symlink():
                raise CoworkSandboxError("目标已存在；sandbox outputs v2 不允许无 baseline 覆盖")
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = destination.parent.resolve(strict=True)
            if resolved_parent != root and not resolved_parent.is_relative_to(root):
                raise CoworkSandboxError("sandbox output 父目录逃逸授权工作区")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.stem}.",
                suffix=f".candidate{destination.suffix}",
                dir=resolved_parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    with source.open("rb") as incoming:
                        shutil.copyfileobj(incoming, stream, length=1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                if suffix in _VALIDATED_OUTPUT_SUFFIXES:
                    report = validate_artifact_in_subprocess(
                        temporary,
                        render_visual=suffix == ".pptx",
                        max_file_bytes=max_bytes,
                    )
                    if not report.deliverable:
                        detail = "；".join(report.quality.warnings[:3]) or "未知验证错误"
                        raise CoworkSandboxError(f"候选产物验证失败：{detail}")
                elif suffix in _TEXT_OUTPUT_SUFFIXES:
                    temporary.read_text(encoding="utf-8")
                if baseline is not None:
                    if _sha256(destination) != baseline:
                        raise CoworkSandboxError("Office 源文件在候选校验期间发生变化")
                    create_file_backup(destination, backup_versions)
                    if _sha256(destination) != baseline:
                        raise CoworkSandboxError("Office 源文件在备份期间发生变化")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            committed.append(str(destination))
        except (CoworkSandboxError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
            warnings.append(f"{relative.as_posix()}: {error}")
    return tuple(committed), tuple(warnings)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_output_destination(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CoworkSandboxError("sandbox output 路径非法")
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise CoworkSandboxError("sandbox output 路径包含符号链接")
    destination = root / relative
    unresolved = destination.resolve(strict=False)
    if unresolved != root and not unresolved.is_relative_to(root):
        raise CoworkSandboxError("sandbox output 路径逃逸授权工作区")
    return destination


__all__ = [
    "CoworkSandboxError",
    "SandboxExecutionResult",
    "SandboxLaunch",
    "SandboxLimits",
    "SandboxRuntime",
    "build_sandbox_launch",
    "execute_sandbox_command",
]
