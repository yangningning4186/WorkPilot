import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.core.db import DbSession as AsyncSession
from app.cowork.authorization import arguments_sha256
from app.cowork.memory import (
    forget_memory,
    get_memory,
    list_memories,
    load_visible_memories,
    memory_payload,
    remember,
    render_memory_block,
    resolve_binding,
    update_memory,
)
from app.cowork.memory_tools import register_memory_tools
from app.cowork.permissions import create_session_root
from app.cowork.runtime import _ephemeral_context, _render_memory_block, _system_prompt
from app.cowork.semantic_approvals import build_trusted_approval_evidence
from app.cowork.tools import CoworkToolContext, CoworkToolError, CoworkToolRegistry
from app.cowork_contracts import (
    CoworkMemoryRecord,
    MemoryNotFoundError,
    MemoryScopeError,
)
from app.runstore.checkpoints import ensure_plan
from app.runstore.runs import create_run, ensure_conversation
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def _record(content: str, *, scope: str = "global", forgotten: bool = False) -> CoworkMemoryRecord:
    now = datetime.now(UTC)
    return CoworkMemoryRecord(
        id=uuid4(),
        scope=scope,  # type: ignore[arg-type]
        conversation_id=None,
        workspace_path=None,
        key=None,
        content=content,
        source="agent",
        created_at=now,
        updated_at=now,
        forgotten_at=now if forgotten else None,
    )


def test_scope_binding_is_derived_not_supplied() -> None:
    """作用域和定位字段必须一一对应，写歪了检索就会漏掉或串作用域。"""

    conversation_id = UUID(int=7)
    assert resolve_binding("global", conversation_id=conversation_id, workspace_path="/x") == (
        None,
        None,
    )
    assert resolve_binding(
        "conversation", conversation_id=conversation_id, workspace_path="/x"
    ) == (conversation_id, None)
    assert resolve_binding("workspace", conversation_id=conversation_id, workspace_path="/x") == (
        None,
        "/x",
    )
    with pytest.raises(MemoryScopeError):
        resolve_binding("workspace", conversation_id=conversation_id, workspace_path=None)


def test_memory_writes_are_store_effects_and_never_run_in_plan_mode() -> None:
    registry = CoworkToolRegistry()
    register_memory_tools(registry)

    for name in ("remember", "memory_update"):
        spec = registry.get(name)
        assert (spec.risk, spec.effect) == ("write", "store")
        assert registry.plan_mode_allows(name) is False


def test_memory_block_truncates_long_items_and_indexes_the_overflow() -> None:
    long_one = _record("长" * 500)
    block = render_memory_block([long_one], max_chars=4000, preview_chars=60)

    assert "长" * 60 in block
    assert "长" * 61 not in block
    assert "memory_read" in block
    assert f"[#{long_one.id}]" in block

    # 总长超限时最近内容保持展开，未展开项至少留下数量与可读取的完整 ID；索引本身
    # 也是一行且受总预算约束，不会为了避免静默失忆反过来撑爆上下文。
    many = [_record(f"事实 {index}") for index in range(50)]
    bounded = render_memory_block(many, max_chars=400, preview_chars=240)
    assert len(bounded) <= 400
    lines = bounded.splitlines()
    index = next(line for line in lines if line.startswith("省略"))
    assert "事实 49" in bounded
    assert "[#" in index
    assert "ID 未展开" in index
    assert "\n" not in index
    assert render_memory_block(many, max_chars=80, preview_chars=240) == ""


def test_forgotten_memories_are_never_injected() -> None:
    block = render_memory_block(
        [_record("已经 retire", forgotten=True)], max_chars=4000, preview_chars=240
    )

    assert block == ""
    assert render_memory_block([], max_chars=4000, preview_chars=240) == ""


def test_memory_lives_in_the_stable_prefix_and_todos_do_not() -> None:
    """记忆是 run 内不变的知识，进 system prompt；清单每轮都在变，只能挂末尾。

    分界不是主题而是"这一次 run 里会不会变"：会变的东西放进 system prompt，模型每动一次
    清单就要把整段前缀重新计费。
    """

    block = render_memory_block([_record("用户偏好 Markdown")], max_chars=4000, preview_chars=240)
    prompt = _system_prompt("", memory_block=block)
    tail = _ephemeral_context(mode="execute", todos=[{"content": "生成报告", "status": "pending"}])

    assert "<known_memories>" in prompt
    assert "<current_todos>" not in prompt
    assert "<current_todos>" in tail
    assert "<known_memories>" not in tail
    assert "<known_memories>" not in _system_prompt("")


