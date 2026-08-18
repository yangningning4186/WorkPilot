import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from app.llm.gateway import ModelGateway
from eval.memory_semantic_experiment import (
    ArmScore,
    GenerationArm,
    GenerationPair,
    JudgeRecord,
    SemanticCase,
    SemanticExperimentError,
    evaluate_preregistered_gate,
    generate_pairs,
    load_suite,
    parse_judge_response,
    run_semantic_judge,
    semantic_rubric_fingerprint,
    summarize_semantic,
)
from tests.fakes import DeterministicProvider


def _case(item_id: str = "semantic-one") -> SemanticCase:
    return SemanticCase(
        id=item_id,
        category="workflow_preference",
        query="给一个检查框架",
        memories=["用户偏好先列阻断项，再列可延期项。"],
        required_points=["给出可执行检查项"],
        personalization_points=["区分阻断项与可延期项"],
        forbidden_behaviors=["编造已经完成检查"],
    )


def _arm(
    condition: Literal["memory_off", "memory_on"],
    answer: str,
    *,
    disclosures: list[str] | None = None,
) -> GenerationArm:
    return GenerationArm(
        condition=condition,
        answer=answer,
        disclosure_hits=list(disclosures or []),
        latency_ms=1,
        input_tokens=2,
        output_tokens=3,
    )


def _pair(item_id: str = "semantic-one") -> GenerationPair:
    return GenerationPair(
        item_id=item_id,
        category="workflow_preference",
        query="给一个检查框架",
        order=["memory_off", "memory_on"],
        memory_off=_arm("memory_off", "列检查项。"),
        memory_on=_arm("memory_on", "先列阻断项，再列可延期项。"),
    )


def test_preregistered_a6_suite_is_new_and_frozen() -> None:
    root = Path(__file__).parents[2]
    path = root / "eval/suites/a6-memory-semantic-preregistered.json"
    raw, cases = load_suite(path)

    assert raw["status"] == "preregistered_unseen"
    assert raw["independent_from"] == [
        "a5-memory-seed",
        "a5-memory-quality-regression",
    ]
    assert len(cases) == 12
    assert len({case.id for case in cases}) == 12
    assert {case.category for case in cases} >= {
        "irrelevant_memory",
        "untrusted_memory",
        "insufficient_memory",
    }
    assert sha256(path.read_bytes()).hexdigest() == (
        "c784e3aceededfee123d354c73c02c903a8b82ebbc044df6f9d9fd5c54a5adf5"
    )
    assert semantic_rubric_fingerprint() == (
        "204522643c83a79c5f32395ba685c8727087886f6c43d1c5ba008d52187beae8"
    )


async def test_generation_pairs_are_alternated_and_only_memory_on_gets_context() -> None:
    provider = DeterministicProvider(
        completion_texts=["关闭臂", "开启臂先列阻断项，再列可延期项。"]
    )
    gateway = ModelGateway(provider, embedding_dimensions=1024)

    pairs = await generate_pairs([_case()], gateway=gateway, max_tokens=200)

    assert len(pairs) == 1
    assert pairs[0].order == ["memory_off", "memory_on"]
    assert pairs[0].memory_off.answer == "关闭臂"
    assert pairs[0].memory_on.answer.startswith("开启臂")
    assert "<user_context>" in provider.last_messages[1].content


def test_semantic_judge_parser_is_strict_and_not_keyword_based() -> None:
    valid = json.dumps(
        {
            "reason": "B 语义覆盖了全部要求，A 只覆盖通用部分。",
            "answer_a": {
                "task_quality": 2,
                "memory_use": 0,
                "source_disclosure": False,
            },
            "answer_b": {
                "task_quality": 2,
                "memory_use": 2,
                "source_disclosure": False,
            },
            "preferred": "B",
        },
        ensure_ascii=False,
    )
    reason, score_a, score_b, preferred = parse_judge_response(valid)
    assert "语义覆盖" in reason
    assert score_a == ArmScore(2, 0, False)
    assert score_b.qualified
    assert preferred == "B"

    invalid = valid.replace('"task_quality": 2', '"task_quality": true', 1)
    with pytest.raises(ValueError, match="task_quality"):
        parse_judge_response(invalid)


