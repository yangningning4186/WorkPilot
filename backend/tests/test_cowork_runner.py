import asyncio
import hashlib
import json
import shlex
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from docx import Document
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.dependencies import (
    get_run_bus,
    get_run_queue_dependency,
    require_owner_identity,
)
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.run_bus import InMemoryRunBus
from app.cowork.browser_tools import register_browser_tools
from app.cowork.context_usage import get_cowork_context_usage
from app.cowork.permissions import (
    CapabilityDeniedError,
    create_session_root,
    grant_capability,
    list_session_roots,
)
from app.cowork.runtime import (
    _encode_tool_result,
    _external_action_sha256,
    _goal_mentions_office,
    initialize_cowork_state,
    load_cowork_checkpoint,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
    _trusted_artifact_mime_type,
    build_default_cowork_registry,
)
from app.main import create_app
from app.platform.request_identity import RequestIdentity
from app.runstore.runs import (
    append_message,
    create_run,
    ensure_conversation,
    get_run,
    list_events,
    reap_expired_runs,
    request_cancel,
)
from app.worker.cowork_run import _cowork_error_detail, _cowork_failure_message, cowork_run
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.providers.openai_compatible import ProviderContextOverflowError
from workpilot_ai.types import CompletionResult, Message, ToolCall, ToolDefinition, Usage

pytestmark = pytest.mark.integration


def test_cowork_failure_message_keeps_actionable_detail_bounded() -> None:
    assert _cowork_failure_message("最新 checkpoint 不是 Cowork v2 state") == (
        "Cowork 执行失败：最新 checkpoint 不是 Cowork v2 state"
    )
    assert len(_cowork_failure_message("x" * 1000)) < 380
    assert _cowork_error_detail(TimeoutError()) == "模型或工具请求超时，请重试"


def test_external_approval_action_hash_is_stable_and_non_null() -> None:
    first = _external_action_sha256("publish", {"b": 2, "a": 1})
    second = _external_action_sha256("publish", {"a": 1, "b": 2})

    assert first == second
    assert len(first) == 64


def test_artifact_mime_must_match_trusted_extension() -> None:
    assert _trusted_artifact_mime_type(Path("report.xml"), None) == "application/xml"
    with pytest.raises(CoworkToolError, match="必须与扩展名一致"):
        _trusted_artifact_mime_type(Path("payload.xml"), "text/html")


def test_ppt_goal_exposes_native_artifact_without_word_edit_route() -> None:
    registry = build_default_cowork_registry()
    names = {
        definition.name for definition in registry.tool_definitions_for("帮我生成一个儿童节 PPT")
    }

    assert "create_native_artifact" in names
    assert "edit_word" not in names


