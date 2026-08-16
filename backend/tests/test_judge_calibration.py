import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.llm.gateway import ModelGateway
from eval.judge_calibration import (
    DEFAULT_JUDGE_CASES,
    INTERIM_JUDGE_CATEGORIES,
    RUBRIC_ID,
    SLICE_REPORT_ONLY_CAVEAT,
    CalibrationExample,
    HumanLabel,
    JudgePrediction,
    _markdown,
    _merge_calibration_namespace,
    agreement_metrics,
    assert_rubric_frozen,
    bootstrap_agreement,
    build_import_rows,
    calibration_report,
    freeze_rubric,
    load_examples,
    load_human_labels,
    load_judge_predictions,
    prepare_bundle,
    prompt_fingerprint,
    quadratic_weighted_kappa,
    rubric_fingerprint,
    run_judge,
)
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
    assert first["minimum_unique_cases"] == DEFAULT_JUDGE_CASES
    assert first["expected_categories"] == list(INTERIM_JUDGE_CATEGORIES)
    assert first["category_coverage_ready"] is False
    assert "temporal" in first["missing_categories"]
    assert "agent_task" not in first["missing_categories"]
    assert first["model_send_authorized"] is False


def test_prepare_accepts_current_70_case_six_category_baseline(tmp_path: Path) -> None:
    report = _generation_report(
        tmp_path / "report.json",
        run_id="00000000-0000-0000-0000-000000000070",
        items=[
            _item(index, INTERIM_JUDGE_CATEGORIES[index % len(INTERIM_JUDGE_CATEGORIES)])
            for index in range(DEFAULT_JUDGE_CASES)
        ],
    )

    manifest = prepare_bundle([report], tmp_path / "bundle")

    assert manifest["example_count"] == DEFAULT_JUDGE_CASES
    assert manifest["unique_case_count_ready"] is True
    assert manifest["category_coverage_ready"] is True
    assert manifest["execution_closure_ready"] is True
    assert manifest["unsupported_categories"] == []
    assert manifest["status"] == "awaiting_human_labels_and_model_authorization"


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
        "example-1,fingerprint-1,1,正确,Alice,2026-08-16T12:00:00+08:00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="人工标签不完整"):
        load_human_labels(labels, examples)

    with labels.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["example_id", "example_fingerprint", "score", "reason", "reviewer", "reviewed_at"]
        )
        row = ["example-1", "fingerprint-1", "1", "正确", "Alice", "2026-08-16"]
        writer.writerow(row)
        writer.writerow(row)
    with pytest.raises(ValueError, match="重复人工标签"):
        load_human_labels(labels, examples)


@pytest.mark.asyncio
async def test_runner_requires_explicit_authorization_before_any_model_call(tmp_path: Path) -> None:
    provider = DeterministicProvider(completion_text='{"reason":"正确","score":1}')
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
    provider = DeterministicProvider(completion_text='{"reason":"核心结论正确","score":1}')
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
    assert loaded.raw_output == '{"reason":"核心结论正确","score":1}'
    assert loaded.input_tokens == 3
    assert loaded.output_tokens == 2
    assert loaded.model == "fake-chat"


def test_qwk_confusion_and_bootstrap_are_deterministic() -> None:
    human = [0, 0, 0, 1, 1, 1]
    judge = [0, 0, 1, 0, 1, 1]
    metrics = agreement_metrics(human, judge)
    first = bootstrap_agreement(human, judge, seed=7, resamples=200)
    second = bootstrap_agreement(human, judge, seed=7, resamples=200)

    assert metrics["confusion_matrix"]["labels"] == [0, 1]
    assert metrics["confusion_matrix"]["values"] == [[2, 1], [1, 2]]
    assert metrics["confusion_matrix"]["human_marginal"] == [3, 3]
    assert metrics["confusion_matrix"]["judge_marginal"] == [3, 3]
    assert quadratic_weighted_kappa([0, 1], [0, 1]) == 1.0
    assert quadratic_weighted_kappa([1, 1], [1, 1]) is None
    assert first == second


