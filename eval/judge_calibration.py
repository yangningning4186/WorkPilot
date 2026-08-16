"""离线 LLM-as-Judge 校准闭环。

该模块把“准备数据 / 人工标注 / Judge 跑批 / 一致性验收”拆成四个可审计阶段。
只有 ``run`` 子命令会接触模型，并且必须显式传入发送授权；其余阶段完全离线。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from app.core.db import close_database, session_factory
from app.llm.gateway import ModelGateway
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.types import Message
from sqlalchemy import text

SCHEMA_VERSION = "judge-calibration.v1"
RUBRIC_ID = "answer-correctness-3-level.v1"
RUBRIC = """评价实际答案相对问题与参考答案的正确性，只能使用以下有序三档：
2（正确）：核心结论正确且覆盖参考答案的必要要点；允许措辞和非关键细节不同。
1（部分正确）：至少一个核心要点正确，但有重要遗漏、含糊或局部错误，仍有实质帮助。
0（错误）：核心结论错误、与问题无关、在可回答时拒答，或在不可回答时编造答案。

边界规则：
- 参考答案用于确定事实与必要要点，不按表面词语重合打分。
- 不因答案更长而加分；多出的错误主张必须降分。
- 对标记为不可回答的问题，明确且合适的拒答计 2，编造事实计 0。
- 先形成可复核理由，再给分；不得从已有自动指标推断分数。
"""
SYSTEM_PROMPT = """你是评测校准 Judge。严格按给定 rubric 独立判断答案正确性。
只输出一个 JSON 对象，字段顺序必须为 reason、score：
{"reason":"具体说明命中、遗漏或错误的要点","score":0}
reason 不得为空；score 只能是整数 0、1、2。不要输出 Markdown 或额外文字。
"""
PROMPT_TEMPLATE = """Rubric:
{rubric}

样本类别: {category}
是否可回答: {answerable}
问题:
{question}

参考答案:
{gold_answer}

