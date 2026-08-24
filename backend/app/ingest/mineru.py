import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pymupdf

from app.ingest.pdf_quality import (
    PdfQualityMetrics,
    PdfSourceAnalysis,
    assess_pdf_quality,
    validate_pdf_document,
)
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument

MINERU_ADAPTER_VERSION = "3"
DISCARDED_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}


class MineruParseError(ValueError):
    pass


class MineruUnavailableError(MineruParseError):
    pass


@dataclass(frozen=True)
class PageSpec:
    width: float
    height: float
    rotation: int


@dataclass(frozen=True)
class MineruResult:
    title: str
    parser_version: str
    backend: str
    document: ParsedDocument
    quality: PdfQualityMetrics


async def parse_pdf_with_mineru(
    path: Path,
    *,
    command: Path,
    expected_revision: str,
    backend: str,
    effort: str,
    method: str,
    timeout_s: float,
    max_pages: int,
    processing_window_size: int,
) -> MineruResult:
    executable = _resolve_command(command)
    page_specs, metadata_title = await asyncio.to_thread(_read_page_specs, path, max_pages)
    with tempfile.TemporaryDirectory(prefix="workpilot-mineru-") as temp_dir:
        output_dir = Path(temp_dir) / "output"
        output_dir.mkdir()
        environment = _mineru_environment(processing_window_size)
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "-p",
            str(path),
            "-o",
            str(output_dir),
            "-b",
            backend,
            "--effort",
            effort,
            "-m",
            method,
            "--image-analysis",
            "true" if effort == "high" else "false",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError as error:
            await _terminate_process_group(process)
            raise MineruParseError(f"MinerU 解析超过 {timeout_s:g} 秒, 已终止") from error
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            raise
        if process.returncode != 0:
            detail = _process_error(stdout, stderr)
            raise MineruParseError(f"MinerU 解析失败: {detail}")

        content_path = _find_one(output_dir, "*_content_list.json")
        content = await asyncio.to_thread(_load_json, content_path)
        if not isinstance(content, list):
            raise MineruParseError("MinerU content_list.json 顶层必须是列表")
        v2_content = await asyncio.to_thread(_load_optional_v2_content, output_dir)
        version, actual_backend = await asyncio.to_thread(
            _read_middle_identity, output_dir, expected_revision, backend
        )
        document, title, source = decode_mineru_content(
            content,
            page_specs=page_specs,
            metadata_title=metadata_title,
            v2_content=v2_content,
        )
        try:
            validate_pdf_document(document)
        except ValueError as error:
            raise MineruParseError(f"MinerU 结构质量门控未通过: {error}") from error
        quality = assess_pdf_quality(document, source)
        if quality.issues:
            raise MineruParseError("MinerU 文本质量门控未通过: " + ", ".join(quality.issues))
        return MineruResult(
            title=title,
            parser_version=(
                f"{version}:{actual_backend}:{effort}:{method}:adapter{MINERU_ADAPTER_VERSION}"
            ),
            backend=actual_backend,
            document=document,
            quality=quality,
        )


def decode_mineru_content(
    content: list[object],
    *,
    page_specs: list[PageSpec],
    metadata_title: str,
    v2_content: list[object] | None = None,
) -> tuple[ParsedDocument, str, PdfSourceAnalysis]:
    blocks: list[ParsedBlock] = []
    parts: list[str] = []
    headings: dict[int, str] = {}
    cursor = 0
    image_count = 0
    pages_with_text: set[int] = set()
    first_title = ""
    for raw_item in content:
        if not isinstance(raw_item, dict):
            raise MineruParseError("MinerU content_list 元素必须是对象")
        item: dict[str, Any] = raw_item
        item_type = str(item.get("type") or "")
        if item_type in DISCARDED_TYPES:
            continue
        page_index = _required_page_index(item, len(page_specs))
        location = _location_from_item(item, page_index, page_specs[page_index])
        block_type, text, heading_level = _decode_item(item)
        if item_type in {"image", "chart"}:
            image_count += 1
        if not text:
            continue
        if heading_level is not None:
            headings[heading_level] = text
            headings = {level: value for level, value in headings.items() if level <= heading_level}
            heading_path = tuple(headings[level] for level in sorted(headings))
            if not first_title:
                first_title = text
        else:
            heading_path = tuple(headings[level] for level in sorted(headings))
        if parts:
            cursor += 2
        char_start = cursor
        cursor += len(text)
        parts.append(text)
        pages_with_text.add(page_index + 1)
        blocks.append(
            ParsedBlock(
                block_idx=len(blocks),
                block_type=block_type,
                text=text,
                char_start=char_start,
                char_end=cursor,
                heading_path=heading_path,
                locations=(location,),
            )
        )
    if not blocks:
        raise MineruParseError("MinerU 没有输出可入库的文本 block")
    if v2_content is not None:
        blocks = _enrich_locations_from_v2(blocks, v2_content, page_specs)
    document = ParsedDocument(
        full_text="\n\n".join(parts),
        blocks=blocks,
        page_count=len(page_specs),
    )
    source = PdfSourceAnalysis(
        image_count=image_count,
        pages_with_text=len(pages_with_text),
    )
    return document, first_title or metadata_title or blocks[0].text[:120], source