async def test_keyed_remember_updates_instead_of_piling_up(db_session: AsyncSession) -> None:
    """同 key 的修正必须替换：新旧并存会让模型无从判断哪个还算数。"""

    conversation_id = await ensure_conversation(db_session, title="Memory upsert")

    first, replaced = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好 PDF 报告",
        key="report-format",
    )
    assert replaced is None

    second, previous = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好 Markdown 报告",
        key="report-format",
    )

    assert second.id != first.id
    assert previous is not None
    assert previous.content == "用户偏好 PDF 报告"
    historical = await get_memory(db_session, memory_id=first.id)
    assert historical is not None and historical.invalid_at is not None
    assert historical.superseded_by == second.id
    assert second.key == first.key == "report-format"
    visible = await load_visible_memories(db_session, conversation_id=conversation_id)
    assert [item.content for item in visible] == ["用户偏好 Markdown 报告"]


async def test_concurrent_keyed_remember_serializes_successors_without_losing_history(
    db_session: AsyncSession,
    store_sql,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Concurrent memory upsert")
    first, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="初始偏好",
        key="concurrent-preference",
    )

    await asyncio.gather(
        remember(
            db_session,
            conversation_id=conversation_id,
            scope="global",
            content="并发偏好 A",
            key="concurrent-preference",
        ),
        remember(
            db_session,
            conversation_id=conversation_id,
            scope="global",
            content="并发偏好 B",
            key="concurrent-preference",
        ),
    )

    rows = store_sql(
        """SELECT id, key, content, invalid_at, superseded_by
           FROM cowork_memories
           WHERE key = ?
           ORDER BY created_at, rowid""",
        ("concurrent-preference",),
    )
    assert len(rows) == 3
    assert {row["content"] for row in rows} == {"初始偏好", "并发偏好 A", "并发偏好 B"}
    assert {row["key"] for row in rows} == {"concurrent-preference"}
    active = [row for row in rows if row["invalid_at"] is None]
    history = [row for row in rows if row["invalid_at"] is not None]
    assert len(active) == 1
    assert len(history) == 2
    assert next(row for row in rows if row["id"] == str(first.id))["superseded_by"] is not None

    repeated, previous = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content=active[0]["content"],
        key="concurrent-preference",
    )
    assert str(repeated.id) == active[0]["id"]
    assert previous is None
    assert (
        len(
            store_sql(
                "SELECT id FROM cowork_memories WHERE key = ?",
                ("concurrent-preference",),
            )
        )
        == 3
    )


async def test_memory_recall_kill_switch_skips_runtime_injection(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Recall disabled")
    await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="不应注入",
    )

    block = await _render_memory_block(
        db_session,
        conversation_id=conversation_id,
        settings=get_settings().model_copy(update={"memory_recall_enabled": False}),
    )

    assert block == ""


async def test_forget_is_soft_and_idempotent_and_restore_brings_it_back(
    db_session: AsyncSession,
) -> None:
    """撤销依赖软删除；硬删掉就没有第二次机会了。"""

    conversation_id = await ensure_conversation(db_session, title="Memory forget")
    record, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="临时结论",
    )

    forgotten = await forget_memory(db_session, memory_id=record.id)
    assert forgotten is not None and forgotten.forgotten_at is not None
    # 重复删除是幂等的，不是错误。
    assert await forget_memory(db_session, memory_id=record.id) is None
    assert await load_visible_memories(db_session, conversation_id=conversation_id) == []

    restored, _ = await update_memory(db_session, memory_id=record.id, restore=True)
    assert restored.forgotten_at is None
    assert [
        item.id for item in await load_visible_memories(db_session, conversation_id=conversation_id)
    ] == [record.id]

    with pytest.raises(MemoryNotFoundError):
        await update_memory(db_session, memory_id=uuid4(), content="不存在")


async def test_manual_unkeyed_update_keeps_a_successor_history(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Memory successor")
    original, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="conversation",
        content="旧事实",
    )

    current, previous = await update_memory(
        db_session,
        memory_id=original.id,
        content="新事实",
        actor="model",
    )

    assert current.id != original.id
    assert current.scope == "conversation"
    assert current.conversation_id == conversation_id
    assert current.source == "agent"
    assert previous.id == original.id
    historical = await get_memory(db_session, memory_id=original.id)
    assert historical is not None
    assert historical.invalid_at is not None
    assert historical.superseded_by == current.id
    with pytest.raises(MemoryNotFoundError):
        await update_memory(
            db_session,
            memory_id=original.id,
            content="不能改写已失效历史",
            actor="model",
        )
    assert [
        item.content
        for item in await load_visible_memories(db_session, conversation_id=conversation_id)
    ] == ["新事实"]


