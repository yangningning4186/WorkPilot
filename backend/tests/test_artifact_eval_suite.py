from pathlib import Path

import pytest

from eval.artifact_suite import (
    DEFAULT_PUBLIC_REGISTRY,
    DEFAULT_SUITE,
    ArtifactSuiteError,
    load_public_registry,
    load_suite,
    run_suite,
    suite_summary,
)


def test_artifact_rendering_suite_has_frozen_format_and_failure_coverage() -> None:
    suite = load_suite(DEFAULT_SUITE)

    assert suite_summary(suite) == {
        "name": "artifact-rendering-dev",
        "version": "1.0",
        "origin": "synthetic",
        "review_status": "pending_human_review",
        "items": 10,
        "splits": {"dev": 10},
        "formats": {"docx": 3, "html": 1, "pdf": 1, "pptx": 4, "xlsx": 1},
        "categories": {
            "active_content_guard": 1,
            "cjk_typography": 1,
            "document_structure": 1,
            "formula_and_clipping": 1,
            "native_chart": 1,
            "offline_visual": 1,
            "overflow_guard": 1,
            "page_rendering": 1,
            "svg_visual": 2,
        },
        "expected_stages": {"passed": 8, "render": 1, "validation": 1},
    }


def test_artifact_rendering_suite_runs_final_file_checks_offline(tmp_path: Path) -> None:
    suite = load_suite(DEFAULT_SUITE)
    output = tmp_path / "artifact-eval"

    report = run_suite(suite, output)

    assert report["summary"] == {
        "passed": 10,
        "failed": 0,
        "pass_rate": 1.0,
        "by_format": {
            "docx": {"passed": 3, "items": 3, "pass_rate": 1.0},
            "html": {"passed": 1, "items": 1, "pass_rate": 1.0},
            "pdf": {"passed": 1, "items": 1, "pass_rate": 1.0},
            "pptx": {"passed": 4, "items": 4, "pass_rate": 1.0},
            "xlsx": {"passed": 1, "items": 1, "pass_rate": 1.0},
        },
    }
    pptx_case = next(
        item for item in report["results"] if item["id"] == "artifact-pptx-001-cjk-bold"
    )
    assert pptx_case["metrics"]["visual_validation_pass"] == 1.0
    assert (output / "report.json").is_file()

    with pytest.raises(ArtifactSuiteError, match="已存在"):
        run_suite(suite, output)


def test_public_artifact_benchmarks_are_license_and_adapter_gated() -> None:
    registry = load_public_registry(DEFAULT_PUBLIC_REGISTRY)

    assert len(registry.benchmarks) == 5
    pptc = next(item for item in registry.benchmarks if item.id == "pptc")
    assert pptc.license.status == "verified"
    assert pptc.license.spdx == "MIT"
    assert pptc.integration_status == "adapter_planned"
    presentbench = next(item for item in registry.benchmarks if item.id == "presentbench")
    assert presentbench.license.status == "review_required"
    assert presentbench.integration_status == "adapter_planned"
    assert all(item.integration_status != "adapter_ready" for item in registry.benchmarks)