def _decode_item(item: dict[str, Any]) -> tuple[str, str, int | None]:
    item_type = str(item.get("type") or "")
    if item_type in {"text", "title", "ref_text"}:
        text = _normalize_text(_first_string(item, "text", "content"))
        raw_level = item.get("text_level")
        level = int(str(raw_level)) if raw_level not in {None, 0, "0"} else None
        if item_type == "title" and level is None:
            level = 1
        return ("title" if level is not None else "paragraph", text, level)
    if item_type == "equation":
        return "formula", _normalize_text(_first_string(item, "text", "content")), None
    if item_type == "table":
        caption = _string_list(item.get("table_caption"))
        body = _table_body_to_markdown(_first_string(item, "table_body", "content", "text"))
        footnote = _string_list(item.get("table_footnote"))
        return "table", _join_sections([*caption, body, *footnote]), None
    if item_type in {"image", "chart"}:
        captions = _string_list(
            item.get("image_caption") or item.get("chart_caption") or item.get("caption")
        )
        generated = _first_string(item, "content", "text")
        return "figure_caption", _join_sections([*captions, generated]), None
    if item_type == "list":
        values = _string_list(item.get("list_items"))
        text = "\n".join(f"- {value}" for value in values)
        return "list", _normalize_text(text), None
    if item_type in {"code", "algorithm"}:
        caption = _string_list(item.get("code_caption"))
        body = _first_string(item, "code_body", "content", "text")
        footnote = _string_list(item.get("code_footnote"))
        return "code", _join_sections([*caption, body, *footnote]), None
    return "paragraph", "", None


def _location_from_item(item: dict[str, Any], page_index: int, page: PageSpec) -> BlockLocation:
    raw_bbox = item.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        raise MineruParseError("MinerU block 缺少四元 bbox")
    bbox = [float(value) for value in raw_bbox]
    scale = 1.0 if max(abs(value) for value in bbox) <= 1.5 else 1000.0
    x0, y0, x1, y1 = (min(1.0, max(0.0, value / scale)) for value in bbox)
    return BlockLocation(
        page_no=page_index + 1,
        page_width=page.width,
        page_height=page.height,
        rotation=page.rotation,
        coord_origin="top_left",
        bbox_norm=(x0, y0, x1, y1),
    )


def _required_page_index(item: dict[str, Any], page_count: int) -> int:
    try:
        page_index = int(item["page_idx"])
    except (KeyError, TypeError, ValueError) as error:
        raise MineruParseError("MinerU block 缺少有效 page_idx") from error
    if not 0 <= page_index < page_count:
        raise MineruParseError("MinerU block page_idx 越界")
    return page_index


def _read_page_specs(path: Path, max_pages: int) -> tuple[list[PageSpec], str]:
    document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
    try:
        if document.needs_pass:
            raise MineruParseError("PDF 已加密, 需要先解除密码保护")
        if document.page_count > max_pages:
            raise MineruParseError(f"PDF 页数 {document.page_count} 超过上限 {max_pages}")
        pages = [
            PageSpec(
                width=float(document[index].mediabox.width),
                height=float(document[index].mediabox.height),
                rotation=int(document[index].rotation),
            )
            for index in range(document.page_count)
        ]
        metadata = document.metadata or {}
        return pages, _normalize_text(str(metadata.get("title") or ""))
    finally:
        document.close()


def _resolve_command(command: Path) -> Path:
    expanded = command.expanduser()
    if expanded.is_absolute() or expanded.parent != Path("."):
        resolved = expanded.resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise MineruUnavailableError(f"MinerU 命令不可执行: {resolved}")
        return resolved
    located = shutil.which(str(command))
    if located is None:
        raise MineruUnavailableError(f"未找到 MinerU 命令: {command}")
    return Path(located)


