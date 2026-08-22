from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.db import session_factory
from app.knowledge_contracts import RagSearchRequest
from eval.cowork_runner import (
    FixtureRagService,
    _metric_slice,
    evaluate_assertion,
    materialize_case,
    rescore_report,
    run_case,
    score_observation,
)
from eval.cowork_task_suite import DEFAULT_SUITE, load_suite
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)


class ScriptedCoworkProvider:
    name = "cowork_eval_test"
    chat_model = "scripted-cowork"
    embedding_model = "unused"

    def __init__(self, completions: list[CompletionResult]) -> None:
        self.completions = completions

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del messages, tools, parallel_tool_calls, max_tokens, temperature
        return self.completions.pop(0)

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del messages, max_tokens, temperature
        raise AssertionError("此测试不应触发 Office 子模型")

    async def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        del messages, max_tokens, temperature
        if False:
            yield ""

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        raise AssertionError(f"此测试不应 embedding: {texts}")

    async def aclose(self) -> None:
        return None


def _tool(call_id: str, name: str, arguments: dict[str, object]) -> CompletionResult:
    return CompletionResult(
        text="",
        model="scripted-cowork",
        provider="cowork_eval_test",
        usage=Usage(input_tokens=10, output_tokens=5),
        tool_calls=(
            ToolCall(id=call_id, name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
        ),
    )


def _final(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        model="scripted-cowork",
        provider="cowork_eval_test",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def test_materialize_and_score_workspace_case(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = suite["items"][0]
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    assert materialized.workspace is not None
    assert (materialized.workspace / "notes/project.md").is_file()
    trace = [
        {"name": "list_workspace_roots", "status": "ok", "arguments": {}, "result": {}},
        {"name": "search_files", "status": "ok", "arguments": {}, "result": {}},
        {"name": "read_text_file", "status": "ok", "arguments": {}, "result": {}},
    ]
    observation = {
        "status": "done",
        "response": "负责人林琪，计划发布日期 2026-09-15，来源 notes/project.md。",
        "interrupt": None,
        "tool_trace": trace,
        "artifacts": [],
        "after_files": materialized.before_files,
    }

    score = score_observation(item, observation, materialized=materialized)

    assert score["task_success"] is True
    assert score["tool_selection"]["passed"] is True
    assert score["step_efficiency"] == 1.0


async def test_fixture_rag_returns_only_evidence_contract() -> None:
    suite = load_suite(DEFAULT_SUITE)
    documents = suite["fixtures"]["knowledge-architecture"]["knowledge_documents"]
    service = FixtureRagService(documents)

    bundle = await service.search(
        None,  # type: ignore[arg-type] - fixture service 不读取 gateway
        RagSearchRequest(query="SearchPipeline 同步问答 流式问答 PostgresRagService", top_k=3),
    )

    assert bundle.backend == "cowork_eval_fixture"
    assert bundle.evidence
    encoded = json.dumps([value.__dict__ for value in bundle.evidence], default=str)
    assert "chunk_id" not in encoded
    assert "score" not in encoded


def test_metric_slice_uses_nearest_rank_p95() -> None:
    records = []
    for index in range(1, 21):
        records.append(
            {
                "score": {
                    "task_success": index <= 15,
                    "tool_selection": {"passed": index <= 10},
                    "step_efficiency": float(index),
                    "within_tool_budget": index <= 18,
                },
                "observation": {
                    "latency_ms": index * 100,
                    "used_tokens": index * 1_000,
                    "tool_trace": [],
                },
            }
        )

    metrics = _metric_slice(records)

    assert metrics["task_success_rate"] == 0.75
    assert metrics["tool_selection_accuracy"] == 0.5
    assert metrics["latency_ms"]["p95"] == 1_900
    assert metrics["tokens"]["p95"] == 19_000


def test_scoring_accepts_annotated_cross_tool_recovery(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == "cowork-core-025")
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    trace = [
        {
            "name": "fetch_url",
            "status": "failed",
            "arguments": {"url": "https://fixture.example/blocked"},
            "result": None,
            "error": "http_403",
        },
        {
            "name": "browser_open",
            "status": "ok",
            "arguments": {"url": "https://fixture.example/blocked"},
            "result": {"session_id": "fixture-browser-0001"},
        },
        {
            "name": "browser_snapshot",
            "status": "ok",
            "arguments": {"session_id": "fixture-browser-0001"},
            "result": {"text": "2026-08-30 02:00–03:00 UTC"},
        },
    ]
    observation = {
        "status": "done",
        "response": "维护窗口是 2026-08-30 02:00–03:00 UTC。",
        "interrupt": None,
        "tool_trace": trace,
        "artifacts": [],
        "after_files": materialized.before_files,
    }

    score = score_observation(item, observation, materialized=materialized)

    assert score["task_success"] is True


def test_evidence_contract_aggregates_multiple_search_calls(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == "cowork-core-034")
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    assertion = next(
        value for value in item["gold"]["assertions"] if value["type"] == "evidence_contract"
    )
    trace = [
        {
            "name": "search_knowledge",
            "status": "ok",
            "arguments": {"query": fixture_id},
            "result": {
                "evidence": [
                    {
                        "citation_id": f"S{index}",
                        "document_id": materialized.document_ids[fixture_id],
                        "quote": fixture_id,
                    }
                ]
            },
        }
        for index, fixture_id in enumerate(("K001", "K002", "K005"), start=1)
    ]

    result = evaluate_assertion(
        assertion,
        response="",
        status="done",
        interrupt=None,
        trace=trace,
        artifacts=[],
        materialized=materialized,
        after_files=materialized.before_files,
    )

    assert result.passed is True


def test_rescore_report_reuses_observations_without_model(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = suite["items"][0]
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    observation = {
        "status": "done",
        "response": "负责人林琪，计划发布日期 2026-09-15，来源 notes/project.md。",
        "interrupt": None,
        "tool_trace": [
            {"name": "list_workspace_roots", "status": "ok", "arguments": {}, "result": {}},
            {"name": "search_files", "status": "ok", "arguments": {}, "result": {}},
            {"name": "read_text_file", "status": "ok", "arguments": {}, "result": {}},
        ],
        "artifacts": [],
        "latency_ms": 123,
        "used_tokens": 456,
        "workspace": str(materialized.workspace),
        "before_files": materialized.before_files,
        "after_files": materialized.before_files,
    }
    source = tmp_path / "source-report.json"
    source.write_text(
        json.dumps(
            {
                "suite": suite["name"],
                "manifest": {"model": {"provider": "fixture", "model": "fixture"}},
                "items": [{"item_id": item["id"], "observation": observation}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _, report_path, _ = rescore_report(
        source_report=source,
        suite_path=DEFAULT_SUITE,
        package=tmp_path / "rescored",
        label="unit-rescore",
        test_access_note=None,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["manifest"]["mode"] == "offline_rescore_no_model_calls"
    assert report["metrics"]["task_success_rate"] == 1.0
    assert report["metrics"]["tokens"]["total"] == 456


@pytest.mark.integration
async def test_run_case_executes_real_cowork_graph(db_engine: AsyncEngine, tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = suite["items"][0]
    provider = ScriptedCoworkProvider(
        [
            _tool("roots", "list_workspace_roots", {}),
            _tool("search", "search_files", {"path": ".", "query": "Atlas"}),
            _tool("read", "read_text_file", {"path": "notes/project.md"}),
            _final("计划发布日期为 2026-09-15，负责人是林琪，来源 notes/project.md。"),
        ]
    )
    gateway = ModelGateway(provider, embedding_dimensions=16)
    settings = get_settings().model_copy(
        update={
            "cowork_store_backend": "postgres",
            "memory_extraction_enabled": False,
            "skill_distillation_enabled": False,
            "run_heartbeat_s": 60.0,
        }
    )

    record = await run_case(
        suite,
        item,
        case_root=tmp_path / "run-case",
        gateway=gateway,
        settings=settings,
        db_sessions=session_factory,
    )

    assert record["observation"]["status"] == "done"
    assert record["score"]["task_success"] is True
    assert record["score"]["tool_selection"]["actual_sequence"] == [
        "list_workspace_roots",
        "search_files",
        "read_text_file",
    ]
