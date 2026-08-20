import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

from app.core.config import Settings
from app.ingest.pdf import ParsedPdf, PdfParserConfig, parse_pdf
from app.rag.pdf_ingestion import pdf_parser_config_from_settings


@dataclass(frozen=True)
class ParserRun:
    parser: str
    parser_version: str
    backend: str
    elapsed_seconds: float
    fallback_reason: str | None
    selection_reasons: list[str]
    quality: dict[str, object]


@dataclass(frozen=True)
class DocumentReport:
    source_uri: str
    page_count: int
    baseline: ParserRun | None
    baseline_error: str | None
    enhanced: ParserRun
    text_retention_ratio: float | None
    text_page_coverage_ratio: float | None
    elapsed_ratio: float | None
    overlays: list[str]


@dataclass(frozen=True)
class EvaluationFailure:
    source_uri: str
    error: str


async def evaluate_library(
    library_root: Path,
    output_root: Path,
    *,
    max_files: int | None,
    sample_pages: int,
) -> Path:
    settings = Settings()
    config = pdf_parser_config_from_settings(settings)
    enhanced_config = replace(config, mode="mineru")
    baseline_config = replace(config, mode="pymupdf")
    pdfs = await asyncio.to_thread(_find_pdfs, library_root)
    if max_files is not None:
        pdfs = pdfs[:max_files]
    if not pdfs:
        raise ValueError(f"{library_root} 中没有 PDF")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / timestamp
    overlay_dir = run_dir / "overlays"
    overlay_dir.mkdir(parents=True, exist_ok=False)
    reports: list[DocumentReport] = []
    failures: list[EvaluationFailure] = []
    for path in pdfs:
        source_uri = path.relative_to(library_root).as_posix()
        baseline: ParsedPdf | None = None
        baseline_error: str | None = None
        try:
            baseline = await _timed_parse(path, baseline_config)
        except Exception as error:
            baseline_error = f"{type(error).__name__}: {str(error)[:2000]}"
        try:
            enhanced = await _timed_parse(path, enhanced_config)
            pages = _sample_page_numbers(enhanced, sample_pages)
            overlays = await asyncio.to_thread(
                render_bbox_overlays,
                path,
                enhanced,
                overlay_dir / path.stem,
                pages,
            )
            reports.append(
                DocumentReport(
                    source_uri=source_uri,
                    page_count=enhanced.document.page_count or 0,
                    baseline=_parser_run(baseline) if baseline is not None else None,
                    baseline_error=baseline_error,
                    enhanced=_parser_run(enhanced),
                    text_retention_ratio=(
                        round(
                            _ratio(
                                enhanced.quality.character_count,
                                baseline.quality.character_count,
                            ),
                            4,
                        )
                        if baseline is not None
                        else None
                    ),
                    text_page_coverage_ratio=(
                        round(
                            _ratio(
                                enhanced.quality.pages_with_text,
                                baseline.quality.pages_with_text,
                            ),
                            4,
                        )
                        if baseline is not None
                        else None
                    ),
                    elapsed_ratio=(
                        round(
                            _ratio(
                                enhanced.parse_elapsed_seconds,
                                baseline.parse_elapsed_seconds,
                            ),
                            3,
                        )
                        if baseline is not None
                        else None
                    ),
                    overlays=[str(item.relative_to(run_dir)) for item in overlays],
                )
            )
        except Exception as error:
            failures.append(
                EvaluationFailure(
                    source_uri=source_uri,
                    error=f"{type(error).__name__}: {str(error)[:2000]}",
                )
            )

    payload: dict[str, object] = {
        "created_at": datetime.now(UTC).isoformat(),
        "library_root": str(library_root),
        "mineru": {
            "revision": config.mineru_revision,
            "backend": config.mineru_backend,
            "effort": config.mineru_effort,
            "method": config.mineru_method,
        },
        "documents": [asdict(report) for report in reports],
        "failures": [asdict(failure) for failure in failures],
        "summary": _summary(reports, failures),
    }
    json_path = run_dir / "report.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = run_dir / "report.md"
    markdown_path.write_text(_markdown_report(payload), encoding="utf-8")
    return markdown_path


async def _timed_parse(path: Path, config: PdfParserConfig) -> ParsedPdf:
    started = time.monotonic()
    parsed = await parse_pdf(path, config)
    elapsed = time.monotonic() - started
    return replace(parsed, parse_elapsed_seconds=elapsed)


def _parser_run(parsed: ParsedPdf) -> ParserRun:
    return ParserRun(
        parser=parsed.parser,
        parser_version=parsed.parser_version,
        backend=parsed.backend,
        elapsed_seconds=round(parsed.parse_elapsed_seconds, 3),
        fallback_reason=parsed.fallback_reason,
        selection_reasons=list(parsed.selection_reasons),
        quality=parsed.quality.to_dict(),
    )


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _sample_page_numbers(parsed: ParsedPdf, count: int) -> list[int]:
    candidates: list[int] = []
    for block_type in ("table", "formula", "figure_caption", "title"):
        candidates.extend(
            location.page_no
            for block in parsed.document.blocks
            if block.block_type == block_type
            for location in block.locations
        )
    candidates.extend([1, parsed.document.page_count or 1])
    unique = list(dict.fromkeys(candidates))
    return unique[: max(1, count)]


def _find_pdfs(library_root: Path) -> list[Path]:
    return sorted(library_root.rglob("*.pdf"))


