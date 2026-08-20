import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import pytest
from uuid6 import uuid7

from app.agent.cowork_rag_tools import register_rag_tools
from app.agent.cowork_tools import CoworkToolContext, CoworkToolRegistry
from app.agent_core.loop import run_tool_loop
from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.retrieval.citations import EvidenceSegment
from app.services import rag_service
from app.services.rag_service import EvidenceBundle, PostgresRagService, RagSearchRequest


class _FakeRag:
    async def search(self, gateway: object, request: RagSearchRequest) -> EvidenceBundle:
        del gateway
        return EvidenceBundle(
            evidence=(
                EvidenceSegment(
                    citation_id="S1",
                    block_id=uuid7(),
                    version_id=uuid7(),
                    document_id=uuid7(),
                    title="Architecture",
                    source_uri="notes/architecture.md",
                    quote=f"evidence for {request.query}",
                    char_start=10,
                    char_end=30,
                    heading_path=["RAG"],
                    locations=[{"page": 2}],
                ),
            ),
            retrieved_chunks=1,
            backend="fake",
        )


@pytest.mark.asyncio
async def test_search_knowledge_returns_only_evidence_bundle_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CoworkToolRegistry()
    register_rag_tools(registry, _FakeRag())
    assert registry.get("search_knowledge").capability == "knowledge.read"
    authorized: list[str] = []

    async def authorize(_session: object, **kwargs: Any) -> None:
        authorized.append(str(kwargs["capability"]))

    monkeypatch.setattr("app.agent.cowork_tools.authorize_capability", authorize)
    run_id = uuid7()
    result = await registry.execute(
        "search_knowledge",
        {"query": "SearchPipeline", "top_k": 3},
        context=CoworkToolContext(
            session=object(),  # type: ignore[arg-type]
            gateway=object(),  # type: ignore[arg-type]
            settings=Settings(),
            conversation_id=uuid7(),
            run_id=run_id,
            worker_id="test-worker",
            plan_step_id=uuid7(),
            tool_call_id="call-1",
        ),
    )

    encoded = json.dumps(result.output)
    assert result.output["backend"] == "fake"
    assert result.output["evidence"][0]["citation_id"] == "S1"
    assert "chunk_id" not in encoded
    assert "dense_score" not in encoded
    assert "fusion_score" not in encoded
    assert "_sa_instance_state" not in encoded
    assert authorized == ["knowledge.read"]


@pytest.mark.asyncio
async def test_postgres_rag_service_delegates_to_search_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []
    evidence = EvidenceSegment(
        citation_id="S1",
        block_id=uuid7(),
        version_id=uuid7(),
        document_id=uuid7(),
        title="doc",
        source_uri="doc.md",
        quote="grounded",
        char_start=0,
        char_end=8,
        heading_path=[],
        locations=[],
    )

    class FakePipeline:
        def __init__(self, session: object, gateway: object) -> None:
            captured.append((session, gateway))

        async def search(self, request: object) -> object:
            captured.append(request)
            return SimpleNamespace(evidence=(evidence,), hits=(object(),))

    class SessionContext:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(rag_service, "SearchPipeline", FakePipeline)
    service = PostgresRagService(
        lambda: SessionContext(),  # type: ignore[arg-type]
        settings=Settings(
            query_decomposition_enabled=True,
            rerank_enabled=True,
            rerank_candidate_k=37,
            document_cap_per_version=2,
        ),
    )
    gateway = cast("ModelGateway", object())
    bundle = await service.search(
        gateway,
        RagSearchRequest(query="one route", top_k=3, candidate_k=9),
    )

    assert captured[0][1] is gateway
    assert captured[1].query == "one route"
    assert captured[1].top_k == 3
    assert captured[1].candidate_k == 37
    assert captured[1].query_decomposition_enabled is True
    assert captured[1].rerank_enabled is True
    assert captured[1].document_cap_per_version == 2
    assert bundle.evidence == (evidence,)
    assert bundle.retrieved_chunks == 1


class _LoopState(TypedDict):
    active: bool
    pending: bool
    count: int


@pytest.mark.asyncio
async def test_agent_core_loop_is_product_neutral() -> None:
    async def decide(state: _LoopState) -> _LoopState:
        if state["count"] == 2:
            return {**state, "active": False}
        return {**state, "pending": True}

    async def execute(state: _LoopState) -> _LoopState:
        return {**state, "pending": False, "count": state["count"] + 1}

    initial: _LoopState = {"active": True, "pending": False, "count": 0}
    result = await run_tool_loop(
        initial,
        state_schema=_LoopState,
        decide=decide,
        execute_tools=execute,
        is_active=lambda state: state["active"],
        has_pending_tools=lambda state: state["pending"],
        recursion_limit=10,
    )

    assert result == {"active": False, "pending": False, "count": 2}


def test_agent_does_not_import_concrete_provider_modules() -> None:
    app_root = Path(__file__).parents[1] / "app"
    violations: list[str] = []
    for package in (app_root / "agent", app_root / "agent_core"):
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "app.llm.providers"
                ):
                    violations.append(str(path.relative_to(app_root)))
    assert violations == []


def test_store_adapter_does_not_import_agent_or_service_implementations() -> None:
    app_root = Path(__file__).parents[1] / "app"
    violations: list[tuple[str, str]] = []
    for path in (app_root / "cowork_store").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "app.agent" or module.startswith("app.agent.") or module.startswith(
                "app.services"
            ):
                violations.append((str(path.relative_to(app_root)), module))
    assert violations == []
