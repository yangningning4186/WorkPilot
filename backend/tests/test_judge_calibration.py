import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from eval.judge_calibration import (
    EXPECTED_CATEGORIES,
    RUBRIC_ID,
    CalibrationExample,
    HumanLabel,
    JudgePrediction,
    _merge_calibration_namespace,
    agreement_metrics,
    bootstrap_agreement,
    build_import_rows,
    calibration_report,
    load_examples,
    load_human_labels,
    load_judge_predictions,
    prepare_bundle,
    prompt_fingerprint,
    quadratic_weighted_kappa,
    rubric_fingerprint,
    run_judge,
)

from app.llm.gateway import ModelGateway
from tests.fakes import DeterministicProvider


def _generation_report(path: Path, *, run_id: str, items: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "report_type": "generation_baseline",
                "run_id": run_id,
                "dataset": "core-dev",
                "config_hash": "config-1",
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _item(index: int, category: str = "single_hop") -> dict[str, object]:
    return {
        "item_id": f"00000000-0000-0000-0000-{index:012d}",
        "category": category,
        "answerable": category != "unanswerable",
        "question": f"问题 {index}",
        "gold_answer": f"参考答案 {index}",
        "answer": f"实际答案 {index}",
        "citations": [{"citation_id": "S1", "quote": f"证据 {index}"}],
        "error": None,
    }


def _example(
    index: int, *, split: str = "calibration", category: str = "single_hop"
) -> CalibrationExample:
    report = _generation_report_data(index, split=split, category=category)
    return CalibrationExample(**report)


def _generation_report_data(index: int, *, split: str, category: str) -> dict[str, object]:
    # 测试统计函数不经过 load_examples, fingerprint 只需在三方严格相等。
    return {
        "example_id": f"example-{index}",
        "source_run_id": "00000000-0000-0000-0000-000000000099",
        "item_id": f"00000000-0000-0000-0000-{index:012d}",
        "dataset": "core-dev",
        "category": category,
        "answerable": category != "unanswerable",
        "split": split,
        "question": f"问题 {index}",
        "gold_answer": f"参考 {index}",
        "answer": f"回答 {index}",
        "citations": (),
        "example_fingerprint": f"fingerprint-{index}",
    }


def _human(example: CalibrationExample, score: int) -> HumanLabel:
    return HumanLabel(
        example_id=example.example_id,
        example_fingerprint=example.example_fingerprint,
        score=score,
        reason="人工理由",
        reviewer="Alice",
        reviewed_at="2026-08-16T12:00:00+08:00",
    )


def _prediction(example: CalibrationExample, score: int) -> JudgePrediction:
    return JudgePrediction(
        example_id=example.example_id,
        example_fingerprint=example.example_fingerprint,
        rubric_id=RUBRIC_ID,
        rubric_fingerprint=rubric_fingerprint(),
        prompt_fingerprint=prompt_fingerprint(),
        score=score,
        reason="Judge 理由",
        model="heavy-model",
        provider="openai_compatible",
        raw_output=json.dumps({"reason": "Judge 理由", "score": score}),
        input_tokens=10,
        output_tokens=5,
        authorization_note_fingerprint="authorization-hash",
    )


def test_prepare_is_reproducible_and_fails_closed_on_missing_categories(tmp_path: Path) -> None:
    report = _generation_report(
        tmp_path / "report.json",
        run_id="00000000-0000-0000-0000-000000000001",
        items=[_item(1), _item(2, "table")],
    )
    output = tmp_path / "bundle"

    first = prepare_bundle([report], output)
    snapshot = {
        name: (output / name).read_bytes()
        for name in ("manifest.json", "examples.jsonl", "human-labels.csv", "rubric.txt")
    }
    second = prepare_bundle([report], output)

    assert first == second
    assert snapshot == {name: (output / name).read_bytes() for name in snapshot}
    assert first["example_count"] == 2
    assert first["category_coverage_ready"] is False
    assert "temporal" in first["missing_categories"]
    assert first["model_send_authorized"] is False


def test_prepare_rejects_same_item_repeated_across_strategy_reports(tmp_path: Path) -> None:
    item = _item(1)
    first = _generation_report(
        tmp_path / "first.json",
        run_id="00000000-0000-0000-0000-000000000001",
        items=[item],
    )
    second = _generation_report(
        tmp_path / "second.json",
        run_id="00000000-0000-0000-0000-000000000002",
        items=[item],
    )

    with pytest.raises(ValueError, match="不能重复计作唯一 calibration case"):
        prepare_bundle([first, second], tmp_path / "bundle")


def test_load_examples_detects_content_drift(tmp_path: Path) -> None:
    report = _generation_report(
        tmp_path / "report.json",
        run_id="00000000-0000-0000-0000-000000000001",
        items=[_item(1)],
    )
    output = tmp_path / "bundle"
    prepare_bundle([report], output)
    row = json.loads((output / "examples.jsonl").read_text())
    row["answer"] = "被修改"
    (output / "examples.jsonl").write_text(json.dumps(row) + "\n")

    with pytest.raises(ValueError, match="内容漂移"):
        load_examples(output / "examples.jsonl")


def test_human_label_import_rejects_missing_and_duplicate_labels(tmp_path: Path) -> None:
    examples = [_example(1), _example(2)]
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "example_id,example_fingerprint,score,reason,reviewer,reviewed_at\n"
        "example-1,fingerprint-1,2,正确,Alice,2026-08-16T12:00:00+08:00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="人工标签不完整"):
        load_human_labels(labels, examples)

    with labels.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["example_id", "example_fingerprint", "score", "reason", "reviewer", "reviewed_at"]
        )
        row = ["example-1", "fingerprint-1", "2", "正确", "Alice", "2026-08-16"]
        writer.writerow(row)
        writer.writerow(row)
    with pytest.raises(ValueError, match="重复人工标签"):
        load_human_labels(labels, examples)


