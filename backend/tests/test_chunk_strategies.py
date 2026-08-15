from app.ingest.chunk_strategies import (
    chunk_fixed,
    chunk_recursive,
    chunk_semantic,
    count_tokens,
    semantic_units,
    tokenize_with_spans,
)
from app.ingest.markdown import parse_markdown
from app.ingest.types import ParsedBlock, ParsedDocument


def _single_block(text: str, *, block_type: str = "paragraph") -> ParsedDocument:
    return ParsedDocument(
        full_text=text,
        blocks=[
            ParsedBlock(
                block_idx=0,
                block_type=block_type,
                text=text,
                char_start=0,
                char_end=len(text),
                heading_path=(),
            )
        ],
    )


def _token_texts(text: str) -> list[str]:
    return [text[token.start : token.end] for token in tokenize_with_spans(text)]


def test_fixed_uses_512_tokens_and_exact_64_token_overlap_with_unicode() -> None:
    family = "👨‍👩‍👧‍👦"
    text = "café " + family + " 你好 " + " ".join(f"word{index}" for index in range(600))
    parsed = _single_block(text)

    chunks = chunk_fixed(parsed)

    assert count_tokens("café " + family + " 你好") == 4
    assert [chunk.content_tokens for chunk in chunks] == [512, 156]
    assert _token_texts(chunks[0].content)[-64:] == _token_texts(chunks[1].content)[:64]
    assert all(
        parsed.full_text[chunk.char_start : chunk.char_end] == chunk.content for chunk in chunks
    )
    assert all((chunk.block_start_idx, chunk.block_end_idx) == (0, 0) for chunk in chunks)


def test_recursive_prefers_paragraph_then_line_then_sentence_boundaries() -> None:
    text = "one two.\nthree four.\n\nfive six. seven eight."
    chunks = chunk_recursive(_single_block(text), max_tokens=4)

    assert [chunk.content for chunk in chunks] == [
        "one two.",
        "three four.",
        "five six.",
        "seven eight.",
    ]
    assert all(chunk.content_tokens <= 4 for chunk in chunks)


def test_table_is_an_atomic_chunk_for_all_strategies() -> None:
    table_cells = " | ".join(f"cell{index}" for index in range(600))
    parsed = parse_markdown(
        "intro paragraph.\n\n| column | value |\n| --- | --- |\n"
        f"| {table_cells} |\n\noutro paragraph."
    )
    table = next(block for block in parsed.blocks if block.block_type == "table")
    units = semantic_units(parsed)
    semantic = chunk_semantic(parsed, units, [[1.0, 0.0] for _ in units])

    for chunks in (chunk_fixed(parsed), chunk_recursive(parsed), semantic):
        table_chunks = [chunk for chunk in chunks if chunk.dominant_block_type == "table"]
        assert len(table_chunks) == 1
        assert table_chunks[0].content == table.text
        assert table_chunks[0].char_start == table.char_start
        assert table_chunks[0].char_end == table.char_end
        assert table_chunks[0].content_tokens > 512


def test_oversized_block_is_hard_split_without_losing_exact_ranges() -> None:
    text = " ".join(f"token{index}" for index in range(900))
    parsed = _single_block(text)
    units = semantic_units(parsed, hard_max_tokens=128)
    semantic = chunk_semantic(
        parsed,
        units,
        [[1.0, 0.0] for _ in units],
        max_tokens=128,
    )

    for chunks in (
        chunk_fixed(parsed, max_tokens=128, overlap_tokens=0),
        chunk_recursive(parsed, max_tokens=128),
        semantic,
    ):
        assert len(chunks) == 8
        assert all(chunk.content_tokens <= 128 for chunk in chunks)
        assert all((chunk.block_start_idx, chunk.block_end_idx) == (0, 0) for chunk in chunks)
        assert all(text[chunk.char_start : chunk.char_end] == chunk.content for chunk in chunks)
        assert _token_texts(" ".join(chunk.content for chunk in chunks)) == _token_texts(text)


def test_semantic_breaks_at_adjacent_sentence_embedding_drop() -> None:
    text = "alpha one. alpha two. beta one. beta two."
    parsed = _single_block(text)
    units = semantic_units(parsed)
    embeddings = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]

    chunks = chunk_semantic(parsed, units, embeddings, min_tokens=0)

    assert [chunk.content for chunk in chunks] == [
        "alpha one. alpha two.",
        "beta one. beta two.",
    ]
    assert [chunk.char_start for chunk in chunks] == [0, text.index("beta")]
    assert all(text[chunk.char_start : chunk.char_end] == chunk.content for chunk in chunks)
