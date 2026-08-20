"""真流式产出与通用知识模式。

这里盯的是流式化之后**新出现**的失败模式:
拒答哨兵句会不会被当成正文流出去、通用知识回答会不会混进引用。
非流式时代这两件事都不可能发生, 所以老测试覆盖不到。
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.rag.answer_stream import (
    AnswerDelta,
    AnswerFinished,
    AnswerStreamEvent,
    produce_answer,
    produce_general_answer,
)
from app.rag.markdown_ingestion import ingest_markdown_file
from app.rag.retrieval.citations import REFUSAL_TEXT
from app.telemetry.llm_calls import SqlLlmCallAudit
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway

pytestmark = pytest.mark.integration

_SUFFICIENT = '{"sufficient":true,"reason":"S1 直接回答","support_ids":["S1"],"missing_aspects":[]}'
_INSUFFICIENT = (
    '{"sufficient":false,"reason":"证据没有给出数字",'
    '"support_ids":[],"missing_aspects":["具体数字"]}'
)


def _settings() -> Settings:
    # flush_chars 调小, 让"一次产出被切成多片"这件事在断言里可见。
    return get_settings().model_copy(update={"run_delta_flush_chars": 12})


async def _ingest(session: AsyncSession, gateway: ModelGateway, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "dense.md").write_text(
        "# 检索\n\n## 稠密检索\n\n稠密检索通过向量相似度召回语义相关内容。\n",
        encoding="utf-8",
    )
    await ingest_markdown_file(session, gateway, path=Path("dense.md"), library_root=library)


async def _collect(stream: AsyncIterator[AnswerStreamEvent]) -> tuple[list[str], AnswerFinished]:
    deltas: list[str] = []
    finished: AnswerFinished | None = None
    async for event in stream:
        if isinstance(event, AnswerDelta):
            deltas.append(event.text)
        else:
            finished = event
    assert finished is not None
    return deltas, finished


async def test_streamed_answer_arrives_in_pieces_and_matches_final_text(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    answer = "稠密检索使用向量相似度召回语义相关内容, 因此同义表述也能命中。[S1]"
    provider = DeterministicProvider(completion_texts=[_SUFFICIENT, answer])
    gateway = ModelGateway(
        provider, embedding_dimensions=1024, audit_sink=SqlLlmCallAudit(db_session)
    )
    await _ingest(db_session, gateway, tmp_path)

    deltas, finished = await _collect(
        produce_answer(
            db_session, gateway, query="稠密检索如何召回内容?", top_k=1, settings=_settings()
        )
    )

    # 分片产出才叫流式: 只有一片说明又退回"整答生成完再切"。
    assert len(deltas) > 1
    assert "".join(deltas) == finished.answer
    assert finished.refused is False
    assert finished.grounded is True
    assert [citation.citation_id for citation in finished.citations] == ["S1"]


async def test_model_written_refusal_never_leaks_as_body_text(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """证据过了门控, 但模型自己判定不足并写出拒答句。

    这是流式化引入的新坑: 哨兵句一旦当作正文流出去, 前端会同时显示
    "资料库中未找到相关信息。" 和拒答卡片。
    """

    provider = DeterministicProvider(completion_texts=[_SUFFICIENT, REFUSAL_TEXT])
    gateway = ModelGateway(
        provider, embedding_dimensions=1024, audit_sink=SqlLlmCallAudit(db_session)
    )
    await _ingest(db_session, gateway, tmp_path)

    deltas, finished = await _collect(
        produce_answer(
            db_session, gateway, query="稠密检索如何召回内容?", top_k=1, settings=_settings()
        )
    )

    assert deltas == []
    assert finished.refused is True
    assert finished.answer == REFUSAL_TEXT
    assert finished.citations == []
    # 门控和生成都真的跑过了, 否则这条用例会退化成"被检索阈值拦下"的假通过。
    assert provider.completion_texts == []


async def test_gate_refusal_generates_nothing_at_all(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    provider = DeterministicProvider(completion_texts=[_INSUFFICIENT])
    gateway = ModelGateway(
        provider, embedding_dimensions=1024, audit_sink=SqlLlmCallAudit(db_session)
    )
    await _ingest(db_session, gateway, tmp_path)

    deltas, finished = await _collect(
        produce_answer(
            db_session, gateway, query="稠密检索如何召回内容?", top_k=1, settings=_settings()
        )
    )

    assert deltas == []
    assert finished.refused is True
    assert finished.refusal_reason == "model_insufficient_evidence"
    # 门控拒答后不该再调一次生成: 队列里只消费了门控那一条。
    assert provider.completion_texts == []


async def test_general_answer_has_no_citations_and_is_marked_ungrounded(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    provider = DeterministicProvider(completion_text="这是通用知识回答, 不来自资料库。")
    gateway = ModelGateway(
        provider, embedding_dimensions=1024, audit_sink=SqlLlmCallAudit(db_session)
    )
    await _ingest(db_session, gateway, tmp_path)
    provider.embed_calls = 0  # 入库本身要embedding, 从这里开始计数

    deltas, finished = await _collect(
        produce_general_answer(
            db_session, gateway, query="稠密检索如何召回内容?", top_k=5, settings=_settings()
        )
    )

    assert "".join(deltas) == finished.answer
    assert finished.grounded is False
    assert finished.citations == []
    assert finished.refused is False
    # 通用知识模式不许检索: 一次 embedding 都不该发生。
    assert provider.embed_calls == 0
    assert "不来自用户的资料库" in provider.last_messages[0].content