def _mineru_environment(processing_window_size: int) -> dict[str, str]:
    environment = os.environ.copy()
    no_proxy = [value.strip() for value in environment.get("NO_PROXY", "").split(",")]
    for host in ("127.0.0.1", "localhost"):
        if host not in no_proxy:
            no_proxy.append(host)
    environment["NO_PROXY"] = ",".join(value for value in no_proxy if value)
    environment["MINERU_PROCESSING_WINDOW_SIZE"] = str(processing_window_size)
    environment["MINERU_API_MAX_CONCURRENT_REQUESTS"] = "1"
    return environment


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        process.kill()
    await process.wait()


def _process_error(stdout: bytes, stderr: bytes) -> str:
    combined = (stderr + b"\n" + stdout).decode(errors="replace").strip()
    return combined[-4000:] or "unknown error"


def _find_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.rglob(pattern))
    if len(matches) != 1:
        raise MineruParseError(f"MinerU 期望唯一 {pattern}, 实际找到 {len(matches)} 个")
    return matches[0]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_optional_v2_content(root: Path) -> list[object] | None:
    matches = sorted(root.rglob("*_content_list_v2.json"))
    if len(matches) != 1:
        return None
    payload = _load_json(matches[0])
    return payload if isinstance(payload, list) else None


def _enrich_locations_from_v2(
    blocks: list[ParsedBlock],
    pages: list[object],
    page_specs: list[PageSpec],
) -> list[ParsedBlock]:
    candidates: list[tuple[str, BlockLocation]] = []
    for page_index, raw_page in enumerate(pages):
        if page_index >= len(page_specs) or not isinstance(raw_page, list):
            continue
        for raw_item in raw_page:
            if not isinstance(raw_item, dict):
                continue
            item: dict[str, Any] = raw_item
            item_type = str(item.get("type") or "")
            if item_type in DISCARDED_TYPES or item_type.startswith("page_"):
                continue
            try:
                location = _location_from_item(item, page_index, page_specs[page_index])
            except MineruParseError:
                continue
            text = _normalize_for_match(_v2_text(item.get("content")))
            if len(text) >= 12:
                candidates.append((text, location))

    enriched: list[ParsedBlock] = []
    for block in blocks:
        block_text = _normalize_for_match(block.text)
        locations = list(block.locations)
        for candidate_text, location in candidates:
            if candidate_text not in block_text and block_text not in candidate_text:
                continue
            identity = (location.page_no, location.bbox_norm)
            if any((item.page_no, item.bbox_norm) == identity for item in locations):
                continue
            locations.append(location)
        enriched.append(
            ParsedBlock(
                block_idx=block.block_idx,
                block_type=block.block_type,
                text=block.text,
                char_start=block.char_start,
                char_end=block.char_end,
                heading_path=block.heading_path,
                locations=tuple(sorted(locations, key=lambda item: (item.page_no, item.bbox_norm))),
            )
        )
    return enriched


def _v2_text(value: object) -> str:
    if isinstance(value, str):
        if "<table" in value.casefold():
            return _table_body_to_markdown(value)
        return value
    if isinstance(value, list):
        return "\n".join(_v2_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    return "\n".join(
        _v2_text(item)
        for key, item in value.items()
        if key not in {"type", "path", "image_source", "image_path"}
    )


def _normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", _normalize_text(value)).strip()


def _read_middle_identity(
    root: Path, fallback_version: str, fallback_backend: str
) -> tuple[str, str]:
    matches = sorted(root.rglob("*_middle.json"))
    if len(matches) != 1:
        return fallback_version, fallback_backend
    payload = _load_json(matches[0])
    if not isinstance(payload, dict):
        return fallback_version, fallback_backend
    return (
        str(payload.get("_version_name") or fallback_version),
        str(payload.get("_backend") or fallback_backend),
    )


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _first_string(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        normalized = _normalize_text(value)
        return [normalized] if normalized else []
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str):
            normalized = _normalize_text(item)
            if normalized:
                output.append(normalized)
    return output


def _join_sections(values: list[str]) -> str:
    return "\n\n".join(value for value in (_normalize_text(item) for item in values) if value)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(_normalize_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _table_body_to_markdown(value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    if "<table" not in normalized.casefold():
        return normalized
    parser = _TableParser()
    parser.feed(value)
    if not parser.rows:
        return normalized
    width = max(len(row) for row in parser.rows)
    rows = [row + [""] * (width - len(row)) for row in parser.rows]

    def render(row: list[str]) -> str:
        cells = [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
        return "| " + " | ".join(cells) + " |"

    return "\n".join([render(rows[0]), render(["---"] * width), *(render(row) for row in rows[1:])])