@pytest.mark.asyncio
async def test_runner_requires_explicit_authorization_before_any_model_call(tmp_path: Path) -> None:
    provider = DeterministicProvider(completion_text='{"reason":"正确","score":2}')
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    with pytest.raises(PermissionError, match="未获得模型发送授权"):
        await run_judge(
            [_example(1)],
            tmp_path / "predictions.jsonl",
            gateway=gateway,
            allow_model_send=False,
            authorization_note="",
            expected_provider="deterministic_test",
            expected_model="fake-chat",
        )
    assert provider.last_messages == []


@pytest.mark.asyncio
async def test_runner_records_identity_raw_output_and_reuses_exact_output(tmp_path: Path) -> None:
    provider = DeterministicProvider(completion_text='{"reason":"核心结论正确","score":2}')
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    example = _example(1)
    output = tmp_path / "predictions.jsonl"

    first = await run_judge(
        [example],
        output,
        gateway=gateway,
        allow_model_send=True,
        authorization_note="ticket-123 approves this fixture",
        expected_provider="deterministic_test",
        expected_model="fake-chat",
    )
    second = await run_judge(
        [example],
        output,
        gateway=gateway,
        allow_model_send=True,
        authorization_note="ticket-123 approves this fixture",
        expected_provider="deterministic_test",
        expected_model="fake-chat",
    )
    loaded = load_judge_predictions(output, [example])[example.example_id]

    assert first["reused"] is False
    assert second["reused"] is True
    assert loaded.raw_output == '{"reason":"核心结论正确","score":2}'
    assert loaded.input_tokens == 3
    assert loaded.output_tokens == 2
    assert loaded.model == "fake-chat"


def test_qwk_confusion_and_bootstrap_are_deterministic() -> None:
    human = [0, 0, 1, 1, 2, 2]
    judge = [0, 1, 1, 2, 2, 0]
    metrics = agreement_metrics(human, judge)
    first = bootstrap_agreement(human, judge, seed=7, resamples=200)
    second = bootstrap_agreement(human, judge, seed=7, resamples=200)

    assert metrics["confusion_matrix"]["values"] == [[1, 1, 0], [0, 1, 1], [1, 0, 1]]
    assert metrics["confusion_matrix"]["human_marginal"] == [2, 2, 2]
    assert metrics["confusion_matrix"]["judge_marginal"] == [2, 2, 2]
    assert quadratic_weighted_kappa([0, 1, 2], [0, 1, 2]) == 1.0
    assert quadratic_weighted_kappa([2, 2], [2, 2]) is None
    assert first == second


def test_calibration_gate_uses_untuned_validation_split(tmp_path: Path) -> None:
    examples: list[CalibrationExample] = []
    supported_categories = tuple(
        category for category in EXPECTED_CATEGORIES if category != "agent_task"
    )
    for index in range(84):
        category = supported_categories[index % len(supported_categories)]
        split = "validation" if index < 30 else "calibration"
        examples.append(_example(index, split=split, category=category))
    human = {row.example_id: _human(row, index % 3) for index, row in enumerate(examples)}
    predictions = {
        row.example_id: _prediction(row, index % 3) for index, row in enumerate(examples)
    }

    passed = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=predictions,
        output_dir=tmp_path / "passed",
        resamples=200,
        required_categories=supported_categories,
    )
    assert passed["status"] == "passed"
    assert passed["gate_split"] == "validation"
    assert passed["split_metrics"]["validation"]["qwk"] == 1.0

    broken = dict(predictions)
    for row in examples[:14]:
        broken[row.example_id] = replace(
            broken[row.example_id], score=2 - human[row.example_id].score
        )
    failed = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "failed",
        resamples=200,
        required_categories=supported_categories,
    )
    assert failed["status"] == "failed"
    assert any(reason.startswith("qwk<") for reason in failed["gate_failures"])


