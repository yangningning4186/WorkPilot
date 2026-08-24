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
    CoworkRunnerError,
    FixtureRagService,
    MaterializedCase,
    _evaluation_settings,
    _metric_slice,
    evaluate_assertion,
    materialize_case,
    rescore_report,
    run_case,
    run_suite,
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


def test_evaluation_settings_disable_token_fuse() -> None:
    base = get_settings().model_copy(
        update={
            "run_budget_tokens": 100_000,
            "run_budget_calls": 20,
            "run_budget_wall_ms": 180_000,
        }
    )

    settings = _evaluation_settings(
        base,
        budget_tokens=None,
        budget_calls=None,
        budget_wall_ms=None,
    )

    assert settings.run_budget_tokens == 0
    assert settings.run_budget_calls == 20
    assert settings.run_budget_wall_ms == 180_000
    with pytest.raises(CoworkRunnerError, match="禁用 token 熔断"):
        _evaluation_settings(
            base,
            budget_tokens=100_000,
            budget_calls=None,
            budget_wall_ms=None,
        )


def test_no_files_changed_ignores_git_internal_metadata(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == "cowork-core-041")
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    assert not any(Path(relative).parts[0] == ".git" for relative in materialized.before_files)

    legacy = MaterializedCase(
        workspace=materialized.workspace,
        fixtures=materialized.fixtures,
        before_files={**materialized.before_files, ".git/index": "before"},
        document_ids=materialized.document_ids,
    )
    result = evaluate_assertion(
        {"type": "no_files_changed"},
        response="只读取版本视图",
        status="done",
        interrupt=None,
        trace=[],
        artifacts=[],
        materialized=legacy,
        after_files={**materialized.before_files, ".git/index": "after"},
    )

    assert result.passed is True
    assert "changed=[]" in result.detail


def test_unanswerable_reading_requires_refusal_before_numeric_claim(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == "cowork-core-047")
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")
    assertion = next(
        value
        for value in item["gold"]["assertions"]
        if value["type"] == "response_refusal_before_claim"
    )

    correct_refusal = evaluate_assertion(
        assertion,
        response=(
            "文中没有任何多语言检索实验，因此无法给出多少个点的提升。"
            "文中另有十一个点，但那是领域子集差距。"
        ),
        status="done",
        interrupt=None,
        trace=[],
        artifacts=[],
        materialized=materialized,
        after_files=materialized.before_files,
    )
    contradictory_claim = evaluate_assertion(
        assertion,
        response=(
            "多语言检索提升了十一个点。实验设置是材料科学和法律子集。"
            "不过文中没有给出多语言独立实验。"
        ),
        status="done",
        interrupt=None,
        trace=[],
        artifacts=[],
        materialized=materialized,
        after_files=materialized.before_files,
    )

    assert correct_refusal.passed is True
    assert contradictory_claim.passed is False


