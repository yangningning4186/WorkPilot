from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from eval.model_cassette import (
    ModelCassetteError,
    RecordingModelGateway,
    ReplayingModelGateway,
    content_sha256,
)
from workpilot_ai.gateway import PromptBudget
from workpilot_ai.types import (
    CompletionChunk,
    CompletionResult,
    EmbeddingResult,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)


class _FakeGateway:
    chat_provider = "fixture"
    chat_model = "fixture-chat"
    embedding_provider = "fixture"
    embedding_model = "fixture-embedding"
    embedding_dimensions = 2

    def __init__(self, *, workspace: Path, tool_name: str = "read_text_file") -> None:
        self.workspace = workspace
        self.tool_name = tool_name
        self.calls = 0

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget:
        return PromptBudget(
            task_type=task_type,
            tier="main",
            model=self.chat_model,
            context_window_tokens=32_768,
            max_output_tokens=max_tokens,
            safety_tokens=512,
        )

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        del messages, task_type, max_tokens, temperature
        self.calls += 1
        return CompletionResult(
            text=f"已读取 {self.workspace / 'input.txt'}",
            model=self.chat_model,
            provider=self.chat_provider,
            usage=Usage(input_tokens=11, output_tokens=7),
        )

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        del messages, tools, parallel_tool_calls, task_type, max_tokens, temperature
        self.calls += 1
        return CompletionResult(
            text="",
            model=self.chat_model,
            provider=self.chat_provider,
            usage=Usage(input_tokens=13, output_tokens=5),
            tool_calls=(
                ToolCall(
                    id="call-1",
                    name=self.tool_name,
                    arguments=json.dumps({"path": str(self.workspace / "input.txt")}),
                ),
            ),
        )

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]:
        result = await self.complete_with_tools(
            messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        yield CompletionChunk(reasoning_delta="先检查文件")
        yield CompletionChunk(result=result)

    async def stream(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        del messages, task_type, max_tokens, temperature
        self.calls += 1
        yield "第一块"
        yield str(self.workspace / "第二块")

    async def embed(self, texts: list[str], *, task_type: str = "embedding") -> EmbeddingResult:
        del texts, task_type
        self.calls += 1
        return EmbeddingResult(
            embeddings=[[0.25, 0.75]],
            model=self.embedding_model,
            provider=self.embedding_provider,
            usage=Usage(input_tokens=3),
        )


def _messages(workspace: Path, *, date: str, time: str) -> list[Message]:
    return [
        Message(
            role="system",
            content=(
                "<environment>\n"
                f"当前日期：{date}（周一）\n"
                f"当前时间：{time} CST（UTC+08:00）\n"
                "操作系统：fixture OS\n"
                "</environment>\n"
                f"workspace={workspace}"
            ),
        ),
        Message(role="user", content="读取 input.txt"),
    ]


TOOLS = [
    ToolDefinition(
        name="read_text_file",
        description="读取文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]


@pytest.mark.asyncio
async def test_record_then_replay_stream_without_delegate_and_rebind_paths(
    tmp_path: Path,
) -> None:
    record_root = tmp_path / "record-case"
    record_workspace = record_root / "workspace"
    record_workspace.mkdir(parents=True)
    cassette = tmp_path / "model-cassette.json"
    real = _FakeGateway(workspace=record_workspace)
    recorder = RecordingModelGateway(real, output=cassette, metadata={"suite_sha256": "a" * 64})
    recorder.begin_case("case-1", case_root=record_root, workspace=record_workspace)

    budget = recorder.prompt_budget("cowork_decision", max_tokens=2048)
    recorded_chunks = [
        chunk
        async for chunk in recorder.stream_with_tools(
            _messages(record_workspace, date="2026-08-24", time="12:00"),
            tools=TOOLS,
            task_type="cowork_decision",
            max_tokens=2048,
        )
    ]
    recorder.end_case()
    recorder.finalize()

    assert budget.max_input_tokens == 30_208
    assert real.calls == 1
    assert cassette.is_file()
    assert not cassette.with_suffix(".json.partial.jsonl").exists()
    assert stat.S_IMODE(cassette.stat().st_mode) == 0o600
    assert recorded_chunks[-1].result is not None

    replay_root = tmp_path / "replay-case"
    replay_workspace = replay_root / "workspace"
    replay_workspace.mkdir(parents=True)
    replay = ReplayingModelGateway.load(cassette)
    replay.begin_case("case-1", case_root=replay_root, workspace=replay_workspace)

    replay_budget = replay.prompt_budget("cowork_decision", max_tokens=2048)
    replayed_chunks = [
        chunk
        async for chunk in replay.stream_with_tools(
            _messages(replay_workspace, date="2030-01-01", time="09:31"),
            tools=TOOLS,
            task_type="cowork_decision",
            max_tokens=2048,
        )
    ]
    replay.end_case()
    replay.assert_complete()

    assert replay_budget == budget
    assert replay.real_model_calls == 0
    result = replayed_chunks[-1].result
    assert result is not None
    arguments = json.loads(result.tool_calls[0].arguments)
    assert arguments["path"] == str(replay_workspace / "input.txt")
    assert replayed_chunks[0].reasoning_delta == "先检查文件"


@pytest.mark.asyncio
async def test_all_gateway_shapes_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "record"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    real = _FakeGateway(workspace=workspace)
    cassette = tmp_path / "all.json"
    recorder = RecordingModelGateway(real, output=cassette)
    recorder.begin_case("all", case_root=root, workspace=workspace)

    completion = await recorder.complete(_messages(workspace, date="2026-01-01", time="10:00"))
    tool_completion = await recorder.complete_with_tools(
        _messages(workspace, date="2026-01-01", time="10:00"), tools=TOOLS
    )
    text_chunks = [
        item
        async for item in recorder.stream(_messages(workspace, date="2026-01-01", time="10:00"))
    ]
    embedding = await recorder.embed(["hello"])
    recorder.end_case()
    recorder.finalize()

    replay_root = tmp_path / "replay"
    replay_workspace = replay_root / "workspace"
    replay_workspace.mkdir(parents=True)
    replay = ReplayingModelGateway.load(cassette)
    replay.begin_case("all", case_root=replay_root, workspace=replay_workspace)
    messages = _messages(replay_workspace, date="2027-02-02", time="11:30")

    replay_completion = await replay.complete(messages)
    replay_tool_completion = await replay.complete_with_tools(messages, tools=TOOLS)
    replay_text = [item async for item in replay.stream(messages)]
    replay_embedding = await replay.embed(["hello"])
    replay.end_case()
    replay.assert_complete()

    assert completion.usage == replay_completion.usage
    assert replay_completion.text == f"已读取 {replay_workspace / 'input.txt'}"
    assert tool_completion.usage == replay_tool_completion.usage
    assert replay_text == [text_chunks[0], str(replay_workspace / "第二块")]
    assert replay_embedding == embedding


@pytest.mark.asyncio
async def test_request_drift_fails_closed_and_never_calls_a_model(tmp_path: Path) -> None:
    root = tmp_path / "case"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    cassette = tmp_path / "strict.json"
    recorder = RecordingModelGateway(_FakeGateway(workspace=workspace), output=cassette)
    recorder.begin_case("strict", case_root=root, workspace=workspace)
    await recorder.complete(_messages(workspace, date="2026-01-01", time="10:00"))
    recorder.end_case()
    recorder.finalize()

    replay = ReplayingModelGateway.load(cassette)
    replay.begin_case("strict", case_root=root, workspace=workspace)
    changed = _messages(workspace, date="2026-01-02", time="10:30")
    changed[1] = Message(role="user", content="读取另一个文件")

    with pytest.raises(ModelCassetteError, match="未录制请求"):
        await replay.complete(changed)
    assert replay.real_model_calls == 0


@pytest.mark.asyncio
async def test_interaction_reordering_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "case"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    cassette = tmp_path / "ordered.json"
    recorder = RecordingModelGateway(_FakeGateway(workspace=workspace), output=cassette)
    recorder.begin_case("ordered", case_root=root, workspace=workspace)
    await recorder.complete(_messages(workspace, date="2026-01-01", time="10:00"))
    await recorder.embed(["hello"])
    recorder.end_case()
    recorder.finalize()

    replay = ReplayingModelGateway.load(cassette)
    replay.begin_case("ordered", case_root=root, workspace=workspace)
    with pytest.raises(ModelCassetteError, match="乱序请求"):
        await replay.embed(["hello"])
    assert replay.real_model_calls == 0


@pytest.mark.asyncio
async def test_tamper_and_unconsumed_interactions_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "case"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    cassette = tmp_path / "sealed.json"
    recorder = RecordingModelGateway(_FakeGateway(workspace=workspace), output=cassette)
    recorder.begin_case("case", case_root=root, workspace=workspace)
    await recorder.complete(_messages(workspace, date="2026-01-01", time="10:00"))
    recorder.end_case()
    recorder.finalize()

    replay = ReplayingModelGateway.load(cassette)
    replay.begin_case("case", case_root=root, workspace=workspace)
    with pytest.raises(ModelCassetteError, match="未消费模型交互"):
        replay.end_case()

    payload = json.loads(cassette.read_text(encoding="utf-8"))
    payload["interactions"][0]["response"]["kind"] = "tampered"
    cassette.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelCassetteError, match="完整性校验失败"):
        ReplayingModelGateway.load(cassette)


def test_malformed_sequence_and_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    cassette = tmp_path / "bad.json"
    unsigned = {
        "schema": "workpilot.model-cassette",
        "schema_version": 1,
        "canonicalization": "workpilot-json-sort-keys-utf8-v1",
        "normalizer": "workpilot-cowork-volatile-v1",
        "metadata": {"complete": True},
        "prompt_budgets": {},
        "interactions": [
            {
                "sequence": 2,
                "case_id": "case",
                "operation": "complete",
                "request_hash": content_sha256({"operation": "complete"}),
                "request": {"operation": "complete"},
                "response": {"kind": "completion", "value": {}},
            }
        ],
    }
    payload = {
        **unsigned,
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": "workpilot-json-sort-keys-utf8-v1",
            "value": content_sha256(unsigned),
        },
    }
    cassette.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelCassetteError, match="必须从 1 开始且连续"):
        ReplayingModelGateway.load(cassette)

    cassette.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(ModelCassetteError, match="重复键"):
        ReplayingModelGateway.load(cassette)


@pytest.mark.asyncio
async def test_recorded_high_risk_tool_can_be_rejected_before_graph_execution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "case"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    cassette = tmp_path / "shell.json"
    recorder = RecordingModelGateway(
        _FakeGateway(workspace=workspace, tool_name="run_shell"), output=cassette
    )
    recorder.begin_case("shell", case_root=root, workspace=workspace)
    await recorder.complete_with_tools(
        _messages(workspace, date="2026-01-01", time="10:00"), tools=TOOLS
    )
    recorder.end_case()
    recorder.finalize()

    replay = ReplayingModelGateway.load(cassette)

    assert replay.cases_using_tool("run_shell") == ("shell",)
