"""工作区沉浸阅读引擎的用例。

按 CLAUDE.md 的门禁划分，这一层挡的是**每个 PR 都要过的那道**：三层匹配升级、locator
语义、引文校验与几何回填，全是纯函数或只碰 tmp 文件，不需要数据库也不需要真 PDF。
真语料上的效果由 `eval/` 那一层负责，两者不互相替代。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from uuid6 import uuid7

from app.core.config import Settings
from app.cowork.reading import (
    Material,
    MaterialCache,
    ReadingError,
    block_locations,
    parse_locators,
    render_locate_block,
    render_outline,
    render_units,
    search_material,
    search_units,
    units_from_pages,
    units_from_sections,
    verify_quote,
)
from app.cowork.reading.units import build_outline, trim_outline
from app.cowork.reading_tools import register_reading_tools
from app.cowork.tools import CoworkToolContext, CoworkToolError, CoworkToolRegistry
from app.cowork_contracts import PathAuthorization
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument


def _block(
    idx: int,
    text: str,
    *,
    page: int | None = None,
    block_type: str = "paragraph",
    heading_path: tuple[str, ...] = (),
    bbox: tuple[float, float, float, float] = (0.1, 0.1, 0.9, 0.2),
) -> ParsedBlock:
    locations = (
        (
            BlockLocation(
                page_no=page,
                page_width=595.0,
                page_height=842.0,
                rotation=0,
                coord_origin="top_left",
                bbox_norm=bbox,
            ),
        )
        if page is not None
        else ()
    )
    return ParsedBlock(
        block_idx=idx,
        block_type=block_type,
        text=text,
        char_start=0,
        char_end=len(text),
        heading_path=heading_path,
        locations=locations,
    )


def _material(units: tuple, *, unit: str = "page") -> Material:
    return Material(
        path=Path("/tmp/paper.pdf"),
        material_id="deadbeefdeadbeef",
        filename="paper.pdf",
        title="Attention",
        unit=unit,  # type: ignore[arg-type]
        units=units,
        outline=build_outline(units),
        parser="pymupdf",
        byte_size=4096,
    )


# --- 切分：locator 与页码必须一一对应 ---------------------------------------


def test_pages_without_text_still_occupy_a_locator() -> None:
    """整页只有图的论文很常见；跳过它会让此后每一页的引用都偏一位。"""
    document = ParsedDocument(
        full_text="",
        blocks=[_block(0, "first page", page=1), _block(1, "third page", page=3)],
        page_count=3,
    )
    units = units_from_pages(document)

    assert [unit.locator for unit in units] == [1, 2, 3]
    assert units[1].is_empty
    assert units[2].text == "third page"


def test_block_spanning_pages_lands_on_its_first_page() -> None:
    spanning = ParsedBlock(
        block_idx=0,
        block_type="table",
        text="跨页表格",
        char_start=0,
        char_end=4,
        heading_path=(),
        locations=(
            BlockLocation(2, 595.0, 842.0, 0, "top_left", (0.1, 0.8, 0.9, 0.95)),
            BlockLocation(3, 595.0, 842.0, 0, "top_left", (0.1, 0.05, 0.9, 0.2)),
        ),
    )
    units = units_from_pages(ParsedDocument(full_text="", blocks=[spanning], page_count=3))

    assert units[1].text == "跨页表格"
    assert units[2].is_empty


def test_sections_break_on_headings_and_never_split_a_block() -> None:
    blocks = [
        _block(0, "标题一", block_type="title", heading_path=("标题一",)),
        _block(1, "正" * 2_000),
        _block(2, "文" * 1_000),
        _block(3, "标题二", block_type="title", heading_path=("标题二",)),
        _block(4, "尾段"),
    ]
    units = units_from_sections(ParsedDocument(full_text="", blocks=blocks))

    joined = [block.text for unit in units for block in unit.blocks]
    assert joined == [block.text for block in blocks], "block 必须整体进出，不能被劈开"
    assert units[-1].blocks[0].block_type == "title", "攒够量后遇标题应当另起一节"


# --- 三层匹配升级 -------------------------------------------------------------


def test_search_escalates_exact_then_normalised_then_terms() -> None:
    units = [
        (1, "The Transformer uses multi-head\nattention over the whole sequence."),
        (2, "Positional encoding is added to the input embeddings."),
    ]

    assert search_units(units, "Positional encoding").mode == "exact"
    # 从 PDF 里复制出来的句子在原文里被硬换行截断过，只有第二层能救回来。
    assert search_units(units, "multi-head attention").mode == "normalised"
    # 自然语言问句在前两层必然全落空。
    loose = search_units(units, "how does the model encode word order?")
    assert loose.mode == "terms"
    assert loose.is_loose


def test_chinese_query_matches_through_bigram_expansion() -> None:
    units = [(1, "本文提出了位置编码方案"), (2, "无关内容")]
    result = search_units(units, "位置编码是怎么做的")

    assert result.mode == "terms"
    assert [hit.locator for hit in result.hits] == [1]


def test_term_hits_are_ordered_by_match_count_then_document_order() -> None:
    units = [
        (1, "beta gamma"),
        (2, "alpha beta gamma"),
        (3, "alpha beta gamma"),
    ]
    # 用一个不可能逐字命中的查询，逼出第三层的排序行为。
    result = search_units(units, "alpha delta gamma")

    assert result.mode == "terms"
    assert [hit.locator for hit in result.hits] == [2, 3, 1]


def test_empty_query_returns_no_mode() -> None:
    assert search_units([(1, "text")], "   ").mode is None


# --- locator 语法 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("12", [12]),
        ("12-14", [12, 13, 14]),
        ("3,12,17", [3, 12, 17]),
        ("14-12", [12, 13, 14]),
        ("3，12", [3, 12]),
        ("2, 2, 2", [2]),
    ],
)
def test_parse_locators_accepts_the_three_documented_forms(spec: str, expected: list[int]) -> None:
    assert parse_locators(spec, 20) == expected


def test_out_of_range_locators_are_dropped_not_clamped() -> None:
    """夹逼会让模型引用到用户根本没问的那一页，比报错糟得多。"""
    assert parse_locators("3,900", 12) == [3]
    with pytest.raises(ReadingError) as error:
        parse_locators("900", 12)
    assert "1..12" in str(error.value), "错误信息要告诉模型合法区间（约束 4）"


def test_huge_range_is_bounded_before_expansion() -> None:
    assert len(parse_locators("1-100000", 100_000)) == 24


# --- 读取与截断 ---------------------------------------------------------------


def test_render_units_labels_each_locator_and_reports_truncation() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[_block(i, "字" * 400, page=i + 1) for i in range(4)],
                page_count=4,
            )
        )
    )
    rendered = render_units(material, "1-4", max_chars=900)

    assert "--- 第 1 页 ---" in rendered.text
    assert rendered.truncated
    assert rendered.locators == (1, 2)
    assert "已截断" in rendered.text


def test_render_units_always_returns_at_least_one_unit() -> None:
    """第一个 unit 就超限时也要返回它，否则模型只拿到一句"已截断"，无从缩小范围。"""
    material = _material(
        units_from_pages(
            ParsedDocument(full_text="", blocks=[_block(0, "字" * 5_000, page=1)], page_count=1)
        )
    )
    rendered = render_units(material, "1", max_chars=100)

    assert rendered.locators == (1,)
    assert not rendered.truncated


# --- 引文校验与高亮几何 -------------------------------------------------------


def test_verified_quote_carries_the_blocks_bbox() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[_block(0, "we propose the Transformer", page=1, bbox=(0.1, 0.2, 0.8, 0.3))],
                page_count=1,
            )
        )
    )
    check = verify_quote(material, 1, "propose the Transformer")

    assert check.verified
    locations = block_locations(check.blocks)
    assert locations[0]["bbox_norm"] == [0.1, 0.2, 0.8, 0.3]
    # 约束 3：只有 bbox 四个数不够，换个渲染器就会高亮错位。
    assert locations[0]["page_width"] == 595.0
    assert locations[0]["coord_origin"] == "top_left"


def test_quote_on_the_wrong_page_is_corrected_not_refused() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[_block(0, "irrelevant", page=1), _block(1, "the real sentence", page=2)],
                page_count=2,
            )
        )
    )
    check = verify_quote(material, 1, "the real sentence")

    assert check.verified
    assert check.moved
    assert check.found_locator == 2


def test_quote_spanning_two_blocks_covers_both() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[
                    _block(0, "first half", page=1, bbox=(0.1, 0.1, 0.9, 0.2)),
                    _block(1, "second half", page=1, bbox=(0.1, 0.3, 0.9, 0.4)),
                ],
                page_count=1,
            )
        )
    )
    check = verify_quote(material, 1, "first half\n\nsecond half")

    assert check.verified
    assert len(block_locations(check.blocks)) == 2


def test_invented_quote_is_refused() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(full_text="", blocks=[_block(0, "real text", page=1)], page_count=1)
        )
    )
    check = verify_quote(material, 1, "a sentence that was never written")

    assert not check.verified
    assert check.blocks == ()


# --- 大纲 ---------------------------------------------------------------------


def test_structural_outline_beats_synthesised_one() -> None:
    units = units_from_sections(
        ParsedDocument(
            full_text="",
            blocks=[
                _block(0, "介绍", block_type="title", heading_path=("介绍",)),
                _block(1, "正文" * 1_500),
                _block(2, "方法", block_type="title", heading_path=("介绍", "方法")),
            ],
        )
    )
    outline = build_outline(units)

    assert [entry.title for entry in outline] == ["介绍", "方法"]
    assert [entry.level for entry in outline] == [1, 2]
    assert not any(entry.synthesised for entry in outline)


def test_synthesised_outline_is_flagged_in_the_rendered_text() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(full_text="", blocks=[_block(0, "只是正文", page=1)], page_count=1)
        )
    )
    assert "只能当线索用" in render_outline(material)


def test_trim_outline_drops_the_deepest_levels_first() -> None:
    from app.cowork.reading.models import OutlineEntry

    entries = tuple(
        OutlineEntry(locator=i + 1, title=f"t{i}", level=1 if i % 2 == 0 else 3) for i in range(200)
    )
    kept, omitted = trim_outline(entries)

    assert omitted > 0
    assert {entry.level for entry in kept} == {1}, "砍掉深层而不是直接截断前 N 行"


# --- 工具层 -------------------------------------------------------------------


PAPER = """# Attention Is All You Need