async def test_semantic_judge_is_blind_and_hard_disclosure_overrides_model() -> None:
    response = json.dumps(
        {
            "reason": "A 暴露来源且个性化不足，B 完整应用偏好。",
            "answer_a": {
                "task_quality": 2,
                "memory_use": 0,
                "source_disclosure": False,
            },
            "answer_b": {
                "task_quality": 2,
                "memory_use": 2,
                "source_disclosure": False,
            },
            "preferred": "B",
        },
        ensure_ascii=False,
    )
    provider = DeterministicProvider(completion_text=response)
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    pair = _pair()
    pair = replace(
        pair,
        memory_off=_arm(
            "memory_off",
            "根据您提供的信息，列检查项。",
            disclosures=["根据您提供的信息"],
        ),
    )

    records = await run_semantic_judge(
        [_case()],
        [pair],
        gateway=gateway,
        generation_identity=("openai_compatible", "generator-model"),
        expected_judge_identity=("deterministic_test", "fake-chat"),
    )

    assert records[0].answer_a.source_disclosure is True
    assert records[0].preferred == "B"
    prompt = provider.last_messages[1].content
    assert "memory_off" not in prompt
    assert "memory_on" not in prompt
    assert "不得按关键词是否逐字出现评分" in prompt


async def test_semantic_judge_rejects_self_judgement() -> None:
    provider = DeterministicProvider(completion_text="{}")
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    identity = ("deterministic_test", "fake-chat")

    with pytest.raises(SemanticExperimentError, match="禁止生成模型自评"):
        await run_semantic_judge(
            [_case()],
            [_pair()],
            gateway=gateway,
            generation_identity=identity,
            expected_judge_identity=identity,
        )


def test_semantic_summary_unblinds_alternating_answers() -> None:
    records = [
        JudgeRecord(
            item_id="one",
            category="x",
            answer_a_condition="memory_off",
            answer_b_condition="memory_on",
            reason="B 更好",
            answer_a=ArmScore(1, 0, False),
            answer_b=ArmScore(2, 2, False),
            preferred="B",
            raw_output="{}",
            model="judge",
            provider="provider",
            input_tokens=1,
            output_tokens=1,
        ),
        JudgeRecord(
            item_id="two",
            category="x",
            answer_a_condition="memory_on",
            answer_b_condition="memory_off",
            reason="A 更好",
            answer_a=ArmScore(2, 2, False),
            answer_b=ArmScore(2, 0, False),
            preferred="A",
            raw_output="{}",
            model="judge",
            provider="provider",
            input_tokens=1,
            output_tokens=1,
        ),
    ]

    summary = summarize_semantic(records)

    assert summary["preference_counts"] == {
        "memory_off": 0,
        "memory_on": 2,
        "tie": 0,
    }
    metrics = summary["metrics"]
    assert metrics["qualified_rate"]["memory_off"] == 0
    assert metrics["qualified_rate"]["memory_on"] == 1
    assert len(semantic_rubric_fingerprint()) == 64


def test_preregistered_gate_fails_on_any_task_regression() -> None:
    record = JudgeRecord(
        item_id="regressed",
        category="x",
        answer_a_condition="memory_off",
        answer_b_condition="memory_on",
        reason="记忆臂损害了通用答案",
        answer_a=ArmScore(2, 0, False),
        answer_b=ArmScore(1, 2, False),
        preferred="A",
        raw_output="{}",
        model="judge",
        provider="provider",
        input_tokens=1,
        output_tokens=1,
    )

    gate = evaluate_preregistered_gate([record] * 12)

    assert gate["status"] == "failed"
    checks = gate["checks"]
    assert checks["no_task_quality_regression"] is False
    assert gate["observed"]["task_regression_item_ids"] == ["regressed"] * 12
