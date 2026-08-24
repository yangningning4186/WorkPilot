from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from app.ingest.types import ParsedBlock, ParsedDocument

DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_SEMANTIC_MIN_TOKENS = 64
HARD_MAX_TOKENS = 8192

_PARAGRAPH_SEPARATOR_RE = re.compile(r"\n[ \t]*\n+")
_LINE_SEPARATOR_RE = re.compile(r"\n")
_SENTENCE_TERMINATORS = frozenset(".!?。\uff01\uff1f;\uff1b")
_SENTENCE_CLOSERS = frozenset("\"'\u201d\u2019\u300d\u300f\u3011\u300b\u3009\u3015\uff3d\uff09)")


@dataclass(frozen=True)
class TokenSpan:
    start: int
    end: int


@dataclass(frozen=True)
class TextSpan:
    char_start: int
    char_end: int


@dataclass(frozen=True)
class StrategyChunk:
    chunk_index: int
    content: str
    content_tokens: int
    block_start_idx: int
    block_end_idx: int
    char_start: int
    char_end: int
    dominant_block_type: str
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class SemanticUnit:
    char_start: int
    char_end: int
    token_count: int
    group_index: int
    is_table: bool


def tokenize_with_spans(text: str) -> list[TokenSpan]:
    """A deterministic Unicode-aware tokenizer used for chunk boundaries.

    It keeps words, individual CJK code points, punctuation and emoji grapheme-like
    ZWJ sequences addressable without converting between byte and code-point offsets.
    The goal is stable offline experiment boundaries, not provider-side billing counts.
    """

    tokens: list[TokenSpan] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if _is_cjk(character):
            tokens.append(TokenSpan(index, index + 1))
            index += 1
            continue
        category = unicodedata.category(character)
        if category[0] in {"L", "N"} or character == "_":
            end = index + 1
            while end < len(text):
                candidate = text[end]
                candidate_category = unicodedata.category(candidate)
                if _is_cjk(candidate):
                    break
                if candidate_category[0] not in {"L", "N", "M"} and candidate != "_":
                    break
                end += 1
            tokens.append(TokenSpan(index, end))
            index = end
            continue
        if category[0] == "M" and tokens and tokens[-1].end == index:
            tokens[-1] = replace(tokens[-1], end=index + 1)
            index += 1
            continue
        if category[0] == "S":
            end = _emoji_sequence_end(text, index)
            tokens.append(TokenSpan(index, end))
            index = end
            continue
        tokens.append(TokenSpan(index, index + 1))
        index += 1
    return tokens


def count_tokens(text: str) -> int:
    return len(tokenize_with_spans(text))


