import pytest
from pydantic import BaseModel, ConfigDict

from app.cowork.browser_tools import register_browser_tools
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
    build_default_cowork_registry,
)


class _EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _read_handler(
    _: CoworkToolContext,
    __: BaseModel,
) -> CoworkToolResult:
    return CoworkToolResult(output={})


def test_path_capability_requires_path_argument() -> None:
    registry = CoworkToolRegistry()

    with pytest.raises(ValueError, match="path_argument"):
        registry.register(
            CoworkToolSpec(
                name="unsafe_read",
                description="incorrect path capability declaration",
                args_model=_EmptyArgs,
                capability="filesystem.read",
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=_read_handler,
            )
        )


def test_pathless_meta_tools_have_no_capability() -> None:
    registry = build_default_cowork_registry()

    for name in (
        "search_tool_catalog",
        "ask_user",
        "request_directory",
        "request_capability",
        "list_workspace_roots",
        "list_office_files",
    ):
        assert registry.get(name).capability is None


def test_extra_capabilities_must_be_global() -> None:
    registry = CoworkToolRegistry()

    with pytest.raises(ValueError, match="extra_capabilities"):
        registry.register(
            CoworkToolSpec(
                name="bad_extra",
                description="路径能力没有第二个目标路径可校验",
                args_model=_EmptyArgs,
                capability="network.read",
                extra_capabilities=("filesystem.read",),
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=_read_handler,
            )
        )


def test_browser_network_tools_also_require_network_read() -> None:
    """browser.control 只表示"能操作页面"，不该顺带包含"能读公网"。

    browser_open 的返回值本身就是完整页面快照，browser_download 会把远端内容落盘；
    两者若只看 browser.control / filesystem.write，就成了 network.read 的绕过路径。
    """

    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    assert registry.get("browser_open").capability == "browser.control"
    assert "network.read" in registry.get("browser_open").extra_capabilities
    assert registry.get("browser_download").capability == "filesystem.write"
    assert "network.read" in registry.get("browser_download").extra_capabilities

    # 纯页面动作不额外要求网络能力：内容已经在本地会话里。
    for name in ("browser_click", "browser_type", "browser_select", "browser_back"):
        assert registry.get(name).extra_capabilities == ()