def test_hitl_assertion_can_pin_tool_arguments(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == "cowork-core-050")
    materialized = materialize_case(suite, item, case_root=tmp_path / "case")

    result = evaluate_assertion(
        {
            "type": "hitl_interrupt",
            "kind": "tool_confirmation",
            "tool": "run_shell",
            "arguments": {"persistent_session": True, "run_in_background": False},
        },
        response="",
        status="waiting_human",
        interrupt={
            "kind": "shell_approval",
            "request": {
                "persistent_session": True,
                "run_in_background": False,
            },
        },
        trace=[{"name": "run_shell", "status": "interrupt", "arguments": {}}],
        artifacts=[],
        materialized=materialized,
        after_files=materialized.before_files,
    )

    assert result.passed is True


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
    assert report["kind"] == "cowork"
    assert report["dataset"] == suite["name"]
    assert report["label"] == "unit-rescore"
    assert report["run_id"] == report["manifest"]["run_id"]
    assert len(report["config_hash"]) == 64
    assert report["config_hash"] == report["manifest"]["config_hash"]
    assert len(report["reproducibility"]["scorer_fingerprint"]) == 64
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
    gateway = ModelGateway(
        provider,
        embedding_dimensions=16,
        default_context_window_tokens=128_000,
    )
    settings = get_settings().model_copy(
        update={
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


@pytest.mark.integration
@pytest.mark.parametrize(
    ("item_id", "call"),
    [
        (
            "cowork-core-049",
            _tool(
                "calendar",
                "feishu_calendar_event_action",
                {
                    "account_id": "11111111-1111-4111-8111-111111111111",
                    "action": "create",
                    "calendar_id": "primary",
                    "event": {
                        "summary": "中文办公栈评审",
                        "start_time": {
                            "timestamp": "1787623200",
                            "timezone": "Asia/Shanghai",
                        },
                        "end_time": {
                            "timestamp": "1787626800",
                            "timezone": "Asia/Shanghai",
                        },
                    },
                },
            ),
        ),
        (
            "cowork-core-050",
            _tool(
                "shell",
                "run_shell",
                {
                    "command": "export WP_STAGE=中文办公栈",
                    "cwd": ".",
                    "reason": "为后续命令准备持久环境",
                    "persistent_session": True,
                },
            ),
        ),
    ],
)
async def test_chinese_office_eval_cases_reach_production_approval_gate(
    db_engine: AsyncEngine,
    tmp_path: Path,
    item_id: str,
    call: CompletionResult,
) -> None:
    suite = load_suite(DEFAULT_SUITE)
    item = next(value for value in suite["items"] if value["id"] == item_id)
    completions = [call]
    if item_id == "cowork-core-049":
        completions.insert(
            0,
            _tool(
                "load-calendar",
                "load_tools",
                {"names": ["feishu_calendar_event_action"]},
            ),
        )
    gateway = ModelGateway(
        ScriptedCoworkProvider(completions),
        embedding_dimensions=16,
        default_context_window_tokens=128_000,
    )
    settings = get_settings().model_copy(
        update={
            "memory_extraction_enabled": False,
            "skill_distillation_enabled": False,
            "run_heartbeat_s": 60.0,
        }
    )

    record = await run_case(
        suite,
        item,
        case_root=tmp_path / item_id,
        gateway=gateway,
        settings=settings,
        db_sessions=session_factory,
    )

    assert record["observation"]["status"] == "waiting_human"
    assert record["score"]["task_success"] is True


@pytest.mark.integration
async def test_run_suite_starts_and_isolates_records_in_its_own_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑批必须能启动，并且运行记录落在包目录里而不是 ~/.workpilot。

    回归的是一条静默死亡：`run_suite` 曾把 `cowork_store_backend` 钉成 "postgres"，
    而那个后端随 ADR-0012 一起没了——入口处的闸门让整条 Cowork 任务集评测直接
    拒绝启动。修法不是删掉闸门了事：它承担的"隔离运行记录"仍然必须成立，
    所以断言两件事——能跑完，以及 SQLite 确实出现在 package 里。
    """

    suite = load_suite(DEFAULT_SUITE)
    item = suite["items"][0]
    gateway = ModelGateway(
        ScriptedCoworkProvider(
            [
                _tool("roots", "list_workspace_roots", {}),
                _tool("search", "search_files", {"path": ".", "query": "Atlas"}),
                _tool("read", "read_text_file", {"path": "notes/project.md"}),
                _final("计划发布日期为 2026-09-15，负责人是林琪，来源 notes/project.md。"),
            ]
        ),
        embedding_dimensions=16,
        default_context_window_tokens=128_000,
    )
    package = tmp_path / "package"

    manifest_path, report_path, _ = await run_suite(
        suite_path=DEFAULT_SUITE,
        items=[item],
        package=package,
        label="isolation-smoke",
        authorization_note="单元测试内的脚本化 provider，不出网",
        test_access_note=None,
        settings=get_settings(),
        gateway=gateway,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["label"] == "isolation-smoke"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "cowork"
    assert report["dataset"] == suite["name"]
    assert report["label"] == "isolation-smoke"
    assert report["config_hash"] == report["manifest"]["config_hash"]
    assert len(report["reproducibility"]["implementation_fingerprint"]) == 64
    assert report["metrics"]["task_success_rate"] == 1.0
    # 隔离的真凭据：控制面库在包里。
    assert (package / "store" / "cowork.db").exists()
    cassette = package / "model-cassette.json"
    assert cassette.is_file()
    assert report["model_io"]["mode"] == "record"
    assert report["model_io"]["recorded_model_interactions"] == 4

    def _network_gateway_must_not_be_built(*args, **kwargs):
        del args, kwargs
        raise AssertionError("cassette replay 不得构造真实模型网关")

    monkeypatch.setattr(
        "eval.cowork_runner.build_model_gateway", _network_gateway_must_not_be_built
    )
    replay_package = tmp_path / "replay-package"
    _, replay_report_path, _ = await run_suite(
        suite_path=DEFAULT_SUITE,
        items=[item],
        package=replay_package,
        label="isolation-replay",
        authorization_note="",
        test_access_note=None,
        settings=get_settings(),
        replay_cassette=cassette,
    )
    replay_report = json.loads(replay_report_path.read_text(encoding="utf-8"))
    assert replay_report["metrics"]["task_success_rate"] == 1.0
    assert replay_report["model_io"]["mode"] == "cassette_replay"
    assert replay_report["model_io"]["real_model_dispatches"] == 0
    assert replay_report["items"][0]["score"] == report["items"][0]["score"]