我们提出了 Transformer，一个完全基于注意力机制的序列转换模型。

## 位置编码

由于模型不含循环与卷积，我们为输入嵌入加上了位置编码。
"""


def _context(settings: Settings | None = None) -> CoworkToolContext:
    return CoworkToolContext(
        session=object(),  # type: ignore[arg-type]
        gateway=object(),  # type: ignore[arg-type]
        settings=settings or Settings(),
        conversation_id=uuid7(),
        run_id=uuid7(),
        worker_id="test-worker",
        plan_step_id=uuid7(),
        tool_call_id="call-1",
    )


@pytest.fixture
def reading_registry(monkeypatch: pytest.MonkeyPatch) -> CoworkToolRegistry:
    registry = CoworkToolRegistry()
    register_reading_tools(registry)

    async def authorize(_session: object, **kwargs: Any) -> PathAuthorization:
        return PathAuthorization(
            conversation_id=kwargs["conversation_id"],
            root_id=uuid4(),
            root_path=Path(kwargs["target_path"]).parent,
            target_path=Path(kwargs["target_path"]),
            access_mode="read_only",
            capability=kwargs["capability"],
        )

    monkeypatch.setattr("app.cowork.tools.authorize_path", authorize)
    return registry


def test_every_reading_tool_is_gated_on_an_authorised_path() -> None:
    """没有 path_argument 的话，模型可以读任何一个未授权目录里的文件。"""
    registry = CoworkToolRegistry()
    register_reading_tools(registry)

    for name in ("material_outline", "search_material", "read_material", "reader_goto"):
        spec = registry.get(name)
        assert spec.capability == "filesystem.read"
        assert spec.path_argument == "path"
        assert spec.effect == "none"


@pytest.mark.asyncio
async def test_outline_and_search_and_read_agree_on_locators(
    reading_registry: CoworkToolRegistry,
    tmp_path: Path,
) -> None:
    paper = tmp_path / "paper.md"
    paper.write_text(PAPER, encoding="utf-8")

    outline = await reading_registry.execute(
        "material_outline", {"path": str(paper)}, context=_context()
    )
    assert outline.output["unit"] == "section"
    assert "位置编码" in outline.output["outline"]

    found = await reading_registry.execute(
        "search_material", {"path": str(paper), "query": "位置编码"}, context=_context()
    )
    locator = found.output["hits"][0]["locator"]
    assert locator <= outline.output["unit_count"]

    read = await reading_registry.execute(
        "read_material", {"path": str(paper), "locators": str(locator)}, context=_context()
    )
    assert "位置编码" in read.output["content"]
    assert read.output["locators"] == [locator]
    assert read.evidence[0]["kind"] == "reading"
    assert read.evidence[0]["locator"] == locator
    assert "位置编码" in read.evidence[0]["quote"]


@pytest.mark.asyncio
async def test_goto_with_a_real_quote_returns_highlight_geometry_is_empty_for_text(
    reading_registry: CoworkToolRegistry,
    tmp_path: Path,
) -> None:
    """Markdown 没有页面几何，所以 locations 为空——但跳转本身仍然成立。"""
    paper = tmp_path / "paper.md"
    paper.write_text(PAPER, encoding="utf-8")

    result = await reading_registry.execute(
        "reader_goto",
        {"path": str(paper), "locator": 1, "quote": "完全基于注意力机制"},
        context=_context(),
    )
    assert result.output["reader_action"] == "goto"
    assert result.output["quote"] == "完全基于注意力机制"
    assert result.output["locations"] == []
    assert result.evidence[0]["verified"] is True


@pytest.mark.asyncio
async def test_goto_still_moves_when_the_quote_cannot_be_verified(
    reading_registry: CoworkToolRegistry,
    tmp_path: Path,
) -> None:
    """跨语言问答里模型给的"引文"是它自己的译文，拒绝跳转会让阅读器看起来是坏的。"""
    paper = tmp_path / "paper.md"
    paper.write_text(PAPER, encoding="utf-8")

    result = await reading_registry.execute(
        "reader_goto",
        {"path": str(paper), "locator": 1, "quote": "a purely attention-based model"},
        context=_context(),
    )
    assert result.output["locator"] == 1
    assert result.output["quote"] == "", "对不上就不画高亮"
    assert "没有高亮" in result.output["message"]


@pytest.mark.asyncio
async def test_missing_file_becomes_an_actionable_model_facing_error(
    reading_registry: CoworkToolRegistry,
    tmp_path: Path,
) -> None:
    with pytest.raises(CoworkToolError) as error:
        await reading_registry.execute(
            "material_outline", {"path": str(tmp_path / "nope.md")}, context=_context()
        )
    assert "list_files" in str(error.value), "错误要给模型下一步动作（约束 4）"


@pytest.mark.asyncio
async def test_binary_file_is_rejected_with_a_readable_reason(
    reading_registry: CoworkToolRegistry,
    tmp_path: Path,
) -> None:
    blob = tmp_path / "weights.bin"
    blob.write_bytes(b"\x00\x01\x02binary")

    with pytest.raises(CoworkToolError) as error:
        await reading_registry.execute("material_outline", {"path": str(blob)}, context=_context())
    assert "二进制" in str(error.value)


@pytest.mark.asyncio
async def test_material_cache_reparses_after_the_file_changes(tmp_path: Path) -> None:
    """缓存键是 stat 三元组：文件改了就必须重解析，否则模型会引用已被删掉的内容。"""
    paper = tmp_path / "paper.md"
    paper.write_text("# 一\n\n原始内容\n", encoding="utf-8")
    cache = MaterialCache()
    settings = Settings()

    first = await cache.load(paper, settings=settings)
    assert "原始内容" in first.units[0].text

    paper.write_text("# 一\n\n改过之后的内容\n", encoding="utf-8")
    import os

    os.utime(paper, ns=(0, 0))
    second = await cache.load(paper, settings=settings)

    assert "改过之后的内容" in second.units[0].text
    assert second.material_id != first.material_id


@pytest.mark.asyncio
async def test_material_cache_coalesces_concurrent_parses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一文件的并发工具调用只解析一次，完成后也不遗留同步对象。"""

    import app.cowork.reading.materials as materials_module

    paper = tmp_path / "paper.md"
    paper.write_text("# 一\n\n并发内容\n", encoding="utf-8")
    cache = MaterialCache()
    settings = Settings()
    original = materials_module._build_material
    calls = 0

    async def delayed_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return await original(*args, **kwargs)

    monkeypatch.setattr(materials_module, "_build_material", delayed_build)
    first, second = await asyncio.gather(
        cache.load(paper, settings=settings),
        cache.load(paper, settings=settings),
    )

    assert first is second
    assert calls == 1
    assert cache._inflight == {}


