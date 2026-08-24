import asyncio
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from app.ingest.mineru import MineruParseError, parse_pdf_with_mineru
from app.ingest.pdf_quality import (
    PdfQualityMetrics,
    PdfSourceAnalysis,
    assess_pdf_quality,
    should_prefer_mineru,
    validate_pdf_document,
)
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument

PDF_POLICY_VERSION = "3"


class PdfParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPdf:
    title: str
    parser: str
    parser_version: str
    backend: str
    document: ParsedDocument
    quality: PdfQualityMetrics
    fallback_reason: str | None = None
    selection_reasons: tuple[str, ...] = ()
    parse_elapsed_seconds: float = 0.0

    def parse_meta(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "fallback_reason": self.fallback_reason,
            "selection_reasons": list(self.selection_reasons),
            "quality": self.quality.to_dict(),
            "policy_version": PDF_POLICY_VERSION,
            "elapsed_seconds": round(self.parse_elapsed_seconds, 3),
        }


@dataclass(frozen=True)
class PdfParserConfig:
    mode: Literal["auto", "pymupdf", "mineru"]
    timeout_s: float
    max_pages: int
    memory_mb: int
    cpu_seconds: int
    mineru_command: Path
    mineru_revision: str
    mineru_backend: str
    mineru_effort: str
    mineru_method: str
    mineru_timeout_s: float
    mineru_fallback_enabled: bool
    mineru_processing_window_size: int


class PdfParser(Protocol):
    async def parse(self, path: Path) -> ParsedPdf: ...


class PyMuPdfParser:
    def __init__(self, config: PdfParserConfig) -> None:
        self.config = config

    async def parse(self, path: Path) -> ParsedPdf:
        return await parse_pdf_in_subprocess(
            path,
            timeout_s=self.config.timeout_s,
            max_pages=self.config.max_pages,
            memory_mb=self.config.memory_mb,
            cpu_seconds=self.config.cpu_seconds,
        )


class MineruPdfParser:
    def __init__(self, config: PdfParserConfig) -> None:
        self.config = config

    async def parse(self, path: Path) -> ParsedPdf:
        result = await parse_pdf_with_mineru(
            path,
            command=self.config.mineru_command,
            expected_revision=self.config.mineru_revision,
            backend=self.config.mineru_backend,
            effort=self.config.mineru_effort,
            method=self.config.mineru_method,
            timeout_s=self.config.mineru_timeout_s,
            max_pages=self.config.max_pages,
            processing_window_size=self.config.mineru_processing_window_size,
        )
        return ParsedPdf(
            title=result.title,
            parser="mineru",
            parser_version=result.parser_version,
            backend=result.backend,
            document=result.document,
            quality=result.quality,
        )


class RoutedPdfParser:
    def __init__(self, config: PdfParserConfig) -> None:
        self.config = config
        self.pymupdf = PyMuPdfParser(config)
        self.mineru = MineruPdfParser(config)

    async def parse(self, path: Path) -> ParsedPdf:
        if self.config.mode == "pymupdf":
            return await self.pymupdf.parse(path)
        if self.config.mode == "mineru":
            return await self._mineru_with_optional_fallback(path, ("configured_mineru",))

        try:
            baseline = await self.pymupdf.parse(path)
        except PdfParseError as baseline_error:
            return await self._mineru_with_optional_fallback(
                path,
                ("pymupdf_failed",),
                baseline_error=baseline_error,
            )
        prefer_mineru, reasons = should_prefer_mineru(baseline.quality)
        if not prefer_mineru:
            return replace(baseline, selection_reasons=("simple_text_layout",))
        return await self._mineru_with_optional_fallback(path, reasons, baseline=baseline)

    async def _mineru_with_optional_fallback(
        self,
        path: Path,
        reasons: tuple[str, ...],
        *,
        baseline: ParsedPdf | None = None,
        baseline_error: Exception | None = None,
    ) -> ParsedPdf:
        try:
            parsed = await self.mineru.parse(path)
            if baseline is not None:
                _validate_mineru_retention(parsed, baseline)
            return replace(parsed, selection_reasons=reasons)
        except (MineruParseError, OSError) as mineru_error:
            if not self.config.mineru_fallback_enabled:
                raise PdfParseError(str(mineru_error)) from mineru_error
            if baseline is None and baseline_error is None:
                try:
                    baseline = await self.pymupdf.parse(path)
                except PdfParseError as error:
                    baseline_error = error
            if baseline is None:
                raise PdfParseError(
                    f"MinerU 失败且 PyMuPDF 无法回退: "
                    f"mineru={mineru_error}; pymupdf={baseline_error}"
                ) from mineru_error
            reason = f"{type(mineru_error).__name__}: {str(mineru_error)[:1000]}"
            return replace(
                baseline,
                fallback_reason=reason,
                selection_reasons=reasons,
            )


