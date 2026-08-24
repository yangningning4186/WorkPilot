"""空转拦截：同一个调用反复做到第 N 次就不再执行。

判据是调用签名而不是工具名——读十个不同文件是正常工作，把同一个文件读十遍不是。
这条边界的价值在评测里是具体的：三条任务因为同一个调用重复二十多次烧完预算，
以 budget_exceeded 收场，而模型在第二次就已经拿到了回答所需的全部信息。
"""

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.permissions import create_session_root
from app.cowork.repetition import (
    DEFAULT_REPEAT_LIMIT,
    DEFAULT_STALL_ROUNDS,
    bump,
    call_signature,
    exhausted_calls,
    normalize_counts,
    parse_arguments,
)
from app.cowork.runtime import (
    _is_idempotent_load_query,
    initialize_cowork_state,
    load_cowork_checkpoint,
)
from app.cowork.tools import build_default_cowork_registry
from app.runstore.runs import append_message, create_run, ensure_conversation, get_run
from app.worker.cowork_run import cowork_run
from tests.test_cowork_runner import (
    NativeToolProvider,
    _final_completion,
    _tool_completion,
)
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import ToolCall

pytestmark = pytest.mark.integration


def test_signature_ignores_key_order_and_whitespace() -> None:
    """同一个调用被模型序列化成不同字符串是常态，不该逃过计数。"""

    left = call_signature("list_files", parse_arguments('{"path": "a", "depth": 1}'))
    right = call_signature("list_files", parse_arguments('{"depth":1,"path":"a"}'))
    assert left == right


def test_signature_separates_different_targets() -> None:
    """读十个不同文件是正常工作，必须和读同一个文件十遍区分开。"""

    first = call_signature("read_text_file", {"path": "a.md"})
    second = call_signature("read_text_file", {"path": "b.md"})
    assert first != second
    # 工具名不同也必须分开，哪怕参数一样。
    assert call_signature("read_text_file", {"path": "a.md"}) != call_signature(
        "search_files", {"path": "a.md"}
    )


def test_unparseable_arguments_do_not_break_counting() -> None:
    """模型偶尔发出非法 JSON；计数器抛异常会直接打断整个 run。"""

    signature = call_signature("run_shell", parse_arguments("{不是 JSON"))
    assert isinstance(signature, str) and signature


def test_limit_counts_repeats_inside_one_batch() -> None:
    """模型有时一口气发五个一模一样的调用，跨轮计数看不住这种。"""

    signature = call_signature("fetch_url", {"url": "https://example.test"})
    spinning = exhausted_calls({}, [signature] * 5, limit=DEFAULT_REPEAT_LIMIT)

    assert spinning == {signature}
    # 前 limit 次仍然放行：第一次拿结果，第二次可能是重试或校验。
    assert exhausted_calls({}, [signature] * DEFAULT_REPEAT_LIMIT) == set()


def test_counts_survive_missing_or_corrupt_checkpoint_field() -> None:
    """老 checkpoint 没有这张表；缺了只该少一层保护，不该让 run 无法恢复。"""

    assert normalize_counts(None) == {}
    assert normalize_counts({"a": 2, "b": "坏值", 7: 1}) == {"a": 2}
    assert bump({"a": 2}, ["a", "c"]) == {"a": 3, "c": 1}


def test_already_loaded_query_is_not_a_repetition_signature() -> None:
    registry = build_default_cowork_registry()
    call = ToolCall(
        id="load-core",
        name="load_tools",
        arguments=json.dumps({"names": ["web_search", "fetch_url"]}),
    )

    assert _is_idempotent_load_query(call, registry) is True


async def test_valid_textual_tool_call_reenters_the_normal_execution_pipeline(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    (tmp_path / "recovered.md").write_text("已恢复", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="正文调用恢复")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="列出文件",
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
    await db_session.commit()

    registry = build_default_cowork_registry()
    bus = InMemoryRunBus()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _final_completion(
                "查看目录。<tool_call><function=list_files>"
                f"<parameter=path>{tmp_path}</parameter>"
                "</function></tool_call>"
            ),
            _final_completion("目录中有 recovered.md。"),
        ]
    )

    await cowork_run(
        {
            "settings": get_settings().model_copy(update={"run_heartbeat_s": 60.0}),
            "session_factory": session_factory,
            "bus": bus,
            "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
            "cowork_registry": registry,
        },
        str(run.id),
    )

    finished = await get_run(db_session, run.id)
    assert finished is not None and finished.status == "done"
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    assert any(
        message.get("role") == "tool" and "recovered.md" in str(message.get("content"))
        for message in checkpoint.state["messages"]
    )


