from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import UUID

import pytest

from app.cowork.tools import CoworkToolResult
from app.knowledge_contracts import EvidenceBundle, EvidenceSegment, RagSearchRequest
from eval.full_chain_cassette import (
    FullChainCassetteError,
    FullChainRecorder,
    FullChainReplayer,
    verify,
)

FULL_CHAIN_FIXTURE = Path(__file__).resolve().parents[2] / "eval/replays/full-chain-v1.json"


class _Gateway:
    chat_provider = "fixture"
    chat_model = "fixture-chat"
    embedding_provider = "fixture"
    embedding_model = "fixture-embedding"
    embedding_dimensions = 3


class _Rag:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, gateway: _Gateway, request: RagSearchRequest) -> EvidenceBundle:
        del gateway, request
        self.calls += 1
        return EvidenceBundle(
            evidence=(
                EvidenceSegment(
                    citation_id="S1",
                    block_id=UUID("00000000-0000-0000-0000-000000000001"),
                    version_id=UUID("00000000-0000-0000-0000-000000000002"),
                    document_id=UUID("00000000-0000-0000-0000-000000000003"),
                    title="合成 RAG 文档",
                    source_uri="synthetic://rag/1",
                    quote="属性过滤应使用部分索引或迭代扫描。",
                    char_start=0,
                    char_end=19,
                    heading_path=["检索器"],
                    locations=[],
                ),
            ),
            retrieved_chunks=1,
            backend="fixture-rag",
        )


@pytest.mark.asyncio
async def test_full_chain_records_then_replays_with_zero_real_io(tmp_path: Path) -> None:
    path = tmp_path / "full-chain.json"
    gateway = _Gateway()
    rag = _Rag()
    tool_calls = 0
    effect_calls = 0

    async def tool_delegate(name: str, arguments: dict[str, object]) -> CoworkToolResult:
        nonlocal tool_calls
        tool_calls += 1
        return CoworkToolResult(output={"tool": name, "content": arguments["path"]})

    async def effect_delegate(
        connector: str,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> object:
        nonlocal effect_calls
        effect_calls += 1
        return {
            "connector": connector,
            "operation": operation,
            "title": payload["title"],
            "receipt": "synthetic-receipt",
            "idempotency_key": idempotency_key,
        }

    request = RagSearchRequest(query="属性过滤怎么做？", top_k=3, kb_slug="fixture-kb")
    recorder = FullChainRecorder(
        output=path,
        data_classification="synthetic",
        metadata={"case_id": "full-chain-synthetic-v1"},
    )
    recorded_rag = await recorder.rag_search(gateway, request, delegate=rag)  # type: ignore[arg-type]
    recorded_tool = await recorder.tool_call(
        "read_file",
        {"path": "papers/rag-survey.md"},
        risk="read",
        effect="none",
        delegate=tool_delegate,
    )
    recorded_effect = await recorder.external_effect(
        "calendar",
        "create_event",
        {"title": "合成评测会议"},
        idempotency_key="synthetic-effect-1",
        delegate=effect_delegate,
    )
    recorder.finalize()

    assert (rag.calls, tool_calls, effect_calls, recorder.real_io_calls) == (1, 1, 1, 3)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"replay touched a real delegate: {args}, {kwargs}")

    replay = FullChainReplayer.load(path)
    replayed_rag = await replay.rag_search(
        gateway,
        request,
        delegate=forbidden,  # type: ignore[arg-type]
    )
    replayed_tool = await replay.tool_call(
        "read_file",
        {"path": "papers/rag-survey.md"},
        risk="read",
        effect="none",
        delegate=forbidden,  # type: ignore[arg-type]
    )
    replayed_effect = await replay.external_effect(
        "calendar",
        "create_event",
        {"title": "合成评测会议"},
        idempotency_key="synthetic-effect-1",
        delegate=forbidden,  # type: ignore[arg-type]
    )
    replay.assert_complete()

    assert replay.real_io_calls == 0
    assert replayed_rag == recorded_rag
    assert replayed_tool == recorded_tool
    assert replayed_effect == recorded_effect
    assert verify(path)["channels"] == {"external_effect": 1, "rag": 1, "tool": 1}


@pytest.mark.asyncio
async def test_replay_is_ordered_and_request_strict(tmp_path: Path) -> None:
    path = tmp_path / "strict.json"
    recorder = FullChainRecorder(output=path, data_classification="synthetic")
    rag = _Rag()
    gateway = _Gateway()
    await recorder.rag_search(
        gateway,
        RagSearchRequest(query="original"),
        delegate=rag,  # type: ignore[arg-type]
    )

    async def tool(name: str, arguments: dict[str, object]) -> CoworkToolResult:
        return CoworkToolResult(output={"name": name, **arguments})

    async def effect(*args: object) -> object:
        return {"ok": True, "args": len(args)}

    await recorder.tool_call(
        "read_file", {"path": "a.md"}, risk="read", effect="none", delegate=tool
    )
    await recorder.external_effect(
        "mail",
        "send",
        {"subject": "fixture"},
        idempotency_key="fixture-1",
        delegate=effect,  # type: ignore[arg-type]
    )
    recorder.finalize()

    replay = FullChainReplayer.load(path)
    with pytest.raises(FullChainCassetteError, match="request mismatch"):
        await replay.rag_search(gateway, RagSearchRequest(query="changed"))


def test_tampering_and_partial_chain_fail_closed(tmp_path: Path) -> None:
    partial = FullChainRecorder(output=tmp_path / "partial.json", data_classification="synthetic")
    with pytest.raises(FullChainCassetteError, match="missing channels"):
        partial.finalize()

    payload = json.loads(FULL_CHAIN_FIXTURE.read_text())
    payload["interactions"][0]["request"]["request"]["query"] = "tampered"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload))
    with pytest.raises(FullChainCassetteError, match="integrity mismatch"):
        FullChainReplayer.load(tampered)


@pytest.mark.asyncio
async def test_committed_full_chain_fixture_replays_without_delegates() -> None:
    replay = FullChainReplayer.load(FULL_CHAIN_FIXTURE)

    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"committed replay touched live I/O: {args}, {kwargs}")

    rag = await replay.rag_search(
        _Gateway(),
        RagSearchRequest(query="属性过滤怎么做？", top_k=3, kb_slug="fixture-kb"),
        delegate=forbidden,  # type: ignore[arg-type]
    )
    tool = await replay.tool_call(
        "read_file",
        {"path": "papers/rag-survey.md"},
        risk="read",
        effect="none",
        delegate=forbidden,  # type: ignore[arg-type]
    )
    effect = await replay.external_effect(
        "calendar",
        "create_event",
        {"title": "合成评测会议"},
        idempotency_key="synthetic-effect-1",
        delegate=forbidden,  # type: ignore[arg-type]
    )
    replay.assert_complete()

    assert rag.evidence[0].citation_id == "S1"
    assert tool.output["content"] == "属性过滤段落"
    assert effect == {
        "receipt": "synthetic-receipt",
        "status": "created",
        "title": "合成评测会议",
    }
    assert replay.real_io_calls == 0
