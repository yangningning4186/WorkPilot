"""只读 Git 视图：状态、差异、历史。

为什么不让模型用 `run_shell` 跑 `git status`：那条路每次都要逐条审批（或者依赖部署级
allowlist 放行整个 `git`，而 `git` 下面有 `push` / `reset --hard` / `clean -fd`）。
版本控制的**只读**部分是日常工作里被问得最多的三件事，把它们做成固定 argv 的工具，
既拿掉了审批摩擦，也不给写操作留入口。

两条边界：

1. **argv 固定，不拼 shell 字符串。** 模型只能填有限的几个受校验参数，选项本身来自代码。
2. **输出按已授权目录收窄。** `git -C <目录>` 会顺着找到仓库根，那个根可能在授权目录
   之外——直接跑就会把用户没授权的文件差异吐出来。所以每条命令都追加
   ``-- <已授权目录>`` pathspec，让 git 自己把结果裁到授权范围内。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.cowork.shell import _minimal_environment, _read_limited, _terminate_process_group

GIT_TIMEOUT_S = 20.0
_TERMINATE_GRACE_S = 2.0


class CoworkGitError(RuntimeError):
    """面向模型的 Git 错误（约束 4：message 是可执行指令）。"""


@dataclass(frozen=True)
class GitOutput:
    text: str
    truncated: bool


def _require_directory(path: Path) -> Path:
    if path.is_file():
        # 模型很容易把文件路径传进来。直接用它的父目录而不是报错：意图是明确的。
        return path.parent
    if not path.is_dir():
        raise CoworkGitError(f"目录不存在: {path}")
    return path


async def _run_git(root: Path, args: tuple[str, ...], *, max_bytes: int) -> GitOutput:
    executable = shutil.which("git")
    if executable is None:
        raise CoworkGitError("这台机器上没有安装 git，无法读取版本信息")
    process = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(root),
        # 分页器会在非 tty 下阻塞；配置里可能开了 color/pager，这里一律关掉。
        "--no-pager",
        "-c",
        "color.ui=false",
        *args,
        cwd=str(root),
        env=_minimal_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # 独立进程组是超时回收的前提：`_terminate_process_group` 走的是 killpg，
        # 不建组的话那一发信号会打到 worker 自己所在的进程组上。
        start_new_session=os.name == "posix",
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        stdout, truncated = await asyncio.wait_for(
            _read_limited(process.stdout, max_bytes), timeout=GIT_TIMEOUT_S
        )
        stderr, _ = await asyncio.wait_for(
            _read_limited(process.stderr, 8 * 1024), timeout=GIT_TIMEOUT_S
        )
        await asyncio.wait_for(process.wait(), timeout=GIT_TIMEOUT_S)
    except TimeoutError as error:
        await _terminate_process_group(process, _TERMINATE_GRACE_S)
        raise CoworkGitError(f"git 命令超过 {GIT_TIMEOUT_S:.0f}s 未返回，已终止") from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "git 返回非零退出码"
        raise CoworkGitError(f"git 执行失败：{detail}")
    return GitOutput(text=stdout.decode("utf-8", errors="replace"), truncated=truncated)


async def _toplevel(root: Path) -> str | None:
    try:
        result = await _run_git(root, ("rev-parse", "--show-toplevel"), max_bytes=4096)
    except CoworkGitError:
        return None
    value = result.text.strip()
    return value or None


async def _branch(root: Path) -> str:
    try:
        result = await _run_git(root, ("rev-parse", "--abbrev-ref", "HEAD"), max_bytes=4096)
    except CoworkGitError:
        # 全新仓库还没有 commit，`--abbrev-ref HEAD` 会失败；分支名照样能读出来。
        try:
            result = await _run_git(root, ("symbolic-ref", "--short", "HEAD"), max_bytes=4096)
        except CoworkGitError:
            return "(unknown)"
    return result.text.strip() or "(unknown)"


def _not_a_repository(root: Path) -> dict[str, object]:
    return {
        "is_repository": False,
        "path": str(root),
        "note": f"{root} 不在 Git 仓库里，版本信息不可用",
    }


async def git_status(root: Path, *, max_bytes: int) -> dict[str, object]:
    """已授权目录范围内的工作区状态。"""

    root = _require_directory(root)
    toplevel = await _toplevel(root)
    if toplevel is None:
        return _not_a_repository(root)
    porcelain = await _run_git(
        root, ("status", "--porcelain=v1", "--", str(root)), max_bytes=max_bytes
    )
    entries = [line for line in porcelain.text.splitlines() if line.strip()]
    return {
        "is_repository": True,
        "path": str(root),
        "toplevel": toplevel,
        "branch": await _branch(root),
        "clean": not entries,
        "entries": entries,
        "truncated": porcelain.truncated,
    }


async def git_diff(
    root: Path,
    *,
    staged: bool,
    max_bytes: int,
    stat_only: bool,
) -> dict[str, object]:
    """已授权目录范围内的未提交改动。"""

    root = _require_directory(root)
    toplevel = await _toplevel(root)
    if toplevel is None:
        return _not_a_repository(root)
    args: list[str] = ["diff"]
    if staged:
        args.append("--cached")
    args.append("--stat" if stat_only else "--patch")
    args.extend(("--", str(root)))
    result = await _run_git(root, tuple(args), max_bytes=max_bytes)
    return {
        "is_repository": True,
        "path": str(root),
        "staged": staged,
        "stat_only": stat_only,
        "diff": result.text,
        "empty": not result.text.strip(),
        "truncated": result.truncated,
        **(
            {"note": "差异被截断了。用 stat_only=true 先看改了哪些文件，再逐个文件读。"}
            if result.truncated
            else {}
        ),
    }


async def git_log(
    root: Path,
    *,
    max_count: int,
    max_bytes: int,
) -> dict[str, object]:
    """已授权目录范围内的提交历史。"""

    root = _require_directory(root)
    toplevel = await _toplevel(root)
    if toplevel is None:
        return _not_a_repository(root)
    result = await _run_git(
        root,
        (
            "log",
            f"--max-count={max_count}",
            # %x1f/%x1e 是 ASCII 的单元/记录分隔符：提交标题里出现它们的概率为零，
            # 用 `|` 之类的可见字符分隔迟早会被某条标题撞上。
            "--pretty=format:%h%x1f%an%x1f%aI%x1f%s%x1e",
            "--",
            str(root),
        ),
        max_bytes=max_bytes,
    )
    commits: list[dict[str, str]] = []
    for record in result.text.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f")
        if len(fields) != 4:
            continue
        commits.append(
            {
                "sha": fields[0],
                "author": fields[1],
                "committed_at": fields[2],
                "subject": fields[3],
            }
        )
    return {
        "is_repository": True,
        "path": str(root),
        "branch": await _branch(root),
        "commits": commits,
        "truncated": result.truncated,
    }
