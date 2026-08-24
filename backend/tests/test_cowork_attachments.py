from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.attachments import (
    CoworkAttachmentError,
    bind_attachments,
    store_attachment,
)
from app.cowork.runtime import initialize_cowork_state, load_cowork_checkpoint
from app.cowork.tools import build_default_cowork_registry
from app.runstore.runs import append_message, create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def test_attachment_is_private_bound_and_loaded_into_canonical_message(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(db_session, title="附件测试")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        app_env="test",
        cowork_attachment_path=tmp_path / "private-attachments",
    )
    attachment = await store_attachment(
        db_session,
        conversation_id=conversation_id,
        filename="notes.md",
        declared_media_type="text/markdown",
        raw="附件正文".encode(),
        settings=settings,
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="总结附件",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    message_id = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    await bind_attachments(
        db_session,
        conversation_id=conversation_id,
        attachment_ids=[attachment.id],
        message_id=message_id,
        run_id=run.id,
        max_count=8,
    )
    await initialize_cowork_state(
        db_session,
        run_id=run.id,
        registry=build_default_cowork_registry(),
    )

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None
    canonical = checkpoint.state["messages"][-1]["attachments"][0]
    assert canonical["filename"] == "notes.md"
    assert canonical["extracted_text"] == "附件正文"
    assert Path(canonical["path"]).is_relative_to(tmp_path / "private-attachments")
    assert not Path(canonical["path"]).is_relative_to(workspace)


async def test_attachment_rejects_spoofed_and_reused_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await ensure_conversation(db_session, title="附件边界")
    settings = Settings(
        app_env="test",
        cowork_attachment_path=tmp_path / "attachments",
    )
    with pytest.raises(CoworkAttachmentError, match="只支持"):
        await store_attachment(
            db_session,
            conversation_id=conversation_id,
            filename="fake.png",
            declared_media_type="image/png",
            raw=b"not-an-image",
            settings=settings,
        )

    attachment = await store_attachment(
        db_session,
        conversation_id=conversation_id,
        filename="a.txt",
        declared_media_type="text/plain",
        raw=b"hello",
        settings=settings,
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="read",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    message_id = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    await bind_attachments(
        db_session,
        conversation_id=conversation_id,
        attachment_ids=[attachment.id],
        message_id=message_id,
        run_id=run.id,
        max_count=8,
    )
    with pytest.raises(CoworkAttachmentError, match="已被使用"):
        await bind_attachments(
            db_session,
            conversation_id=conversation_id,
            attachment_ids=[attachment.id],
            message_id=message_id,
            run_id=run.id,
            max_count=8,
        )
