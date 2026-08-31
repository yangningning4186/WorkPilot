import json
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent_core.tools import render_tool_prompt_instructions
from app.core.config import get_settings
from app.cowork.browser_tools import register_browser_tools
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
    PreviewPresentationArgs,
    _artifact_image_owners,
    _preview_presentation,
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


def test_render_artifact_accepts_one_layer_stringified_spec() -> None:
    registry = build_default_cowork_registry()
    spec = {
        "schema_version": 1,
        "artifact_type": "pptx",
        "title": "确定性兼容",
        "slides": [{"id": "s1", "layout": "title", "title": "只解析一次"}],
    }

    parsed = registry.parse_arguments(
        "render_artifact",
        {"path": "/tmp/compat.pptx", "spec": json.dumps(spec, ensure_ascii=False)},
    )

    assert parsed["spec"]["artifact_type"] == "pptx"
    assert parsed["spec"]["title"] == "确定性兼容"
    assert parsed["spec"]["slides"][0]["id"] == "s1"


def test_preview_presentation_is_deferred_and_accepts_stringified_spec() -> None:
    registry = build_default_cowork_registry()
    spec = {
        "schema_version": 1,
        "artifact_type": "pptx",
        "title": "封面试制",
        "slides": [{"id": "s1", "layout": "title", "title": "先看再写"}],
    }

    parsed = registry.parse_arguments(
        "preview_presentation",
        {"spec": json.dumps(spec, ensure_ascii=False), "pages": [1]},
    )

    assert parsed["spec"]["title"] == "封面试制"
    assert "preview_presentation" in registry.deferred_tool_names()
    assert registry.get("preview_presentation").effect == "none"
    assert registry.get("preview_presentation").execution_mode == "sequential"


def test_canvas_images_join_artifact_image_authorization_inventory() -> None:
    args = PreviewPresentationArgs.model_validate(
        {
            "spec": {
                "artifact_type": "pptx",
                "title": "安全画布图片",
                "slides": [
                    {
                        "id": "canvas",
                        "layout": "canvas",
                        "title": "图片仍需路径授权",
                        "canvas": {
                            "elements": [
                                {
                                    "type": "image",
                                    "id": "visual",
                                    "x": 0,
                                    "y": 5,
                                    "width": 45,
                                    "height": 80,
                                    "image_path": "assets/visual.png",
                                    "image_alt": "产品流程主视觉",
                                },
                                {
                                    "type": "text",
                                    "id": "explanation",
                                    "x": 55,
                                    "y": 18,
                                    "width": 40,
                                    "height": 50,
                                    "text": "图片右侧的结论性说明",
                                },
                            ]
                        },
                    }
                ],
            }
        }
    )

    owners = _artifact_image_owners(args.spec)

    assert [owner.image_path for owner in owners] == ["assets/visual.png"]


async def test_preview_presentation_returns_model_visible_page_images(tmp_path) -> None:
    settings = get_settings().model_copy(
        update={"office_preview_cache_path": tmp_path / "preview-cache"}
    )
    context = SimpleNamespace(
        settings=settings,
        session=None,
        conversation_id=uuid4(),
        authorization_annotations=[],
    )
    args = PreviewPresentationArgs.model_validate(
        {
            "spec": {
                "schema_version": 1,
                "artifact_type": "pptx",
                "title": "可视试制",
                "visual_kit": "consulting-02",
                "slides": [
                    {
                        "id": "cover",
                        "role": "hero",
                        "rhythm": "peak",
                        "layout": "title",
                        "title": "先看封面，再写整稿",
                        "subtitle": "真实页图会回到下一轮模型",
                    }
                ],
            },
            "pages": [1],
        }
    )

    result = await _preview_presentation(context, args)  # type: ignore[arg-type]

    assert result.content["page_count"] == 1
    assert result.content["previewed_pages"] == [1]
    assert len(result.attachments) == 1
    assert result.attachments[0].kind == "image"
    assert result.attachments[0].media_type == "image/png"
    candidate = next((tmp_path / "preview-cache").rglob("candidate.pptx"))
    with ZipFile(candidate) as archive:
        slide_xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
    assert "F4F7F8" in slide_xml
    assert "1B9C95" in slide_xml


async def test_preview_presentation_includes_full_deck_montage(tmp_path) -> None:
    settings = get_settings().model_copy(
        update={"office_preview_cache_path": tmp_path / "preview-cache"}
    )
    context = SimpleNamespace(
        settings=settings,
        session=None,
        conversation_id=uuid4(),
        authorization_annotations=[],
    )
    args = PreviewPresentationArgs.model_validate(
        {
            "spec": {
                "schema_version": 1,
                "artifact_type": "pptx",
                "title": "全稿总览",
                "slides": [
                    {
                        "id": "cover",
                        "role": "hero",
                        "rhythm": "peak",
                        "layout": "title",
                        "title": "先看全稿节奏",
                        "subtitle": "总览图与代表页一起返回",
                    },
                    {
                        "id": "thesis",
                        "layout": "statement",
                        "title": "第二页承担核心结论",
                        "body": "总览图帮助识别连续同构和视觉重量失衡。",
                    },
                ],
            },
            "pages": [1, 2],
        }
    )

    result = await _preview_presentation(context, args)  # type: ignore[arg-type]

    assert result.content["montage_included"] is True
    assert result.content["previewed_pages"] == [1, 2]
    assert len(result.attachments) == 3
    assert result.attachments[0].filename == "montage.png"


@pytest.mark.parametrize("raw", ["[]", '"{\\"artifact_type\\":\\"pptx\\"}"', "not-json"])
def test_render_artifact_rejects_non_object_or_recursive_string_spec(raw: str) -> None:
    registry = build_default_cowork_registry()

    with pytest.raises(CoworkToolError, match="JSON object"):
        registry.parse_arguments("render_artifact", {"path": "/tmp/compat.pptx", "spec": raw})


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
    registry = build_default_cowork_registry()
    schema = registry.get("request_capability").resolved_input_schema()
    advertised = schema["properties"]["capability"]["enum"]

    assert "host.execute" in advertised
    assert "sandbox.execute" in advertised
    assert "shell.execute" not in advertised
    assert "office.word.edit" not in advertised
    assert "office.excel.edit" not in advertised
    assert "request_capability" not in {
        item.name for item in registry.tool_definitions_for("打开网页并处理文件")
    }

    with pytest.raises(ValueError, match="网络权限目标"):
        registry.get("request_capability").args_model.model_validate(
            {
                "capability": "network.fetch",
                "reason": "错误地合并了多个站点",
                "resource_scope": (
                    "origin:https://www.baidu.com, origin:https://baike.baidu.com"
                ),
            }
        )


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


def test_browser_tools_use_action_approval_without_repeated_global_capabilities() -> None:
    """浏览器保留公网安全校验与真正的动作审批，不再先申请抽象全局能力。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    assert registry.get("browser_open").capability is None
    assert registry.get("browser_open").extra_capabilities == ()
    assert registry.get("browser_download").capability == "filesystem.write"
    assert registry.get("browser_download").extra_capabilities == ()

    assert registry.get("browser_click").approval_required is False
    assert registry.get("browser_submit").approval_required is True
    assert registry.get("browser_submit").approval_can_be_waived is False
    assert registry.get("browser_type").capability is None
    assert registry.get("browser_select").capability is None
    assert registry.get("browser_back").capability is None