@pytest.mark.asyncio
async def test_material_cache_drops_failed_inflight_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析失败不能按路径永久积累锁/任务；修好文件后必须能重试。"""

    import app.cowork.reading.materials as materials_module

    paper = tmp_path / "paper.md"
    paper.write_text("# 一\n\n内容\n", encoding="utf-8")
    cache = MaterialCache()
    settings = Settings()

    async def failed_build(*args, **kwargs):
        raise ReadingError("模拟解析失败")

    monkeypatch.setattr(materials_module, "_build_material", failed_build)
    with pytest.raises(ReadingError, match="模拟解析失败"):
        await cache.load(paper, settings=settings)
    await asyncio.sleep(0)

    assert cache._inflight == {}


def test_search_material_reports_loose_matches_as_loose() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[_block(0, "positional encoding is added", page=1)],
                page_count=1,
            )
        )
    )
    assert search_material(material, "how is word order handled", limit=5).is_loose


# --- locate 预检索 -------------------------------------------------------------


def test_locate_block_reports_the_hits_and_their_confidence() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[
                    _block(0, "irrelevant preamble", page=1),
                    _block(1, "we add positional encodings to the input embeddings", page=2),
                ],
                page_count=2,
            )
        )
    )
    block = render_locate_block(material, "positional encodings")

    assert "第 2 页" in block
    assert "逐字命中" in block
    # 片段是不可信数据，必须和 search_material 的返回同一个口径。
    assert "不可信数据" in block
    # 模型不能拿预检索的片段直接当原文引用。
    assert "read_material" in block


def test_locate_block_marks_loose_hits_as_loose() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(
                full_text="",
                blocks=[_block(0, "positional encoding is added", page=1)],
                page_count=1,
            )
        )
    )
    assert "宽松命中" in render_locate_block(material, "how is word order handled")


def test_locate_block_is_empty_when_nothing_matches() -> None:
    material = _material(
        units_from_pages(
            ParsedDocument(full_text="", blocks=[_block(0, "alpha", page=1)], page_count=1)
        )
    )
    assert render_locate_block(material, "完全无关的问题") == ""
    # 太短的目标没有检索价值，白搭一段提示词。
    assert render_locate_block(material, "a") == ""


def _locate_state(**overrides: Any) -> Any:
    base = {
        "work_mode": "reading",
        "reading_path": "/tmp/paper.md",
        "conversation_id": str(uuid7()),
        "run_id": str(uuid7()),
        "goal": "位置编码是怎么做的",
    }
    return {**base, **overrides}


@pytest.mark.asyncio
async def test_locate_pre_pass_refuses_an_unauthorised_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`reading_path` 来自请求体，是用户可控输入。

    不过目录授权这道闸，它就是一条把任意本机文件的片段读进提示词的路径——而工具那一侧
    每一次调用都在校验。这条用例挡的就是这个绕过。
    """
    from app.cowork import runtime
    from app.cowork_contracts import CapabilityDeniedError

    secret = tmp_path / "secret.md"
    secret.write_text("# 机密\n\n不该出现在提示词里的内容\n", encoding="utf-8")

    async def deny(_session: object, **_kwargs: Any) -> None:
        raise CapabilityDeniedError("目标路径未获得 filesystem.read 权限")

    monkeypatch.setattr(runtime, "authorize_path", deny)
    block = await runtime._render_locate_block(
        object(),  # type: ignore[arg-type]
        _locate_state(reading_path=str(secret), goal="机密"),
        settings=Settings(),
    )

    assert block == ""


