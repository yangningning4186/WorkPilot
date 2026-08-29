import pytest
from pydantic import BaseModel, ConfigDict

from app.agent_core.tools import render_tool_prompt_instructions
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


class _QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


async def _read_handler(
    _: CoworkToolContext,
    __: BaseModel,
) -> CoworkToolResult:
    return CoworkToolResult(content={})


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

    for name in ("ask_user", "request_directory", "request_capability"):
        assert registry.get(name).capability is None


def test_tool_argument_compatibility_runs_before_schema_validation() -> None:
    registry = CoworkToolRegistry()
    registry.register(
        CoworkToolSpec(
            name="compat_search",
            description="兼容旧参数名",
            args_model=_QueryArgs,
            capability=None,
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_read_handler,
            prepare_arguments=lambda raw: {"query": raw.get("query", raw.get("q"))},
        )
    )

    assert registry.parse_arguments("compat_search", {"q": "季度报告"}) == {"query": "季度报告"}


def test_tool_prompt_guidance_and_sequential_execution_are_declared_on_spec() -> None:
    registry = CoworkToolRegistry()
    for name, execution_mode in (("guided", "sequential"), ("other", "auto")):
        registry.register(
            CoworkToolSpec(
                name=name,
                description="有自带提示的工具",
                args_model=_EmptyArgs,
                capability=None,
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=_read_handler,
                prompt_snippet="只在已有证据不足时调用。" if name == "guided" else "",
                prompt_guidelines=("先缩小查询范围",) if name == "guided" else (),
                execution_mode=execution_mode,  # type: ignore[arg-type]
            )
        )

    definitions = {item.name: item for item in registry.tool_definitions()}
    rendered = render_tool_prompt_instructions(definitions.values())

    assert definitions["guided"].prompt_snippet == "只在已有证据不足时调用。"
    assert definitions["guided"].prompt_guidelines == ("先缩小查询范围",)
    assert "<tool_prompt_guidance>" in rendered
    assert "[guided]" in rendered
    assert registry.parallel_safe(["guided", "other"]) is False


def test_retired_duplicate_tools_are_not_registered() -> None:
    names = build_default_cowork_registry().names()

    assert {
        "list_workspace_roots",
        "load_skill_resource",
        "search_tool_catalog",
    }.isdisjoint(names)


def test_consolidated_tools_are_visible_and_legacy_names_are_hidden() -> None:
    registry = build_default_cowork_registry()
    visible = {item.name for item in registry.tool_definitions_for("处理工作区文件")}
    deferred = registry.deferred_tool_names()

    assert {"read_file", "write_file"} <= visible
    assert {
        "read_text_file",
        "write_text_file",
        "read_pdf",
        "create_artifact",
    }.isdisjoint(visible | deferred)
    assert registry.get("read_text_file").replacement == "read_file"
    assert registry.get("create_artifact").replacement == "write_file"


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
