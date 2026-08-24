"""会话挂载 KB → 预检索 → 工具再检索这条链路的用例。

这条链上最容易悄悄坏掉的三件事：

1. **没挂载也预检索**。本地 KB 在只有一个库时会好心地"就用那一个"，那是给模型主动调用
   `search_knowledge` 准备的默认；放到预检索上就变成任何一个普通办公会话都会被塞进一段
   检索结果。用户没挂就是没挂。
2. **搜错库**。工具的 slug 必须来自会话挂载，不能来自模型参数——模型不知道用户挂的是
   哪个，让它填等于让它带着一份看起来很正经的出处答题。
3. **检索不上就让 run 起不来**。没建索引、embedding 换了、库被删了，都该退化成"没有
   预检索"，把可执行的错误留给模型第一次调工具时看见。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.cowork.knowledge_prepass import (
    CITATION_PREFIX,
    knowledge_prepass_evidence,
    render_knowledge_block,
)
from app.cowork.rag_tools import SearchKnowledgeArgs, register_rag_tools
from app.cowork.runtime import _render_knowledge_block
from app.cowork.tools import CoworkToolContext, CoworkToolRegistry
from app.knowledge_contracts import (
    EvidenceBundle,
    EvidenceSegment,
    KnowledgeUnavailableError,
    RagSearchRequest,
)

_DOC = UUID("00000000-0000-5000-8000-000000000001")


def _segment(citation_id: str, *, quote: str, page_no: int | None = 12) -> EvidenceSegment:
    return EvidenceSegment(
        citation_id=citation_id,
        block_id=_DOC,
        version_id=_DOC,
        document_id=_DOC,
        title="RRF 融合综述",
        source_uri="rrf.pdf",
        quote=quote,
        char_start=0,
        char_end=0,
        heading_path=[],
        locations=[] if page_no is None else [{"page_no": page_no}],
    )


def _bundle(*segments: EvidenceSegment) -> EvidenceBundle:
    return EvidenceBundle(
        evidence=segments, retrieved_chunks=len(segments), backend="local_faiss_bm25"
    )


class RecordingRag:
    """记录收到的请求；可配置成抛错。"""

    def __init__(
        self, bundle: EvidenceBundle | None = None, *, error: Exception | None = None
    ) -> None:
        self.requests: list[RagSearchRequest] = []
        self._bundle = bundle if bundle is not None else _bundle()
        self._error = error

    async def search(self, gateway: object, request: RagSearchRequest) -> EvidenceBundle:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._bundle


def _state(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": str(uuid4()),
        "goal": "RRF 是怎么融合两路排序的",
        "kb_slug": "papers",
    }
    base.update(overrides)
    return base


# -- 渲染 -----------------------------------------------------------------


def test_block_carries_page_numbers_and_its_own_citation_prefix() -> None:
    block = render_knowledge_block(
        _bundle(_segment("S1", quote="RRF 在排名层面融合"), _segment("S2", quote="不需要归一化")),
        kb_name="papers",
    )

    assert f"[{CITATION_PREFIX}1]" in block
    assert f"[{CITATION_PREFIX}2]" in block
    assert "p.12" in block
    # 两套编号必须在同一段里说清楚，否则模型会把 [K1] 和工具返回的 [S1] 混着用。
    assert "S1" in block and "不要混用" in block
    assert "不可信数据" in block


def test_empty_bundle_renders_nothing() -> None:
    assert render_knowledge_block(_bundle(), kb_name="papers") == ""


def test_prepass_text_and_structured_evidence_share_ids_and_quotes() -> None:
    bundle = _bundle(_segment("S1", quote="RRF 在排名层面融合"))

    block = render_knowledge_block(bundle, kb_name="papers")
    evidence = knowledge_prepass_evidence(bundle)

    assert f"[{evidence[0]['citation_id']}]" in block
    assert evidence[0]["quote"] in block
    assert evidence[0]["kind"] == "knowledge"


def test_block_stays_within_its_character_budget() -> None:
    """预检索进的是 system 稳定前缀，整段 run 都在为它付费。

    命中恰好都很长时必须截断而不是照单全收——一次检索塞进两万字，之后每一轮决策都要
    重新为它计费。
    """
    block = render_knowledge_block(
        _bundle(*(_segment(f"S{i}", quote="片段" * 2_000) for i in range(1, 6))),
        kb_name="papers",
    )

    assert len(block) < 4_000


def test_segment_without_page_falls_back_to_the_title() -> None:
    block = render_knowledge_block(
        _bundle(_segment("S1", quote="一条笔记", page_no=None)), kb_name="notes"
    )

    assert "RRF 融合综述" in block
    assert "p." not in block


# -- 预检索触发条件 --------------------------------------------------------


@pytest.mark.asyncio
async def test_prepass_searches_the_mounted_kb() -> None:
    rag = RecordingRag(_bundle(_segment("S1", quote="RRF 在排名层面融合")))

    block = await _render_knowledge_block(_state(), rag=rag, gateway=None)  # type: ignore[arg-type]

    assert len(rag.requests) == 1
    assert rag.requests[0].kb_slug == "papers"
    assert rag.requests[0].query == "RRF 是怎么融合两路排序的"
    assert f"[{CITATION_PREFIX}1]" in block


@pytest.mark.asyncio
async def test_prepass_does_not_fire_without_a_mount() -> None:
    """没挂载就一次检索都不发。

    只断言返回空串是不够的：本地 KB 只有一个库时会自己兜底，"发了但结果没用上"和
    "没发"在提示词上看不出区别，在成本和延迟上区别很大。
    """
    rag = RecordingRag(_bundle(_segment("S1", quote="不该出现")))

    block = await _render_knowledge_block(_state(kb_slug=None), rag=rag, gateway=None)  # type: ignore[arg-type]

    assert block == ""
    assert rag.requests == []


@pytest.mark.asyncio
async def test_prepass_skips_a_too_short_question() -> None:
    rag = RecordingRag()

    assert await _render_knowledge_block(_state(goal="嗯"), rag=rag, gateway=None) == ""  # type: ignore[arg-type]
    assert rag.requests == []


@pytest.mark.asyncio
async def test_prepass_degrades_when_the_index_is_unusable() -> None:
    """索引坏了不该让 run 起不来。

    可执行的那条错误消息属于模型第一次调 search_knowledge 的时候——那才是能让用户看到
    "请重建索引"的地方。开局直接失败只会让用户看到一个起不来的任务。
    """
    rag = RecordingRag(error=KnowledgeUnavailableError("索引与当前 embedding 对不上，请重建"))

    assert await _render_knowledge_block(_state(), rag=rag, gateway=None) == ""  # type: ignore[arg-type]


# -- 工具 -----------------------------------------------------------------


async def _call_search(rag: object, *, kb_slug: str | None) -> dict[str, object]:
    registry = CoworkToolRegistry()
    register_rag_tools(registry, rag)  # type: ignore[arg-type]
    spec = registry.get("search_knowledge")
    assert spec.handler is not None
    context = CoworkToolContext(
        session=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        conversation_id=uuid4(),
        run_id=uuid4(),
        worker_id="test",
        plan_step_id=uuid4(),
        tool_call_id="call-1",
        kb_slug=kb_slug,
    )
    result = await spec.handler(context, SearchKnowledgeArgs(query="RRF 怎么融合"))
    return result.output


@pytest.mark.asyncio
async def test_tool_searches_the_mounted_kb_not_a_model_chosen_one() -> None:
    rag = RecordingRag(_bundle(_segment("S1", quote="RRF 在排名层面融合")))

    output = await _call_search(rag, kb_slug="papers")

    assert rag.requests[0].kb_slug == "papers"
    assert output["knowledge_base"] == "papers"
    # 模型不能自己指定库：参数模型必须拒绝多出来的字段。
    with pytest.raises(ValueError):
        SearchKnowledgeArgs(query="x", kb_slug="other")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_tool_hands_the_actionable_message_back_to_the_model() -> None:
    """约束 4：工具失败返回的是写给模型看的下一步指令，不是栈。"""
    rag = RecordingRag(error=KnowledgeUnavailableError("有多个知识库（papers, notes），必须指明"))

    with pytest.raises(ValueError, match="必须指明"):
        await _call_search(rag, kb_slug=None)


@pytest.mark.asyncio
async def test_tool_output_never_leaks_chunk_internals() -> None:
    rag = RecordingRag(_bundle(_segment("S1", quote="RRF 在排名层面融合")))

    output = await _call_search(rag, kb_slug="papers")

    evidence = output["evidence"]
    assert isinstance(evidence, list) and evidence
    for key in ("chunk_id", "score", "orm", "node_id"):
        assert key not in evidence[0]
