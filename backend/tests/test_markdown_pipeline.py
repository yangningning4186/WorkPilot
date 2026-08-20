from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.chunking import chunk_by_heading
from app.ingest.markdown import parse_markdown
from app.knowledge_contracts import LibraryPathError
from app.rag.markdown_ingestion import ingest_markdown_file
from app.rag.retrieval.dense import dense_search
from app.telemetry.llm_calls import SqlLlmCallAudit
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def test_markdown_blocks_keep_exact_offsets_and_heading_paths() -> None:
    content = "# 总览\r\n\r\n第一段。\r\n\r\n## 细节\r\n\r\n- A\r\n- B\r\n"
    parsed = parse_markdown(content)

    assert [block.block_type for block in parsed.blocks] == ["title", "paragraph", "title", "list"]
    assert parsed.blocks[-1].heading_path == ("总览", "细节")
    assert all(
        parsed.full_text[block.char_start : block.char_end] == block.text for block in parsed.blocks
    )

    chunks = chunk_by_heading(parsed)
    assert len(chunks) == 2
    assert chunks[1].content.startswith("## 细节")


@pytest.mark.integration
async def test_markdown_to_dense_search_minimum_chain(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    document = library / "retrieval.md"
    document.write_text(
        "# 检索系统\n\n## 稀疏检索\n\nBM25 依赖词项匹配。\n\n"
        "## 稠密检索\n\n稠密检索通过向量相似度召回语义相关内容。\n",
        encoding="utf-8",
    )
    gateway = ModelGateway(
        DeterministicProvider(),
        embedding_dimensions=1024,
        audit_sink=SqlLlmCallAudit(db_session),
    )

    ingested = await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("retrieval.md"),
        library_root=library,
    )
    hits = await dense_search(
        db_session,
        gateway,
        query="稠密检索",
        top_k=2,
    )
    await db_session.commit()

    assert ingested.activated is True
    assert ingested.chunk_count == 2
    assert hits[0].heading_path == ["检索系统", "稠密检索"]
    assert "向量相似度" in hits[0].content
    assert hits[0].blocks
    assert all("block_id" in block for block in hits[0].blocks)
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM llm_calls WHERE task_type LIKE '%embedding'")
        )
    ).scalar_one() == 2


@pytest.mark.integration
async def test_ingestion_rejects_real_path_outside_library(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# outside", encoding="utf-8")
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)

    with pytest.raises(LibraryPathError):
        await ingest_markdown_file(
            db_session,
            gateway,
            path=outside,
            library_root=library,
        )


@pytest.mark.integration
async def test_embedding_revision_change_reindexes_unchanged_markdown(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    document = library / "revision.md"
    document.write_text("# Revision\n\nSame content.", encoding="utf-8")

    revision_one = ModelGateway(
        DeterministicProvider(), embedding_dimensions=1024, embedding_revision="fixture-v1"
    )
    first = await ingest_markdown_file(
        db_session, revision_one, path=Path("revision.md"), library_root=library
    )
    unchanged = await ingest_markdown_file(
        db_session, revision_one, path=Path("revision.md"), library_root=library
    )
    revision_two = ModelGateway(
        DeterministicProvider(), embedding_dimensions=1024, embedding_revision="fixture-v2"
    )
    rebuilt = await ingest_markdown_file(
        db_session, revision_two, path=Path("revision.md"), library_root=library
    )

    identities = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT v.version_no, v.embedding_revision,
                           c.embedding_revision AS chunk_embedding_revision,
                           c.is_searchable
                    FROM document_versions v
                    JOIN chunks c ON c.version_id = v.id
                    WHERE v.document_id = :document_id
                    ORDER BY v.version_no
                    """
                ),
                {"document_id": first.document_id},
            )
        )
        .mappings()
        .all()
    )

    assert unchanged.unchanged is True
    assert rebuilt.unchanged is False
    assert rebuilt.version_no == 2
    assert [row["embedding_revision"] for row in identities] == ["fixture-v1", "fixture-v2"]
    assert [row["chunk_embedding_revision"] for row in identities] == [
        "fixture-v1",
        "fixture-v2",
    ]
    assert [row["is_searchable"] for row in identities] == [False, True]