async def test_keyed_manual_update_keeps_key_but_versions_the_identity(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Keyed memory update")
    original, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="旧偏好",
        key="answer-style",
    )

    updated, _ = await update_memory(db_session, memory_id=original.id, content="新偏好")

    assert updated.id != original.id
    assert updated.key == "answer-style"
    assert updated.invalid_at is None
    historical = await get_memory(db_session, memory_id=original.id)
    assert historical is not None and historical.invalid_at is not None
    assert historical.superseded_by == updated.id
    assert historical.content == "旧偏好"


async def test_visibility_never_leaks_across_conversations_or_workspaces(
    db_session: AsyncSession, tmp_path
) -> None:
    """作用域漏了就是把无关事实喂给模型，所以按可见性而不是按 id 过滤。"""

    mine = await ensure_conversation(db_session, title="Memory mine")
    theirs = await ensure_conversation(db_session, title="Memory theirs")
    my_root = tmp_path / "mine"
    other_root = tmp_path / "other"
    my_root.mkdir()
    other_root.mkdir()
    await create_session_root(
        db_session,
        conversation_id=mine,
        requested_path=str(my_root),
        access_mode="read_write",
    )

    await remember(db_session, conversation_id=mine, scope="global", content="全局偏好")
    await remember(db_session, conversation_id=mine, scope="conversation", content="本会话笔记")
    await remember(db_session, conversation_id=theirs, scope="conversation", content="别的会话笔记")
    await remember(
        db_session,
        conversation_id=mine,
        scope="workspace",
        content="本目录约定",
        workspace_path=str(my_root.resolve()),
    )
    await remember(
        db_session,
        conversation_id=mine,
        scope="workspace",
        content="别的目录约定",
        workspace_path=str(other_root.resolve()),
    )

    visible = {
        item.content for item in await load_visible_memories(db_session, conversation_id=mine)
    }

    assert visible == {"全局偏好", "本会话笔记", "本目录约定"}


