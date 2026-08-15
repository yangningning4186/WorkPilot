from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.gateway import ModelGateway
from app.services.chunk_building import build_chunk_strategies
from app.services.markdown_ingestion import ingest_markdown_file
from tests.fakes import DeterministicProvider


@pytest.mark.integration
async def test_offline_chunk_build_coexists_with_heading_and_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    document = library / "strategies.md"
    document.write_text(
        "# Chunk strategies\n\n"
        + " ".join(f"alpha{index}" for index in range(620))
        + "\n\n| key | value |\n| --- | --- |\n| unicode | café 你好 👨‍👩‍👧‍👦 |\n\n"
        + "Beta topic starts here. Beta details continue.\n",
        encoding="utf-8",
    )
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    ingested = await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("strategies.md"),
        library_root=library,
    )

    first = await build_chunk_strategies(
        db_session,
        gateway,
        version_id=ingested.version_id,
    )
    calls_after_first = provider.embed_calls
    first_rows = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT id, strategy, chunk_index, content, content_tokens,
                           block_start_idx, block_end_idx, char_start, char_end,
                           build_signature, is_searchable
                    FROM chunks WHERE version_id=:version_id
                    ORDER BY strategy, chunk_index
                    """
                ),
                {"version_id": ingested.version_id},
            )
        )
        .mappings()
        .all()
    )
    second = await build_chunk_strategies(
        db_session,
        gateway,
        version_id=ingested.version_id,
    )
    second_rows = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT id, strategy, chunk_index, content, content_tokens,
                           block_start_idx, block_end_idx, char_start, char_end,
                           build_signature, is_searchable
                    FROM chunks WHERE version_id=:version_id
                    ORDER BY strategy, chunk_index
                    """
                ),
                {"version_id": ingested.version_id},
            )
        )
        .mappings()
        .all()
    )
    version = (
        (
            await db_session.execute(
                text("SELECT full_text FROM document_versions WHERE id=:version_id"),
                {"version_id": ingested.version_id},
            )
        )
        .mappings()
        .one()
    )
    blocks = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT block_idx, char_start, char_end FROM parsed_blocks
                    WHERE version_id=:version_id ORDER BY block_idx
                    """
                ),
                {"version_id": ingested.version_id},
            )
        )
        .mappings()
        .all()
    )

    assert {row["strategy"] for row in first_rows} == {
        "fixed",
        "heading",
        "recursive",
        "semantic",
    }
    assert all(item.rebuilt for item in first.strategies)
    assert all(not item.rebuilt for item in second.strategies)
    assert provider.embed_calls == calls_after_first
    assert first_rows == second_rows
    assert all(row["is_searchable"] for row in first_rows)
    assert all(
        row["build_signature"] is not None
        for row in first_rows
        if row["strategy"] != "heading"
    )
    assert all(
        row["build_signature"] is None
        for row in first_rows
        if row["strategy"] == "heading"
    )
    for row in first_rows:
        assert version["full_text"][row["char_start"] : row["char_end"]] == row["content"]
        intersecting = [
            block
            for block in blocks
            if block["char_start"] < row["char_end"]
            and block["char_end"] > row["char_start"]
        ]
        assert row["block_start_idx"] == intersecting[0]["block_idx"]
        assert row["block_end_idx"] == intersecting[-1]["block_idx"]