@pytest.mark.asyncio
async def test_locate_pre_pass_runs_on_an_authorised_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.cowork import runtime

    paper = tmp_path / "paper.md"
    paper.write_text(PAPER, encoding="utf-8")

    async def allow(_session: object, **kwargs: Any) -> PathAuthorization:
        return PathAuthorization(
            conversation_id=kwargs["conversation_id"],
            root_id=uuid4(),
            root_path=Path(kwargs["target_path"]).parent,
            target_path=Path(kwargs["target_path"]),
            access_mode="read_only",
            capability=kwargs["capability"],
        )

    monkeypatch.setattr(runtime, "authorize_path", allow)
    block = await runtime._render_locate_block(
        object(),  # type: ignore[arg-type]
        _locate_state(reading_path=str(paper)),
        settings=Settings(),
    )

    assert "位置编码" in block
    assert "<reading_locate" in block


@pytest.mark.asyncio
async def test_locate_pre_pass_is_inert_outside_reading_mode() -> None:
    """日常办公档不该为了一段用不上的检索去解析文件。"""
    from app.cowork import runtime

    assert (
        await runtime._render_locate_block(
            object(),  # type: ignore[arg-type]
            _locate_state(work_mode="office"),
            settings=Settings(),
        )
        == ""
    )


# --- 事件与阅读器数据面 ---------------------------------------------------------


