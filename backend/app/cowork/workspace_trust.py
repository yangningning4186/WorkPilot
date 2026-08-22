"""仓库自带的命令白名单，以及它生效的前提：用户信任那个目录。

一个仓库最清楚"在我这里跑什么是安全的"——`uv run pytest`、`npm test`、`make lint`。
把这些写进部署级 allowlist 不合适：那是全局的、要改配置文件、而且对别的目录也生效。
写进仓库自己的 `.workpilot/config.toml` 才是对的粒度。

**但仓库自己说了不算。** clone 一个陌生仓库就等于执行它声明的命令，那是一条从
"读代码"到"跑代码"的静默升级。所以这里是两段式：仓库负责声明，用户负责信任，
两者都成立才放行。

信任跟着**规范化路径**走，不跟着某一次配置内容的快照走：用户信任的是"这个目录"，
之后仓库更新了 allowlist 也照旧生效，直到用户撤销信任。这是刻意的——反过来
（每次配置变更都重新问一遍）会把信任变成又一个人们闭眼点过的弹窗。
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork.permissions import list_session_roots
from app.cowork.shell import CoworkShellError, parse_shell_command
from app.cowork_store.routing import cowork_store

WORKSPACE_CONFIG_RELATIVE_PATH = Path(".workpilot") / "config.toml"

# 一个仓库最多声明这么多条命令前缀。没有上限的话，一份被污染的配置可以把整张
# allowlist 撑成"什么都放行"。
MAX_WORKSPACE_ALLOWLIST_ENTRIES = 32
MAX_CONFIG_BYTES = 64 * 1024


class WorkspaceTrustError(RuntimeError):
    """面向用户的配置错误。"""


@dataclass(frozen=True)
class WorkspaceAllowlist:
    root: Path
    entries: tuple[str, ...]
    # 配置里被拒绝的条目与原因。要显示给用户看：一条被静默丢掉的 allowlist 项，
    # 表现出来就是"我明明写了它却还在弹审批"。
    rejected: tuple[tuple[str, str], ...]


def read_workspace_allowlist(root: Path) -> WorkspaceAllowlist:
    """读取仓库声明的命令前缀。**不**判断信任，也不判断是否该放行。"""

    config_path = root / WORKSPACE_CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        return WorkspaceAllowlist(root=root, entries=(), rejected=())
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise WorkspaceTrustError(
            f"{WORKSPACE_CONFIG_RELATIVE_PATH} 超过 {MAX_CONFIG_BYTES} 字节，拒绝解析"
        )
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise WorkspaceTrustError(f"{WORKSPACE_CONFIG_RELATIVE_PATH} 解析失败：{error}") from error

    raw = document.get("shell", {})
    if not isinstance(raw, dict):
        raise WorkspaceTrustError("配置里的 [shell] 必须是一个表")
    declared = raw.get("allow", [])
    if not isinstance(declared, list):
        raise WorkspaceTrustError("[shell].allow 必须是字符串数组")

    entries: list[str] = []
    rejected: list[tuple[str, str]] = []
    for item in declared[:MAX_WORKSPACE_ALLOWLIST_ENTRIES]:
        if not isinstance(item, str):
            rejected.append((str(item), "不是字符串"))
            continue
        try:
            parsed = parse_shell_command(item)
        except CoworkShellError as error:
            rejected.append((item, str(error)))
            continue
        if parsed.has_operators:
            # 与部署级 allowlist 同一条不变量：带操作符的条目放行的是两条命令，
            # 后一条从没被任何人看过。
            rejected.append((item, "包含 shell 操作符，不能作为白名单条目"))
            continue
        entries.append(item)
    if len(declared) > MAX_WORKSPACE_ALLOWLIST_ENTRIES:
        rejected.append(
            (
                "...",
                f"最多只接受 {MAX_WORKSPACE_ALLOWLIST_ENTRIES} 条，其余已忽略",
            )
        )
    return WorkspaceAllowlist(root=root, entries=tuple(entries), rejected=tuple(rejected))


async def set_workspace_trust(
    session: AsyncSession, *, canonical_path: str, trusted: bool
) -> bool:
    store = cowork_store()
    return await store.set_workspace_trust(canonical_path=canonical_path, trusted=trusted)


async def is_workspace_trusted(session: AsyncSession, *, canonical_path: str) -> bool:
    store = cowork_store()
    return await store.is_workspace_trusted(canonical_path=canonical_path)


async def list_workspace_trust(session: AsyncSession) -> list[str]:
    store = cowork_store()
    return await store.list_workspace_trust()


async def workspace_allows_command(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    cwd: Path,
    argv: Sequence[str],
    has_operators: bool,
) -> str | None:
    """这条命令有没有被"用户信任的目录 + 仓库自己声明的白名单"放行。

    命中时返回那条 allowlist 条目，供事件留痕；否则返回 None。

    三个条件缺一不可：命令里没有 shell 操作符、cwd 落在某个已授权 root 里、
    那个 root 的规范化路径被用户信任过。任何一条不成立都退回逐次审批。
    """

    if has_operators or not argv:
        return None
    roots = await list_session_roots(session, conversation_id=conversation_id)
    resolved_cwd = await asyncio.to_thread(cwd.resolve)
    for root in roots:
        root_path = Path(root.canonical_path)
        try:
            resolved_cwd.relative_to(root_path)
        except ValueError:
            continue
        if not await is_workspace_trusted(session, canonical_path=root.canonical_path):
            continue
        try:
            allowlist = await asyncio.to_thread(read_workspace_allowlist, root_path)
        except WorkspaceTrustError:
            # 配置坏了不该让命令悄悄获得放行，也不该让整次运行失败：退回审批即可。
            continue
        for entry in allowlist.entries:
            prefix = parse_shell_command(entry).argv
            if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
                return entry
    return None
