import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pymupdf
import pytest
from uuid6 import uuid7

from app.agent_core.budget import CompletionClient
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork import tools as cowork_tools
from app.cowork.artifacts import list_artifacts
from app.cowork.authorization import arguments_sha256
from app.cowork.permissions import create_session_root
from app.cowork.semantic_approvals import (
    build_semantic_approval_evidence,
    build_trusted_approval_evidence,
)
from app.cowork.tools import (
    CoworkToolCancelledOutcomeUnknownError,
    CoworkToolContext,
    CoworkToolError,
    CoworkToolOutcomeUnknownError,
    build_default_cowork_registry,
)
from app.runstore.checkpoints import ensure_plan
from app.runstore.runs import create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def _plan_step(session: AsyncSession, run_id: UUID, index: int) -> UUID:
    step_id = uuid7()
    await ensure_plan(
        session,
        run_id=run_id,
        steps=[
            {
                "id": str(step_id),
                "idx": index,
                "description": "test tool",
                "tool": "write_file",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    return step_id


async def test_general_tools_create_index_and_reuse_artifact_exactly_once(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="General tools")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="Create report",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    step_id = await _plan_step(db_session, run.id, 0)
    await db_session.commit()

    registry = build_default_cowork_registry()
    context = CoworkToolContext(
        session=db_session,
        gateway=cast("CompletionClient", object()),
        settings=Settings(
            app_env="test",
            cowork_file_read_max_bytes=1024 * 1024,
            cowork_file_write_max_bytes=1024 * 1024,
        ),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="general-tools-worker",
        plan_step_id=step_id,
        tool_call_id="artifact-call",
    )
    arguments = {
        "path": "report.md",
        "content": "# Report\n\nEvidence-backed result.\n",
        "purpose": "artifact",
        "kind": "report",
        "title": "Research report",
    }

    first = await registry.execute("write_file", arguments, context=context)
    replay = await registry.execute("write_file", arguments, context=context)

    assert first.reused is False
    assert replay.reused is True
    assert replay.output["artifact_id"] == first.output["artifact_id"]
    assert first.output["file"]["path"] == str(tmp_path / "report.md")
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Report")
    artifacts = await list_artifacts(db_session, conversation_id=conversation_id)
    assert len(artifacts) == 1
    assert artifacts[0].kind == "report"
    assert artifacts[0].title == "Research report"
    assert artifacts[0].meta["diff"]["available"] is True
    assert artifacts[0].meta["diff"]["created"] is True
    assert "+# Report" in artifacts[0].meta["diff"]["text"]

    read_result = await registry.execute(
        "read_file",
        {"path": str(tmp_path / "report.md"), "max_lines": 10},
        context=context,
    )
    # 行号前缀是给模型引用 path:line 用的，不属于文件内容；这里同时锁住格式与起始行号。
    assert read_result.output["content"].startswith("     1\t# Report")
    assert len(read_result.output["baseline_sha256"]) == 64

    pdf_path = tmp_path / "brief.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "WorkPilot PDF auto detection")
    document.save(pdf_path)
    document.close()
    pdf_result = await registry.execute(
        "read_file",
        {"path": str(pdf_path)},
        context=context,
    )
    assert pdf_result.output["page_count"] == 1
    assert "WorkPilot PDF auto detection" in pdf_result.output["content"]

    search_result = await registry.execute(
        "search_files",
        {"path": str(tmp_path), "query": "Evidence-backed", "pattern": "*.md"},
        context=context,
    )
    assert search_result.output["matches"][0]["relative_path"] == "report.md"


async def test_completed_effect_with_failed_ledger_settlement_is_never_replayed(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Unknown local effect")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="Write once",
        budget_tokens=1_000,
        budget_calls=5,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    step_id = await _plan_step(db_session, run.id, 0)
    await db_session.commit()
    registry = build_default_cowork_registry()
    context = CoworkToolContext(
        session=db_session,
        gateway=cast("CompletionClient", object()),
        settings=Settings(app_env="test"),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="unknown-local-effect-worker",
        plan_step_id=step_id,
        tool_call_id="write-once",
    )
    arguments = {
        "path": "once.txt",
        "content": "one write",
        "purpose": "workspace",
    }

    async def fail_after_effect(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated ledger settlement failure")

    monkeypatch.setattr(cowork_tools, "complete_invocation", fail_after_effect)
    with pytest.raises(CoworkToolOutcomeUnknownError):
        await registry.execute("write_file", arguments, context=context)
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "one write"

    # acquire 在 handler 前拒绝同一 identity；即使结算函数仍故障，也不会再写第二次。
    with pytest.raises(CoworkToolOutcomeUnknownError):
        await registry.execute("write_file", arguments, context=context)


async def test_cancelled_effectful_handler_is_never_replayed(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Cancelled local effect")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="Write once before cancellation",
        budget_tokens=1_000,
        budget_calls=5,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    step_id = await _plan_step(db_session, run.id, 0)
    registry = build_default_cowork_registry()
    context = CoworkToolContext(
        session=db_session,
        gateway=cast("CompletionClient", object()),
        settings=Settings(app_env="test"),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="cancelled-local-effect-worker",
        plan_step_id=step_id,
        tool_call_id="cancelled-write-once",
    )
    arguments = {
        "path": "cancelled-once.txt",
        "content": "one write",
        "purpose": "workspace",
    }
    applied = 0

    async def apply_then_cancel(path: Path, **kwargs: object) -> object:
        nonlocal applied
        del kwargs
        applied += 1
        await asyncio.to_thread(path.write_text, "one write", encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(cowork_tools, "write_text_file", apply_then_cancel)
    with pytest.raises(CoworkToolCancelledOutcomeUnknownError):
        await registry.execute("write_file", arguments, context=context)
    assert applied == 1
    assert (tmp_path / "cancelled-once.txt").read_text(encoding="utf-8") == "one write"

    with pytest.raises(CoworkToolOutcomeUnknownError):
        await registry.execute("write_file", arguments, context=context)
    assert applied == 1


async def test_protected_workspace_and_control_paths_require_non_waivable_human_approval(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Protected workspace paths")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="验证受保护路径",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    step_id = await _plan_step(db_session, run.id, 0)
    await db_session.commit()

    registry = build_default_cowork_registry()
    arguments = {
        "path": ".github/workflows/ci.yml",
        "content": "name: protected\n",
        "create_parents": True,
        "purpose": "workspace",
    }
    canonical = registry.parse_arguments("write_file", arguments)
    base = CoworkToolContext(
        session=db_session,
        gateway=cast("CompletionClient", object()),
        settings=Settings(app_env="test", cowork_data_path=tmp_path / "control-plane"),
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="protected-path-worker",
        plan_step_id=step_id,
        tool_call_id="protected-write",
        semantic_approval_signing_key="1" * 64,
    )

    assert registry.requires_approval_for("write_file", arguments)
    with pytest.raises(CoworkToolError, match="尚未获得本次调用的用户批准"):
        await registry.execute("write_file", arguments, context=base)

    policy_context = replace(
        base,
        approved_call_ids=frozenset({"protected-write"}),
        approval_evidence={
            "protected-write": build_semantic_approval_evidence(
                signing_key="1" * 64,
                run_id=run.id,
                tool_call_id="protected-write",
                tool="write_file",
                arguments_sha256=arguments_sha256(canonical),
                review_receipt_id="2" * 64,
            )
        },
    )
    with pytest.raises(ValueError, match="不可豁免的人工批准"):
        await registry.execute("write_file", arguments, context=policy_context)

    user_context = replace(
        policy_context,
        approval_evidence={
            "protected-write": build_trusted_approval_evidence(
                signing_key="1" * 64,
                source="user",
                run_id=run.id,
                tool_call_id="protected-write",
                tool="write_file",
                arguments_sha256=arguments_sha256(canonical),
                details={"inbox_id": str(UUID(int=99)), "standing_rule_id": None},
            )
        },
    )
    await registry.execute("write_file", arguments, context=user_context)
    assert (tmp_path / ".github/workflows/ci.yml").is_file()

    # 配置可自定义到不含 .workpilot marker 的位置；执行边界仍按 settings 控制根拦截。
    control_arguments = {
        "path": str(tmp_path / "control-plane/cowork.db"),
        "content": "tamper",
        "create_parents": True,
        "purpose": "workspace",
    }
    (tmp_path / "control-plane").mkdir()
    assert not registry.requires_approval_for("write_file", control_arguments)
    preflight_reason = await registry.preflight_human_only_approval_reason(
        "write_file",
        control_arguments,
        session=db_session,
        conversation_id=conversation_id,
        settings=base.settings,
    )
    assert preflight_reason is not None
    assert "受保护控制面" in preflight_reason

    # 相对路径与符号链接都必须按真正授权后的 canonical target 判断，不能只扫参数字符串。
    alias = tmp_path / "control-alias"
    alias.symlink_to(tmp_path / "control-plane", target_is_directory=True)
    alias_reason = await registry.preflight_human_only_approval_reason(
        "write_file",
        {
            "path": "control-alias/cowork.db",
            "content": "tamper",
            "create_parents": True,
            "purpose": "workspace",
        },
        session=db_session,
        conversation_id=conversation_id,
        settings=base.settings,
    )
    assert alias_reason is not None
    assert "受保护控制面" in alias_reason

    with pytest.raises(CoworkToolError, match="尚未获得本次调用的用户批准"):
        await registry.execute(
            "write_file",
            control_arguments,
            context=replace(base, plan_step_id=uuid7(), tool_call_id="control-write"),
        )
