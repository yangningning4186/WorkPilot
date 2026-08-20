from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.pdf import parse_pdf_in_subprocess
from app.rag.pdf_ingestion import ingest_pdf_file
from app.rag.retrieval.dense import dense_search
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def _write_two_column_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    document.set_metadata({"title": "Two Column Fixture"})
    page.insert_textbox(pymupdf.Rect(50, 40, 550, 80), "Document title", fontsize=16)
    page.insert_textbox(pymupdf.Rect(50, 120, 270, 180), "Left first evidence", fontsize=12)
    page.insert_textbox(pymupdf.Rect(50, 220, 270, 280), "Left second evidence", fontsize=12)
    page.insert_textbox(pymupdf.Rect(330, 120, 550, 180), "Right first evidence", fontsize=12)
    page.insert_textbox(pymupdf.Rect(330, 220, 550, 280), "Right second evidence", fontsize=12)
    document.save(path)
    document.close()


async def test_pdf_subprocess_extracts_columns_offsets_and_locations(tmp_path: Path) -> None:
    path = tmp_path / "columns.pdf"
    _write_two_column_pdf(path)

    parsed = await parse_pdf_in_subprocess(
        path,
        timeout_s=10,
        max_pages=10,
        memory_mb=512,
        cpu_seconds=10,
    )

    assert parsed.title == "Two Column Fixture"
    assert [block.text for block in parsed.document.blocks] == [
        "Document title",
        "Left first evidence",
        "Left second evidence",
        "Right first evidence",
        "Right second evidence",
    ]
    assert parsed.document.page_count == 1
    for block in parsed.document.blocks:
        assert parsed.document.full_text[block.char_start : block.char_end] == block.text
        location = block.locations[0]
        assert location.page_no == 1
        assert location.page_width == 600
        assert location.page_height == 800
        assert location.rotation == 0
        assert location.coord_origin == "top_left"
        assert all(0 <= value <= 1 for value in location.bbox_norm)


async def test_pdf_subprocess_rejects_a_page_without_text(tmp_path: Path) -> None:
    path = tmp_path / "scanned.pdf"
    document = pymupdf.open()
    document.new_page(width=600, height=800)
    document.save(path)
    document.close()

    with pytest.raises(ValueError, match="OCR/MinerU"):
        await parse_pdf_in_subprocess(
            path,
            timeout_s=10,
            max_pages=10,
            memory_mb=512,
            cpu_seconds=10,
        )


async def test_pdf_subprocess_removes_headers_repeated_across_pages(tmp_path: Path) -> None:
    path = tmp_path / "headers.pdf"
    document = pymupdf.open()
    for page_no in range(1, 4):
        page = document.new_page(width=600, height=800)
        page.insert_text((50, 40), "Repeated header", fontsize=10)
        page.insert_text((50, 200), f"Unique body {page_no}", fontsize=12)
    document.save(path)
    document.close()

    parsed = await parse_pdf_in_subprocess(
        path,
        timeout_s=10,
        max_pages=10,
        memory_mb=512,
        cpu_seconds=10,
    )

    texts = [block.text for block in parsed.document.blocks]
    assert "Repeated header" not in texts
    assert texts == ["Unique body 1", "Unique body 2", "Unique body 3"]


@pytest.mark.integration
async def test_pdf_ingestion_persists_bbox_and_returns_it_from_dense_search(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    path = library / "columns.pdf"
    _write_two_column_pdf(path)
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)

    ingested = await ingest_pdf_file(
        db_session,
        gateway,
        path=path,
        library_root=library,
        timeout_s=10,
        max_pages=10,
        memory_mb=512,
        cpu_seconds=10,
    )
    hits = await dense_search(db_session, gateway, query="Right second evidence", top_k=1)
    await db_session.commit()

    assert ingested.activated is True
    assert ingested.block_count == 5
    assert hits[0].blocks
    locations = [location for block in hits[0].blocks for location in block["locations"]]
    assert locations
    assert {location["page_no"] for location in locations} == {1}
    assert all(location["coord_origin"] == "top_left" for location in locations)
    assert all(len(location["bbox_norm"]) == 4 for location in locations)
    assert (
        await db_session.execute(
            text("SELECT page_count FROM document_versions WHERE id=:id"),
            {"id": ingested.version_id},
        )
    ).scalar_one() == 1