实际答案:
{answer}
"""

DEFAULT_SEED = 12345
DEFAULT_RESAMPLES = 10_000
DEFAULT_CI_LEVEL = 0.95
EXPECTED_CATEGORIES = (
    "single_hop",
    "multi_hop",
    "table",
    "temporal",
    "unanswerable",
    "global",
    "agent_task",
)


class JudgeGateway(Protocol):
    chat_model: str
    chat_provider: str

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str,
        max_tokens: int,
        temperature: float,
    ) -> Any: ...


@dataclass(frozen=True)
class CalibrationExample:
    example_id: str
    source_run_id: str
    item_id: str
    dataset: str
    category: str
    answerable: bool
    split: str
    question: str
    gold_answer: str
    answer: str
    citations: tuple[dict[str, object], ...]
    example_fingerprint: str


@dataclass(frozen=True)
class HumanLabel:
    example_id: str
    example_fingerprint: str
    score: int
    reason: str
    reviewer: str
    reviewed_at: str


@dataclass(frozen=True)
class JudgePrediction:
    example_id: str
    example_fingerprint: str
    rubric_id: str
    rubric_fingerprint: str
    prompt_fingerprint: str
    score: int
    reason: str
    model: str
    provider: str
    raw_output: str
    input_tokens: int
    output_tokens: int
    authorization_note_fingerprint: str


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def rubric_fingerprint() -> str:
    return sha256_json({"rubric_id": RUBRIC_ID, "rubric": RUBRIC})


def prompt_fingerprint() -> str:
    return sha256_json(
        {
            "system_prompt": SYSTEM_PROMPT,
            "prompt_template": PROMPT_TEMPLATE,
            "rubric_fingerprint": rubric_fingerprint(),
        }
    )


def prepare_bundle(
    report_paths: Sequence[Path],
    output_dir: Path,
    *,
    expected_categories: Sequence[str] = EXPECTED_CATEGORIES,
    seed: int = DEFAULT_SEED,
    validation_ratio: float = 0.25,
    min_cases: int = 80,
) -> dict[str, object]:
    if not report_paths:
        raise ValueError("至少需要一份 generation report")
    examples: list[CalibrationExample] = []
    pending_cores: list[dict[str, object]] = []
    reports: list[dict[str, object]] = []
    if not 0 < validation_ratio < 1:
        raise ValueError("validation_ratio 必须位于 (0,1)")
    if min_cases < 1:
        raise ValueError("min_cases 必须是正整数")
    # 校准 case 的唯一性由底层 eval item 决定。同一题在四个策略 report 中出现四次，
    # 仍然只是一个 case，不能把重复 metric 行凑成“80 条”。直接拒绝比静默去重更可审计。
    seen: dict[tuple[str, str], str] = {}
    for path in report_paths:
        raw = path.read_bytes()
        payload = json.loads(raw)
        config = payload.get("config")
        is_generation = payload.get("report_type") == "generation_baseline" or (
            isinstance(config, dict) and config.get("track") == "generation"
        )
        if not is_generation:
            raise ValueError(f"{path}: 只接受 generation_baseline report")
        run_id = _required_text(payload, "run_id", source=path)
        dataset = _required_text(payload, "dataset", source=path)
        reports.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "run_id": run_id,
                "dataset": dataset,
                "config_hash": payload.get("config_hash"),
            }
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise TypeError(f"{path}: items 必须是数组")
        for item in items:
            if not isinstance(item, dict):
                raise TypeError(f"{path}: item 必须是对象")
            if item.get("error"):
                raise ValueError(f"{path}: item {item.get('item_id')} 存在生成错误")
            item_id = _required_text(item, "item_id", source=path)
            key = (dataset, item_id)
            if key in seen:
                raise ValueError(
                    "同一 eval item 出现在多份 report，不能重复计作唯一 calibration case: "
                    f"dataset={dataset}, item_id={item_id}, runs={seen[key]},{run_id}"
                )
            seen[key] = run_id
            core = {
                "source_run_id": run_id,
                "item_id": item_id,
                "dataset": dataset,
                "category": _required_text(item, "category", source=path),
                "answerable": _required_bool(item, "answerable", source=path),
                "question": _required_text(item, "question", source=path),
                # unanswerable 的 gold_answer 合法地为空；是否可答由 answerable/category 决定。
                "gold_answer": _required_string(item, "gold_answer", source=path),
                "answer": _required_text(item, "answer", source=path),
                "citations": tuple(item.get("citations") or []),
            }
            pending_cores.append(core)
    by_category: dict[str, list[dict[str, object]]] = {}
    for core in pending_cores:
        by_category.setdefault(str(core["category"]), []).append(core)
    for category_cores in by_category.values():
        ordered = sorted(
            category_cores,
            key=lambda core: _split_rank(str(core["item_id"]), seed=seed),
        )
        validation_count = round(len(ordered) * validation_ratio)
        if len(ordered) >= 2:
            validation_count = min(len(ordered) - 1, max(1, validation_count))
        validation_ids = {str(core["item_id"]) for core in ordered[:validation_count]}
        for core in ordered:
            materialized = {
                **core,
                "split": "validation"
                if str(core["item_id"]) in validation_ids
                else "calibration",
            }
            fingerprint = sha256_json(materialized)
            example_id = hashlib.sha256(
                f"{materialized['source_run_id']}:{materialized['item_id']}:{fingerprint}".encode()
            ).hexdigest()[:24]
            examples.append(
                CalibrationExample(
                    **materialized,
                    example_id=example_id,
                    example_fingerprint=fingerprint,
                )
            )
    examples.sort(key=lambda row: (row.source_run_id, row.item_id))
    categories = Counter(row.category for row in examples)
    missing_categories = sorted(set(expected_categories) - set(categories))
    unexpected_categories = sorted(set(categories) - set(expected_categories))
    # 当前项目还没有 agent_task 的执行/判分闭环。即使扩集已出现该类别，
    # 也不能把“有题”误报成“可正式校准”；待闭环落地后再显式移除此能力阻断。
    unsupported = sorted(set(expected_categories) & {"agent_task"})
    split_counts = Counter(row.split for row in examples)
    split_category_counts = {
        split: dict(
            sorted(
                Counter(row.category for row in examples if row.split == split).items()
            )
        )
        for split in ("calibration", "validation")
    }
    missing_split_categories = {
        split: sorted(set(categories) - set(counts))
        for split, counts in split_category_counts.items()
    }
    ready = (
        len(examples) >= min_cases
        and not missing_categories
        and not unexpected_categories
        and not unsupported
        and not any(missing_split_categories.values())
    )
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "awaiting_human_labels_and_model_authorization"
        if ready
        else "pending",
        "metric": "answer_correctness",
        "label_scale": [0, 1, 2],
        "rubric_id": RUBRIC_ID,
        "rubric_fingerprint": rubric_fingerprint(),
        "prompt_fingerprint": prompt_fingerprint(),
        "example_count": len(examples),
        "minimum_unique_cases": min_cases,
        "unique_case_count_ready": len(examples) >= min_cases,
        "unique_case_definition": "unique (dataset,item_id); repeated strategy/run rows are forbidden",
        "example_set_fingerprint": sha256_json(
            [(row.example_id, row.example_fingerprint) for row in examples]
        ),
        "category_counts": dict(sorted(categories.items())),
        "expected_categories": list(expected_categories),
        "missing_categories": missing_categories,
        "unexpected_categories": unexpected_categories,
        "category_coverage_ready": not missing_categories,
        "unsupported_categories": unsupported,
        "execution_closure_ready": not unsupported,
        "split_policy": {
            "method": "deterministic stratified hash",
            "seed": seed,
            "validation_ratio": validation_ratio,
            "counts": dict(sorted(split_counts.items())),
            "category_counts": split_category_counts,
            "missing_categories": missing_split_categories,
            "coverage_ready": not any(missing_split_categories.values()),
            "rule": "rubric may be revised on calibration only; acceptance gate uses validation",
        },
        "model_send_authorized": False,
        "fallback_enabled": False,
        "reports": reports,
        "files": {
            "examples": "examples.jsonl",
            "human_labels": "human-labels.csv",
            "judge_predictions": "judge-predictions.jsonl",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "examples.jsonl", (asdict(row) for row in examples))
    _write_label_template(output_dir / "human-labels.csv", examples)
    (output_dir / "rubric.txt").write_text(RUBRIC, encoding="utf-8")
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def load_examples(path: Path) -> list[CalibrationExample]:
    rows = _read_jsonl(path)
    examples: list[CalibrationExample] = []
    seen: set[str] = set()
    for line, row in rows:
        try:
            citations = row.get("citations", [])
            if not isinstance(citations, list):
                raise TypeError("citations 必须为数组")
            example = CalibrationExample(
                example_id=str(row["example_id"]),
                source_run_id=str(row["source_run_id"]),
                item_id=str(row["item_id"]),
                dataset=str(row["dataset"]),
                category=str(row["category"]),
                answerable=_strict_bool(row["answerable"]),
                split=str(row["split"]),
                question=str(row["question"]),
                gold_answer=str(row["gold_answer"]),
                answer=str(row["answer"]),
                citations=tuple(citations),
                example_fingerprint=str(row["example_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line}: 无效校准样本: {exc}") from exc
        core = {
            key: value
            for key, value in asdict(example).items()
            if key not in {"example_id", "example_fingerprint"}
        }
        if sha256_json(core) != example.example_fingerprint:
            raise ValueError(f"{path}:{line}: example 内容漂移")
        expected_id = hashlib.sha256(
            f"{example.source_run_id}:{example.item_id}:{example.example_fingerprint}".encode()
        ).hexdigest()[:24]
        if example.example_id != expected_id:
            raise ValueError(f"{path}:{line}: example_id 与内容不匹配")
        if example.example_id in seen:
            raise ValueError(f"{path}:{line}: 重复 example_id {example.example_id}")
        seen.add(example.example_id)
        if example.split not in {"calibration", "validation"}:
            raise ValueError(f"{path}:{line}: split 必须是 calibration/validation")
        examples.append(example)
    return examples


def load_human_labels(
    path: Path, examples: Sequence[CalibrationExample]
) -> dict[str, HumanLabel]:
    expected = {row.example_id: row for row in examples}
    labels: dict[str, HumanLabel] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            example_id = (row.get("example_id") or "").strip()
            if example_id in labels:
                raise ValueError(f"{path}:{line}: 重复人工标签 {example_id}")
            if example_id not in expected:
                raise ValueError(f"{path}:{line}: 未知 example_id {example_id}")
            fingerprint = (row.get("example_fingerprint") or "").strip()
            if fingerprint != expected[example_id].example_fingerprint:
                raise ValueError(f"{path}:{line}: example 内容已漂移 {example_id}")
            value = (row.get("score") or "").strip()
            if value not in {"0", "1", "2"}:
                raise ValueError(f"{path}:{line}: score 必须是 0/1/2")
            label = HumanLabel(
                example_id=example_id,
                example_fingerprint=fingerprint,
                score=int(value),
                reason=(row.get("reason") or "").strip(),
                reviewer=(row.get("reviewer") or "").strip(),
                reviewed_at=(row.get("reviewed_at") or "").strip(),
            )
            if not all((label.reason, label.reviewer, label.reviewed_at)):
                raise ValueError(f"{path}:{line}: 人工标签归因字段不完整")
            labels[example_id] = label
    missing = sorted(set(expected) - set(labels))
    if missing:
        raise ValueError(f"人工标签不完整: missing={missing}")
    return labels


def load_judge_predictions(
    path: Path, examples: Sequence[CalibrationExample]
) -> dict[str, JudgePrediction]:
    expected = {row.example_id: row for row in examples}
    predictions: dict[str, JudgePrediction] = {}
    for line, row in _read_jsonl(path):
        try:
            prediction = JudgePrediction(
                example_id=str(row["example_id"]),
                example_fingerprint=str(row["example_fingerprint"]),
                rubric_id=str(row["rubric_id"]),
                rubric_fingerprint=str(row["rubric_fingerprint"]),
                prompt_fingerprint=str(row["prompt_fingerprint"]),
                score=_score(row["score"]),
                reason=str(row["reason"]).strip(),
                model=str(row["model"]).strip(),
                provider=str(row["provider"]).strip(),
                raw_output=str(row["raw_output"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                authorization_note_fingerprint=str(
                    row["authorization_note_fingerprint"]
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line}: 无效 Judge 输出: {exc}") from exc
        example = expected.get(prediction.example_id)
        if example is None:
            raise ValueError(f"{path}:{line}: 未知 example_id {prediction.example_id}")
        if prediction.example_id in predictions:
            raise ValueError(f"{path}:{line}: 重复 Judge 输出 {prediction.example_id}")
        if prediction.example_fingerprint != example.example_fingerprint:
            raise ValueError(f"{path}:{line}: Judge 输入内容已漂移")
        if prediction.rubric_id != RUBRIC_ID:
            raise ValueError(f"{path}:{line}: rubric_id 漂移")
        if prediction.rubric_fingerprint != rubric_fingerprint():
            raise ValueError(f"{path}:{line}: rubric 内容漂移")
        if prediction.prompt_fingerprint != prompt_fingerprint():
            raise ValueError(f"{path}:{line}: prompt 内容漂移")
        if not all(
            (
                prediction.reason,
                prediction.model,
                prediction.provider,
                prediction.raw_output,
                prediction.authorization_note_fingerprint,
            )
        ):
            raise ValueError(f"{path}:{line}: Judge 理由或模型身份为空")
        if prediction.input_tokens < 0 or prediction.output_tokens < 0:
            raise ValueError(f"{path}:{line}: token audit 不能为负")
        predictions[prediction.example_id] = prediction
    missing = sorted(set(expected) - set(predictions))
    if missing:
        raise ValueError(f"Judge 输出不完整: missing={missing}")
    identities = {(row.provider, row.model) for row in predictions.values()}
    if len(identities) != 1:
        raise ValueError(f"Judge 跑批混入多个实际模型身份: {sorted(identities)}")
    return predictions


def build_import_rows(
    examples: Sequence[CalibrationExample],
    human_labels: Mapping[str, HumanLabel],
    predictions: Mapping[str, JudgePrediction],
) -> list[dict[str, object]]:
    """生成 DB 导入行；payload 只占用版本化 ``judge_calibration`` namespace。

    M0 已有的 ``human_label.citation_accuracy`` 不在该 namespace 内，应用时必须保留。
    """
    if set(human_labels) != {row.example_id for row in examples}:
        raise ValueError("人工标签集合与 calibration case 不一致")
    if set(predictions) != {row.example_id for row in examples}:
        raise ValueError("Judge 输出集合与 calibration case 不一致")
    rows: list[dict[str, object]] = []
    for example in examples:
        human = human_labels[example.example_id]
        judge = predictions[example.example_id]
        rows.append(
            {
                "example_id": example.example_id,
                "run_id": example.source_run_id,
                "item_id": example.item_id,
                "namespace": "judge_calibration",
                "rubric_id": RUBRIC_ID,
                "metric": "answer_correctness",
                "human_payload": {
                    "score": human.score,
                    "reason": human.reason,
                    "reviewer": human.reviewer,
                    "reviewed_at": human.reviewed_at,
                    "example_fingerprint": example.example_fingerprint,
                },
                "judge_payload": {
                    "score": judge.score,
                    "reason": judge.reason,
                    "raw_output": judge.raw_output,
                    "rubric_fingerprint": judge.rubric_fingerprint,
                    "prompt_fingerprint": judge.prompt_fingerprint,
                    "model": judge.model,
                    "provider": judge.provider,
                    "input_tokens": judge.input_tokens,
                    "output_tokens": judge.output_tokens,
                    "authorization_note_fingerprint": judge.authorization_note_fingerprint,
                    "example_fingerprint": example.example_fingerprint,
                },
            }
        )
    return rows


async def import_to_database(
    rows: Sequence[Mapping[str, object]], *, apply: bool
) -> dict[str, object]:
    status = "applied" if apply else "validated"
    changed = 0
    reused = 0
    async with session_factory() as session:
        for row in rows:
            stored = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT human_label, judge_raw
                        FROM eval_results
                        WHERE run_id=CAST(:run_id AS uuid) AND item_id=CAST(:item_id AS uuid)
                        """
                        ),
                        {"run_id": row["run_id"], "item_id": row["item_id"]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if stored is None:
                raise ValueError(
                    f"DB 缺少 eval_result: run={row['run_id']} item={row['item_id']}"
                )
            human = dict(stored["human_label"] or {})
            judge = dict(stored["judge_raw"] or {})
            human_changed = _merge_calibration_namespace(
                human,
                rubric_id=str(row["rubric_id"]),
                metric=str(row["metric"]),
                payload=row["human_payload"],
            )
            judge_changed = _merge_calibration_namespace(
                judge,
                rubric_id=str(row["rubric_id"]),
                metric=str(row["metric"]),
                payload=row["judge_payload"],
            )
            if human_changed or judge_changed:
                changed += 1
                if apply:
                    await session.execute(
                        text(
                            """
                            UPDATE eval_results
                            SET human_label=CAST(:human AS jsonb), judge_raw=CAST(:judge AS jsonb)
                            WHERE run_id=CAST(:run_id AS uuid) AND item_id=CAST(:item_id AS uuid)
                            """
                        ),
                        {
                            "run_id": row["run_id"],
                            "item_id": row["item_id"],
                            "human": json.dumps(human, ensure_ascii=False),
                            "judge": json.dumps(judge, ensure_ascii=False),
                        },
                    )
            else:
                reused += 1
        if apply:
            await session.commit()
        else:
            await session.rollback()
    await close_database()
    return {"status": status, "rows": len(rows), "changed": changed, "reused": reused}


def _merge_calibration_namespace(
    root: dict[str, object], *, rubric_id: str, metric: str, payload: object
) -> bool:
    namespace = root.setdefault("judge_calibration", {})
    if not isinstance(namespace, dict):
        raise TypeError("已有 judge_calibration namespace 不是对象")
    rubric = namespace.setdefault(rubric_id, {})
    if not isinstance(rubric, dict):
        raise TypeError(f"已有 rubric namespace {rubric_id} 不是对象")
    if metric in rubric:
        if rubric[metric] != payload:
            raise ValueError(f"已有 {rubric_id}/{metric} 与待导入内容冲突")
        return False
    rubric[metric] = payload
    return True


async def run_judge(
    examples: Sequence[CalibrationExample],
    output_path: Path,
    *,
    gateway: JudgeGateway,
    allow_model_send: bool,
    authorization_note: str,
    expected_provider: str,
    expected_model: str,
) -> dict[str, object]:
    if not allow_model_send or not authorization_note.strip():
        raise PermissionError("未获得模型发送授权；需要显式授权标志和授权说明")
    if output_path.exists():
        predictions = load_judge_predictions(output_path, examples)
        return {
            "status": "complete",
            "reused": True,
            "prediction_count": len(predictions),
            "actual_models": sorted(
                {f"{row.provider}/{row.model}" for row in predictions.values()}
            ),
        }
    if (
        gateway.chat_provider != expected_provider
        or gateway.chat_model != expected_model
    ):
        raise ValueError(
            "Judge gateway 配置身份不符: "
            f"expected={expected_provider}/{expected_model}, "
            f"configured={gateway.chat_provider}/{gateway.chat_model}"
        )
    predictions: list[JudgePrediction] = []
    authorization_fingerprint = hashlib.sha256(
        authorization_note.strip().encode()
    ).hexdigest()
    for example in examples:
        result = await gateway.complete(
            [
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=_render_prompt(example)),
            ],
            task_type="judge",
            max_tokens=500,
            temperature=0.0,
        )
        if result.provider != expected_provider or result.model != expected_model:
            raise ValueError(
                "Judge 实际模型身份漂移，禁止 fallback 或混跑: "
                f"expected={expected_provider}/{expected_model}, "
                f"actual={result.provider}/{result.model}"
            )
        reason, score = _parse_judge_response(result.text)
        predictions.append(
            JudgePrediction(
                example_id=example.example_id,
                example_fingerprint=example.example_fingerprint,
                rubric_id=RUBRIC_ID,
                rubric_fingerprint=rubric_fingerprint(),
                prompt_fingerprint=prompt_fingerprint(),
                score=score,
                reason=reason,
                model=str(result.model),
                provider=str(result.provider),
                raw_output=str(result.text),
                input_tokens=int(result.usage.input_tokens),
                output_tokens=int(result.usage.output_tokens),
                authorization_note_fingerprint=authorization_fingerprint,
            )
        )
    identities = {(row.provider, row.model) for row in predictions}
    if len(identities) != 1:
        raise ValueError(f"Judge 跑批混入多个实际模型身份: {sorted(identities)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_path, (asdict(row) for row in predictions))
    return {
        "status": "complete",
        "reused": False,
        "prediction_count": len(predictions),
        "actual_models": sorted(f"{p}/{m}" for p, m in identities),
    }


def calibration_report(
    *,
    examples: Sequence[CalibrationExample],
    human_labels: Mapping[str, HumanLabel],
    predictions: Mapping[str, JudgePrediction],
    output_dir: Path,
    min_samples: int = 80,
    min_validation_samples: int = 20,
    min_qwk: float = 0.85,
    min_accuracy: float = 0.85,
    min_slice_accuracy: float = 0.70,
    # 低于这个样本量的类别切片不判 accuracy(见下方切片保护的注释)。
    min_slice_samples: int = 5,
    required_categories: Sequence[str] = EXPECTED_CATEGORIES,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
) -> dict[str, object]:
    _validate_thresholds(
        min_samples, min_qwk, min_accuracy, min_slice_accuracy, resamples, ci_level
    )
    ids = [row.example_id for row in examples]
    if set(human_labels) != set(ids) or set(predictions) != set(ids):
        raise ValueError("example / human / judge 集合不一致，拒绝生成部分覆盖报告")
    human = [human_labels[key].score for key in ids]
    judge = [predictions[key].score for key in ids]
    overall = agreement_metrics(human, judge)
    split_indexes = {
        split: [i for i, row in enumerate(examples) if row.split == split]
        for split in ("calibration", "validation")
    }
    if not all(split_indexes.values()):
        raise ValueError("calibration/validation 任一 split 为空，拒绝报告")
    split_metrics = {
        split: agreement_metrics(
            [human[i] for i in indexes], [judge[i] for i in indexes]
        )
        for split, indexes in split_indexes.items()
    }
    validation_indexes = split_indexes["validation"]
    validation_human = [human[i] for i in validation_indexes]
    validation_judge = [judge[i] for i in validation_indexes]
    bootstrap = bootstrap_agreement(
        validation_human,
        validation_judge,
        seed=seed,
        resamples=resamples,
        ci_level=ci_level,
    )
    gate_metrics = split_metrics["validation"]
    slices: dict[str, dict[str, object]] = {}
    category_names = sorted({row.category for row in examples})
    for category in category_names:
        indexes = [i for i, row in enumerate(examples) if row.category == category]
        slices[f"all/category:{category}"] = agreement_metrics(
            [human[i] for i in indexes], [judge[i] for i in indexes]
        )
        validation_category = [
            i for i in validation_indexes if examples[i].category == category
        ]
        if validation_category:
            slices[f"validation/category:{category}"] = agreement_metrics(
                [human[i] for i in validation_category],
                [judge[i] for i in validation_category],
            )
    for answerable in (True, False):
        indexes = [i for i, row in enumerate(examples) if row.answerable is answerable]
        if indexes:
            slices[f"all/answerable:{str(answerable).lower()}"] = agreement_metrics(
                [human[i] for i in indexes], [judge[i] for i in indexes]
            )
    for dataset in sorted({row.dataset for row in examples}):
        indexes = [i for i, row in enumerate(examples) if row.dataset == dataset]
        slices[f"all/dataset:{dataset}"] = agreement_metrics(
            [human[i] for i in indexes], [judge[i] for i in indexes]
        )
    missing_categories = sorted(set(required_categories) - set(category_names))
    unexpected_categories = sorted(set(category_names) - set(required_categories))
    validation_categories = {examples[i].category for i in validation_indexes}
    missing_validation_categories = sorted(
        set(required_categories) - validation_categories
    )
    # 切片样本量保护: 2~3 条的切片上, accuracy 只能取 0 / 0.5 / 0.67 / 1 这几个值,
    # 错一条就必然跌破 0.70。不设下限的话, 门禁报的是"Judge 在这个类别上不行",
    # 实际说的只是"这个类别的样本太少, 判不了"——把抽样噪声写成质量结论。
    # 所以样本不足的切片不判 accuracy, 但也**不当作通过**: 它是一条独立的、
    # 指向"需要更多 validation 样本"的失败原因, 与"Judge 准确率不达标"分开。
    # 同一口径见 strategy_matrix: 零分母指标标记为不可用, 永远不写成 0。
    gated_slices: list[str] = []
    insufficient_slices: dict[str, int] = {}
    for name, value in sorted(slices.items()):
        if not name.startswith("validation/category:"):
            continue
        sample_count = int(value["sample_count"])  # type: ignore[call-overload]
        if sample_count < min_slice_samples:
            insufficient_slices[name] = sample_count
            value["accuracy_gate"] = "unavailable"
            value["accuracy_gate_reason"] = (
                f"validation 样本 {sample_count} < {min_slice_samples}, 不足以判定类别准确率"
            )
            continue
        value["accuracy_gate"] = "ok"
        gated_slices.append(name)
    low_slices = sorted(
        name
        for name in gated_slices
        if isinstance(slices[name].get("accuracy"), int | float)
        and float(slices[name]["accuracy"]) < min_slice_accuracy  # type: ignore[arg-type]
    )
    failures: list[str] = []
    if len(examples) < min_samples:
        failures.append(f"sample_count<{min_samples}")
    if len(validation_indexes) < min_validation_samples:
        failures.append(f"validation_sample_count<{min_validation_samples}")
    if missing_categories:
        failures.append(f"missing_categories:{','.join(missing_categories)}")
    if unexpected_categories:
        failures.append(f"unexpected_categories:{','.join(unexpected_categories)}")
    if missing_validation_categories:
        failures.append(
            f"validation_missing_categories:{','.join(missing_validation_categories)}"
        )
    if "agent_task" in category_names:
        failures.append("agent_task_execution_closure_pending")
    if gate_metrics["qwk"] is None or float(gate_metrics["qwk"]) < min_qwk:
        failures.append(f"qwk<{min_qwk}")
    if float(gate_metrics["accuracy"]) < min_accuracy:
        failures.append(f"accuracy<{min_accuracy}")
    if low_slices:
        failures.append(f"low_slice_accuracy:{','.join(low_slices)}")
    if insufficient_slices:
        detail = ",".join(
            f"{name.split(':', 1)[1]}={count}"
            for name, count in sorted(insufficient_slices.items())
        )
        failures.append(f"slice_sample_count<{min_slice_samples}:{detail}")
    if bootstrap["qwk"]["effective_resamples"] < resamples:
        failures.append("qwk_bootstrap_incomplete")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "judge_calibration",
        "status": "passed" if not failures else "failed",
        "metric": "answer_correctness",
        "rubric_id": RUBRIC_ID,
        "rubric_fingerprint": rubric_fingerprint(),
        "prompt_fingerprint": prompt_fingerprint(),
        "example_count": len(examples),
        "example_set_fingerprint": sha256_json(
            [(row.example_id, row.example_fingerprint) for row in examples]
        ),
        "actual_models": sorted(
            {f"{row.provider}/{row.model}" for row in predictions.values()}
        ),
        "fallback_enabled": False,
        "thresholds": {
            "min_samples": min_samples,
            "min_validation_samples": min_validation_samples,
            "min_qwk": min_qwk,
            "min_accuracy": min_accuracy,
            "min_slice_accuracy": min_slice_accuracy,
            "min_slice_samples": min_slice_samples,
            "required_categories": list(required_categories),
        },
        "slice_gate": {
            "gated": gated_slices,
            "insufficient_samples": insufficient_slices,
            "note": "样本量不足的切片不判准确率，但也不算通过；它是独立的失败原因，"
            "指向需要更多 validation 样本，不能读成 Judge 在该类别上不合格。",
        },
        "gate_failures": failures,
        "overall": overall,
        "split_metrics": split_metrics,
        "gate_split": "validation",
        "bootstrap": bootstrap,
        "slices": slices,
        "disagreements": [
            {
                "example_id": key,
                "dataset": examples[i].dataset,
                "category": examples[i].category,
                "human_score": human[i],
                "judge_score": judge[i],
                "human_reason": human_labels[key].reason,
                "judge_reason": predictions[key].reason,
            }
            for i, key in enumerate(ids)
            if human[i] != judge[i]
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "report.json", payload)
    (output_dir / "report.md").write_text(_markdown(payload), encoding="utf-8")
    return payload


def agreement_metrics(human: Sequence[int], judge: Sequence[int]) -> dict[str, object]:
    if len(human) != len(judge) or not human:
        raise ValueError("一致率需要数量相同且非空的配对标签")
    for value in (*human, *judge):
        _score(value)
    matrix = [[0, 0, 0] for _ in range(3)]
    for actual, predicted in zip(human, judge, strict=True):
        matrix[actual][predicted] += 1
    return {
        "sample_count": len(human),
        "accuracy": sum(a == b for a, b in zip(human, judge, strict=True)) / len(human),
        "qwk": quadratic_weighted_kappa(human, judge),
        "confusion_matrix": {
            "rows": "human",
            "columns": "judge",
            "labels": [0, 1, 2],
            "values": matrix,
            "human_marginal": [sum(row) for row in matrix],
            "judge_marginal": [sum(matrix[i][j] for i in range(3)) for j in range(3)],
        },
    }


def quadratic_weighted_kappa(
    human: Sequence[int], judge: Sequence[int]
) -> float | None:
    if len(human) != len(judge) or not human:
        raise ValueError("QWK 需要数量相同且非空的配对标签")
    count = len(human)
    observed = [[0 for _ in range(3)] for _ in range(3)]
    human_hist = [0, 0, 0]
    judge_hist = [0, 0, 0]
    for actual, predicted in zip(human, judge, strict=True):
        actual = _score(actual)
        predicted = _score(predicted)
        observed[actual][predicted] += 1
        human_hist[actual] += 1
        judge_hist[predicted] += 1
    observed_disagreement = 0.0
    expected_disagreement = 0.0
    for i in range(3):
        for j in range(3):
            weight = ((i - j) / 2) ** 2
            observed_disagreement += weight * observed[i][j] / count
            expected_disagreement += (
                weight * human_hist[i] * judge_hist[j] / (count * count)
            )
    if expected_disagreement == 0:
        # 双方都只有同一个常量标签时，accuracy 有定义但 chance agreement 的分母为 0，
        # Kappa 不可定义；不能把“没有标签方差”伪报成完美一致。
        return None
    return 1 - observed_disagreement / expected_disagreement


def bootstrap_agreement(
    human: Sequence[int],
    judge: Sequence[int],
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
) -> dict[str, object]:
    if len(human) != len(judge) or not human:
        raise ValueError("bootstrap 需要数量相同且非空的配对标签")
    if resamples < 1 or not 0 < ci_level < 1:
        raise ValueError("无效 bootstrap 参数")
    rng = random.Random(seed)
    accuracy_samples: list[float] = []
    qwk_samples: list[float] = []
    for _ in range(resamples):
        indexes = [rng.randrange(len(human)) for _ in human]
        left = [human[i] for i in indexes]
        right = [judge[i] for i in indexes]
        accuracy_samples.append(
            sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
        )
        qwk = quadratic_weighted_kappa(left, right)
        if qwk is not None and math.isfinite(qwk):
            qwk_samples.append(qwk)
    return {
        "method": "paired percentile bootstrap",
        "seed": seed,
        "resamples": resamples,
        "ci_level": ci_level,
        "accuracy": _bootstrap_summary(accuracy_samples, resamples, ci_level),
        "qwk": _bootstrap_summary(qwk_samples, resamples, ci_level),
    }


def _bootstrap_summary(
    values: list[float], resamples: int, ci_level: float
) -> dict[str, object]:
    values.sort()
    tail = (1 - ci_level) / 2
    return {
        "ci_low": _percentile(values, tail) if values else None,
        "ci_high": _percentile(values, 1 - tail) if values else None,
        "effective_resamples": len(values),
        "requested_resamples": resamples,
    }


def _parse_judge_response(text: str) -> tuple[str, int]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Judge 必须只返回 JSON 对象") from exc
    if not isinstance(payload, dict) or list(payload) != ["reason", "score"]:
        raise ValueError("Judge JSON 字段必须按 reason、score 顺序且不得增减")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Judge reason 不得为空")
    return reason.strip(), _score(payload["score"])


def _render_prompt(example: CalibrationExample) -> str:
    return PROMPT_TEMPLATE.format(
        rubric=RUBRIC,
        category=example.category,
        answerable=str(example.answerable).lower(),
        question=example.question,
        gold_answer=example.gold_answer,
        answer=example.answer,
    )


def _split_rank(item_id: str, *, seed: int) -> bytes:
    """用于 category 内稳定排序，按比例取前段为 validation。"""
    digest = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()
    return digest


def _write_label_template(path: Path, examples: Sequence[CalibrationExample]) -> None:
    fieldnames = [
        "example_id",
        "example_fingerprint",
        "dataset",
        "category",
        "question",
        "gold_answer",
        "answer",
        "score",
        "reason",
        "reviewer",
        "reviewed_at",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in examples:
            writer.writerow(
                {
                    "example_id": row.example_id,
                    "example_fingerprint": row.example_fingerprint,
                    "dataset": row.dataset,
                    "category": row.category,
                    "question": row.question,
                    "gold_answer": row.gold_answer,
                    "answer": row.answer,
                    "score": "",
                    "reason": "",
                    "reviewer": "",
                    "reviewed_at": "",
                }
            )


def _markdown(payload: Mapping[str, object]) -> str:
    overall = payload["split_metrics"]["validation"]
    bootstrap = payload["bootstrap"]
    assert isinstance(overall, dict) and isinstance(bootstrap, dict)
    lines = [
        "# Judge 校准报告",
        "",
        f"- 状态：**{payload['status']}**",
        f"- 样本：{payload['example_count']}",
        f"- 实际模型：`{', '.join(payload['actual_models'])}`",
        f"- rubric：`{payload['rubric_id']}` / `{str(payload['rubric_fingerprint'])[:12]}`",
        f"- prompt：`{str(payload['prompt_fingerprint'])[:12]}`",
        "",
        "## Validation 一致性（门禁口径）",
        "",
        "| 指标 | 点估计 | 95% CI |",
        "|---|---:|---:|",
    ]
    for name in ("accuracy", "qwk"):
        interval = bootstrap[name]
        assert isinstance(interval, dict)
        lines.append(
            f"| {name} | {_fmt(overall[name])} | "
            f"[{_fmt(interval['ci_low'])}, {_fmt(interval['ci_high'])}] |"
        )
    lines.extend(["", "## 门禁", ""])
    failures = payload["gate_failures"]
    assert isinstance(failures, list)
    lines.append("- 通过" if not failures else "- 失败：" + "; ".join(failures))
    lines.extend(
        [
            "",
            "## 分切片一致率",
            "",
            "| 切片 | n | accuracy | QWK | 是否参与门禁 |",
            "|---|---:|---:|---:|:---:|",
        ]
    )
    slices = payload["slices"]
    assert isinstance(slices, dict)
    for name, metrics in slices.items():
        assert isinstance(metrics, dict)
        gate = metrics.get("accuracy_gate")
        gate_cell = {"ok": "是", "unavailable": "样本不足"}.get(str(gate), "—")
        lines.append(
            f"| {name} | {metrics['sample_count']} | {_fmt(metrics['accuracy'])} | "
            f"{_fmt(metrics['qwk'])} | {gate_cell} |"
        )
    slice_gate = payload.get("slice_gate")
    if isinstance(slice_gate, dict) and slice_gate.get("insufficient_samples"):
        insufficient = slice_gate["insufficient_samples"]
        assert isinstance(insufficient, dict)
        detail = "、".join(
            f"`{name.split(':', 1)[1]}`（n={count}）"
            for name, count in sorted(insufficient.items())
        )
        lines.extend(
            [
                "",
                f"**样本不足、未判定准确率的类别切片**：{detail}。",
                "",
                "这些切片标记为不可用而不是失败——2~3 条样本上 accuracy 只能取到少数几个离散值，",
                "错一条就必然跌破阈值，那反映的是样本量而不是 Judge 质量。但它们也**不算通过**：",
                "`gate_failures` 里有一条独立的 `slice_sample_count<N`，指向需要扩大 validation 规模。",
            ]
        )
    lines.extend(
        [
            "",
            "混淆矩阵完整值与逐条分歧理由见 `report.json`。任何缺失/重复标签、",
            "样本漂移、rubric/prompt 漂移或模型身份混杂都会在生成报告前直接失败。",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value: object) -> str:
    return "N/A" if not isinstance(value, int | float) else f"{float(value):.4f}"


def _percentile(values: Sequence[float], quantile: float) -> float:
    position = (len(values) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise ValueError("score 必须是整数 0/1/2")
    return value


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("answerable 必须是布尔值")
    return value


def _required_text(payload: Mapping[str, object], key: str, *, source: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {key} 必须是非空字符串")
    return value


def _required_string(payload: Mapping[str, object], key: str, *, source: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{source}: {key} 必须是字符串")
    return value


def _required_bool(payload: Mapping[str, object], key: str, *, source: Path) -> bool:
    try:
        return _strict_bool(payload.get(key))
    except TypeError as exc:
        raise TypeError(f"{source}: {key} 必须是布尔值") from exc


def _read_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    for line, line_text in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line_text.strip():
            continue
        try:
            value = json.loads(line_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line}: 无效 JSON") from exc
        if not isinstance(value, dict):
            raise TypeError(f"{path}:{line}: JSONL 行必须是对象")
        rows.append((line, value))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    content = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    )
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_thresholds(
    min_samples: int,
    min_qwk: float,
    min_accuracy: float,
    min_slice_accuracy: float,
    resamples: int,
    ci_level: float,
) -> None:
    if min_samples < 1 or resamples < 1:
        raise ValueError("样本门槛和 bootstrap 次数必须为正整数")
    if any(
        not 0 <= value <= 1 for value in (min_qwk, min_accuracy, min_slice_accuracy)
    ):
        raise ValueError("一致性门槛必须位于 [0,1]")
    if not 0 < ci_level < 1:
        raise ValueError("ci_level 必须位于 (0,1)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线 Judge 校准数据、跑批与门禁")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="从 generation report 导出待标注校准包")
    prepare.add_argument(
        "--generation-report", action="append", type=Path, required=True
    )
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--validation-ratio", type=float, default=0.25)
    prepare.add_argument("--min-cases", type=int, default=80)

    run = sub.add_parser("run", help="显式授权后调用 Judge；默认拒绝发送")
    run.add_argument("--examples", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--allow-model-send", action="store_true")
    run.add_argument("--authorization-note", default="")
    run.add_argument("--provider", required=True, choices=["openai_compatible"])
    run.add_argument("--model", required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--api-key-env", default="CLUSTER_API_KEY")
    run.add_argument("--timeout-s", type=float, default=60.0)

    import_cmd = sub.add_parser(
        "import", help="校验标签/输出并生成或应用版本化 DB patch"
    )
    import_cmd.add_argument("--examples", type=Path, required=True)
    import_cmd.add_argument("--human-labels", type=Path, required=True)
    import_cmd.add_argument("--judge-predictions", type=Path, required=True)
    import_cmd.add_argument("--output", type=Path, required=True)
    import_cmd.add_argument("--apply", action="store_true")

    calibrate = sub.add_parser(
        "calibrate", help="离线计算一致性并执行 fail-closed 门禁"
    )
    calibrate.add_argument("--examples", type=Path, required=True)
    calibrate.add_argument("--human-labels", type=Path, required=True)
    calibrate.add_argument("--judge-predictions", type=Path, required=True)
    calibrate.add_argument("--output-dir", type=Path, required=True)
    calibrate.add_argument("--min-samples", type=int, default=80)
    calibrate.add_argument("--min-validation-samples", type=int, default=20)
    calibrate.add_argument("--min-qwk", type=float, default=0.85)
    calibrate.add_argument("--min-accuracy", type=float, default=0.85)
    calibrate.add_argument("--min-slice-accuracy", type=float, default=0.70)
    calibrate.add_argument("--min-slice-samples", type=int, default=5)
    calibrate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    calibrate.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    calibrate.add_argument("--ci-level", type=float, default=DEFAULT_CI_LEVEL)
    return parser.parse_args()


async def _run_cli(args: argparse.Namespace) -> dict[str, object]:
    examples = load_examples(args.examples)
    api_key = os.getenv(args.api_key_env, "")
    provider = OpenAICompatibleProvider(
        base_url=args.base_url,
        api_key=api_key,
        chat_model=args.model,
        embedding_model="judge-does-not-use-embedding",
        timeout_s=args.timeout_s,
        trust_env=False,
    )
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    try:
        return await run_judge(
            examples,
            args.output,
            gateway=gateway,
            allow_model_send=args.allow_model_send,
            authorization_note=args.authorization_note,
            expected_provider=args.provider,
            expected_model=args.model,
        )
    finally:
        await gateway.aclose()


def main() -> None:
    args = _parse_args()
    if args.command == "prepare":
        result = prepare_bundle(
            args.generation_report,
            args.output_dir,
            seed=args.seed,
            validation_ratio=args.validation_ratio,
            min_cases=args.min_cases,
        )
    elif args.command == "run":
        result = asyncio.run(_run_cli(args))
    elif args.command == "import":
        examples = load_examples(args.examples)
        labels = load_human_labels(args.human_labels, examples)
        predictions = load_judge_predictions(args.judge_predictions, examples)
        rows = build_import_rows(examples, labels, predictions)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(args.output, rows)
        result = (
            asyncio.run(import_to_database(rows, apply=True))
            if args.apply
            else {"status": "validated", "rows": len(rows), "output": str(args.output)}
        )
    else:
        examples = load_examples(args.examples)
        labels = load_human_labels(args.human_labels, examples)
        predictions = load_judge_predictions(args.judge_predictions, examples)
        result = calibration_report(
            examples=examples,
            human_labels=labels,
            predictions=predictions,
            output_dir=args.output_dir,
            min_samples=args.min_samples,
            min_validation_samples=args.min_validation_samples,
            min_slice_samples=args.min_slice_samples,
            min_qwk=args.min_qwk,
            min_accuracy=args.min_accuracy,
            min_slice_accuracy=args.min_slice_accuracy,
            seed=args.seed,
            resamples=args.resamples,
            ci_level=args.ci_level,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "calibrate" and result["status"] != "passed":
        sys.exit(2)


if __name__ == "__main__":
    main()
