from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.rag.editor import (
    DocumentConflictError,
    apply_document_content,
    get_editable_document,
    propose_document_edit,
)
from app.rag.local_dir import register_local_dir
from app.rag.markdown_ingestion import ingest_markdown_file
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway

pytestmark = pytest.mark.integration


async def _editable_fixture(
    session: AsyncSession, tmp_path: Path, *, completion: str = '{"replacement":"第二版"}'
) -> tuple[Path, Settings, ModelGateway, UUID]:
    library = tmp_path / "library"
    library.mkdir()
    note = library / "note.md"
    note.write_text("# Note\n\n第一版", encoding="utf-8")
    settings = Settings.model_validate({"local_library_path": library})
    source = await register_local_dir(
        session, requested_root=Path("."), allowed_root=library, name="fixture"
    )
    gateway = ModelGateway(
        DeterministicProvider(completion_text=completion), embedding_dimensions=1024
    )
    ingested = await ingest_markdown_file(
        session,
        gateway,
        path=note,
        library_root=library,
        source_id=source.id,
    )
    return note, settings, gateway, ingested.document_id


async def test_instruction_proposal_applies_to_real_file_and_activates_version(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    note, settings, gateway, document_id = await _editable_fixture(db_session, tmp_path)
    loaded = await get_editable_document(
        db_session, document_id=document_id, settings=settings
    )
    start = loaded.content.index("第一版")
    proposal = await propose_document_edit(
        db_session,
        gateway,
        document_id=document_id,
        baseline_sha256=loaded.baseline_sha256,
        content=loaded.content,
        instruction="改成第二版",
        selection_start=start,
        selection_end=start + len("第一版"),
        settings=settings,
    )
    result = await apply_document_content(
        db_session,
        gateway,
        document_id=document_id,
        baseline_sha256=loaded.baseline_sha256,
        content=proposal.proposed_content,
        settings=settings,
    )

    assert proposal.original_text == "第一版"
    assert proposal.replacement_text == "第二版"
    assert note.read_text(encoding="utf-8") == "# Note\n\n第二版"
    assert result.indexed is True
    assert result.version_no == 2
    active = (
        await db_session.execute(
            text(
                """
                SELECT v.full_text
                FROM document_versions v
                WHERE v.document_id=:document_id
                  AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                """
            ),
            {"document_id": document_id},
        )
    ).scalar_one()
    assert active == "# Note\n\n第二版"


async def test_apply_refuses_to_overwrite_external_change(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    note, settings, gateway, document_id = await _editable_fixture(db_session, tmp_path)
    loaded = await get_editable_document(
        db_session, document_id=document_id, settings=settings
    )
    note.write_text("# Note\n\nObsidian 外部修改", encoding="utf-8")

    with pytest.raises(DocumentConflictError):
        await apply_document_content(
            db_session,
            gateway,
            document_id=document_id,
            baseline_sha256=loaded.baseline_sha256,
            content="# Note\n\nWorkPilot 修改",
            settings=settings,
        )

    assert note.read_text(encoding="utf-8") == "# Note\n\nObsidian 外部修改"


async def test_empty_selection_means_whole_document(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _, settings, gateway, document_id = await _editable_fixture(
        db_session,
        tmp_path,
        completion='{"replacement":"# Note\\n\\n全文已整理"}',
    )
    loaded = await get_editable_document(
        db_session, document_id=document_id, settings=settings
    )

    proposal = await propose_document_edit(
        db_session,
        gateway,
        document_id=document_id,
        baseline_sha256=loaded.baseline_sha256,
        content=loaded.content,
        instruction="整理全文",
        selection_start=0,
        selection_end=0,
        settings=settings,
    )

    assert proposal.selection_start == 0
    assert proposal.selection_end == len(loaded.content)
    assert proposal.proposed_content == "# Note\n\n全文已整理"