def chunk_fixed(
    parsed: ParsedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    hard_max_tokens: int = HARD_MAX_TOKENS,
) -> list[StrategyChunk]:
    """Split non-table block groups into fixed token windows with exact overlap."""

    _validate_limits(max_tokens, overlap_tokens, hard_max_tokens)
    spans: list[TextSpan] = []
    for blocks in _groups_with_isolated_tables(parsed.blocks):
        group_span = TextSpan(blocks[0].char_start, blocks[-1].char_end)
        if blocks[0].block_type == "table":
            spans.extend(
                _atomic_or_hard_split(
                    parsed.full_text,
                    group_span,
                    hard_max_tokens=hard_max_tokens,
                )
            )
            continue
        spans.extend(
            _fixed_spans(
                parsed.full_text,
                group_span,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
        )
    return _materialize(parsed, spans)


def chunk_recursive(
    parsed: ParsedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    hard_max_tokens: int = HARD_MAX_TOKENS,
) -> list[StrategyChunk]:
    """Recursively split by paragraph, line and sentence boundaries, then hard-split."""

    _validate_limits(max_tokens, 0, hard_max_tokens)
    spans: list[TextSpan] = []
    for blocks in _groups_with_isolated_tables(parsed.blocks):
        group_span = TextSpan(blocks[0].char_start, blocks[-1].char_end)
        if blocks[0].block_type == "table":
            spans.extend(
                _atomic_or_hard_split(
                    parsed.full_text,
                    group_span,
                    hard_max_tokens=hard_max_tokens,
                )
            )
            continue
        atoms = _recursive_atoms(parsed.full_text, group_span, max_tokens=max_tokens, level=0)
        spans.extend(_pack_spans(parsed.full_text, atoms, max_tokens=max_tokens))
    return _materialize(parsed, spans)


def semantic_units(
    parsed: ParsedDocument,
    *,
    hard_max_tokens: int = HARD_MAX_TOKENS,
) -> list[SemanticUnit]:
    """Return stable sentence units whose embeddings determine semantic breakpoints."""

    if hard_max_tokens < 1:
        raise ValueError("hard_max_tokens 必须大于 0")
    units: list[SemanticUnit] = []
    for group_index, blocks in enumerate(_groups_with_isolated_tables(parsed.blocks)):
        group_span = TextSpan(blocks[0].char_start, blocks[-1].char_end)
        is_table = blocks[0].block_type == "table"
        if is_table:
            spans = _atomic_or_hard_split(
                parsed.full_text,
                group_span,
                hard_max_tokens=hard_max_tokens,
            )
        else:
            spans = []
            for sentence in _sentence_spans(parsed.full_text, group_span):
                spans.extend(
                    _atomic_or_hard_split(
                        parsed.full_text,
                        sentence,
                        hard_max_tokens=hard_max_tokens,
                    )
                )
        for span in spans:
            units.append(
                SemanticUnit(
                    char_start=span.char_start,
                    char_end=span.char_end,
                    token_count=count_tokens(parsed.full_text[span.char_start : span.char_end]),
                    group_index=group_index,
                    is_table=is_table,
                )
            )
    return units


def chunk_semantic(
    parsed: ParsedDocument,
    units: Sequence[SemanticUnit],
    embeddings: Sequence[Sequence[float]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = DEFAULT_SEMANTIC_MIN_TOKENS,
) -> list[StrategyChunk]:
    """Split where adjacent sentence embeddings have a statistically sharp drop."""

    if max_tokens < 1:
        raise ValueError("max_tokens 必须大于 0")
    if not 0 <= min_tokens <= max_tokens:
        raise ValueError("min_tokens 必须位于 0..max_tokens")
    if len(units) != len(embeddings):
        raise ValueError("semantic unit 与 embedding 数量不一致")
    if any(not vector for vector in embeddings):
        raise ValueError("semantic embedding 不能为空")

    spans: list[TextSpan] = []
    cursor = 0
    while cursor < len(units):
        group_end = cursor + 1
        while group_end < len(units) and units[group_end].group_index == units[cursor].group_index:
            group_end += 1
        group_units = units[cursor:group_end]
        group_embeddings = embeddings[cursor:group_end]
        if group_units[0].is_table:
            spans.extend(TextSpan(unit.char_start, unit.char_end) for unit in group_units)
        else:
            spans.extend(
                _semantic_group_spans(
                    group_units,
                    group_embeddings,
                    max_tokens=max_tokens,
                    min_tokens=min_tokens,
                )
            )
        cursor = group_end
    return _materialize(parsed, spans)


def _semantic_group_spans(
    units: Sequence[SemanticUnit],
    embeddings: Sequence[Sequence[float]],
    *,
    max_tokens: int,
    min_tokens: int,
) -> list[TextSpan]:
    if not units:
        return []
    distances = [1.0 - _cosine_similarity(left, right) for left, right in pairwise(embeddings)]
    threshold = _semantic_threshold(distances)
    spans: list[TextSpan] = []
    start = 0
    tokens = units[0].token_count
    for index in range(1, len(units)):
        next_tokens = units[index].token_count
        exceeds_target = tokens + next_tokens > max_tokens
        semantic_break = tokens >= min_tokens and distances[index - 1] >= threshold
        if exceeds_target or semantic_break:
            spans.append(TextSpan(units[start].char_start, units[index - 1].char_end))
            start = index
            tokens = next_tokens
        else:
            tokens += next_tokens
    spans.append(TextSpan(units[start].char_start, units[-1].char_end))
    return spans


def _semantic_threshold(distances: Sequence[float]) -> float:
    if not distances:
        return math.inf
    if len(distances) == 1:
        return 0.35
    ordered = sorted(distances)
    median = _quantile(ordered, 0.5)
    deviations = sorted(abs(value - median) for value in distances)
    mad = _quantile(deviations, 0.5)
    return max(0.35, median + max(0.1, 1.5 * mad))


def _quantile(ordered: Sequence[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("semantic embedding 维度不一致")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _recursive_atoms(
    text: str,
    span: TextSpan,
    *,
    max_tokens: int,
    level: int,
) -> list[TextSpan]:
    trimmed = _trim_span(text, span)
    if trimmed is None:
        return []
    if count_tokens(text[trimmed.char_start : trimmed.char_end]) <= max_tokens:
        return [trimmed]
    if level == 0:
        pieces = _regex_split_spans(text, trimmed, _PARAGRAPH_SEPARATOR_RE)
    elif level == 1:
        pieces = _regex_split_spans(text, trimmed, _LINE_SEPARATOR_RE)
    elif level == 2:
        pieces = _sentence_spans(text, trimmed)
    else:
        return _fixed_spans(text, trimmed, max_tokens=max_tokens, overlap_tokens=0)
    if len(pieces) <= 1:
        return _recursive_atoms(text, trimmed, max_tokens=max_tokens, level=level + 1)
    atoms: list[TextSpan] = []
    for piece in pieces:
        atoms.extend(_recursive_atoms(text, piece, max_tokens=max_tokens, level=level + 1))
    return atoms


def _pack_spans(text: str, spans: Iterable[TextSpan], *, max_tokens: int) -> list[TextSpan]:
    packed: list[TextSpan] = []
    current: TextSpan | None = None
    for span in spans:
        if current is None:
            current = span
            continue
        candidate = TextSpan(current.char_start, span.char_end)
        if count_tokens(text[candidate.char_start : candidate.char_end]) <= max_tokens:
            current = candidate
        else:
            packed.append(current)
            current = span
    if current is not None:
        packed.append(current)
    return packed


def _regex_split_spans(text: str, span: TextSpan, pattern: re.Pattern[str]) -> list[TextSpan]:
    pieces: list[TextSpan] = []
    cursor = span.char_start
    for match in pattern.finditer(text, span.char_start, span.char_end):
        end = match.end()
        trimmed = _trim_span(text, TextSpan(cursor, end))
        if trimmed is not None:
            pieces.append(trimmed)
        cursor = end
    trimmed = _trim_span(text, TextSpan(cursor, span.char_end))
    if trimmed is not None:
        pieces.append(trimmed)
    return pieces


def _sentence_spans(text: str, span: TextSpan) -> list[TextSpan]:
    pieces: list[TextSpan] = []
    cursor = span.char_start
    index = span.char_start
    while index < span.char_end:
        character = text[index]
        boundary = character in _SENTENCE_TERMINATORS or character == "\n"
        if boundary:
            end = index + 1
            while end < span.char_end and text[end] in _SENTENCE_CLOSERS:
                end += 1
            trimmed = _trim_span(text, TextSpan(cursor, end))
            if trimmed is not None:
                pieces.append(trimmed)
            cursor = end
            index = end
        else:
            index += 1
    trimmed = _trim_span(text, TextSpan(cursor, span.char_end))
    if trimmed is not None:
        pieces.append(trimmed)
    return pieces


def _atomic_or_hard_split(
    text: str,
    span: TextSpan,
    *,
    hard_max_tokens: int,
) -> list[TextSpan]:
    trimmed = _trim_span(text, span)
    if trimmed is None:
        return []
    if count_tokens(text[trimmed.char_start : trimmed.char_end]) <= hard_max_tokens:
        return [trimmed]
    return _fixed_spans(text, trimmed, max_tokens=hard_max_tokens, overlap_tokens=0)


def _fixed_spans(
    text: str,
    span: TextSpan,
    *,
    max_tokens: int,
    overlap_tokens: int,
) -> list[TextSpan]:
    local_tokens = tokenize_with_spans(text[span.char_start : span.char_end])
    if not local_tokens:
        return []
    step = max_tokens - overlap_tokens
    spans: list[TextSpan] = []
    start_token = 0
    while start_token < len(local_tokens):
        end_token = min(start_token + max_tokens, len(local_tokens))
        spans.append(
            TextSpan(
                span.char_start + local_tokens[start_token].start,
                span.char_start + local_tokens[end_token - 1].end,
            )
        )
        if end_token == len(local_tokens):
            break
        start_token += step
    return spans


def _materialize(parsed: ParsedDocument, spans: Iterable[TextSpan]) -> list[StrategyChunk]:
    chunks: list[StrategyChunk] = []
    for span in spans:
        intersecting = [
            block
            for block in parsed.blocks
            if block.char_start < span.char_end and block.char_end > span.char_start
        ]
        if not intersecting:
            raise ValueError(f"chunk 字符区间未覆盖任何 block: {span}")
        content = parsed.full_text[span.char_start : span.char_end]
        dominant = _dominant_block_type(intersecting, span)
        chunks.append(
            StrategyChunk(
                chunk_index=len(chunks),
                content=content,
                content_tokens=count_tokens(content),
                block_start_idx=intersecting[0].block_idx,
                block_end_idx=intersecting[-1].block_idx,
                char_start=span.char_start,
                char_end=span.char_end,
                dominant_block_type=dominant,
                heading_path=intersecting[-1].heading_path,
            )
        )
    return chunks


def _dominant_block_type(blocks: Sequence[ParsedBlock], span: TextSpan) -> str:
    candidates = [block for block in blocks if block.block_type != "title"] or list(blocks)
    return max(
        candidates,
        key=lambda block: (
            min(block.char_end, span.char_end) - max(block.char_start, span.char_start),
            -block.block_idx,
        ),
    ).block_type


def _groups_with_isolated_tables(blocks: Sequence[ParsedBlock]) -> list[list[ParsedBlock]]:
    if not blocks:
        raise ValueError("文档没有可分块的 parsed block")
    groups: list[list[ParsedBlock]] = []
    current: list[ParsedBlock] = []
    for expected_index, block in enumerate(blocks):
        if block.block_idx != expected_index:
            raise ValueError("parsed block_idx 必须从 0 连续递增")
        if block.block_type == "table":
            if current:
                groups.append(current)
                current = []
            groups.append([block])
        else:
            current.append(block)
    if current:
        groups.append(current)
    return groups


def _trim_span(text: str, span: TextSpan) -> TextSpan | None:
    start = span.char_start
    end = span.char_end
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return None if start == end else TextSpan(start, end)


def _validate_limits(max_tokens: int, overlap_tokens: int, hard_max_tokens: int) -> None:
    if max_tokens < 1:
        raise ValueError("max_tokens 必须大于 0")
    if not 0 <= overlap_tokens < max_tokens:
        raise ValueError("overlap_tokens 必须位于 0..max_tokens-1")
    if hard_max_tokens < max_tokens:
        raise ValueError("hard_max_tokens 不得小于 max_tokens")


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _emoji_sequence_end(text: str, start: int) -> int:
    end = start + 1
    while end < len(text) and _is_emoji_modifier(text[end]):
        end += 1
    while end + 1 < len(text) and text[end] == "\u200d":
        end += 2
        while end < len(text) and _is_emoji_modifier(text[end]):
            end += 1
    return end


def _is_emoji_modifier(character: str) -> bool:
    codepoint = ord(character)
    return (
        character == "\ufe0f"
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or unicodedata.category(character) == "M"
    )
