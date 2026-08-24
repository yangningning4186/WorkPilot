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


def test_cowork_core_suite_has_frozen_coverage() -> None:
    """整份快照断言，而不是逐字段挑几个。

    配额漂移是最容易悄悄发生的一类改动：加两条 dev 用例、忘了同步 test，成功率的分母
    就变了，而跨版本对比看起来仍然"正常"。整份比对会在那一刻直接失败。
    """

    summary = summarize_suite(load_suite(DEFAULT_SUITE))

    assert summary == {
        "name": "cowork-core-50",
        "version": "1.4.0",
        "items": 50,
        "splits": {"dev": 39, "test": 11},
        "categories": {
            "artifact": 8,
            "connector": 1,
            "knowledge": 8,
            "office": 6,
            "reading": 4,
            "safety_hitl": 4,
            "shell": 1,
            "web": 6,
            "workspace": 12,
        },
        "difficulties": {"1": 10, "2": 26, "3": 14},
        "hitl_items": 7,
        "average_optimal_tool_calls": 2.06,
        "review_status": "pending_human_review",
    }


def test_chinese_office_regressions_pin_specialized_connector_and_persistent_shell() -> None:
    suite = load_suite(DEFAULT_SUITE)
    calendar = next(item for item in suite["items"] if item["id"] == "cowork-core-049")
    shell = next(item for item in suite["items"] if item["id"] == "cowork-core-050")

    assert calendar["gold"]["required_tools"] == ["feishu_calendar_event_action"]
    assert "act_connector_api" in calendar["gold"]["forbidden_tools"]
    shell_interrupt = next(
        assertion
        for assertion in shell["gold"]["assertions"]
        if assertion["type"] == "hitl_interrupt"
    )
    assert shell_interrupt["arguments"]["persistent_session"] is True


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


def test_reading_tasks_require_locator_grounded_citations() -> None:
    """阅读类任务的产品承诺是"每个论断都能落回原文的具体位置"。

    一条只断言"回答里出现了某个词"的阅读任务是通过不了这个承诺的：模型不读文档、
    凭对同名论文的印象也能写出那个词。因此除了纯导航题，都必须断言 `[p.` 出现。
    """
    suite = load_suite(DEFAULT_SUITE)
    reading = [item for item in suite["items"] if item["category"] == "reading"]

    assert len(reading) == 4
    for item in reading:
        tools = item["gold"]["required_tools"]
        assert tools, f"{item['id']}: 阅读任务必须指明该走哪些阅读工具"
        assert all(
            name in {"material_outline", "search_material", "read_material", "reader_goto"}
            for name in tools
        ), f"{item['id']}: 阅读任务的 gold 工具应当全是阅读工具"
        # 只用 material_outline 的是纯导航题（"这篇分了哪几节"），没有可引用的论断。
        if tools != ["material_outline"]:
            values = [
                value
                for assertion in item["gold"]["assertions"]
                if assertion["type"] == "response_contains"
                for value in assertion["values"]
            ]
            has_locator = any("[p." in value for value in values)
            refuses = any(
                assertion["type"] == "response_contains_any"
                and any(word in value for value in assertion["values"] for word in ("没有", "未提"))
                for assertion in item["gold"]["assertions"]
            )
            assert has_locator or refuses, f"{item['id']}: 缺少 locator 引用断言"
