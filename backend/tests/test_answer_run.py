import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.run_bus import InMemoryRunBus
from app.retrieval.citations import Citation
from app.services.answer_stream import AnswerDelta, AnswerFinished, split_deltas
from app.services.runs import create_run, ensure_conversation, get_run, list_events, request_cancel
from app.worker.answer_run import answer_run

pytestmark = pytest.mark.integration


def _citation() -> Citation:
    return Citation(
        citation_id="S1",
        block_id=uuid4(),
        version_id=uuid4(),
        document_id=uuid4(),
        title="稠密检索",
        source_uri="dense.md",
        quote="向量相似度召回语义相关内容。",
        char_start=10,
        char_end=24,
        heading_path=["检索", "稠密检索"],
        locations=[
            {
                "page_no": 3,
                "bbox_norm": [0.1, 0.2, 0.5, 0.3],
                "page_width": 595,
                "page_height": 842,
                "rotation": 0,
                "coord_origin": "top-left",
            }
        ],
    )


def _finished(answer: str, *, refused: bool = False, citations: list[Citation] | None = None):
    return AnswerFinished(
        answer=answer,
        citations=citations if citations is not None else [_citation()],
        refused=refused,
        refusal_reason=None,
    )


def _producer(answer: str, *, citations: list[Citation] | None = None, delay_s: float = 0.0):
    async def produce(session, gateway, *, query, top_k, settings) -> AsyncIterator[Any]:
        del session, gateway, query, top_k
        for piece in split_deltas(answer, max_chars=settings.run_delta_flush_chars):
            if delay_s:
                await asyncio.sleep(delay_s)
            yield AnswerDelta(text=piece)
        yield _finished(answer, citations=citations)

    return produce


async def _make_ctx(db_engine: AsyncEngine, producer, **overrides: Any) -> dict[str, Any]:
    settings = get_settings().model_copy(update=overrides)
    return {
        "settings": settings,
        "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
        "bus": InMemoryRunBus(),
        "answer_producer": producer,
    }


async def _seed_run(session: AsyncSession, goal: str = "稠密检索如何召回内容?") -> UUID:
    conversation_id = await ensure_conversation(session)
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal=goal,
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )
    await session.commit()
    return run.id


async def test_answer_run_emits_ordered_events_and_completes_message(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run_id = await _seed_run(db_session)
    ctx = await _make_ctx(db_engine, _producer("这是答案。[S1]"))

    await answer_run(ctx, str(run_id), 3)

    events = await list_events(db_session, run_id=run_id)
    assert [event.type for event in events] == [
        "message.start",
        "message.delta",
        "citation",
        "message.done",
    ]
    assert [event.seq for event in events] == [1, 2, 3, 4]

    citation_payload = events[2].payload
    # 约束 3: 引用必须带完整定位元数据, 只有 bbox 四个数换渲染器就会高亮错位。
    assert citation_payload["block_id"]
    assert citation_payload["doc_id"]
    assert citation_payload["locations"][0]["page_width"] == 595
    assert citation_payload["locations"][0]["coord_origin"] == "top-left"
    assert events[3].payload["refused"] is False
    assert "cost_usd" in events[3].payload

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.status == "done"

    message = (
        (
            await db_session.execute(
                text(
                    "SELECT content, status, citations FROM messages "
                    "WHERE run_id = :run_id AND role = 'assistant'"
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one()
    )
    assert message["status"] == "completed"
    assert message["content"] == "这是答案。[S1]"
    assert message["citations"][0]["citation_id"] == "S1"


async def test_answer_run_batches_deltas_instead_of_one_event_per_token(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run_id = await _seed_run(db_session)
    answer = "甲" * 300
    ctx = await _make_ctx(db_engine, _producer(answer), run_delta_flush_chars=100)

    await answer_run(ctx, str(run_id), 3)

    deltas = [
        event
        for event in await list_events(db_session, run_id=run_id)
        if event.type == "message.delta"
    ]
    assert 1 <= len(deltas) <= 4
    assert "".join(event.payload["text"] for event in deltas) == answer


async def test_second_worker_cannot_double_run_the_same_job(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run_id = await _seed_run(db_session)
    ctx = await _make_ctx(db_engine, _producer("答案。[S1]"))

    await answer_run(ctx, str(run_id), 3)
    before = len(await list_events(db_session, run_id=run_id))
    # 队列重投递: run 已是终态, 抢占失败, 不得再写一遍事件或再计一次费。
    await answer_run(ctx, str(run_id), 3)

    assert len(await list_events(db_session, run_id=run_id)) == before


async def test_cancelled_run_stops_and_marks_message_cancelled(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        run_id = await _seed_run(session)

    ctx = await _make_ctx(
        db_engine,
        _producer("甲" * 2000, delay_s=0.05),
        run_heartbeat_s=0.05,
        run_delta_flush_chars=50,
    )

    async def cancel_soon() -> None:
        await asyncio.sleep(0.15)
        async with factory() as session:
            await request_cancel(session, run_id=run_id)
            await session.commit()

    canceller = asyncio.create_task(cancel_soon())
    await answer_run(ctx, str(run_id), 3)
    await canceller

    async with factory() as session:
        run = await get_run(session, run_id)
        assert run is not None
        assert run.status == "cancelled"

        events = await list_events(session, run_id=run_id)
        assert events[-1].type == "error"
        assert events[-1].payload["code"] == "cancelled"

        status = (
            await session.execute(
                text("SELECT status FROM messages WHERE run_id = :run_id AND role = 'assistant'"),
                {"run_id": run_id},
            )
        ).scalar_one()
        assert status == "cancelled"


async def test_producer_failure_surfaces_as_error_event_not_silent_success(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run_id = await _seed_run(db_session)

    async def broken(session, gateway, *, query, top_k, settings) -> AsyncIterator[Any]:
        del session, gateway, query, top_k, settings
        yield AnswerDelta(text="半截")
        raise RuntimeError("provider 挂了")

    ctx = await _make_ctx(db_engine, broken)
    await answer_run(ctx, str(run_id), 3)

    events = await list_events(db_session, run_id=run_id)
    assert events[-1].type == "error"
    assert events[-1].payload["retryable"] is True

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.status == "failed"

    message = (
        (
            await db_session.execute(
                text(
                    "SELECT content, status FROM messages "
                    "WHERE run_id = :run_id AND role = 'assistant'"
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one()
    )
    # 半截回答保留但显式标失败, 不能伪装成一条完整回答。
    assert message["status"] == "failed"
    assert message["content"] == "半截"


def test_split_deltas_never_yields_empty_or_oversized_pieces() -> None:
    text_value = "第一句。第二句！第三句？" + "尾" * 50  # noqa: RUF001 - 断句依赖全角标点
    pieces = list(split_deltas(text_value, max_chars=10))
    assert "".join(pieces) == text_value
    assert all(piece for piece in pieces)
    assert all(len(piece) <= 10 for piece in pieces)


def test_settings_keep_heartbeat_shorter_than_lease() -> None:
    """心跳必须明显短于租约, 否则正常执行的 run 会被 watchdog 误判失联。"""

    settings = Settings()
    assert settings.run_heartbeat_s < settings.run_lease_s
