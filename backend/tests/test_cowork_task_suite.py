from __future__ import annotations

from copy import deepcopy

import pytest

from eval.cowork_task_suite import (
    DEFAULT_SUITE,
    CoworkSuiteError,
    load_suite,
    summarize_suite,
    validate_suite,
)


def test_cowork_core_40_has_frozen_coverage() -> None:
    summary = summarize_suite(load_suite(DEFAULT_SUITE))

    assert summary == {
        "name": "cowork-core-40",
        "version": "1.0.0",
        "items": 40,
        "splits": {"dev": 32, "test": 8},
        "categories": {
            "artifact": 8,
            "knowledge": 8,
            "office": 6,
            "safety_hitl": 4,
            "web": 6,
            "workspace": 8,
        },
        "difficulties": {"1": 7, "2": 21, "3": 12},
        "hitl_items": 5,
        "average_optimal_tool_calls": 1.9,
        "review_status": "pending_human_review",
    }


def test_cowork_knowledge_tasks_pin_evidence_contract() -> None:
    suite = load_suite(DEFAULT_SUITE)
    knowledge = [item for item in suite["items"] if item["category"] == "knowledge"]

    assert len(knowledge) == 8
    for item in knowledge:
        assert "search_knowledge" in item["gold"]["required_tools"]
        contracts = [
            assertion
            for assertion in item["gold"]["assertions"]
            if assertion["type"] == "evidence_contract"
        ]
        assert len(contracts) == 1
        assert contracts[0]["prohibited_keys"] == ["chunk_id", "score", "orm"]


def test_cowork_suite_rejects_tool_label_drift() -> None:
    suite = deepcopy(load_suite(DEFAULT_SUITE))
    suite["items"][0]["gold"]["required_tools"].append("raw_chunk_search")

    with pytest.raises(CoworkSuiteError, match="未知工具"):
        validate_suite(suite)


def test_cowork_suite_rejects_test_quota_drift() -> None:
    suite = deepcopy(load_suite(DEFAULT_SUITE))
    test_item = next(item for item in suite["items"] if item["split"] == "test")
    test_item["split"] = "dev"

    with pytest.raises(CoworkSuiteError, match="split 配额漂移"):
        validate_suite(suite)
