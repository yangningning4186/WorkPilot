from typing import Any, cast

import pytest
from uuid6 import uuid7

from app.cowork.browser_tools import (
    PlaywrightBrowserManager,
    _BrowserSession,
    _fresh_control,
    _inspect_control,
    _is_consequential_control,
    register_browser_tools,
)
from app.cowork.tools import CoworkToolError, build_default_cowork_registry
from app.cowork.web import CoworkWebError


class _ClosingBrowserContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.aborted = False
        self.continued = False

    async def abort(self, _: str) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakePage:
    def __init__(self) -> None:
        self.guard: Any = None
        self.routes: list[_FakeRoute] = []

    async def goto(self, _: str, **__: Any) -> None:
        for url in (
            "https://allowed.example/",
            "https://exfil.example/pixel?secret=private",
        ):
            route = _FakeRoute(url)
            self.routes.append(route)
            await self.guard(route)


class _FakeControlHandle:
    def __init__(self, info: dict[str, object]) -> None:
        self.info = info

    async def evaluate(self, _: str) -> dict[str, object]:
        return dict(self.info)


class _FakeBrowserContext(_ClosingBrowserContext):
    def __init__(self) -> None:
        super().__init__()
        self.page = _FakePage()

    async def new_page(self) -> _FakePage:
        return self.page

    async def route(self, _: str, guard: Any) -> None:
        self.page.guard = guard

    async def route_web_socket(self, *_: Any) -> None:
        return None


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeBrowserContext()

    async def new_context(self, **_: Any) -> _FakeBrowserContext:
        return self.context

    def is_connected(self) -> bool:
        return True


def test_browser_navigation_splits_action_levels_and_consequential_approval() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    for name in ("browser_open", "browser_back"):
        spec = registry.get(name)
        assert spec.approval_required is False
        assert spec.exclusive is True
        assert spec.effect != "none"

    assert registry.get("browser_open").capability is None
    assert registry.get("browser_open").extra_capabilities == ()
    assert registry.get("browser_back").capability is None
    assert registry.get("browser_type").approval_required is True
    assert registry.get("browser_type").approval_can_be_waived is False
    assert registry.get("browser_select").approval_required is True
    assert registry.get("browser_select").approval_can_be_waived is False
    assert registry.get("browser_click").capability is None
    assert registry.get("browser_click").approval_required is False
    assert registry.get("browser_submit").approval_required is True
    assert registry.get("browser_submit").approval_can_be_waived is False
    assert registry.get("browser_upload").approval_required is True
    assert registry.get("browser_upload").approval_can_be_waived is False
    assert registry.get("browser_upload").exclusive is True
    assert registry.get("browser_download").extra_capabilities == ()
    assert registry.get("browser_download").approval_required is True
    assert registry.get("browser_download").approval_can_be_waived is False
    assert registry.get("browser_download").exclusive is True
    assert registry.get("browser_screenshot").approval_required is False
    assert registry.get("browser_snapshot").effect == "none"
    assert registry.get("browser_find").effect == "none"
    assert "query" in registry.get("browser_snapshot").resolved_input_schema()["properties"]
    assert registry.get("browser_find").model_visible is False
    assert "browser_find" not in registry.deferred_tool_names()


def test_browser_click_classifies_navigation_separately_from_submissions() -> None:
    assert not _is_consequential_control(
        {"tag": "a", "href": "https://example.com/next", "label": "下一页"}
    )
    assert not _is_consequential_control(
        {
            "tag": "button",
            "in_form": False,
            "type": "button",
            "label": "展开菜单",
            "aria_controls": "menu-1",
            "aria_expanded": "false",
        }
    )
    assert _is_consequential_control(
        {"tag": "button", "in_form": True, "type": "submit", "label": "继续"}
    )
    assert _is_consequential_control({"tag": "a", "href": "#", "label": "删除这条记录"})
    assert _is_consequential_control(
        {"tag": "button", "in_form": False, "type": "button", "label": "Archive"}
    )


async def test_browser_action_rebinds_the_same_dom_node_before_execution() -> None:
    url = "https://example.com/form"
    page = cast(Any, type("Page", (), {"url": url})())
    handle = _FakeControlHandle(
        {
            "connected": True,
            "tag": "button",
            "type": "button",
            "role": "",
            "name": "Save",
            "placeholder": "",
            "text": "Save",
            "href": "",
            "raw_href": "",
            "target": "",
            "download": "",
            "aria_expanded": "",
            "aria_controls": "",
            "in_form": True,
            "disabled": False,
        }
    )
    cached = await _inspect_control(cast(Any, handle))
    session = _BrowserSession(
        context=cast(Any, _ClosingBrowserContext()),
        page=page,
        conversation_id=uuid7(),
        idle_expires_at=100,
        hard_expires_at=200,
        controls=[cast(Any, handle)],
        control_info=[cached],
        snapshot_url=url,
    )

    rebound, _ = await _fresh_control(
        session,
        0,
        expected_url=url,
        expected_label="Save",
    )
    assert rebound is handle

    handle.info["name"] = "Delete"
    handle.info["text"] = "Delete"
    with pytest.raises(CoworkToolError, match="发生变化"):
        await _fresh_control(
            session,
            0,
            expected_url=url,
            expected_label="Save",
        )


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


async def test_browser_public_target_guard_checks_every_subresource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _FakeBrowser()
    manager = PlaywrightBrowserManager()
    manager._browser = cast(Any, browser)
    checked: list[str] = []

    async def public_target(url: str) -> None:
        checked.append(url)
        if "exfil.example" in url:
            raise CoworkWebError("目标解析到私网地址")

    monkeypatch.setattr("app.cowork.browser_tools.assert_public_target", public_target)

    _, session = await manager.open(
        "https://allowed.example/",
        conversation_id=uuid7(),
        timeout_s=1,
    )

    assert checked == [
        "https://allowed.example/",
        "https://allowed.example/",
        "https://exfil.example/pixel?secret=private",
    ]
    assert browser.context.page.routes[0].continued is True
    assert browser.context.page.routes[1].aborted is True
    assert session.blocked_url == "https://exfil.example/pixel?secret=private"


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