def test_binary_qwk_equals_cohen_kappa_and_rejects_off_scale_labels() -> None:
    """二分类下 QWK 必须恒等于无权重 Cohen's kappa。

    改 rubric 档数时最容易出的错是权重分母仍按旧档数算，指标会静默偏移；
    这里用手算的 Cohen's kappa 钉死，而不是只测"能跑通"。
    """
    human = [0, 0, 0, 1, 1, 1]
    judge = [0, 0, 1, 0, 1, 1]
    observed_agreement = 4 / 6
    chance_agreement = (3 / 6) * (3 / 6) + (3 / 6) * (3 / 6)
    cohen = (observed_agreement - chance_agreement) / (1 - chance_agreement)
    assert quadratic_weighted_kappa(human, judge) == pytest.approx(cohen)

    # 旧三档标签必须被拒绝，不能悄悄落进二分类统计
    with pytest.raises(ValueError, match="score 必须是整数 0/1"):
        quadratic_weighted_kappa([0, 2], [0, 1])


def test_calibration_gate_uses_untuned_validation_split(tmp_path: Path) -> None:
    examples: list[CalibrationExample] = []
    supported_categories = INTERIM_JUDGE_CATEGORIES
    for index in range(84):
        category = supported_categories[index % len(supported_categories)]
        split = "validation" if index < 30 else "calibration"
        examples.append(_example(index, split=split, category=category))
    human = {row.example_id: _human(row, index % 2) for index, row in enumerate(examples)}
    predictions = {
        row.example_id: _prediction(row, index % 2) for index, row in enumerate(examples)
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
            broken[row.example_id], score=1 - human[row.example_id].score
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
        {example.example_id: _human(example, 1)},
        {example.example_id: _prediction(example, 1)},
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
    assert existing["judge_calibration"][RUBRIC_ID]["answer_correctness"]["score"] == 1


def _gate_examples(
    validation_per_category: int, total_per_category: int = 14
) -> tuple[list[CalibrationExample], dict[str, HumanLabel], dict[str, JudgePrediction]]:
    """每个类别造相同数量的样本, 只调 validation 切片的大小。"""
    categories = INTERIM_JUDGE_CATEGORIES
    examples: list[CalibrationExample] = []
    index = 0
    for category in categories:
        for position in range(total_per_category):
            split = "validation" if position < validation_per_category else "calibration"
            examples.append(_example(index, split=split, category=category))
            index += 1
    human = {row.example_id: _human(row, i % 2) for i, row in enumerate(examples)}
    predictions = {row.example_id: _prediction(row, i % 2) for i, row in enumerate(examples)}
    return examples, human, predictions


_SUPPORTED = INTERIM_JUDGE_CATEGORIES


def _break_one_per_validation_category(
    examples: list[CalibrationExample],
    human: dict[str, HumanLabel],
    predictions: dict[str, JudgePrediction],
    *,
    only: str | None = None,
    per_category: int = 1,
) -> dict[str, JudgePrediction]:
    broken = dict(predictions)
    seen: dict[str, int] = {}
    for row in examples:
        if row.split != "validation":
            continue
        if only is not None and row.category != only:
            continue
        seen[row.category] = seen.get(row.category, 0) + 1
        if seen[row.category] <= per_category:
            broken[row.example_id] = replace(
                broken[row.example_id], score=1 - human[row.example_id].score
            )
    return broken


def test_category_slices_are_diagnostic_and_never_block_acceptance(
    tmp_path: Path,
) -> None:
    """方案 3: 验收只卡整体 QWK/accuracy, 类别切片只报告不设门禁。

    6 类各要 5 条可解读样本 ⇒ validation 至少 30 条 ⇒ 约 120 个 Judge case,
    超出 70 条 dev 基线。在 2~3 条样本上判类别准确率, 报的是抽样噪声。
    代价是类别级可靠性没有被验证, 所以报告必须强制带上那句 caveat。
    """
    examples, human, predictions = _gate_examples(validation_per_category=3)
    broken = _break_one_per_validation_category(examples, human, predictions)

    report = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "report-only",
        resamples=200,
        required_categories=_SUPPORTED,
    )

    failures = report["gate_failures"]
    assert not any(reason.startswith("low_slice_accuracy") for reason in failures)
    assert not any(reason.startswith("slice_sample_count") for reason in failures)

    slice_gate = report["slice_gate"]
    assert slice_gate["policy"] == "report_only"
    assert slice_gate["enforced"] is False
    # 通过不代表逐类达标, 这句话必须跟着报告走
    assert "类别级可靠性未验证" in str(slice_gate["caveat"])
    assert len(slice_gate["insufficient_samples"]) == len(_SUPPORTED)
    assert str(SLICE_REPORT_ONLY_CAVEAT) in _markdown(report)


