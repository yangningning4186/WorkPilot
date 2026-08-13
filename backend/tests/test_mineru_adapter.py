from pathlib import Path

import pytest

from app.ingest.mineru import MineruParseError, PageSpec, decode_mineru_content
from app.ingest.pdf import ParsedPdf, PdfParserConfig, RoutedPdfParser
from app.ingest.pdf_quality import PdfSourceAnalysis, assess_pdf_quality
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument


def _baseline(*, images: int = 0) -> ParsedPdf:
    text = "Simple source text with enough characters for a healthy PDF quality baseline."
    document = ParsedDocument(
        full_text=text,
        blocks=[
            ParsedBlock(
                block_idx=0,
                block_type="paragraph",
                text=text,
                char_start=0,
                char_end=len(text),
                heading_path=(),
                locations=(
                    BlockLocation(
                        page_no=1,
                        page_width=600,
                        page_height=800,
                        rotation=0,
                        coord_origin="top_left",
                        bbox_norm=(0.1, 0.1, 0.9, 0.2),
                    ),
                ),
            )
        ],
        page_count=1,
    )
    return ParsedPdf(
        title="Simple",
        parser="pymupdf",
        parser_version="1",
        backend="pymupdf-text",
        document=document,
        quality=assess_pdf_quality(document, PdfSourceAnalysis(image_count=images)),
    )


def _config(*, mode: str = "auto", fallback: bool = True) -> PdfParserConfig:
    return PdfParserConfig(  # type: ignore[arg-type]
        mode=mode,
        timeout_s=10,
        max_pages=10,
        memory_mb=512,
        cpu_seconds=10,
        mineru_command=Path("mineru"),
        mineru_revision="3.4.4",
        mineru_backend="hybrid-engine",
        mineru_effort="medium",
        mineru_method="auto",
        mineru_timeout_s=20,
        mineru_fallback_enabled=fallback,
        mineru_processing_window_size=1,
    )


def test_decode_mineru_content_preserves_structure_and_bbox() -> None:
    items: list[object] = [
        {
            "type": "text",
            "text": "Paper title",
            "text_level": 1,
            "bbox": [100, 50, 900, 100],
            "page_idx": 0,
        },
        {
            "type": "text",
            "text": "Evidence paragraph",
            "bbox": [100, 120, 900, 240],
            "page_idx": 0,
        },
        {
            "type": "equation",
            "text": "$$E=mc^2$$",
            "bbox": [300, 260, 700, 320],
            "page_idx": 0,
        },
        {
            "type": "table",
            "table_caption": ["Table 1"],
            "table_body": "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>",
            "bbox": [100, 350, 900, 600],
            "page_idx": 0,
        },
        {
            "type": "list",
            "list_items": ["first", "second"],
            "bbox": [100, 620, 500, 700],
            "page_idx": 0,
        },
        {
            "type": "image",
            "image_caption": ["Figure 1 Caption"],
            "bbox": [500, 620, 900, 800],
            "page_idx": 0,
        },
        {
            "type": "header",
            "text": "discard me",
            "bbox": [0, 0, 1000, 30],
            "page_idx": 0,
        },
    ]

    document, title, source = decode_mineru_content(
        items,
        page_specs=[PageSpec(width=600, height=800, rotation=0)],
        metadata_title="metadata",
    )

    assert title == "Paper title"
    assert [block.block_type for block in document.blocks] == [
        "title",
        "paragraph",
        "formula",
        "table",
        "list",
        "figure_caption",
    ]
    assert "| A | B |" in document.blocks[3].text
    assert "| 1 | 2 |" in document.blocks[3].text
    assert document.blocks[1].heading_path == ("Paper title",)
    assert document.blocks[0].locations[0].bbox_norm == (0.1, 0.05, 0.9, 0.1)
    assert source.image_count == 1
    assert "discard me" not in document.full_text
    for block in document.blocks:
        assert document.full_text[block.char_start : block.char_end] == block.text


def test_decode_mineru_content_rejects_missing_bbox() -> None:
    with pytest.raises(MineruParseError, match="bbox"):
        decode_mineru_content(
            [{"type": "text", "text": "missing", "page_idx": 0}],
            page_specs=[PageSpec(width=600, height=800, rotation=0)],
            metadata_title="",
        )


def test_decode_mineru_content_enriches_cross_page_locations_from_v2() -> None:
    text = "First page fragment followed by the second page fragment."
    document, _, _ = decode_mineru_content(
        [
            {
                "type": "text",
                "text": text,
                "bbox": [100, 800, 900, 950],
                "page_idx": 0,
            }
        ],
        page_specs=[
            PageSpec(width=600, height=800, rotation=0),
            PageSpec(width=600, height=800, rotation=0),
        ],
        metadata_title="",
        v2_content=[
            [
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [{"type": "text", "content": "First page fragment"}]
                    },
                    "bbox": [100, 800, 900, 950],
                }
            ],
            [
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "the second page fragment"}
                        ]
                    },
                    "bbox": [100, 50, 900, 180],
                }
            ],
        ],
    )

    assert [location.page_no for location in document.blocks[0].locations] == [1, 2]


async def test_auto_parser_records_mineru_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = RoutedPdfParser(_config())
    baseline = _baseline(images=1)

    async def parse_baseline(path: Path) -> ParsedPdf:
        del path
        return baseline

    async def fail_mineru(path: Path) -> ParsedPdf:
        del path
        raise MineruParseError("model unavailable")

    monkeypatch.setattr(parser.pymupdf, "parse", parse_baseline)
    monkeypatch.setattr(parser.mineru, "parse", fail_mineru)

    parsed = await parser.parse(Path("fixture.pdf"))

    assert parsed.parser == "pymupdf"
    assert parsed.fallback_reason is not None
    assert "model unavailable" in parsed.fallback_reason
    assert "embedded_images" in parsed.selection_reasons


async def test_mineru_mode_can_disable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = RoutedPdfParser(_config(mode="mineru", fallback=False))

    async def fail_mineru(path: Path) -> ParsedPdf:
        del path
        raise MineruParseError("model unavailable")

    monkeypatch.setattr(parser.mineru, "parse", fail_mineru)

    with pytest.raises(ValueError, match="model unavailable"):
        await parser.parse(Path("fixture.pdf"))


async def test_auto_parser_falls_back_when_mineru_loses_most_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = RoutedPdfParser(_config())
    baseline = _baseline(images=1)
    short = _baseline()
    short_text = "tiny"
    short_document = ParsedDocument(
        full_text=short_text,
        blocks=[
            ParsedBlock(
                block_idx=0,
                block_type="paragraph",
                text=short_text,
                char_start=0,
                char_end=len(short_text),
                heading_path=(),
                locations=short.document.blocks[0].locations,
            )
        ],
        page_count=1,
    )
    short = ParsedPdf(
        title="Short",
        parser="mineru",
        parser_version="3.4.4",
        backend="hybrid",
        document=short_document,
        quality=assess_pdf_quality(short_document),
    )

    async def parse_baseline(path: Path) -> ParsedPdf:
        del path
        return baseline

    async def parse_short(path: Path) -> ParsedPdf:
        del path
        return short

    monkeypatch.setattr(parser.pymupdf, "parse", parse_baseline)
    monkeypatch.setattr(parser.mineru, "parse", parse_short)

    parsed = await parser.parse(Path("fixture.pdf"))

    assert parsed.parser == "pymupdf"
    assert parsed.fallback_reason is not None
    assert "文本保留率过低" in parsed.fallback_reason