def render_bbox_overlays(
    source: Path,
    parsed: ParsedPdf,
    output_dir: Path,
    page_numbers: list[int],
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    colors = {
        "title": (0.1, 0.4, 0.9),
        "paragraph": (0.1, 0.7, 0.2),
        "table": (0.9, 0.3, 0.1),
        "formula": (0.7, 0.1, 0.8),
        "figure_caption": (0.9, 0.7, 0.1),
        "list": (0.1, 0.7, 0.7),
        "code": (0.4, 0.4, 0.4),
    }
    document: Any = pymupdf.open(source)  # type: ignore[no-untyped-call]
    outputs: list[Path] = []
    try:
        for page_no in page_numbers:
            page: Any = document[page_no - 1]
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            for block in parsed.document.blocks:
                for location in block.locations:
                    if location.page_no != page_no:
                        continue
                    x0, y0, x1, y1 = location.bbox_norm
                    page.draw_rect(
                        pymupdf.Rect(  # type: ignore[no-untyped-call]
                            x0 * width, y0 * height, x1 * width, y1 * height
                        ),
                        color=colors.get(block.block_type, (0.8, 0.1, 0.1)),
                        width=1.2,
                        overlay=True,
                    )
            matrix = pymupdf.Matrix(1.5, 1.5)  # type: ignore[no-untyped-call]
            pixmap: Any = page.get_pixmap(matrix=matrix, alpha=False)
            output = output_dir / f"page-{page_no:04d}.png"
            pixmap.save(output)
            outputs.append(output)
    finally:
        document.close()
    return outputs


def _summary(reports: list[DocumentReport], failures: list[EvaluationFailure]) -> dict[str, object]:
    enhanced_runs = [report.enhanced for report in reports]
    return {
        "document_count": len(reports),
        "failure_count": len(failures),
        "baseline_failure_count": sum(report.baseline_error is not None for report in reports),
        "page_count": sum(report.page_count for report in reports),
        "block_count": sum(_quality_int(run, "block_count") for run in enhanced_runs),
        "character_count": sum(_quality_int(run, "character_count") for run in enhanced_runs),
        "table_count": sum(_block_count(run, "table") for run in enhanced_runs),
        "formula_count": sum(_block_count(run, "formula") for run in enhanced_runs),
        "figure_caption_count": sum(_block_count(run, "figure_caption") for run in enhanced_runs),
        "fallback_count": sum(run.fallback_reason is not None for run in enhanced_runs),
        "issue_count": sum(_quality_issue_count(run) for run in enhanced_runs),
        "enhanced_elapsed_seconds": round(sum(run.elapsed_seconds for run in enhanced_runs), 3),
        "minimum_text_retention_ratio": min(
            (
                report.text_retention_ratio
                for report in reports
                if report.text_retention_ratio is not None
            ),
            default=None,
        ),
    }


def _block_count(run: ParserRun, block_type: str) -> int:
    counts = run.quality["block_type_counts"]
    return int(counts.get(block_type, 0)) if isinstance(counts, dict) else 0


def _quality_int(run: ParserRun, key: str) -> int:
    value = run.quality[key]
    return int(value) if isinstance(value, int | str) else 0


def _quality_issue_count(run: ParserRun) -> int:
    issues = run.quality["issues"]
    return len(issues) if isinstance(issues, list) else 0


def _markdown_report(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    documents = payload["documents"]
    assert isinstance(documents, list)
    failures = payload["failures"]
    assert isinstance(failures, list)
    lines = [
        "# PDF 解析质量跑批",
        "",
        f"- 文档: {summary['document_count']}",
        f"- 失败: {summary['failure_count']}",
        f"- PyMuPDF 基线失败但 MinerU 成功: {summary['baseline_failure_count']}",
        f"- 页数: {summary['page_count']}",
        f"- blocks: {summary['block_count']}",
        f"- 表格: {summary['table_count']}",
        f"- 公式: {summary['formula_count']}",
        f"- 回退: {summary['fallback_count']}",
        f"- 质量问题: {summary['issue_count']}",
        "",
        "| 文档 | 页 | MinerU blocks | 表格 | 公式 | 文本保留率 | 耗时(s) | 问题 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for raw in documents:
        assert isinstance(raw, dict)
        enhanced = raw["enhanced"]
        assert isinstance(enhanced, dict)
        quality = enhanced["quality"]
        assert isinstance(quality, dict)
        counts = quality["block_type_counts"]
        assert isinstance(counts, dict)
        issues = quality["issues"]
        issue_text = (
            ", ".join(str(issue) for issue in issues)
            if isinstance(issues, list | tuple)
            else str(issues)
        )
        retention = raw["text_retention_ratio"]
        retention_text = f"{retention:.1%}" if isinstance(retention, float) else "-"
        lines.append(
            (
                "| {source} | {pages} | {blocks} | {tables} | {formulas} | "
                "{retention} | {elapsed} | {issues} |"
            ).format(
                source=raw["source_uri"],
                pages=raw["page_count"],
                blocks=quality["block_count"],
                tables=counts.get("table", 0),
                formulas=counts.get("formula", 0),
                retention=retention_text,
                elapsed=enhanced["elapsed_seconds"],
                issues=issue_text or "-",
            )
        )
    if failures:
        lines.extend(["", "## 失败", "", "| 文档 | 错误 |", "|---|---|"])
        for raw in failures:
            assert isinstance(raw, dict)
            lines.append(f"| {raw['source_uri']} | {raw['error']} |")
    lines.append("")
    lines.append("叠图位于同一跑批目录的 `overlays/`, 只包含忽略提交的本地验收产物。")
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比 PyMuPDF/MinerU 并输出 bbox 叠图")
    parser.add_argument("--library-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("eval/outputs/pdf-parsing-quality"))
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--sample-pages", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = Settings()
    library_root = (args.library_root or settings.local_library_path).expanduser().resolve()
    report = asyncio.run(
        evaluate_library(
            library_root,
            args.output_dir,
            max_files=args.max_files,
            sample_pages=args.sample_pages,
        )
    )
    print(report)


if __name__ == "__main__":
    main()