async def test_new_cowork_run_inherits_assistant_and_tool_history(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(
        db_session,
        scope="local_owner",
        title="Cowork 跨 run 上下文",
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    first = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="搜索今天的 AI 热点",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=first.goal,
        run_id=first.id,
    )
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=first.id, registry=registry)
    checkpoint = await load_cowork_checkpoint(db_session, run_id=first.id)
    assert checkpoint is not None
    checkpoint.state["messages"].extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "web-1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query":"AI 热点"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "web-1",
                "content": '{"ok":true,"result":{"items":["新闻一"]}}',
            },
            {"role": "assistant", "content": "这里是今天的五条 AI 热点。"},
        ]
    )
    checkpoint.state["status"] = "done"
    await db_session.execute(
        text(
            """
            UPDATE agent_checkpoints
            SET state = CAST(:state AS jsonb)
            WHERE run_id = :run_id AND checkpoint_id = :checkpoint_id
            """
        ),
        {
            "state": json.dumps(checkpoint.state, ensure_ascii=False),
            "run_id": first.id,
            "checkpoint_id": checkpoint.checkpoint_id,
        },
    )
    await db_session.execute(
        text("UPDATE agent_runs SET status = 'done' WHERE id = :run_id"),
        {"run_id": first.id},
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="assistant",
        content="这里是今天的五条 AI 热点。",
        run_id=first.id,
    )

    second = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="把上面的新闻总结成一份文档",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=second.goal,
        run_id=second.id,
    )
    state = await initialize_cowork_state(
        db_session,
        run_id=second.id,
        registry=registry,
    )

    assert [message["role"] for message in state["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert state["messages"][2]["tool_call_id"] == "web-1"
    assert state["messages"][3]["content"] == "这里是今天的五条 AI 热点。"
    assert state["messages"][-1]["content"] == second.goal

    usage = await get_cowork_context_usage(
        db_session,
        conversation_id=conversation_id,
        settings=get_settings(),
    )
    assert usage["used_tokens"] > 0
    assert usage["trigger_ratio"] == 0.85
    assert usage["trigger_tokens"] < usage["context_window_tokens"]
    assert usage["breakdown"]["tool_activity"] > 0


def test_cowork_tool_result_structurally_truncates_content_and_keeps_baseline() -> None:
    baseline = "a" * 64
    result = CoworkToolResult(
        output={
            "file_id": "opaque-file-id",
            "content": "正文" * 10_000,
            "baseline_sha256": baseline,
            "editable": True,
        }
    )
    encoded = _encode_tool_result(result, 1_000)
    payload = json.loads(encoded)

    assert len(encoded) <= 1_000
    assert payload["result"]["baseline_sha256"] == baseline
    assert payload["result"]["content_truncated"] is True
    assert payload["result"]["content_original_chars"] == 20_000
    assert len(payload["result"]["content"]) < 20_000


def test_office_goal_detection_uses_word_boundaries() -> None:
    assert _goal_mentions_office("整理 Word 文档")
    assert _goal_mentions_office("分析Excel表格")
    assert _goal_mentions_office("更新 report.xlsx")
    assert not _goal_mentions_office("extract keyword statistics")
    assert not _goal_mentions_office("audit password policy on wordpress")
    assert not _goal_mentions_office("calculate word count for this text")
    assert not _goal_mentions_office("build an Excel-like grid")


async def test_shell_rejects_read_only_cwd(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Read-only shell cwd"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_only",
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="shell.execute",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="查看当前目录",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1024),
        settings=get_settings().model_copy(update={"cowork_shell_allowlist": ["pwd"]}),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="readonly-shell-worker",
        plan_step_id=UUID(int=43),
        tool_call_id="readonly-shell-call",
    )

    with pytest.raises(CapabilityDeniedError, match=r"filesystem\.write"):
        await build_default_cowork_registry().execute(
            "run_shell",
            {"command": "pwd", "cwd": str(tmp_path), "reason": "查看目录"},
            context=context,
        )


async def test_browser_open_requires_network_read_on_top_of_browser_control(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """browser_open 的返回值就是完整页面快照，不能成为 network.read 的绕过路径。"""

    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Browser capability split"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="browser.control",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="打开网页",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1024),
        settings=get_settings(),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="browser-capability-worker",
        plan_step_id=UUID(int=71),
        tool_call_id="browser-capability-call",
    )

    # 校验发生在 handler 之前，所以这里不会真的去启动 Chromium。
    with pytest.raises(CapabilityDeniedError, match=r"network\.read"):
        await registry.execute(
            "browser_open",
            {"url": "https://example.com/"},
            context=context,
        )


async def test_shell_requires_executor_approval_and_reuses_completed_invocation(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Shell executor guard"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="shell.execute",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="执行有副作用的诊断命令",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await db_session.commit()

    output = tmp_path / "shell-executions.txt"
    script = (
        "from pathlib import Path; "
        f"p=Path({str(output)!r}); p.open('a', encoding='utf-8').write('run\\n')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    arguments = {"command": command, "cwd": str(tmp_path), "reason": "验证 Shell 幂等边界"}
    registry = build_default_cowork_registry()
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    settings = get_settings().model_copy(update={"cowork_shell_allowlist": []})
    base_context = dict(
        session=db_session,
        gateway=gateway,
        settings=settings,
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="shell-guard-worker",
        plan_step_id=UUID(int=42),
        tool_call_id="shell-guard-call",
        cancel_event=None,
    )

    with pytest.raises(CoworkToolError, match="未获得当前 tool call"):
        await registry.execute(
            "run_shell",
            arguments,
            context=CoworkToolContext(**base_context),
        )
    assert not output.exists()

    approved_context = CoworkToolContext(
        **base_context, approved_call_ids=frozenset({"shell-guard-call"})
    )
    first = await registry.execute("run_shell", arguments, context=approved_context)
    replay = await registry.execute("run_shell", arguments, context=approved_context)

    assert first.reused is False
    assert replay.reused is True
    assert output.read_text(encoding="utf-8") == "run\n"


class RecordingCoworkQueue:
    def __init__(self) -> None:
        self.run_ids: list[UUID] = []
        self.attempts: list[int] = []

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        self.run_ids.append(run_id)
        self.attempts.append(attempt)


def _tool_completion(*calls: ToolCall) -> CompletionResult:
    return CompletionResult(
        text="",
        model="fake-chat",
        provider="deterministic_test",
        usage=Usage(input_tokens=3, output_tokens=2),
        tool_calls=tuple(calls),
    )


def _final_completion(text: str) -> CompletionResult:
    return CompletionResult(
        text=text,
        model="fake-chat",
        provider="deterministic_test",
        usage=Usage(input_tokens=3, output_tokens=2),
    )


class NativeToolProvider(DeterministicProvider):
    def __init__(
        self,
        tool_completions: list[CompletionResult],
        *,
        regular_completions: list[str] | None = None,
    ) -> None:
        super().__init__(completion_texts=regular_completions)
        self.tool_completions = list(tool_completions)
        self.tool_calls = 0
        self.tool_histories: list[list[Message]] = []
        self.last_tools: list[ToolDefinition] = []
        self.parallel_flags: list[bool] = []

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del max_tokens, temperature
        self.tool_calls += 1
        self.tool_histories.append(messages)
        self.last_tools = tools
        self.parallel_flags.append(parallel_tool_calls)
        return self.tool_completions.pop(0)


class CancellingCoworkProvider(NativeToolProvider):
    """在第一次模型决策返回后模拟用户点击停止。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        run_id: UUID,
    ) -> None:
        super().__init__(
            [_tool_completion(ToolCall(id="cancel-call", name="list_office_files", arguments="{}"))]
        )
        self.session_factory = session_factory
        self.run_id = run_id

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        result = await super().complete_with_tools(
            messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if self.tool_calls == 1:
            async with self.session_factory() as session:
                await request_cancel(session, run_id=self.run_id)
                await session.commit()
        return result


class OverflowRecoveringCoworkProvider(NativeToolProvider):
    def __init__(
        self,
        tool_completions: list[CompletionResult],
        *,
        overflow_calls: set[int],
        regular_completions: list[str],
    ) -> None:
        super().__init__(tool_completions, regular_completions=regular_completions)
        self.overflow_calls = overflow_calls

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del max_tokens, temperature
        self.tool_calls += 1
        self.tool_histories.append(messages)
        self.last_tools = tools
        self.parallel_flags.append(parallel_tool_calls)
        if self.tool_calls in self.overflow_calls:
            raise ProviderContextOverflowError("模型服务返回 400：context_length_exceeded")
        return self.tool_completions.pop(0)


class EmptyToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FakeBrowserControl:
    def __init__(self) -> None:
        self.clicked = False

    async def click(self, **options: int) -> None:
        assert options["timeout"] > 0
        self.clicked = True


class _FakeBrowserBody:
    async def inner_text(self, **options: int) -> str:
        assert options["timeout"] > 0
        return "测试页面"


class _FakeBrowserControls:
    async def count(self) -> int:
        return 0


class _FakeBrowserPage:
    url = "https://example.com/after-click"

    def locator(self, selector: str) -> object:
        return _FakeBrowserBody() if selector == "body" else _FakeBrowserControls()

    async def wait_for_load_state(self, state: str, **options: int) -> None:
        assert state == "domcontentloaded"
        assert options["timeout"] > 0

    async def title(self) -> str:
        return "测试页面"


class _FakeBrowserSession:
    def __init__(self, control: _FakeBrowserControl) -> None:
        self.page = _FakeBrowserPage()
        self.controls: list[object] = [control]
        self.action_no = 0
        self.last_used = 0.0


class _FakeBrowserManager:
    def __init__(self, session_id: str, session: _FakeBrowserSession) -> None:
        self.session_id = session_id
        self.session = session

    async def get(
        self,
        session_id: str,
        *,
        conversation_id: UUID,
    ) -> _FakeBrowserSession:
        assert session_id == self.session_id
        assert isinstance(conversation_id, UUID)
        return self.session


async def test_granted_browser_control_clicks_without_inbox_round_trip(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork browser control"
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="browser.control",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="点击浏览器控件",
        budget_tokens=50_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    session_id = "test-browser-session-01"
    control = _FakeBrowserControl()
    registry = build_default_cowork_registry()
    register_browser_tools(
        registry,
        _FakeBrowserManager(session_id, _FakeBrowserSession(control)),  # type: ignore[arg-type]
    )
    bus = InMemoryRunBus()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="browser-click",
                    name="browser_click",
                    arguments=json.dumps({"session_id": session_id, "control_index": 0}),
                )
            ),
            _final_completion("点击完成。"),
        ]
    )

    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
            ),
            "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None and refreshed.status == "done"
    assert control.clicked is True
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert "interrupt" not in {event.type for event in events}


async def test_batched_exclusive_browser_actions_are_rejected_without_execution(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork exclusive browser control"
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="browser.control",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="连续点击两个浏览器控件",
        budget_tokens=50_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    session_id = "test-browser-session-02"
    control = _FakeBrowserControl()
    registry = build_default_cowork_registry()
    register_browser_tools(
        registry,
        _FakeBrowserManager(session_id, _FakeBrowserSession(control)),  # type: ignore[arg-type]
    )
    bus = InMemoryRunBus()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="browser-click-1",
                    name="browser_click",
                    arguments=json.dumps({"session_id": session_id, "control_index": 0}),
                ),
                ToolCall(
                    id="browser-click-2",
                    name="browser_click",
                    arguments=json.dumps({"session_id": session_id, "control_index": 0}),
                ),
            ),
            _final_completion("已改为逐个操作。"),
        ]
    )

    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
            ),
            "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    assert control.clicked is False
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    tool_errors = [
        json.loads(message["content"])
        for message in checkpoint.state["messages"]
        if message["role"] == "tool"
    ]
    assert len(tool_errors) == 2
    assert all("独占工具必须单独调用" in item["error"] for item in tool_errors)
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert "interrupt" not in {event.type for event in events}
    assert "tool.start" not in {event.type for event in events}


async def test_cowork_runner_lists_inspects_edits_word_and_registers_artifact(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    workspace = tmp_path / "cowork"
    workspace.mkdir()
    word_path = workspace / "brief.docx"
    document = Document()
    document.add_paragraph("旧内容")
    document.save(str(word_path))
    baseline = hashlib.sha256(word_path.read_bytes()).hexdigest()

    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork Runner"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(workspace),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="把 brief.docx 第一段改成新内容",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    provider = NativeToolProvider(
        [
            _tool_completion(ToolCall(id="list-call", name="list_office_files", arguments="{}")),
            _tool_completion(
                ToolCall(
                    id="inspect-call",
                    name="inspect_office_file",
                    arguments=json.dumps({"path": str(word_path)}),
                )
            ),
            _tool_completion(
                ToolCall(
                    id="edit-call",
                    name="edit_word",
                    arguments=json.dumps(
                        {
                            "path": str(word_path),
                            "baseline_sha256": baseline,
                            "instruction": "把第一段改成新内容",
                        }
                    ),
                )
            ),
            _final_completion("已更新 brief.docx 第一段，并创建恢复副本。"),
        ],
        regular_completions=[
            (
                '{"summary":"更新第一段","operations":['
                '{"op":"replace_paragraph","paragraph":0,"text":"新内容"}]}'
            ),
        ],
    )
    settings = get_settings().model_copy(update={"cowork_max_steps": 8, "run_heartbeat_s": 60.0})
    ctx = {
        "settings": settings,
        "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(ctx, str(run.id))

    assert Document(str(word_path)).paragraphs[0].text == "新内容"
    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.used_calls == 5
    events = await list_events(db_session, run_id=run.id, limit=200)
    event_types = [event.type for event in events]
    assert event_types.count("tool.start") == 3
    assert event_types.count("tool.result") == 3
    assert "artifact" in event_types
    assert "interrupt" not in event_types
    artifact = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT uri, meta FROM artifacts
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run.id},
            )
        )
        .mappings()
        .one()
    )
    assert artifact["uri"] == str(word_path)
    assert artifact["meta"]["change_count"] == 1
    invocation = (
        await db_session.execute(
            text(
                """
                SELECT status, effect_ref FROM tool_invocations
                WHERE run_id = :run_id AND tool_name = 'edit_word'
                """
            ),
            {"run_id": run.id},
        )
    ).one()
    assert invocation.status == "succeeded"
    assert invocation.effect_ref.startswith(f"file:{word_path}#sha256=")
    assert provider.parallel_flags == [True, True, True, True]
    final_history = provider.tool_histories[-1]
    assistant_calls = [message for message in final_history if message.tool_calls]
    tool_results = [message for message in final_history if message.role == "tool"]
    assert [message.tool_calls[0].id for message in assistant_calls] == [
        "list-call",
        "inspect-call",
        "edit-call",
    ]
    assert [message.tool_call_id for message in tool_results] == [
        "list-call",
        "inspect-call",
        "edit-call",
    ]


async def test_cowork_repairs_invalid_edit_baseline_from_latest_structured_inspection(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    workspace = tmp_path / "cowork-baseline-repair"
    workspace.mkdir()
    word_path = workspace / "long.docx"
    document = Document()
    document.add_paragraph("旧标题")
    document.add_paragraph("很长的正文" * 2_000)
    document.save(str(word_path))
    baseline = hashlib.sha256(word_path.read_bytes()).hexdigest()

    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork baseline repair"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(workspace),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理 long.docx 的 Word 标题",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(ToolCall(id="repair-list", name="list_office_files", arguments="{}")),
            _tool_completion(
                ToolCall(
                    id="repair-inspect",
                    name="inspect_office_file",
                    arguments=json.dumps({"path": str(word_path)}),
                )
            ),
            _tool_completion(
                ToolCall(
                    id="repair-edit",
                    name="edit_word",
                    arguments=json.dumps(
                        {
                            "path": str(word_path),
                            "baseline_sha256": "01a01508-ef29-718b-84bc-0ac6bab5e73e",
                            "instruction": "把第一段改成新标题",
                        }
                    ),
                )
            ),
            _final_completion("已更新标题。"),
        ],
        regular_completions=[
            (
                '{"summary":"更新标题","operations":['
                '{"op":"replace_paragraph","paragraph":0,"text":"新标题"}]}'
            )
        ],
    )
    settings = get_settings().model_copy(
        update={
            "cowork_max_steps": 8,
            "cowork_tool_result_max_chars": 1_000,
            "run_heartbeat_s": 60.0,
        }
    )
    await cowork_run(
        {
            "settings": settings,
            "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    assert Document(str(word_path)).paragraphs[0].text == "新标题"
    final_history = provider.tool_histories[-1]
    inspect_result = json.loads(
        next(
            message.content
            for message in final_history
            if message.role == "tool" and message.tool_call_id == "repair-inspect"
        )
    )
    assert inspect_result["result"]["baseline_sha256"] == baseline
    assert inspect_result["result"]["content_truncated"] is True
    edit_call = next(
        message.tool_calls[0]
        for message in final_history
        if message.tool_calls and message.tool_calls[0].id == "repair-edit"
    )
    assert json.loads(edit_call.arguments)["baseline_sha256"] == baseline
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert any(
        event.type == "tool.arguments.repaired" and event.payload.get("tool") == "edit_word"
        for event in events
    )


async def test_cowork_runner_consumes_cancel_before_pending_tool(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork cancel"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="扫描文件，但我可能随时停止",
        budget_tokens=50_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    provider = CancellingCoworkProvider(session_factory, run.id)
    settings = get_settings().model_copy(update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0})
    await cowork_run(
        {
            "settings": settings,
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"
    assert provider.tool_calls == 1
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert "tool.start" not in [event.type for event in events]
    assert any(
        event.type == "step.update" and event.payload.get("status") == "skipped" for event in events
    )
    assert any(
        event.type == "error" and event.payload.get("code") == "cancelled" for event in events
    )
    assert any(
        event.type == "run.done" and event.payload.get("status") == "cancelled" for event in events
    )
    assistant_status = (
        await db_session.execute(
            text(
                """
                SELECT status FROM messages
                WHERE run_id = :run_id AND role = 'assistant'
                """
            ),
            {"run_id": run.id},
        )
    ).scalar_one()
    assert assistant_status == "cancelled"
    latest_state = (
        await db_session.execute(
            text(
                """
                SELECT state FROM agent_checkpoints
                WHERE run_id = :run_id
                ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
                """
            ),
            {"run_id": run.id},
        )
    ).scalar_one()
    assert latest_state["messages"][-2]["tool_calls"][0]["id"] == "cancel-call"
    assert latest_state["messages"][-1]["role"] == "tool"
    assert latest_state["messages"][-1]["tool_call_id"] == "cancel-call"


async def test_cowork_runner_executes_parallel_safe_read_batch_concurrently(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    active = 0
    max_active = 0
    both_started = asyncio.Event()

    async def parallel_read(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        nonlocal active, max_active
        del context, raw
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            await asyncio.sleep(0.02)
            return CoworkToolResult(output={"active": active})
        finally:
            active -= 1

    registry = CoworkToolRegistry()
    for name in ("read_a", "read_b"):
        registry.register(
            CoworkToolSpec(
                name=name,
                description=f"并行读取 {name}",
                args_model=EmptyToolArgs,
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=parallel_read,
            )
        )

    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork parallel reads"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="并行读取两个来源",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(id="read-a-call", name="read_a", arguments="{}"),
                ToolCall(id="read-b-call", name="read_b", arguments="{}"),
            ),
            _final_completion("两个来源均已读取。"),
        ]
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={
                    "cowork_max_steps": 4,
                    "cowork_decision_max_tokens": 2048,
                    "run_heartbeat_s": 60.0,
                }
            ),
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert max_active == 2
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert [event.type for event in events].count("tool.start") == 2
    assert [event.type for event in events].count("tool.result") == 2
    latest_state = (
        await db_session.execute(
            text(
                """
                SELECT state FROM agent_checkpoints
                WHERE run_id = :run_id
                ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
                """
            ),
            {"run_id": run.id},
        )
    ).scalar_one()
    canonical = [message for message in latest_state["messages"] if message["role"] != "user"]
    assert canonical[0]["role"] == "assistant"
    assert [call["id"] for call in canonical[0]["tool_calls"]] == [
        "read-a-call",
        "read-b-call",
    ]
    assert [message["tool_call_id"] for message in canonical[1:3]] == [
        "read-a-call",
        "read-b-call",
    ]
    assert canonical[3] == {"role": "assistant", "content": "两个来源均已读取。"}


async def test_cowork_recovers_provider_context_overflow_without_mutating_canonical_history(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    for index in range(100):
        (tmp_path / f"{index:03d}-{'x' * 80}.docx").write_bytes(b"x")
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork overflow recovery"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="列出文件后总结",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = OverflowRecoveringCoworkProvider(
        [
            _tool_completion(
                ToolCall(id="list-before-overflow", name="list_office_files", arguments="{}")
            ),
            _final_completion("已列出文件并完成总结。"),
        ],
        overflow_calls={2},
        regular_completions=['{"summary":"已经调用 list_office_files 并取得文件清单。"}'],
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={
                        "cowork_max_steps": 4,
                        # 本用例只验证 provider overflow 恢复；固定较小输出预算，避免
                        # 全局原生交付物 token 上限变化把测试转成 run token 熔断用例。
                        "cowork_decision_max_tokens": 2048,
                        "cowork_compaction_trigger_ratio": 1.0,
                    "run_heartbeat_s": 60.0,
                }
            ),
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.used_calls == 4  # 决策、已发出的 400、摘要、恢复后的决策
    assert provider.tool_calls == 3
    latest_state = (
        await db_session.execute(
            text(
                """
                SELECT state FROM agent_checkpoints
                WHERE run_id = :run_id
                ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
                """
            ),
            {"run_id": run.id},
        )
    ).scalar_one()
    canonical = latest_state["messages"]
    assert [item["role"] for item in canonical] == ["user", "assistant", "tool", "assistant"]
    assert canonical[1]["tool_calls"][0]["id"] == "list-before-overflow"
    assert canonical[2]["tool_call_id"] == "list-before-overflow"
    assert latest_state["compaction"]["summary_upto"] == 3
    recovery_history = provider.tool_histories[-1]
    assert any("cowork_history_summary" in item.content for item in recovery_history)
    assert not any(item.role == "tool" for item in recovery_history)
    events = await list_events(db_session, run_id=run.id, limit=200)
    compacted = [event for event in events if event.type == "context.compacted"]
    assert len(compacted) == 1
    assert compacted[0].payload["reason"] == "provider_overflow"


async def test_cowork_context_overflow_progress_guard_stops_recovery_loop(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    for index in range(100):
        (tmp_path / f"{index:03d}-{'x' * 80}.docx").write_bytes(b"x")
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork overflow guard"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="列出文件后继续",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = OverflowRecoveringCoworkProvider(
        [
            _tool_completion(
                ToolCall(id="list-loop-guard", name="list_office_files", arguments="{}")
            )
        ],
        overflow_calls={2, 3},
        regular_completions=['{"summary":"已经取得文件清单。"}'],
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={
                        "cowork_max_steps": 4,
                        "cowork_decision_max_tokens": 2048,
                        "cowork_compaction_trigger_ratio": 1.0,
                    "cowork_context_overflow_max_recoveries": 2,
                    "run_heartbeat_s": 60.0,
                }
            ),
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert provider.tool_calls == 3
    events = await list_events(db_session, run_id=run.id, limit=200)
    overflow_errors = [
        event
        for event in events
        if event.type == "error" and event.payload.get("code") == "cowork_context_overflow"
    ]
    assert len(overflow_errors) == 1
    assert overflow_errors[0].payload["recoveries"] == 2


def test_default_registry_exposes_risk_and_capability_contract() -> None:
    registry = build_default_cowork_registry()
    catalog = {item["name"]: item for item in registry.catalog()}

    assert catalog["inspect_office_file"]["parallel_safe"] is True
    assert catalog["edit_word"]["capability"] == "office.word.edit"
    assert catalog["edit_word"]["risk"] == "write"
    assert catalog["edit_excel"]["effect"] == "filesystem"
    assert catalog["list_workspace_roots"]["parallel_safe"] is True
    assert catalog["list_files"]["parallel_safe"] is True
    assert catalog["read_text_file"]["capability"] == "filesystem.read"
    assert catalog["search_files"]["effect"] == "none"
    assert catalog["read_pdf"]["parallel_safe"] is True
    assert catalog["fetch_url"]["capability"] == "network.read"
    assert catalog["write_text_file"]["effect"] == "filesystem"
    assert catalog["create_artifact"]["risk"] == "write"
    assert registry.parallel_safe(["list_office_files", "inspect_office_file"]) is True
    assert registry.parallel_safe(["list_files", "search_files", "read_pdf"]) is True
    assert registry.parallel_safe(["inspect_office_file", "edit_word"]) is False
    assert catalog["ask_user"]["execution"] == "interaction"
    assert catalog["request_directory"]["execution"] == "interaction"
    assert catalog["request_capability"]["execution"] == "interaction"


@pytest.mark.parametrize(
    ("interaction_body", "expected_tool_fragment"),
    [
        ({"approved": True, "answer": "按部门"}, "按部门"),
        ({"approved": False}, "基于现有信息自行判断"),
    ],
    ids=("answered", "declined"),
)
async def test_cowork_steering_and_ask_user_pause_resume_with_canonical_tool_history(
    interaction_body: dict[str, object],
    expected_tool_fragment: str,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork interaction"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理材料",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    queue = RecordingCoworkQueue()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(scope="local_owner")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        steering_response = await client.post(
            f"/api/v1/runs/{run.id}/steering",
            json={"message": "先只整理财务相关材料"},
        )
    assert steering_response.status_code == 202

    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="ask-scope",
                    name="ask_user",
                    arguments=json.dumps(
                        {"question": "按月份还是按部门整理？", "choices": ["月份", "部门"]}
                    ),
                )
            ),
            _final_completion("已按部门完成整理。"),
        ]
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    context = {
        "settings": get_settings().model_copy(
            update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }
    await cowork_run(context, str(run.id))

    waiting = await get_run(db_session, run.id)
    assert waiting is not None and waiting.status == "waiting_human"
    assert any(
        message.role == "user" and message.content == "先只整理财务相关材料"
        for message in provider.tool_histories[0]
    )
    events = await list_events(db_session, run_id=run.id, limit=200)
    interrupt = next(event for event in events if event.type == "interrupt")
    assert interrupt.payload["kind"] == "ask_user"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run.id}/interactions/{interrupt.payload['resume_token']}/respond",
            json=interaction_body,
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert queue.run_ids == [run.id]
    assert queue.attempts[0] > 0
    if isinstance(interaction_body.get("answer"), str):
        leaked_interaction_messages = (
            await db_session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE conversation_id = :conversation_id
                      AND run_id = :run_id
                      AND role = 'user'
                      AND content = :answer
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "run_id": run.id,
                    "answer": interaction_body["answer"],
                },
            )
        ).scalar_one()
        assert leaked_interaction_messages == 0

    await cowork_run(context, str(run.id))
    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    resumed_history = provider.tool_histories[1]
    assistant_call = next(message for message in resumed_history if message.tool_calls)
    assert assistant_call.tool_calls[0].id == "ask-scope"
    tool_result = next(
        message for message in resumed_history if message.role == "tool" and message.tool_call_id
    )
    assert tool_result.tool_call_id == "ask-scope"
    assert expected_tool_fragment in tool_result.content
    if interaction_body["approved"] is False:
        assert '"status":"rejected"' in tool_result.content


@pytest.mark.parametrize("tool_name", ["request_directory", "request_capability"])
async def test_cowork_runtime_permission_requests_apply_only_after_user_approval(
    tool_name: str, db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    initial_root = tmp_path / f"initial-{tool_name}"
    initial_root.mkdir()
    extra_root = tmp_path / f"extra-{tool_name}"
    extra_root.mkdir()
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title=f"Cowork {tool_name}"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(initial_root),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="需要额外权限",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    arguments = (
        {"reason": "读取补充材料", "access_mode": "read_only"}
        if tool_name == "request_directory"
        else {"reason": "同步外部系统", "capability": "external.action"}
    )
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id=f"permission-{tool_name}",
                    name=tool_name,
                    arguments=json.dumps(arguments),
                )
            )
        ]
    )
    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
            ),
            "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )
    events = await list_events(db_session, run_id=run.id, limit=200)
    interrupt = next(event for event in events if event.type == "interrupt")
    assert interrupt.payload["kind"] == (
        "directory_request" if tool_name == "request_directory" else "capability_request"
    )

    queue = RecordingCoworkQueue()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(scope="local_owner")
    body: dict[str, object] = {"approved": True}
    if tool_name == "request_directory":
        body["path"] = str(extra_root)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run.id}/interactions/{interrupt.payload['resume_token']}/respond",
            json=body,
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    if tool_name == "request_directory":
        granted = (
            await db_session.execute(
                text(
                    """
                    SELECT access_mode FROM session_roots
                    WHERE conversation_id = :conversation_id AND canonical_path = :path
                    """
                ),
                {"conversation_id": conversation_id, "path": str(extra_root.resolve())},
            )
        ).scalar_one()
        assert granted == "read_only"
    else:
        granted = (
            await db_session.execute(
                text(
                    """
                    SELECT capability FROM capability_grants
                    WHERE conversation_id = :conversation_id
                      AND capability = 'external.action' AND revoked_at IS NULL
                    """
                ),
                {"conversation_id": conversation_id},
            )
        ).scalar_one()
        assert granted == "external.action"


async def test_cowork_invalid_shell_arguments_are_returned_to_model_for_correction(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork shell argument recovery"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="分析工作目录",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="shell-missing-cwd",
                    name="run_shell",
                    arguments=json.dumps({"command": "pwd", "reason": "检查当前目录"}),
                )
            ),
            _final_completion("参数不完整，已改用现有工具完成分析。"),
        ]
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    context = {
        "settings": get_settings().model_copy(
            update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    assert completed.error is None
    assert provider.tool_calls == 2
    correction_history = provider.tool_histories[1]
    tool_result = next(
        message
        for message in correction_history
        if message.role == "tool" and message.tool_call_id == "shell-missing-cwd"
    )
    assert '"ok":false' in tool_result.content
    assert "cwd" in tool_result.content
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert any(
        event.type == "tool.error"
        and event.payload.get("tool") == "run_shell"
        and "cwd" in str(event.payload.get("error"))
        for event in events
    )


async def test_cowork_office_flow_hides_and_rejects_shell(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork Office shell boundary"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理工作空间里的 Word 文档",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="forbidden-office-shell",
                    name="run_shell",
                    arguments=json.dumps({"command": "python3 inspect.py", "cwd": str(tmp_path)}),
                )
            ),
            _final_completion("已改用 Office 专用工具边界处理。"),
        ]
    )
    await cowork_run(
        {
            "settings": get_settings().model_copy(
                update={"cowork_max_steps": 4, "run_heartbeat_s": 60.0}
            ),
            "session_factory": async_sessionmaker(db_engine, expire_on_commit=False),
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    assert all(tool.name != "run_shell" for tool in provider.last_tools)
    correction_history = provider.tool_histories[1]
    shell_result = next(
        message
        for message in correction_history
        if message.role == "tool" and message.tool_call_id == "forbidden-office-shell"
    )
    assert "Office 工作流禁止使用 run_shell" in shell_result.content
    events = await list_events(db_session, run_id=run.id, limit=200)
    assert not any(
        event.type == "interrupt" and event.payload.get("kind") == "shell_approval"
        for event in events
    )


async def test_cowork_shell_requires_once_approval_then_executes_exact_pending_call(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork shell approval"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="shell.execute",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="运行一条诊断命令",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="shell-once",
                    name="run_shell",
                    arguments=json.dumps(
                        {
                            "command": "/usr/bin/printf shell-approved",
                            "cwd": str(tmp_path),
                            "reason": "确认本地命令执行链",
                        }
                    ),
                )
            ),
            _final_completion("诊断命令已完成。"),
        ]
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    context = {
        "settings": get_settings().model_copy(
            update={
                "cowork_max_steps": 4,
                "run_heartbeat_s": 60.0,
                "cowork_shell_allowlist": [],
            }
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }
    await cowork_run(context, str(run.id))
    waiting = await get_run(db_session, run.id)
    assert waiting is not None and waiting.status == "waiting_human"
    events = await list_events(db_session, run_id=run.id, limit=200)
    interrupt = next(event for event in events if event.type == "interrupt")
    assert interrupt.payload["kind"] == "shell_approval"
    assert interrupt.payload["payload"]["command"] == "/usr/bin/printf shell-approved"
    assert interrupt.payload["payload"]["allowlisted"] is False

    queue = RecordingCoworkQueue()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(scope="local_owner")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run.id}/interactions/{interrupt.payload['resume_token']}/respond",
            json={"approved": True},
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"

    await cowork_run(context, str(run.id))
    completed = await get_run(db_session, run.id)
    assert completed is not None and completed.status == "done"
    resumed_history = provider.tool_histories[1]
    tool_result = next(
        message
        for message in resumed_history
        if message.role == "tool" and message.tool_call_id == "shell-once"
    )
    assert "shell-approved" in tool_result.content
    assert '"execution_mode":"argv"' in tool_result.content


async def test_cowork_cancel_terminates_running_allowlisted_shell_process(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork shell cancel"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="shell.execute",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="启动后停止诊断进程",
        budget_tokens=50_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote("__import__('time').sleep(30)")
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="shell-cancel",
                    name="run_shell",
                    arguments=json.dumps(
                        {"command": command, "cwd": str(tmp_path), "reason": "测试停止"}
                    ),
                )
            ),
            _final_completion("不应执行到这里"),
        ]
    )
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    context = {
        "settings": get_settings().model_copy(
            update={
                "cowork_max_steps": 4,
                "run_heartbeat_s": 60.0,
                "cowork_cancel_poll_s": 0.05,
                "cowork_shell_allowlist": [shlex.quote(sys.executable)],
                "cowork_shell_timeout_s": 60,
                "cowork_shell_terminate_grace_s": 0.1,
            }
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }
    started = time.monotonic()
    worker_task = asyncio.create_task(cowork_run(context, str(run.id)))
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        async with session_factory() as session:
            events = await list_events(session, run_id=run.id, limit=200)
        if any(
            event.type == "tool.start" and event.payload.get("tool") == "run_shell"
            for event in events
        ):
            break
        await asyncio.sleep(0.02)
    else:
        worker_task.cancel()
        raise AssertionError("shell 工具未在期限内启动")

    async with session_factory() as session:
        await request_cancel(session, run_id=run.id)
        await session.commit()
    await asyncio.wait_for(worker_task, timeout=5)

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None and refreshed.status == "cancelled"
    assert time.monotonic() - started < 5
    assert provider.tool_calls == 1


async def test_cowork_run_api_initializes_checkpoint_and_enqueues(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(db_session, scope="local_owner", title="Cowork API")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    await db_session.commit()
    queue = RecordingCoworkQueue()
    bus = InMemoryRunBus()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    settings = get_settings().model_copy(
        update={"cowork_default_workspace_path": tmp_path / "WorkPilot"}
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(scope="local_owner")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runs/cowork",
            json={
                "conversation_id": str(conversation_id),
                "goal": "整理目录中的预算表",
            },
        )

    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])
    assert response.json()["workflow_type"] == "cowork"
    assert queue.run_ids == [run_id]
    checkpoint_schema = (
        await db_session.execute(
            text(
                """
                SELECT state->>'schema_version' FROM agent_checkpoints
                WHERE run_id = :run_id ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"run_id": run_id},
        )
    ).scalar_one()
    assert checkpoint_schema == "cowork.v2"