async def test_memory_tools_reject_foreign_conversation_and_workspace_ids(
    db_session: AsyncSession, tmp_path
) -> None:
    mine = await ensure_conversation(db_session, title="Memory tool mine")
    theirs = await ensure_conversation(db_session, title="Memory tool theirs")
    my_root = tmp_path / "mine-tool"
    other_root = tmp_path / "other-tool"
    my_root.mkdir()
    other_root.mkdir()
    await create_session_root(
        db_session,
        conversation_id=mine,
        requested_path=str(my_root),
        access_mode="read_write",
    )
    global_memory, _ = await remember(
        db_session, conversation_id=mine, scope="global", content="全局可见"
    )
    mine_workspace, _ = await remember(
        db_session,
        conversation_id=mine,
        scope="workspace",
        workspace_path=str(my_root.resolve()),
        content="当前目录可见",
    )
    foreign_conversation, _ = await remember(
        db_session,
        conversation_id=theirs,
        scope="conversation",
        content="其他会话私有",
    )
    foreign_workspace, _ = await remember(
        db_session,
        conversation_id=theirs,
        scope="workspace",
        workspace_path=str(other_root.resolve()),
        content="其他目录私有",
    )
    registry = CoworkToolRegistry()
    register_memory_tools(registry)
    run = await create_run(
        db_session,
        conversation_id=mine,
        goal="测试记忆工具作用域",
        budget_tokens=1_000,
        budget_calls=10,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    step_id = uuid4()
    await ensure_plan(
        db_session,
        run_id=run.id,
        steps=[
            {
                "id": str(step_id),
                "idx": 0,
                "description": "测试记忆工具作用域",
                "tool": "memory_forget",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1024),
        settings=get_settings(),
        conversation_id=mine,
        run_id=run.id,
        worker_id="memory-scope-test",
        plan_step_id=step_id,
        tool_call_id="memory-scope-call",
        semantic_approval_signing_key="4" * 64,
    )

    def approved_forget(memory_id: UUID, *, call_id: str) -> CoworkToolContext:
        arguments = {"memory_id": str(memory_id)}
        return replace(
            context,
            tool_call_id=call_id,
            approved_call_ids=frozenset({call_id}),
            approval_evidence={
                call_id: build_trusted_approval_evidence(
                    signing_key="4" * 64,
                    source="user",
                    run_id=run.id,
                    tool_call_id=call_id,
                    tool="memory_forget",
                    arguments_sha256=arguments_sha256(
                        registry.parse_arguments("memory_forget", arguments)
                    ),
                    details={"inbox_id": str(uuid4()), "standing_rule_id": None},
                )
            },
        )

    # global 与当前授权 workspace 正常读取，证明负测不是把所有 ID 工具一起关掉。
    for allowed in (global_memory, mine_workspace):
        result = await registry.execute(
            "memory_read", {"memory_id": str(allowed.id)}, context=context
        )
        assert result.output["memory"]["id"] == str(allowed.id)

    historical, _ = await remember(
        db_session,
        conversation_id=mine,
        scope="global",
        content="已经失效的旧事实",
    )
    await update_memory(db_session, memory_id=historical.id, content="当前事实")
    for tool, arguments in (
        ("memory_read", {"memory_id": str(historical.id)}),
        ("memory_update", {"memory_id": str(historical.id), "content": "篡改历史"}),
    ):
        with pytest.raises(CoworkToolError, match="不存在"):
            await registry.execute(tool, arguments, context=context)
    repeated_forget = await registry.execute(
        "memory_forget",
        {"memory_id": str(historical.id)},
        context=approved_forget(historical.id, call_id="forget-historical"),
    )
    assert repeated_forget.output == {
        "memory_id": str(historical.id),
        "already_forgotten": True,
    }
    unchanged_history = await get_memory(db_session, memory_id=historical.id)
    assert unchanged_history is not None
    assert unchanged_history.content == "已经失效的旧事实"
    assert unchanged_history.invalid_at is not None
    assert unchanged_history.forgotten_at is None

    for foreign in (foreign_conversation, foreign_workspace):
        for tool, arguments in (
            ("memory_read", {"memory_id": str(foreign.id)}),
            ("memory_update", {"memory_id": str(foreign.id), "content": "越权改写"}),
        ):
            with pytest.raises(CoworkToolError, match=r"不存在|不可见"):
                await registry.execute(tool, arguments, context=context)
        with pytest.raises(CoworkToolError, match=r"不存在|不可见"):
            await registry.execute(
                "memory_forget",
                {"memory_id": str(foreign.id)},
                context=approved_forget(foreign.id, call_id=f"forget-{foreign.id}"),
            )
        untouched = await get_memory(db_session, memory_id=foreign.id)
        assert untouched is not None and untouched.content == foreign.content
        assert untouched.active


async def test_list_memories_can_include_forgotten_for_the_panel(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Memory panel")
    record, _ = await remember(
        db_session, conversation_id=conversation_id, scope="global", content="已 retire"
    )
    await forget_memory(db_session, memory_id=record.id)

    active = await list_memories(db_session, conversation_id=conversation_id, workspace_paths=[])
    archived = await list_memories(
        db_session,
        conversation_id=conversation_id,
        workspace_paths=[],
        include_forgotten=True,
    )

    assert active == []
    assert [item.id for item in archived] == [record.id]
    assert memory_payload(archived[0])["forgotten"] is True


async def test_content_length_is_bounded(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session, title="Memory bounds")

    with pytest.raises(MemoryScopeError):
        await remember(db_session, conversation_id=conversation_id, scope="global", content="   ")
    with pytest.raises(MemoryScopeError):
        await remember(
            db_session,
            conversation_id=conversation_id,
            scope="global",
            content="x" * 4001,
        )


async def test_model_memory_tools_never_persist_credentials_or_unproven_sensitive_facts(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Model memory safety")
    existing, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="conversation",
        content="普通的旧偏好",
    )
    registry = CoworkToolRegistry()
    register_memory_tools(registry)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="测试模型记忆安全门禁",
        budget_tokens=1_000,
        budget_calls=10,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    step_id = uuid4()
    await ensure_plan(
        db_session,
        run_id=run.id,
        steps=[
            {
                "id": str(step_id),
                "idx": 0,
                "description": "测试模型记忆安全门禁",
                "tool": "remember",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1024),
        settings=get_settings(),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="model-memory-safety",
        plan_step_id=step_id,
        tool_call_id="model-memory-safety-call",
    )

    rejected = (
        (
            "remember",
            {"scope": "global", "content": "请记住 API key 是 sk-abcdefghijklmnop1234"},
            "credential_or_secret_never_auto_saved",
        ),
        (
            "remember",
            {"scope": "global", "content": "请记住我每天服用二甲双胍"},
            "sensitive_health_or_medical_requires_explicit_memory_consent",
        ),
        (
            "memory_update",
            {"memory_id": str(existing.id), "content": "我的银行账户余额是 100 万"},
            "sensitive_financial_requires_explicit_memory_consent",
        ),
    )
    for tool_name, arguments, reason in rejected:
        with pytest.raises(CoworkToolError, match=reason):
            await registry.execute(tool_name, arguments, context=context)

    visible = await get_memory(db_session, memory_id=existing.id)
    assert visible is not None and visible.content == "普通的旧偏好"
    memories = await list_memories(
        db_session,
        conversation_id=conversation_id,
        workspace_paths=[],
    )
    assert [item.content for item in memories] == ["普通的旧偏好"]