def test_reader_event_is_a_narrow_contract_not_the_whole_tool_output() -> None:
    """事件只带面板真正要用的字段。

    把工具输出整个塞进事件流，就意味着某天工具多返回一个字段、前端行为就悄悄变了，
    而且工具输出可能很大、也可能含不该进事件流的内容。
    """
    from app.cowork.runtime import _reader_event

    event = _reader_event(
        "reader_goto",
        {
            "reader_action": "goto",
            "path": "/w/paper.pdf",
            "material_id": "abc123",
            "unit": "page",
            "locator": 3,
            "quote": "positional encodings",
            "locations": [{"page_no": 3, "bbox_norm": [0.1, 0.2, 0.8, 0.3]}],
            "message": "阅读器已定位到第 3 页。",
            "internal_debug": "不该出现在事件里",
        },
    )

    assert event is not None
    name, payload = event
    assert name == "reading.goto"
    assert payload["locator"] == 3
    assert set(payload) == {"path", "material_id", "unit", "locator", "quote", "locations"}


def test_reader_event_ignores_other_tools() -> None:
    from app.cowork.runtime import _reader_event

    assert _reader_event("read_material", {"reader_action": "goto", "locator": 3}) is None
    assert _reader_event("reader_goto", {"locator": 3}) is None


@pytest.mark.asyncio
async def test_reader_rest_surface_refuses_an_unauthorised_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """面板要按路径取内容，于是这里是除工具执行边界之外第二个接受用户可控路径的地方。

    不过授权这道闸，任何知道会话 id 的人就能把本机任意文件读出来。
    """
    from fastapi import HTTPException

    from app.api import cowork as cowork_api
    from app.cowork_contracts import CapabilityDeniedError

    secret = tmp_path / "secret.md"
    secret.write_text("# 机密\n\n不该被读出来\n", encoding="utf-8")

    async def deny(_session: object, **_kwargs: Any) -> None:
        raise CapabilityDeniedError("目标路径未获得 filesystem.read 权限")

    monkeypatch.setattr(cowork_api, "authorize_path", deny)
    with pytest.raises(HTTPException) as error:
        await cowork_api._authorized_material(
            object(),  # type: ignore[arg-type]
            conversation_id=uuid7(),
            path=str(secret),
            settings=Settings(),
        )
    assert error.value.status_code == 403