def test_enforce_policy_restores_both_slice_gates(tmp_path: Path) -> None:
    """规模够了以后切 enforce, 判据一直在算, 两条门禁都会回来。"""
    small, human, predictions = _gate_examples(validation_per_category=3)
    broken = _break_one_per_validation_category(small, human, predictions)
    enforced = calibration_report(
        examples=small,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "enforce-small",
        resamples=200,
        required_categories=_SUPPORTED,
        slice_gate_policy="enforce",
    )
    assert enforced["status"] == "failed"
    assert any(reason.startswith("slice_sample_count<5") for reason in enforced["gate_failures"])
    assert enforced["slice_gate"]["caveat"] is None

    # 切片够大时, 真实的低准确率仍然要作为质量问题拦下来
    big, human, predictions = _gate_examples(validation_per_category=6)
    broken = _break_one_per_validation_category(
        big, human, predictions, only=_SUPPORTED[0], per_category=3
    )
    report = calibration_report(
        examples=big,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "enforce-big",
        resamples=200,
        required_categories=_SUPPORTED,
        slice_gate_policy="enforce",
    )
    assert any(
        reason.startswith("low_slice_accuracy") and _SUPPORTED[0] in reason
        for reason in report["gate_failures"]
    )
    assert not any(reason.startswith("slice_sample_count") for reason in report["gate_failures"])


def test_below_threshold_slices_stay_visible_under_report_only(tmp_path: Path) -> None:
    """不设门禁不等于不报: 低于阈值的切片必须仍然出现在诊断里。"""
    examples, human, predictions = _gate_examples(validation_per_category=6)
    broken = _break_one_per_validation_category(
        examples, human, predictions, only=_SUPPORTED[0], per_category=3
    )
    report = calibration_report(
        examples=examples,
        human_labels=human,
        predictions=broken,
        output_dir=tmp_path / "visible",
        resamples=200,
        required_categories=_SUPPORTED,
    )

    assert report["slice_gate"]["below_threshold"] == [f"validation/category:{_SUPPORTED[0]}"]
    assert not any(reason.startswith("low_slice_accuracy") for reason in report["gate_failures"])
    assert "低于阈值" in _markdown(report)


