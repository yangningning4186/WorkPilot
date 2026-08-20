from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.agent_core.budget import CompletionClient
from app.core.config import Settings
from app.cowork.artifacts import list_artifacts
from app.cowork.permissions import create_session_root
from app.cowork.tools import CoworkToolContext, build_default_cowork_registry
from app.runstore.runs import create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def _plan_step(session: AsyncSession, run_id: UUID, index: int) -> UUID:
    step_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO agent_plan_steps
                (id, run_id, step_idx, description, tool, depends_on, status)
            VALUES (:id, :run_id, :idx, 'test tool', 'create_artifact', '{}', 'running')
            """
        ),
        {"id": step_id, "run_id": run_id, "idx": index},
    )
    return step_id


async def test_general_tools_create_index_and_reuse_artifact_exactly_once(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="General tools"
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
    roots_result = await registry.execute("list_workspace_roots", {}, context=context)
    assert roots_result.output["has_workspace"] is True
    assert len(roots_result.output["roots"]) == 1
    root = roots_result.output["roots"][0]
    assert UUID(root["id"])
    assert root["label"] == tmp_path.name
    assert root["path"] == str(tmp_path)
    assert root["access_mode"] == "read_write"
    arguments = {
        "path": "report.md",
        "content": "# Report\n\nEvidence-backed result.\n",
        "kind": "report",
        "title": "Research report",
    }

    first = await registry.execute("create_artifact", arguments, context=context)
    replay = await registry.execute("create_artifact", arguments, context=context)

    assert first.reused is False
    assert replay.reused is True
    assert replay.output["artifact_id"] == first.output["artifact_id"]
    assert first.output["file"]["path"] == str(tmp_path / "report.md")
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Report")
    artifacts = await list_artifacts(db_session, conversation_id=conversation_id)
    assert len(artifacts) == 1
    assert artifacts[0].kind == "report"
    assert artifacts[0].title == "Research report"

    read_result = await registry.execute(
        "read_text_file",
        {"path": str(tmp_path / "report.md"), "max_lines": 10},
        context=context,
    )
    assert read_result.output["content"].startswith("# Report")
    assert len(read_result.output["baseline_sha256"]) == 64

    search_result = await registry.execute(
        "search_files",
        {"path": str(tmp_path), "query": "Evidence-backed", "pattern": "*.md"},
        context=context,
    )
    assert search_result.output["matches"][0]["relative_path"] == "report.md"