def _validate_mineru_retention(parsed: ParsedPdf, baseline: ParsedPdf) -> None:
    if baseline.quality.issues:
        return
    baseline_characters = baseline.quality.character_count
    if baseline_characters and parsed.quality.character_count < baseline_characters * 0.5:
        ratio = parsed.quality.character_count / baseline_characters
        raise MineruParseError(f"MinerU 相对 PyMuPDF 文本保留率过低: {ratio:.1%}")
    baseline_pages = baseline.quality.pages_with_text
    if baseline_pages and parsed.quality.pages_with_text < baseline_pages * 0.5:
        ratio = parsed.quality.pages_with_text / baseline_pages
        raise MineruParseError(f"MinerU 相对 PyMuPDF 文本页覆盖率过低: {ratio:.1%}")


async def parse_pdf(path: Path, config: PdfParserConfig) -> ParsedPdf:
    parser: PdfParser = RoutedPdfParser(config)
    return await parser.parse(path)


async def parse_pdf_in_subprocess(
    path: Path,
    *,
    timeout_s: float,
    max_pages: int,
    memory_mb: int,
    cpu_seconds: int,
) -> ParsedPdf:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.ingest.pdf_worker",
        str(path),
        str(max_pages),
        str(memory_mb),
        str(cpu_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise PdfParseError(f"PDF 解析超过 {timeout_s:g} 秒, 已终止子进程") from error
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[-2000:]
        raise PdfParseError(f"PDF 解析子进程失败: {detail or f'exit {process.returncode}'}")
    try:
        payload: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PdfParseError("PDF 解析子进程返回了无效结果") from error
    if not payload.get("ok"):
        raise PdfParseError(str(payload.get("error") or "PDF 解析失败"))
    return _decode_document(payload["document"])


def _decode_document(payload: dict[str, Any]) -> ParsedPdf:
    blocks = [
        ParsedBlock(
            block_idx=int(block["block_idx"]),
            block_type=str(block["block_type"]),
            text=str(block["text"]),
            char_start=int(block["char_start"]),
            char_end=int(block["char_end"]),
            heading_path=tuple(block["heading_path"]),
            locations=tuple(
                BlockLocation(
                    page_no=int(location["page_no"]),
                    page_width=float(location["page_width"]),
                    page_height=float(location["page_height"]),
                    rotation=int(location["rotation"]),
                    coord_origin=str(location["coord_origin"]),
                    bbox_norm=tuple(float(value) for value in location["bbox_norm"]),  # type: ignore[arg-type]
                )
                for location in block["locations"]
            ),
        )
        for block in payload["blocks"]
    ]
    document = ParsedDocument(
        full_text=str(payload["full_text"]),
        blocks=blocks,
        page_count=int(payload["page_count"]),
    )
    validate_pdf_document(document)
    raw_analysis = payload.get("source_analysis") or {}
    source = PdfSourceAnalysis(
        image_count=int(raw_analysis.get("image_count") or 0),
        multi_column_pages=int(raw_analysis.get("multi_column_pages") or 0),
        pages_with_text=int(raw_analysis.get("pages_with_text") or 0),
    )
    return ParsedPdf(
        title=str(payload["title"]),
        parser="pymupdf",
        parser_version=str(payload["parser_version"]),
        backend="pymupdf-text",
        document=document,
        quality=assess_pdf_quality(document, source),
    )
