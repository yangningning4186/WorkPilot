import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.run_bus import InMemoryRunBus
from app.platform.demo_sessions import resolve_demo_session
from app.rag.answer_stream import AnswerDelta, AnswerFinished, split_deltas
from app.rag.memory.recall import RecalledMemoryContext
from app.rag.retrieval.citations import Citation
from app.runstore.runs import (
    append_message,
    create_run,
    ensure_conversation,
    finish_run,
    get_run,
    list_events,
    request_cancel,
)
from app.worker.answer_run import answer_run

pytestmark = pytest.mark.integration


class RecordingMemoryQueue:
    def __init__(self) -> None:
        self.memory_jobs: list[tuple[UUID, int]] = []

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        self.memory_jobs.append((job_id, attempt))


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


def _producer(
    answer: str,
    *,
    citations: list[Citation] | None = None,
    delay_s: float = 0.0,
    memory_contexts: list[str] | None = None,
    conversation_contexts: list[str] | None = None,
    retrieval_queries: list[str | None] | None = None,
):
    async def produce(
        session,
        gateway,
        *,
        query,
        top_k,
        settings,
        memory_context="",
        conversation_context="",
        retrieval_query=None,
    ) -> AsyncIterator[Any]:
        del session, gateway, query, top_k
        if memory_contexts is not None:
            memory_contexts.append(memory_context)
        if conversation_contexts is not None:
            conversation_contexts.append(conversation_context)
        if retrieval_queries is not None:
            retrieval_queries.append(retrieval_query)
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


async def test_owner_answer_schedules_memory_after_success(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="我偏好简洁回答",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        status="completed",
        run_id=run.id,
    )
    await db_session.commit()
    queue = RecordingMemoryQueue()
    ctx = await _make_ctx(
        db_engine,
        _producer("答案。[S1]"),
        memory_extraction_enabled=True,
    )
    ctx["run_queue"] = queue

    await answer_run(ctx, str(run.id), 3)

    row = (
        await db_session.execute(
            text(
                "SELECT id, status, attempts FROM memory_extraction_jobs WHERE run_id = :run_id"
            ),
            {"run_id": run.id},
        )
    ).one()
    assert row.status == "queued"
    assert queue.memory_jobs == [(row.id, 0)]


async def test_recall_context_is_injected_for_owner_but_never_demo(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer_run_module = importlib.import_module("app.worker.answer_run")
    recall_calls = 0

    async def fake_recall(*args: Any, **kwargs: Any) -> RecalledMemoryContext:
        nonlocal recall_calls
        del args, kwargs
        recall_calls += 1
        return RecalledMemoryContext(memories=[], text="<user_context>owner</user_context>")

    monkeypatch.setattr(answer_run_module, "recall_memory_context", fake_recall)
    owner_run_id = await _seed_run(db_session)
    owner_contexts: list[str] = []
    owner_ctx = await _make_ctx(
        db_engine,
        _producer("owner answer", citations=[], memory_contexts=owner_contexts),
        memory_recall_enabled=True,
        memory_extraction_enabled=False,
    )
    await answer_run(owner_ctx, str(owner_run_id), 3)

    resolved = await resolve_demo_session(db_session, cookie_token=None, ttl_s=300)
    demo_conversation_id = await ensure_conversation(
        db_session,
        scope="demo",
        demo_session_id=resolved.session.id,
    )
    demo_run = await create_run(
        db_session,
        conversation_id=demo_conversation_id,
        goal="匿名问题",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )
    await db_session.commit()
    demo_contexts: list[str] = []
    demo_ctx = await _make_ctx(
        db_engine,
        _producer("demo answer", citations=[], memory_contexts=demo_contexts),
        memory_recall_enabled=True,
        memory_extraction_enabled=False,
    )
    await answer_run(demo_ctx, str(demo_run.id), 3)

    assert recall_calls == 1
    assert owner_contexts == ["<user_context>owner</user_context>"]
    assert demo_contexts == [""]


async def test_recall_failure_degrades_to_answer_without_memory(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer_run_module = importlib.import_module("app.worker.answer_run")

    async def broken_recall(*args: Any, **kwargs: Any) -> RecalledMemoryContext:
        del args, kwargs
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(answer_run_module, "recall_memory_context", broken_recall)
    run_id = await _seed_run(db_session)
    contexts: list[str] = []
    ctx = await _make_ctx(
        db_engine,
        _producer("仍然回答", citations=[], memory_contexts=contexts),
        memory_recall_enabled=True,
        memory_extraction_enabled=False,
    )

    await answer_run(ctx, str(run_id), 3)

    run = await get_run(db_session, run_id)
    assert run is not None and run.status == "done"
    assert contexts == [""]


async def test_previous_turn_is_passed_as_conversation_context_and_rewritten_for_retrieval(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer_run_module = importlib.import_module("app.worker.answer_run")
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    previous = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="RAG 有哪些优势？",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=previous.goal,
        run_id=previous.id,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="assistant",
        content="第一是可溯源，第二是知识可更新。",
        run_id=previous.id,
    )
    assert await finish_run(db_session, run_id=previous.id, status="done")
    current = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="第二点展开说说",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=current.goal,
        run_id=current.id,
    )
    await db_session.commit()

    async def fake_rewrite(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "RAG 的知识可更新优势是什么？"

    compactions: list[dict[str, Any]] = []

    async def fake_compact(*args: Any, **kwargs: Any) -> bool:
        del args
        compactions.append(kwargs)
        return False

    monkeypatch.setattr(answer_run_module, "compact_conversation_context", fake_compact)
    monkeypatch.setattr(answer_run_module, "resolve_contextual_query", fake_rewrite)
    contexts: list[str] = []
    queries: list[str | None] = []
    ctx = await _make_ctx(
        db_engine,
        _producer(
            "展开回答",
            citations=[],
            conversation_contexts=contexts,
            retrieval_queries=queries,
        ),
        memory_recall_enabled=False,
        memory_extraction_enabled=False,
    )

    await answer_run(ctx, str(current.id), 3)

    assert "RAG 有哪些优势" in contexts[0]
    assert "第二点展开说说" not in contexts[0]
    assert queries == ["RAG 的知识可更新优势是什么？"]
    assert compactions[0]["conversation_id"] == conversation_id
    assert compactions[0]["current_run_id"] == current.id


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

    async def broken(
        session,
        gateway,
        *,
        query,
        top_k,
        settings,
        memory_context="",
        conversation_context="",
        retrieval_query=None,
    ) -> AsyncIterator[Any]:
        del (
            session,
            gateway,
            query,
            top_k,
            settings,
            memory_context,
            conversation_context,
            retrieval_query,
        )
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
    text_value = "第一句。第二句！第三句？" + "尾" * 50
    pieces = list(split_deltas(text_value, max_chars=10))
    assert "".join(pieces) == text_value
    assert all(piece for piece in pieces)
    assert all(len(piece) <= 10 for piece in pieces)


def test_settings_keep_heartbeat_shorter_than_lease() -> None:
    """心跳必须明显短于租约, 否则正常执行的 run 会被 watchdog 误判失联。"""

    settings = Settings()
    assert settings.run_heartbeat_s < settings.run_lease_s
    assert settings.conversation_summary_trigger_ratio == 0.9
    assert int(
        settings.tier_main_context_window_tokens
        * settings.conversation_summary_trigger_ratio
    ) == 92_160
    assert settings.conversation_summary_keep_recent_turns <= settings.conversation_context_max_turns
