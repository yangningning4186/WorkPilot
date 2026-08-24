"""仓库自带的命令白名单，以及它生效的前提。

要挡住的具体风险是：clone 一个陌生仓库就等于执行它声明的命令。所以这里锁两件事——
没有信任时仓库声明的东西一条都不算数，以及信任之后仓库也不能声明出"什么都放行"。
"""

from pathlib import Path

import pytest

from app.core.db import DbSession as AsyncSession
from app.cowork.permissions import create_session_root, grant_capability
from app.cowork.workspace_trust import (
    MAX_WORKSPACE_ALLOWLIST_ENTRIES,
    WorkspaceTrustError,
    is_workspace_trusted,
    read_workspace_allowlist,
    set_workspace_trust,
    workspace_allows_command,
)
from app.runstore.runs import ensure_conversation


def _write_config(root: Path, body: str) -> None:
    (root / ".workpilot").mkdir(parents=True, exist_ok=True)
    (root / ".workpilot" / "config.toml").write_text(body, encoding="utf-8")


def test_entries_with_shell_operators_are_rejected_with_a_reason(tmp_path: Path) -> None:
    """带操作符的条目放行的是两条命令，后一条从没被任何人看过。

    被拒绝的条目必须带原因回来：静默丢掉它，用户看到的现象就是"我明明写了它却还在弹审批"。
    """

    _write_config(tmp_path, '[shell]\nallow = ["npm test", "npm test && rm -rf ~"]\n')
    allowlist = read_workspace_allowlist(tmp_path)
    assert allowlist.entries == ("npm test",)
    assert len(allowlist.rejected) == 1
    assert "操作符" in allowlist.rejected[0][1]


def test_the_entry_count_is_capped(tmp_path: Path) -> None:
    """没有上限的话，一份被污染的配置可以把白名单撑成"什么都放行"。"""

    entries = ", ".join(f'"cmd{index}"' for index in range(MAX_WORKSPACE_ALLOWLIST_ENTRIES + 5))
    _write_config(tmp_path, f"[shell]\nallow = [{entries}]\n")
    allowlist = read_workspace_allowlist(tmp_path)
    assert len(allowlist.entries) == MAX_WORKSPACE_ALLOWLIST_ENTRIES
    assert any("最多只接受" in reason for _, reason in allowlist.rejected)


def test_a_missing_config_is_not_an_error(tmp_path: Path) -> None:
    """Cowork 的工作目录经常只是个文档文件夹。"""

    assert read_workspace_allowlist(tmp_path).entries == ()


def test_a_broken_config_is_an_actionable_error(tmp_path: Path) -> None:
    _write_config(tmp_path, "[shell\nallow = ")
    with pytest.raises(WorkspaceTrustError, match="解析失败"):
        read_workspace_allowlist(tmp_path)


@pytest.mark.integration
async def test_a_declared_command_does_nothing_until_the_user_trusts_the_directory(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Workspace trust")
    root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await grant_capability(db_session, conversation_id=conversation_id, capability="shell.execute")
    await db_session.commit()
    _write_config(Path(root.canonical_path), '[shell]\nallow = ["npm test"]\n')

    # 仓库已经声明了，但没人信任过这个目录：一条都不算数。
    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=Path(root.canonical_path),
            argv=("npm", "test"),
            has_operators=False,
        )
    ) is None

    await set_workspace_trust(db_session, canonical_path=root.canonical_path, trusted=True)
    await db_session.commit()
    assert await is_workspace_trusted(db_session, canonical_path=root.canonical_path)
    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=Path(root.canonical_path),
            argv=("npm", "test", "--", "-w"),
            has_operators=False,
        )
        == "npm test"
    )
    # 没声明过的命令不沾光。
    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=Path(root.canonical_path),
            argv=("npm", "publish"),
            has_operators=False,
        )
    ) is None
    # 带操作符的命令即使前缀命中也不放行。
    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=Path(root.canonical_path),
            argv=("npm", "test"),
            has_operators=True,
        )
    ) is None

    await set_workspace_trust(db_session, canonical_path=root.canonical_path, trusted=False)
    await db_session.commit()
    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=Path(root.canonical_path),
            argv=("npm", "test"),
            has_operators=False,
        )
    ) is None


@pytest.mark.integration
async def test_trust_does_not_reach_outside_the_granted_roots(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """cwd 落在授权目录之外时，即使那个目录被信任过也不放行。"""

    conversation_id = await ensure_conversation(db_session, title="Outside root")
    inside = tmp_path / "granted"
    outside = tmp_path / "elsewhere"
    inside.mkdir()
    outside.mkdir()
    root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(inside),
        access_mode="read_write",
    )
    await set_workspace_trust(db_session, canonical_path=root.canonical_path, trusted=True)
    await db_session.commit()
    _write_config(Path(root.canonical_path), '[shell]\nallow = ["npm test"]\n')

    assert (
        await workspace_allows_command(
            db_session,
            conversation_id=conversation_id,
            cwd=outside,
            argv=("npm", "test"),
            has_operators=False,
        )
    ) is None
