from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eval.suite_generation_runner import (
    _validate_dev_only_suite,
    run_suite_generation,
)
from eval.suites import EvalSuite, SuiteDataset


def test_suite_generation_requires_explicit_model_send_authorization(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="必须显式授权"):
        asyncio.run(
            run_suite_generation(
                suite_path=Path("../eval/suites/m1-dev-70.json"),
                retrieval_manifest_path=tmp_path / "not-read.json",
                label="blocked-without-authorization",
                authorization_note="",
                allow_model_send=False,
                output_root=tmp_path,
            )
        )


def test_suite_generation_rejects_test_dataset() -> None:
    suite = EvalSuite(
        name="invalid-dev-suite",
        description="must fail before model send",
        origin="human",
        item_count=70,
        datasets=(SuiteDataset(name="core-test", item_count=70),),
        provenance={
            "core-test": {"reviewer": "owner", "reviewed_at": "2026-08-16"}
        },
        category_counts={
            "single_hop": 19,
            "multi_hop": 14,
            "table": 12,
            "unanswerable": 13,
            "temporal": 6,
            "global": 6,
        },
    )

    with pytest.raises(ValueError, match="禁止包含 test dataset"):
        _validate_dev_only_suite(suite)
