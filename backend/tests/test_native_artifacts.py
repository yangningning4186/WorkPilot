import hashlib
import stat
from pathlib import Path

import fitz  # type: ignore[import-untyped]
import pytest
from docx import Document
from openpyxl import load_workbook  # type: ignore[import-untyped]

from app.services.native_artifacts import create_native_artifact


def test_native_artifact_generation_docx_xlsx_pdf(tmp_path: Path) -> None:
    docx_path = tmp_path / "report.docx"
    xlsx_path = tmp_path / "table.xlsx"
    pdf_path = tmp_path / "brief.pdf"

    docx_result = create_native_artifact(
        docx_path,
        format="docx",
        title="季度报告",
        content="# 结论\n- 收入增长\n- 风险可控",
        sheets=[],
        baseline_sha256=None,
    )
    create_native_artifact(
        xlsx_path,
        format="xlsx",
        title="数据",
        content="",
        sheets=[{"name": "汇总", "rows": [["项目", "金额"], ["收入", 100]]}],
        baseline_sha256=None,
    )
    create_native_artifact(
        pdf_path,
        format="pdf",
        title="一页摘要",
        content="这是可直接预览的原生 PDF。",
        sheets=[],
        baseline_sha256=None,
    )

    assert docx_result.sha256
    assert Document(str(docx_path)).core_properties.title == "季度报告"
    workbook = load_workbook(xlsx_path, read_only=True)
    try:
        assert workbook["汇总"]["B2"].value == 100
    finally:
        workbook.close()
    with fitz.open(pdf_path) as pdf:
        assert pdf.page_count == 1
        assert pdf.metadata["title"] == "一页摘要"

    baseline = hashlib.sha256(docx_path.read_bytes()).hexdigest()
    overwritten = create_native_artifact(
        docx_path,
        format="docx",
        title="修订报告",
        content="新正文",
        sheets=[],
        baseline_sha256=baseline,
        backup_versions=1,
    )
    assert overwritten.backup_path is not None
    assert overwritten.backup_path.exists()


def test_native_pdf_paginates_cjk_and_overwrite_preserves_mode(tmp_path: Path) -> None:
    pdf_path = tmp_path / "long.pdf"
    tail = "末尾不可丢失"
    create_native_artifact(
        pdf_path,
        format="pdf",
        title="中文长文",
        content="正文" * 4_000 + tail,
        sheets=[],
        baseline_sha256=None,
    )
    with fitz.open(pdf_path) as pdf:
        extracted = "".join(page.get_text() for page in pdf)
        assert pdf.page_count > 1
        assert tail in extracted

    pdf_path.chmod(0o640)
    baseline = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    create_native_artifact(
        pdf_path,
        format="pdf",
        title="修订",
        content="修订正文",
        sheets=[],
        baseline_sha256=baseline,
    )
    assert stat.S_IMODE(pdf_path.stat().st_mode) == 0o640

    with pytest.raises(ValueError, match="必须省略"):
        create_native_artifact(
            tmp_path / "missing.pdf",
            format="pdf",
            title="不存在",
            content="",
            sheets=[],
            baseline_sha256="0" * 64,
        )
