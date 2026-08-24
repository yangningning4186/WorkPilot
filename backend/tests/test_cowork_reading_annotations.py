"""持久化批注：写入、幂等、上限、以及和 reader_goto 刻意相反的校验强度。

`reader_goto` 在引文对不上时降级成"只翻页不高亮"——跳转是一次性的，落错页只是让
用户多滚一下。批注不是：它会留在磁盘上、下次打开还在、还带着一块画在具体坐标上的
颜色。**所以它必须直接拒绝。** 这一组用例挡的就是这条不对称性被谁顺手"统一"掉。
"""

from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from uuid6 import uuid7

from app.agent_core.budget import CompletionClient
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.permissions import create_session_root
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    build_default_cowork_registry,
)
from app.cowork_store.routing import cowork_store
from app.runstore.checkpoints import ensure_plan
from app.runstore.runs import create_run, ensure_conversation

pytestmark = pytest.mark.integration

PAPER = """# 注意力机制综述

本文提出一个完全基于注意力机制的序列转换模型，去掉了循环与卷积。

## 位置编码

由于模型不含循环结构，我们注入位置编码来提供序列顺序信息。
"""


async def _plan_step(session: AsyncSession, run_id: UUID) -> UUID:
    step_id = uuid7()
    await ensure_plan(
        session,
        run_id=run_id,
        steps=[
            {
                "id": str(step_id),
                "idx": 0,
                "description": "annotate",
                "tool": "reader_annotate",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    return step_id


async def _context(
    session: AsyncSession,
    tmp_path: Path,
    *,
    tool_call_id: str = "annotate-call",
    settings: Settings | None = None,
) -> CoworkToolContext:
    conversation_id = await ensure_conversation(session, title="Reading annotations")
    await create_session_root(
        session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_only",
    )
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="读论文",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    step_id = await _plan_step(session, run.id)
    await session.commit()
    return CoworkToolContext(
        session=session,
        gateway=cast("CompletionClient", object()),
        settings=settings or Settings(app_env="test"),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="annotation-worker",
        plan_step_id=step_id,
        tool_call_id=tool_call_id,
    )


def _paper(tmp_path: Path) -> Path:
    path = tmp_path / "paper.md"
    path.write_text(PAPER, encoding="utf-8")
    return path


def test_annotate_is_a_write_that_takes_an_idempotency_lease() -> None:
    """三条注册期不变量，每一条都对应一个具体后果。"""

    registry = CoworkToolRegistry()
    from app.cowork.reading_tools import register_reading_tools

    register_reading_tools(registry)
    spec = registry.get("reader_annotate")

    # 每次调用都重跑目录授权：会话中途撤销授权后不能继续标注。
    assert spec.capability == "filesystem.read"
    assert spec.path_argument == "path"
    # effect != none → 走 tool_invocations 租约（约束 9），恢复重跑不会标两遍。
    assert spec.effect == "store"
    # risk=write → 计划模式拒绝执行；判据是副作用落在哪里，不是一张工具名单。
    assert spec.risk == "write"
    assert registry.plan_mode_allows("reader_annotate") is False
    assert registry.plan_mode_allows("reader_goto") is True


async def test_annotate_refuses_a_quote_that_is_not_verbatim(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """同一句译文：reader_goto 照样翻页，reader_annotate 必须拒绝。"""

    paper = _paper(tmp_path)
    registry = build_default_cowork_registry()
    context = await _context(db_session, tmp_path)

    moved = await registry.execute(
        "reader_goto",
        {"path": str(paper), "locator": 1, "quote": "a purely attention-based model"},
        context=context,
    )
    assert moved.output["locator"] == 1, "跳转仍然发生"
    assert moved.output["quote"] == "", "只是不高亮"

    with pytest.raises(CoworkToolError) as error:
        await registry.execute(
            "reader_annotate",
            {
                "path": str(paper),
                "locator": 1,
                "quote": "a purely attention-based model",
                "note": "核心主张",
            },
            context=context,
        )
    message = str(error.value)
    assert "并非逐字出现" in message
    assert "read_material" in message, "错误要给模型下一步动作（约束 4）"
    assert await cowork_store().list_reading_annotations(material_id="") == []


async def test_annotate_persists_and_survives_replay(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    paper = _paper(tmp_path)
    registry = build_default_cowork_registry()
    context = await _context(db_session, tmp_path)
    arguments = {
        "path": str(paper),
        "locator": 1,
        "quote": "完全基于注意力机制",
        "note": "这是全文的核心主张",
        "color": "green",
    }

    first = await registry.execute("reader_annotate", arguments, context=context)
    # 从 interrupt 恢复会从节点开头重跑；副作用不能重放（约束 9）。
    replay = await registry.execute("reader_annotate", arguments, context=context)

    assert first.reused is False
    assert replay.reused is True
    assert replay.output["annotation_id"] == first.output["annotation_id"]
    assert first.output["reader_action"] == "annotate"
    assert first.output["color"] == "green"

    material_id = first.output["material_id"]
    stored = await cowork_store().list_reading_annotations(material_id=material_id)
    assert len(stored) == 1, "重放没有变成两条批注"
    assert stored[0].quote == "完全基于注意力机制"
    assert stored[0].note == "这是全文的核心主张"
    assert stored[0].conversation_id == context.conversation_id


async def test_annotation_cap_is_enforced_inside_the_write(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """一页几十个高亮等于没有高亮，而模型在长文里逐句标注很容易发生。"""

    paper = _paper(tmp_path)
    registry = build_default_cowork_registry()
    settings = Settings(app_env="test", cowork_reading_max_annotations=1)
    context = await _context(db_session, tmp_path, settings=settings)

    await registry.execute(
        "reader_annotate",
        {
            "path": str(paper),
            "locator": 1,
            "quote": "完全基于注意力机制",
            "note": "第一条",
        },
        context=context,
    )
    with pytest.raises(CoworkToolError, match="上限"):
        await registry.execute(
            "reader_annotate",
            {
                "path": str(paper),
                "locator": 1,
                "quote": "去掉了循环与卷积",
                "note": "第二条",
            },
            # 换一个 call id，否则会命中上一条的幂等结果而不是真的再写一次。
            context=await _context(db_session, tmp_path, tool_call_id="second", settings=settings),
        )


async def test_annotations_from_an_older_version_are_counted_not_shown(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """改了一版 PDF 之后批注全没了却查不到为什么，是最糟的那种失败。"""

    store = cowork_store()
    path = str(tmp_path / "paper.md")
    await store.create_reading_annotation(
        material_id="hash-of-v1",
        path=path,
        locator=1,
        quote="旧版里的一句话",
        note="旧批注",
        color="yellow",
        locations=[],
        conversation_id=None,
        run_id=None,
        max_per_material=10,
    )

    # 新版本：同一路径，不同内容哈希。
    assert await store.list_reading_annotations(material_id="hash-of-v2") == []
    assert await store.count_stale_reading_annotations(path=path, material_id="hash-of-v2") == 1
    # 旧版本自己仍然完整可见，没有被删掉。
    assert len(await store.list_reading_annotations(material_id="hash-of-v1")) == 1


async def test_deleting_an_annotation_is_idempotent_and_reports_misses() -> None:
    store = cowork_store()
    record = await store.create_reading_annotation(
        material_id="hash",
        path="/tmp/x.md",
        locator=1,
        quote="q",
        note="n",
        color="blue",
        locations=[],
        conversation_id=None,
        run_id=None,
        max_per_material=10,
    )

    assert await store.delete_reading_annotation(annotation_id=record.id) is True
    assert await store.delete_reading_annotation(annotation_id=record.id) is False
    assert await store.delete_reading_annotation(annotation_id=uuid4()) is False
    assert await store.list_reading_annotations(material_id="hash") == []
