from typing import Any, cast

import pytest
from uuid6 import uuid7

from app.cowork.browser_tools import (
    PlaywrightBrowserManager,
    _BrowserSession,
    register_browser_tools,
)
from app.cowork.tools import CoworkToolError, build_default_cowork_registry


class _ClosingBrowserContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_browser_navigation_uses_session_grant_and_only_upload_needs_approval() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    for name in (
        "browser_open",
        "browser_click",
        "browser_back",
        "browser_type",
        "browser_select",
    ):
        spec = registry.get(name)
        assert spec.capability == "browser.control"
        assert spec.approval_required is False
        assert spec.exclusive is True
        assert spec.effect != "none"

    assert registry.get("browser_upload").approval_required is True
    assert registry.get("browser_upload").exclusive is True
    assert registry.get("browser_download").approval_required is False
    assert registry.get("browser_download").exclusive is True
    assert registry.get("browser_screenshot").approval_required is False
    assert registry.get("browser_snapshot").effect == "none"
    assert registry.get("browser_find").effect == "none"


def test_readonly_subagent_cannot_receive_browser_actions() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    names = {
        definition.name
        for definition in registry.read_only_tool_definitions(
            exclude=frozenset(), query="浏览网页并填写表单"
        )
    }

    assert "browser_snapshot" in names
    assert "browser_click" not in names
    assert "browser_type" not in names
    assert "browser_upload" not in names


async def test_browser_session_is_bound_to_conversation_and_expires() -> None:
    now = [100.0]
    owner_id = uuid7()
    other_id = uuid7()
    context = _ClosingBrowserContext()
    manager = PlaywrightBrowserManager(
        idle_ttl_s=60,
        max_ttl_s=1_000,
        clock=lambda: now[0],
    )
    session = _BrowserSession(
        context=cast(Any, context),
        page=cast(Any, object()),
        conversation_id=owner_id,
        idle_expires_at=160.0,
        hard_expires_at=1_100.0,
        last_used=100.0,
    )
    manager._sessions["browser-session"] = session

    assert await manager.get("browser-session", conversation_id=owner_id) is session
    with pytest.raises(CoworkToolError, match="不存在"):
        await manager.get("browser-session", conversation_id=other_id)
    with pytest.raises(CoworkToolError, match="不存在"):
        await manager.close_session("browser-session", conversation_id=other_id)
    assert context.closed is False

    # 空闲窗口按使用顺延：持续活跃的浏览任务不该在原始 TTL 处被掐断。
    now[0] = 150.0
    assert await manager.get("browser-session", conversation_id=owner_id) is session
    assert session.idle_expires_at == 210.0
    now[0] = 205.0
    assert await manager.get("browser-session", conversation_id=owner_id) is session

    now[0] = 300.0
    with pytest.raises(CoworkToolError, match="已过期"):
        await manager.get("browser-session", conversation_id=owner_id)
    assert context.closed is True
    assert "browser-session" not in manager._sessions


async def test_browser_session_absolute_ttl_is_never_extended_by_use() -> None:
    """持续活跃也逃不掉硬上限；顺延后的空闲窗口不能越过 hard_expires_at。"""

    now = [0.0]
    owner_id = uuid7()
    context = _ClosingBrowserContext()
    manager = PlaywrightBrowserManager(
        idle_ttl_s=100,
        max_ttl_s=250,
        clock=lambda: now[0],
    )
    manager._sessions["browser-session"] = _BrowserSession(
        context=cast(Any, context),
        page=cast(Any, object()),
        conversation_id=owner_id,
        idle_expires_at=100.0,
        hard_expires_at=250.0,
        last_used=0.0,
    )

    for tick in (90.0, 180.0):
        now[0] = tick
        session = await manager.get("browser-session", conversation_id=owner_id)
        assert session.idle_expires_at <= session.hard_expires_at

    now[0] = 251.0
    with pytest.raises(CoworkToolError, match="已过期"):
        await manager.get("browser-session", conversation_id=owner_id)
    assert context.closed is True


def test_browser_manager_rejects_idle_ttl_above_absolute_ttl() -> None:
    with pytest.raises(ValueError, match="绝对 TTL"):
        PlaywrightBrowserManager(idle_ttl_s=600, max_ttl_s=300)
