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
    ):
        assert registry.get(name).capability is None


def test_request_capability_schema_does_not_advertise_retired_office_grants() -> None:
    schema = build_default_cowork_registry().get("request_capability").resolved_input_schema()
    advertised = schema["properties"]["capability"]["enum"]

    assert "host.execute" in advertised
    assert "sandbox.execute" in advertised
    assert "shell.execute" not in advertised
    assert "office.word.edit" not in advertised
    assert "office.excel.edit" not in advertised


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


def test_browser_tools_split_read_write_destructive_and_network_scope() -> None:
    """浏览器动作等级与网络 origin 是两道独立权限。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    assert registry.get("browser_open").capability == "browser.read"
    assert "network.fetch" in registry.get("browser_open").extra_capabilities
    assert registry.get("browser_download").capability == "filesystem.write"
    assert "browser.destructive" in registry.get("browser_download").extra_capabilities

    assert registry.get("browser_click").capability == "browser.destructive"
    assert registry.get("browser_type").capability == "browser.write"
    assert registry.get("browser_select").capability == "browser.write"
    assert registry.get("browser_back").capability == "browser.read"