async def test_repeated_call_is_refused_with_an_actionable_instruction(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    """第四次同样的调用不再执行，模型收到的是可执行的纠正指令而不是又一份相同结果。"""

    (tmp_path / "notes.md").write_text("内容", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="空转")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="看看有哪些文件",
        budget_tokens=200_000,
        budget_calls=60,
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
    await db_session.commit()

    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    arguments = json.dumps({"path": str(tmp_path)}, ensure_ascii=False)
    provider = NativeToolProvider(
        [
            *(
                _tool_completion(
                    ToolCall(id=f"list-{index}", name="list_files", arguments=arguments)
                )
                for index in range(DEFAULT_REPEAT_LIMIT + 1)
            ),
            _final_completion("目录里只有 notes.md。"),
        ]
    )
    context = {
        "settings": get_settings().model_copy(update={"run_heartbeat_s": 60.0}),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    done = await get_run(db_session, run.id)
    assert done is not None and done.status == "done"

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    tool_messages = [
        message for message in checkpoint.state["messages"] if message.get("role") == "tool"
    ]
    refused = [message for message in tool_messages if "已经执行过" in str(message["content"])]
    # 前 limit 次照常执行，之后的被拦下。
    assert len(tool_messages) - len(refused) == DEFAULT_REPEAT_LIMIT
    assert refused, "重复调用必须被拦下"
    payload = json.loads(str(refused[0]["content"]))
    assert payload["ok"] is False
    # 面向模型的错误必须是可执行指令：说清事实，并给出出路。
    assert "本次未执行" in payload["error"]
    assert "直接回答用户" in payload["error"]


async def test_mixed_batch_keeps_the_call_that_still_makes_progress(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    """整批拒绝会把一次空转放大成一轮空转，同批里有进展的调用必须照常执行。"""

    (tmp_path / "a.md").write_text("甲", encoding="utf-8")
    (tmp_path / "b.md").write_text("乙", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="混批")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="读这两个文件",
        budget_tokens=200_000,
        budget_calls=60,
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
    await db_session.commit()

    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    repeated = json.dumps({"path": str(tmp_path / "a.md")}, ensure_ascii=False)
    fresh = json.dumps({"path": str(tmp_path / "b.md")}, ensure_ascii=False)
    provider = NativeToolProvider(
        [
            *(
                _tool_completion(
                    ToolCall(id=f"a-{index}", name="read_text_file", arguments=repeated)
                )
                for index in range(DEFAULT_REPEAT_LIMIT)
            ),
            # 第四次读 a.md 会被拦，同一批里的 b.md 必须照常读到。
            _tool_completion(
                ToolCall(id="a-last", name="read_text_file", arguments=repeated),
                ToolCall(id="b-1", name="read_text_file", arguments=fresh),
            ),
            _final_completion("甲和乙都读到了。"),
        ]
    )
    context = {
        "settings": get_settings().model_copy(update={"run_heartbeat_s": 60.0}),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    by_id = {
        str(message.get("tool_call_id")): str(message["content"])
        for message in checkpoint.state["messages"]
        if message.get("role") == "tool"
    }
    assert "已经执行过" in by_id["a-last"]
    assert "乙" in by_id["b-1"]
    # 被拦的那次不该计进签名表，否则一次拦截会把计数推得越来越高。
    signature = call_signature("read_text_file", {"path": str(tmp_path / "a.md")})
    assert checkpoint.state["call_signatures"][signature] == DEFAULT_REPEAT_LIMIT
    assert UUID(checkpoint.state["run_id"]) == run.id


async def test_stalling_takes_the_tools_away_instead_of_burning_the_budget(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    """拒绝拦不住模型：它会一次次重发到预算熔断。真正的刹车是收回工具。

    评测里 cowork-core-039 就是这样死的——收到 22 次"这个调用已经做过"之后仍在重发，
    最后以 budget_exceeded 收场，用户拿到一句系统报错而不是答案。
    """

    (tmp_path / "notes.md").write_text("内容", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="空转熔断")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="看看有哪些文件",
        budget_tokens=200_000,
        budget_calls=60,
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
    await db_session.commit()

    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)

    arguments = json.dumps({"path": str(tmp_path)}, ensure_ascii=False)
    # 模型固执地重发同一个调用；到第 limit + stall 轮必须被强制收尾。
    provider = NativeToolProvider(
        [
            *(
                _tool_completion(
                    ToolCall(id=f"list-{index}", name="list_files", arguments=arguments)
                )
                for index in range(DEFAULT_REPEAT_LIMIT + DEFAULT_STALL_ROUNDS)
            ),
            _final_completion("我一直在重复列目录，没有新进展；目录里只有 notes.md。"),
        ]
    )
    context = {
        "settings": get_settings().model_copy(update={"run_heartbeat_s": 60.0}),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    done = await get_run(db_session, run.id)
    # 关键：以 done 收场并交付了一段正文，而不是 budget_exceeded。
    assert done is not None and done.status == "done"

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    assert checkpoint.state["final_message"].strip()
    stall_prompt = [
        message
        for message in checkpoint.state["messages"]
        if message.get("role") == "user" and "工具已经全部收回" in str(message["content"])
    ]
    assert stall_prompt, "必须显式告诉模型工具已被收回"


async def test_textual_tool_call_after_stall_becomes_safe_fallback_without_execution(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path
) -> None:
    """工具已收回后，模型把伪调用写进正文也不能形成“已完成”的假成功。"""

    (tmp_path / "notes.md").write_text("内容", encoding="utf-8")
    conversation_id = await ensure_conversation(db_session, title="伪工具调用")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="看看有哪些文件",
        budget_tokens=200_000,
        budget_calls=60,
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
    await db_session.commit()

    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    arguments = json.dumps({"path": str(tmp_path)}, ensure_ascii=False)
    provider = NativeToolProvider(
        [
            *(
                _tool_completion(
                    ToolCall(id=f"list-{index}", name="list_files", arguments=arguments)
                )
                for index in range(DEFAULT_REPEAT_LIMIT + DEFAULT_STALL_ROUNDS)
            ),
        ],
        regular_completions=[
            "我再检查一次。\n<tool_call>\n<function=list_files>\n</function>\n</tool_call>"
        ],
    )
    context = {
        "settings": get_settings().model_copy(update={"run_heartbeat_s": 60.0}),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }

    await cowork_run(context, str(run.id))

    finished = await get_run(db_session, run.id)
    assert finished is not None and finished.status == "done"
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    assert "<tool_call>" not in checkpoint.state["final_message"]
    assert "没有执行该调用" in checkpoint.state["final_message"]