def test_versioned_import_payload_preserves_existing_citation_review() -> None:
    example = _example(1)
    rows = build_import_rows(
        [example],
        {example.example_id: _human(example, 2)},
        {example.example_id: _prediction(example, 2)},
    )
    existing: dict[str, object] = {"citation_accuracy": {"rate": 0.95}}

    changed = _merge_calibration_namespace(
        existing,
        rubric_id=RUBRIC_ID,
        metric="answer_correctness",
        payload=rows[0]["human_payload"],
    )
    reused = _merge_calibration_namespace(
        existing,
        rubric_id=RUBRIC_ID,
        metric="answer_correctness",
        payload=rows[0]["human_payload"],
    )

    assert changed is True
    assert reused is False
    assert existing["citation_accuracy"] == {"rate": 0.95}
    assert existing["judge_calibration"][RUBRIC_ID]["answer_correctness"]["score"] == 2


def _gate_examples(
    validation_per_category: int, total_per_category: int = 14
) -> tuple[list[CalibrationExample], dict[str, HumanLabel], dict[str, JudgePrediction]]:
    """每个类别造相同数量的样本, 只调 validation 切片的大小。"""
    categories = tuple(category for category in EXPECTED_CATEGORIES if category != "agent_task")
    examples: list[CalibrationExample] = []
    index = 0
    for category in categories:
        for position in range(total_per_category):
            split = "validation" if position < validation_per_category else "calibration"
            examples.append(_example(index, split=split, category=category))
            index += 1
    human = {row.example_id: _human(row, i % 3) for i, row in enumerate(examples)}
    predictions = {row.example_id: _prediction(row, i % 3) for i, row in enumerate(examples)}
    return examples, human, predictions


def test_small_category_slices_are_unavailable_rather_than_failed(tmp_path: Path) -> None:
    """样本量不足的类别切片不判准确率, 但也不算通过。

    2~3 条的切片上 accuracy 只能取到少数几个离散值, 错一条就必然跌破 0.70——
    那反映的是样本量而不是 Judge 质量。把它记成"准确率不达标"是把抽样噪声
    写成质量结论; 直接跳过又等于悄悄没测。所以标记为不可用 + 独立失败原因。
    """
    examples, human, predictions = _gate_examples(validation_per_category=3)
    # 每个类别在 validation 上都错一条: 3 条里错 1 条 = 0.667 < 0.70
    broken = dict(predictions)
    seen: dict[str, int] = {}
    for row in examples:
        if row.split != "validation":
            continue
        seen[row.category] = seen.get(row.category, 0) + 1
        if seen[row.category] == 1:
            broken[row.example_id] = replace(
                broken[row.example_id], score=2 - human[row.example_id].score
            )

    report = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "small-slices",
        resamples=200,
        required_categories=tuple(
            category for category in EXPECTED_CATEGORIES if category != "agent_task"
        ),
    )

    failures = report["gate_failures"]
    # 不能把小切片记成"准确率不达标"
    assert not any(reason.startswith("low_slice_accuracy") for reason in failures)
    # 但也不能算通过: 必须有一条指向样本量的独立失败原因
    assert any(reason.startswith("slice_sample_count<5") for reason in failures)
    assert report["status"] == "failed"

    slice_gate = report["slice_gate"]
    assert slice_gate["gated"] == []
    assert len(slice_gate["insufficient_samples"]) == 6
    for metrics in report["slices"].values():
        if str(metrics.get("accuracy_gate")) == "unavailable":
            assert metrics["sample_count"] < 5


def test_large_slices_still_fail_on_genuine_low_accuracy(tmp_path: Path) -> None:
    """切片样本够大时, 准确率不达标仍然要按质量问题报出来。"""
    categories = tuple(category for category in EXPECTED_CATEGORIES if category != "agent_task")
    examples, human, predictions = _gate_examples(validation_per_category=6)
    broken = dict(predictions)
    seen = 0
    for row in examples:
        # 只打坏一个类别, 且打坏到足以跌破 0.70
        if row.split == "validation" and row.category == categories[0] and seen < 3:
            broken[row.example_id] = replace(
                broken[row.example_id], score=2 - human[row.example_id].score
            )
            seen += 1

    report = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "large-slices",
        resamples=200,
        required_categories=categories,
    )

    failures = report["gate_failures"]
    assert any(
        reason.startswith("low_slice_accuracy") and f"validation/category:{categories[0]}" in reason
        for reason in failures
    )
    assert not any(reason.startswith("slice_sample_count") for reason in failures)
    assert len(report["slice_gate"]["gated"]) == len(categories)