def _bundle_with_labels(
    tmp_path: Path, *, labeled_splits: tuple[str, ...]
) -> tuple[Path, Path]:
    """造一个 bundle，并按 split 决定哪些行已经填了 score。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    report = _generation_report(
        tmp_path / "report.json",
        run_id="00000000-0000-0000-0000-000000000070",
        items=[
            _item(index, INTERIM_JUDGE_CATEGORIES[index % len(INTERIM_JUDGE_CATEGORIES)])
            for index in range(DEFAULT_JUDGE_CASES)
        ],
    )
    bundle = tmp_path / "bundle"
    prepare_bundle([report], bundle)
    labels = bundle / "draft.csv"
    with labels.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["example_id", "example_fingerprint", "score"])
        for line in (bundle / "examples.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            score = "1" if row["split"] in labeled_splits else ""
            writer.writerow([row["example_id"], row["example_fingerprint"], score])
    return bundle, labels


def test_freeze_requires_complete_calibration_and_untouched_validation(tmp_path: Path) -> None:
    """冻结的全部意义在于顺序: calibration 标完、validation 还没碰。"""
    bundle, partial = _bundle_with_labels(tmp_path / "partial", labeled_splits=())
    with pytest.raises(ValueError, match="calibration 尚未标完"):
        freeze_rubric(bundle, note="", labels_path=partial)

    peeked_bundle, peeked = _bundle_with_labels(
        tmp_path / "peeked", labeled_splits=("calibration", "validation")
    )
    with pytest.raises(ValueError, match="validation 已有标签"):
        freeze_rubric(peeked_bundle, note="", labels_path=peeked)

    ok_bundle, ok_labels = _bundle_with_labels(tmp_path / "ok", labeled_splits=("calibration",))
    record = freeze_rubric(ok_bundle, note="二分类冻结", labels_path=ok_labels)
    assert record["rubric_id"] == RUBRIC_ID
    assert record["rubric_fingerprint"] == rubric_fingerprint()
    assert record["validation_labeled_at_freeze"] == 0
    assert (ok_bundle / "rubric-freeze.json").exists()


def test_frozen_rubric_drift_blocks_acceptance(tmp_path: Path) -> None:
    """rubric 冻结后被改动，验收必须拒绝出数，而不是照常给一个 QWK。"""
    bundle, labels = _bundle_with_labels(tmp_path / "drift", labeled_splits=("calibration",))
    freeze_rubric(bundle, note="", labels_path=labels)
    freeze_path = bundle / "rubric-freeze.json"
    assert assert_rubric_frozen(freeze_path)["rubric_id"] == RUBRIC_ID

    tampered = json.loads(freeze_path.read_text(encoding="utf-8"))
    tampered["rubric_fingerprint"] = "0" * 64
    freeze_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="rubric 内容自冻结后已变更"):
        assert_rubric_frozen(freeze_path)

    examples, human, predictions = _gate_examples(validation_per_category=4)
    with pytest.raises(ValueError, match="rubric 内容自冻结后已变更"):
        calibration_report(
            examples=examples,
            human_labels=human,
            predictions=predictions,
            output_dir=tmp_path / "blocked",
            rubric_freeze=freeze_path,
            resamples=200,
            required_categories=_SUPPORTED,
        )


@pytest.mark.asyncio
async def test_judge_repairs_one_flaky_response_but_still_fails_closed(tmp_path: Path) -> None:
    """端点在连续批处理下即使 temperature=0 也会偶发抖动。

    政策与 E5 的 evidence gate 一致：同问题最多补一次；补上了就继续，
    补不上仍然 fail-closed，且报错必须能定位到是哪一条、已完成多少。
    """
    example = _example(1)
    good = '{"reason":"核心结论正确","score":1}'

    flaky = DeterministicProvider(completion_texts=["不是 JSON", good])
    gateway = ModelGateway(flaky, embedding_dimensions=1024)
    result = await run_judge(
        [example],
        tmp_path / "repaired.jsonl",
        gateway=gateway,
        allow_model_send=True,
        authorization_note="授权说明",
        expected_provider="deterministic_test",
        expected_model="fake-chat",
    )
    assert result["prediction_count"] == 1
    assert result["repair_retries"] == 1

    broken = DeterministicProvider(completion_texts=["还是不是 JSON", "依然不是 JSON"])
    gateway = ModelGateway(broken, embedding_dimensions=1024)
    with pytest.raises(ValueError) as caught:
        await run_judge(
            [example],
            tmp_path / "failed.jsonl",
            gateway=gateway,
            allow_model_send=True,
            authorization_note="授权说明",
            expected_provider="deterministic_test",
            expected_model="fake-chat",
        )
    message = str(caught.value)
    assert example.example_id in message
    assert "已完成=0/1" in message
    assert "依然不是 JSON" in message
    assert not (tmp_path / "failed.jsonl").exists()