async def test_cowork_run_api_does_not_require_workspace_for_default_permissions(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(db_session, scope="local_owner", title="默认权限")
    await db_session.commit()
    queue = RecordingCoworkQueue()
    bus = InMemoryRunBus()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    default_workspace = tmp_path / "WorkPilot"
    settings = get_settings().model_copy(
        update={"cowork_default_workspace_path": default_workspace}
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(scope="local_owner")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/runs/cowork",
            json={"conversation_id": str(conversation_id), "goal": "解释什么是 RAG"},
        )

    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])
    assert queue.run_ids == [run_id]
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run_id)
    assert checkpoint is not None
    assert checkpoint.state["goal"] == "解释什么是 RAG"
    roots = await list_session_roots(db_session, conversation_id=conversation_id)
    assert roots[0].canonical_path == str(default_workspace)
    assert roots[0].label == "WorkPilot 默认文件夹"


async def test_expired_cowork_run_is_recovered_to_cowork_queue_class(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="Cowork recovery"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="恢复 Cowork",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    await initialize_cowork_state(
        db_session,
        run_id=run.id,
        registry=build_default_cowork_registry(),
    )
    await db_session.execute(
        text(
            """
            UPDATE agent_runs
            SET status = 'executing', worker_id = 'lost-worker',
                lease_until = now() - interval '1 second'
            WHERE id = :run_id
            """
        ),
        {"run_id": run.id},
    )
    await db_session.commit()

    reaped = await reap_expired_runs(db_session)
    await db_session.commit()

    assert reaped.failed == []
    assert reaped.recovered == []
    assert reaped.recovered_cowork == [(run.id, 1)]
