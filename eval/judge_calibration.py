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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import text

from app.core.db import close_database, session_factory
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider, ProviderResponseError
from workpilot_ai.types import Message

SCHEMA_VERSION = "judge-calibration.v1"
# v2 相对 v1 只增加两条边界澄清，是在 calibration 51 条上标注时撞出来的歧义；
# 两条都不改变 v1 下已给出的任何一个标签，只是把当时的口头判据写成规则。
# validation 19 条在 v2 冻结之后才标，不得反向用于再改 rubric。
RUBRIC_ID = "answer-correctness-binary.v2"
# 二分类标签集。按升序排列，最后一个是"正确"。
# 注意：标签只有两档时，quadratic weighted kappa 退化为无权重的 Cohen's kappa
# ——相邻分歧不再被平方权重折价，因此同一个 min_qwk 数值比三档时**更难**达标。
LABELS: tuple[int, ...] = (0, 1)
LABEL_COUNT = len(LABELS)
RUBRIC = """评价实际答案相对问题与参考答案的正确性，只能使用以下两档：
1（正确）：核心结论正确且覆盖参考答案的全部必要要点；允许措辞和非关键细节不同。
0（不正确）：核心结论错误、遗漏任一必要要点、与问题无关、在可回答时拒答，
或在不可回答时编造答案。

边界规则：
- 参考答案用于确定事实与必要要点，不按表面词语重合打分。
- "必要要点"指回答问题所问的那部分事实；参考答案里问题没问到的附带细节缺失不降分。
- 部分正确一律记 0：核心方向对但缺关键事实、比较对象错、数值错，都不算正确。
- 不因答案更长而加分；出现与参考答案冲突的主张记 0。概括或四舍五入本身不算冲突：
  若概括值紧接着被正确数值限定，不因此记 0；与参考答案矛盾的数值才记 0。
- 清单类问题：覆盖参考答案的全部条目即记 1；额外补充的、与参考答案不冲突的正确内容
  既不加分也不减分。漏掉任一条目记 0。
- 对标记为不可回答的问题，明确且合适的拒答计 1，编造事实计 0。
- 先形成可复核理由，再给分；不得从已有自动指标推断分数。
"""
SYSTEM_PROMPT = """你是评测校准 Judge。严格按给定 rubric 独立判断答案正确性。
只输出一个 JSON 对象，字段顺序必须为 reason、score：
{"reason":"具体说明命中、遗漏或错误的要点","score":0}
reason 不得为空；score 只能是整数 0、1。不要输出 Markdown 或额外文字。
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
# 类别切片是诊断线索还是验收门槛。当前数据规模撑不起逐类判定, 默认 report_only。
SliceGatePolicy = Literal["report_only", "enforce"]
SLICE_GATE_POLICIES: tuple[SliceGatePolicy, ...] = ("report_only", "enforce")
# 只要类别切片不设门禁, 这句话就必须跟着报告走, 不允许把整体达标读成逐类达标。
SLICE_REPORT_ONLY_CAVEAT = (
    "类别级可靠性未验证：本次验收只卡整体 QWK/accuracy，"
    "类别切片仅作诊断，通过不代表每个类别都达标。"
)
EXPECTED_CATEGORIES = (
    "single_hop",
    "multi_hop",
    "table",
    "temporal",
    "unanswerable",
    "global",
    "agent_task",
)
# 当前 70 条 dev 基线只覆盖六类；agent_task 必须等真实 Agent 执行闭环后显式加入。
INTERIM_JUDGE_CATEGORIES = tuple(
    category for category in EXPECTED_CATEGORIES if category != "agent_task"
)
DEFAULT_JUDGE_CASES = 70
DEFAULT_MIN_VALIDATION_CASES = 17
# heavy Judge 是 reasoning 模型：推理 token 先于 content 产出，且计入同一个 max_tokens。
# 实测长样本上 500 会被推理耗尽，content 恒为 null，整批 fail-closed。2048 留足余量。
# 该值不进 prompt_fingerprint（不改变发给模型的文本），但会影响能否拿到结构化输出，
# 因此改动必须记账。
JUDGE_MAX_TOKENS = 2048
# 同问题最多补跑几次。与 E5 的 evidence gate 同政策：只吸收单点抖动，不放宽校验。
JUDGE_REPAIR_ATTEMPTS = 1


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
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
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
    expected_categories: Sequence[str] = INTERIM_JUDGE_CATEGORIES,
    seed: int = DEFAULT_SEED,
    validation_ratio: float = 0.25,
    min_cases: int = DEFAULT_JUDGE_CASES,
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
    # 仍然只是一个 case，不能把重复 metric 行凑到门槛。直接拒绝比静默去重更可审计。
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
                "split": "validation" if str(core["item_id"]) in validation_ids else "calibration",
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
        split: dict(sorted(Counter(row.category for row in examples if row.split == split).items()))
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
        "status": "awaiting_human_labels_and_model_authorization" if ready else "pending",
        "metric": "answer_correctness",
        "label_scale": list(LABELS),
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
            "human_review_guide": "human-review-guide.md",
            "judge_predictions": "judge-predictions.jsonl",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "examples.jsonl", (asdict(row) for row in examples))
    _write_label_template(output_dir / "human-labels.csv", examples)
    _write_human_review_guide(output_dir / "human-review-guide.md", examples)
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


def load_human_labels(path: Path, examples: Sequence[CalibrationExample]) -> dict[str, HumanLabel]:
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
            if value not in {str(label) for label in LABELS}:
                raise ValueError(f"{path}:{line}: score 必须是 {_label_hint()}")
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
                authorization_note_fingerprint=str(row["authorization_note_fingerprint"]),
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
                raise ValueError(f"DB 缺少 eval_result: run={row['run_id']} item={row['item_id']}")
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
    if gateway.chat_provider != expected_provider or gateway.chat_model != expected_model:
        raise ValueError(
            "Judge gateway 配置身份不符: "
            f"expected={expected_provider}/{expected_model}, "
            f"configured={gateway.chat_provider}/{gateway.chat_model}"
        )
    predictions: list[JudgePrediction] = []
    authorization_fingerprint = hashlib.sha256(authorization_note.strip().encode()).hexdigest()
    repairs = 0
    for example in examples:
        # 服务端在连续批处理下即使 temperature=0 也非严格确定，偶发空 content 或截断。
        # 与 E5 的 evidence gate 同一条政策：同问题最多补一次，第二次仍非法继续
        # fail-closed。不放宽校验，只吸收单点抖动。
        result = None
        failure: Exception | None = None
        for attempt in range(1 + JUDGE_REPAIR_ATTEMPTS):
            try:
                result = await gateway.complete(
                    [
                        Message(role="system", content=SYSTEM_PROMPT),
                        Message(role="user", content=_render_prompt(example)),
                    ],
                    task_type="judge",
                    max_tokens=JUDGE_MAX_TOKENS,
                    temperature=0.0,
                )
                if result.provider != expected_provider or result.model != expected_model:
                    raise ValueError(
                        "Judge 实际模型身份漂移，禁止 fallback 或混跑: "
                        f"expected={expected_provider}/{expected_model}, "
                        f"actual={result.provider}/{result.model}"
                    )
                reason, score = _parse_judge_response(result.text)
                repairs += attempt
                break
            except ValueError as exc:
                if "身份漂移" in str(exc):
                    raise
                failure = exc
            except ProviderResponseError as exc:
                failure = exc
        else:
            # 70 次串行调用死在第 N 条却不说是哪条，等于让人从头猜。
            # 继续 fail-closed，但必须把定位信息和原始输出带出来。
            raise ValueError(
                f"Judge 响应重试 {JUDGE_REPAIR_ATTEMPTS} 次后仍无法解析: "
                f"example_id={example.example_id} category={example.category} "
                f"split={example.split} 已完成={len(predictions)}/{len(examples)}；"
                f"{failure}；raw={str(getattr(result, 'text', None))[:400]!r}"
            ) from failure
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
        # 补跑次数必须报出来：如果它不是 0，说明端点在抖，结论的可复现性要打折扣。
        "repair_retries": repairs,
    }


FREEZE_FILENAME = "rubric-freeze.json"


def freeze_rubric(bundle_dir: Path, *, note: str, labels_path: Path) -> dict[str, object]:
    """把 "rubric 已冻结" 从一句声明变成可校验的产物。

    冻结的意义在于：rubric 只能依据 calibration 修，改完就锁死，validation 必须在
    锁死之后独立标注，不能反过来调 rubric。所以这里 fail-closed 两件事：

    - 冻结时 calibration 必须已经标完（否则"依据 calibration 修 rubric"无从谈起）；
    - 冻结时 validation 一条都不能有标签（否则等于先看答案再定标尺）。
    """
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["rubric_fingerprint"] != rubric_fingerprint():
        raise ValueError("bundle 的 rubric 指纹与当前代码不一致，先重新 prepare 再冻结")
    examples = {row.example_id: row for row in load_examples(bundle_dir / "examples.jsonl")}
    labeled: dict[str, str] = {}
    with labels_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            example_id = (row.get("example_id") or "").strip()
            if (row.get("score") or "").strip():
                labeled[example_id] = (row.get("score") or "").strip()
    calibration = {i for i, row in examples.items() if row.split == "calibration"}
    validation = {i for i, row in examples.items() if row.split == "validation"}
    unlabeled = sorted(calibration - set(labeled))
    if unlabeled:
        raise ValueError(f"calibration 尚未标完，不能冻结: 缺 {len(unlabeled)} 条 {unlabeled[:3]}")
    peeked = sorted(validation & set(labeled))
    if peeked:
        raise ValueError(f"validation 已有标签，冻结失去意义: {peeked[:3]}")
    record = {
        "schema_version": SCHEMA_VERSION,
        "frozen_at": datetime.now(UTC).isoformat(),
        "rubric_id": RUBRIC_ID,
        "rubric_fingerprint": rubric_fingerprint(),
        "prompt_fingerprint": prompt_fingerprint(),
        "example_set_fingerprint": manifest["example_set_fingerprint"],
        "label_scale": list(LABELS),
        "frozen_on": {
            "split": "calibration",
            "labeled": len(labeled),
            "source": labels_path.name,
            "label_digest": sha256_json(sorted(labeled.items())),
        },
        "validation_labeled_at_freeze": 0,
        "note": note,
        "rule": "validation 只能在本记录之后标注，且不得反向用于修改 rubric 或 Judge prompt",
    }
    (bundle_dir / FREEZE_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def assert_rubric_frozen(freeze_path: Path) -> dict[str, object]:
    """验收前确认 rubric 自冻结以来没被动过。"""
    record = json.loads(freeze_path.read_text(encoding="utf-8"))
    if record["rubric_id"] != RUBRIC_ID:
        raise ValueError(f"rubric_id 自冻结后已变更: {record['rubric_id']} -> {RUBRIC_ID}")
    if record["rubric_fingerprint"] != rubric_fingerprint():
        raise ValueError("rubric 内容自冻结后已变更，验收结果不可信")
    if record["prompt_fingerprint"] != prompt_fingerprint():
        raise ValueError("Judge prompt 自冻结后已变更，验收结果不可信")
    return record


def calibration_report(
    *,
    examples: Sequence[CalibrationExample],
    human_labels: Mapping[str, HumanLabel],
    predictions: Mapping[str, JudgePrediction],
    output_dir: Path,
    rubric_freeze: Path | None = None,
    min_samples: int = DEFAULT_JUDGE_CASES,
    min_validation_samples: int = DEFAULT_MIN_VALIDATION_CASES,
    min_qwk: float = 0.85,
    min_accuracy: float = 0.85,
    min_slice_accuracy: float = 0.70,
    # 低于这个样本量的类别切片不判 accuracy(见下方切片定位的注释)。
    min_slice_samples: int = 5,
    # 类别切片默认只报告不设门禁; 数据规模够了再切 "enforce"。
    slice_gate_policy: SliceGatePolicy = "report_only",
    required_categories: Sequence[str] = INTERIM_JUDGE_CATEGORIES,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    ci_level: float = DEFAULT_CI_LEVEL,
) -> dict[str, object]:
    _validate_thresholds(
        min_samples, min_qwk, min_accuracy, min_slice_accuracy, resamples, ci_level
    )
    freeze = assert_rubric_frozen(rubric_freeze) if rubric_freeze else None
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
        split: agreement_metrics([human[i] for i in indexes], [judge[i] for i in indexes])
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
        validation_category = [i for i in validation_indexes if examples[i].category == category]
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
    missing_validation_categories = sorted(set(required_categories) - validation_categories)
    # 类别切片的定位: 诊断线索, 不是验收门槛(默认 report_only)。
    #
    # 为什么不设门禁: 6 类 × 每类至少 5 条 ⇒ validation 至少 30 条, 按 0.25 的比例
    # 反推需要约 120 个 Judge case, 而当前 dev 基线只有 70 条。在 2~3 条的切片上
    # accuracy 只能取 0 / 0.5 / 0.67 / 1 几个值, 错一条就必然跌破 0.70——那报的是
    # 样本量, 不是 Judge 质量。在噪声上硬凑一个数字, 比不报更糟。
    #
    # 代价必须写明: 通过验收**不代表**每个类别都可靠, 类别级可靠性在当前规模下
    # 根本没有被验证。报告里因此强制带上这句话, 不允许读成"逐类都达标"。
    # 数据规模上来后把 policy 切成 enforce 即可, 判据本身一直在算。
    gated_slices: list[str] = []
    insufficient_slices: dict[str, int] = {}
    for name, value in sorted(slices.items()):
        if not name.startswith("validation/category:"):
            continue
        sample_count = int(value["sample_count"])  # type: ignore[call-overload]
        if sample_count < min_slice_samples:
            insufficient_slices[name] = sample_count
            value["slice_status"] = "insufficient_samples"
            value["slice_status_reason"] = (
                f"validation 样本 {sample_count} < {min_slice_samples}, 不足以判定类别准确率"
            )
            continue
        value["slice_status"] = "interpretable"
        gated_slices.append(name)
    low_slices = sorted(
        name
        for name in gated_slices
        if isinstance(slices[name].get("accuracy"), int | float)
        and float(slices[name]["accuracy"]) < min_slice_accuracy  # type: ignore[arg-type]
    )
    for name in low_slices:
        slices[name]["slice_status"] = "below_threshold"
    enforce_slices = slice_gate_policy == "enforce"
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
        failures.append(f"validation_missing_categories:{','.join(missing_validation_categories)}")
    if "agent_task" in category_names:
        failures.append("agent_task_execution_closure_pending")
    if gate_metrics["qwk"] is None or float(gate_metrics["qwk"]) < min_qwk:
        failures.append(f"qwk<{min_qwk}")
    if float(gate_metrics["accuracy"]) < min_accuracy:
        failures.append(f"accuracy<{min_accuracy}")
    if enforce_slices and low_slices:
        failures.append(f"low_slice_accuracy:{','.join(low_slices)}")
    if enforce_slices and insufficient_slices:
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
        "actual_models": sorted({f"{row.provider}/{row.model}" for row in predictions.values()}),
        "fallback_enabled": False,
        "rubric_freeze": {"frozen_at": freeze["frozen_at"], "path": str(rubric_freeze)}
        if freeze
        else None,
        "thresholds": {
            "min_samples": min_samples,
            "min_validation_samples": min_validation_samples,
            "min_qwk": min_qwk,
            "min_accuracy": min_accuracy,
            "min_slice_accuracy": min_slice_accuracy,
            "min_slice_samples": min_slice_samples,
            "slice_gate_policy": slice_gate_policy,
            "required_categories": list(required_categories),
        },
        "slice_gate": {
            "policy": slice_gate_policy,
            "enforced": enforce_slices,
            "interpretable": gated_slices,
            "below_threshold": low_slices,
            "insufficient_samples": insufficient_slices,
            "caveat": None if enforce_slices else SLICE_REPORT_ONLY_CAVEAT,
            "note": "样本量不足的切片不判准确率；样本足够但低于阈值的切片记为 "
            "below_threshold。report_only 下两者都不进 gate_failures，只作诊断。",
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
    matrix = [[0] * LABEL_COUNT for _ in range(LABEL_COUNT)]
    for actual, predicted in zip(human, judge, strict=True):
        matrix[actual][predicted] += 1
    return {
        "sample_count": len(human),
        "accuracy": sum(a == b for a, b in zip(human, judge, strict=True)) / len(human),
        "qwk": quadratic_weighted_kappa(human, judge),
        "confusion_matrix": {
            "rows": "human",
            "columns": "judge",
            "labels": list(LABELS),
            "values": matrix,
            "human_marginal": [sum(row) for row in matrix],
            "judge_marginal": [
                sum(matrix[i][j] for i in range(LABEL_COUNT)) for j in range(LABEL_COUNT)
            ],
        },
    }


def quadratic_weighted_kappa(human: Sequence[int], judge: Sequence[int]) -> float | None:
    """二次加权 kappa。

    标签只有两档时权重矩阵退化为 0/1，本函数等价于无权重的 Cohen's kappa；
    这不是近似，是数学恒等——不要因为名字里有 quadratic 就以为二分类下仍有折价。
    """
    if len(human) != len(judge) or not human:
        raise ValueError("QWK 需要数量相同且非空的配对标签")
    count = len(human)
    observed = [[0] * LABEL_COUNT for _ in range(LABEL_COUNT)]
    human_hist = [0] * LABEL_COUNT
    judge_hist = [0] * LABEL_COUNT
    for actual, predicted in zip(human, judge, strict=True):
        actual = _score(actual)
        predicted = _score(predicted)
        observed[actual][predicted] += 1
        human_hist[actual] += 1
        judge_hist[predicted] += 1
    observed_disagreement = 0.0
    expected_disagreement = 0.0
    span = LABEL_COUNT - 1
    for i in range(LABEL_COUNT):
        for j in range(LABEL_COUNT):
            weight = ((i - j) / span) ** 2
            observed_disagreement += weight * observed[i][j] / count
            expected_disagreement += weight * human_hist[i] * judge_hist[j] / (count * count)
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
    # 有限 validation 集的普通 bootstrap 会偶尔抽到单一标签。此时 accuracy 仍有定义，
    # 但 Kappa 的 chance-agreement 分母为 0，数学上不可定义。旧实现直接丢掉这些 QWK，
    # 随后又要求 effective_resamples == requested_resamples，导致只要发生一次合法的退化抽样，
    # 整批校准就必然失败。n=19、少数类=5 时这不是异常，而是约 0.3% 的预期事件。
    #
    # 这里保持 paired percentile bootstrap：退化样本整对丢弃，并用同一个 RNG 确定性补抽，
    # 直到 accuracy/QWK 同时得到请求数量。若原始标签本身常量，或在 10 倍尝试内仍凑不齐，
    # 继续 fail-closed；报告显式记录尝试数与丢弃数，不能把补抽藏起来。
    original_qwk = quadratic_weighted_kappa(human, judge)
    max_attempts = resamples * 10
    attempts = 0
    while len(qwk_samples) < resamples and attempts < max_attempts:
        attempts += 1
        indexes = [rng.randrange(len(human)) for _ in human]
        left = [human[i] for i in indexes]
        right = [judge[i] for i in indexes]
        qwk = quadratic_weighted_kappa(left, right)
        if qwk is not None and math.isfinite(qwk):
            accuracy_samples.append(
                sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
            )
            qwk_samples.append(qwk)
        if original_qwk is None and attempts >= resamples:
            # 原始样本就没有标签方差时，补抽不可能让 QWK 变得可解释。
            break
    return {
        "method": "paired percentile bootstrap with deterministic redraw for undefined QWK",
        "seed": seed,
        "resamples": resamples,
        "attempted_resamples": attempts,
        "discarded_undefined_qwk": attempts - len(qwk_samples),
        "max_attempts": max_attempts,
        "ci_level": ci_level,
        "accuracy": _bootstrap_summary(accuracy_samples, resamples, ci_level),
        "qwk": _bootstrap_summary(qwk_samples, resamples, ci_level),
    }


def _bootstrap_summary(values: list[float], resamples: int, ci_level: float) -> dict[str, object]:
    values.sort()
    tail = (1 - ci_level) / 2
    return {
        "ci_low": _percentile(values, tail) if values else None,
        "ci_high": _percentile(values, 1 - tail) if values else None,
        "effective_resamples": len(values),
        "requested_resamples": resamples,
    }


def _parse_judge_response(text: str) -> tuple[str, int]:
    # reasoning 模型会把 content 留空、token 全花在推理上。这时报"不是 JSON"会把人
    # 引到 prompt 上去查，真正的原因是 max_tokens 不够。所以单独识别这一种失败。
    if not text or not text.strip():
        raise ValueError(
            f"Judge 返回空 content；reasoning 模型可能已耗尽 max_tokens"
            f"（当前 {JUDGE_MAX_TOKENS}），调大后重跑"
        )
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
        "split",
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
        for row in sorted(
            examples,
            key=lambda item: (
                item.split != "calibration",
                item.dataset,
                item.category,
                item.example_id,
            ),
        ):
            writer.writerow(
                {
                    "example_id": row.example_id,
                    "example_fingerprint": row.example_fingerprint,
                    "split": row.split,
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


def _write_human_review_guide(path: Path, examples: Sequence[CalibrationExample]) -> None:
    split_counts = Counter(row.split for row in examples)
    lines = [
        "# Judge 人工正确性复核说明",
        "",
        f"本批共 {len(examples)} 条：calibration {split_counts['calibration']}，"
        f"validation {split_counts['validation']}。",
        "",
        "## 评分",
        "",
        "- `1`：答案与 gold 含义一致，问题所问的必要要点齐全，没有实质错误。",
        "- `0`：错误、遗漏必要要点、比较对象或数值不对、与问题无关，"
        "或对 answerable 题直接拒答；unanswerable 题冒答也记 0。",
        "",
        "二分类没有中间档：**部分正确记 0**。判定只看问题问到的要点，"
        "gold 里问题没问到的附带细节缺失不降分。",
        "",
        "## 填写纪律",
        "",
        "- 只填写 `score/reason/reviewer/reviewed_at`，不要修改冻结内容或 fingerprint。",
        "- `reason` 写可复核的具体差异，不能只写“对/错”。",
        "- 先完成 calibration；rubric 若需修改，只能依据 calibration。",
        "- rubric 冻结后再独立完成 validation，validation 不得用于调 rubric 或 Judge prompt。",
        "- AI 建议、规则指标和 Judge 预测都不能冒充 human；reviewer 必须是真实复核人。",
        "",
        "正式填写文件：`human-labels.csv`。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        status = str(metrics.get("slice_status", ""))
        status_cell = {
            "interpretable": "可解读",
            "below_threshold": "低于阈值",
            "insufficient_samples": "样本不足",
        }.get(status, "—")
        lines.append(
            f"| {name} | {metrics['sample_count']} | {_fmt(metrics['accuracy'])} | "
            f"{_fmt(metrics['qwk'])} | {status_cell} |"
        )
    slice_gate = payload.get("slice_gate")
    if isinstance(slice_gate, dict):
        caveat = slice_gate.get("caveat")
        if caveat:
            lines.extend(
                [
                    "",
                    f"> **{caveat}**",
                    "",
                    "类别切片在当前规模下只作诊断，不进 `gate_failures`：6 类各要 5 条可解读样本，",
                    "意味着 validation 至少 30 条、约 120 个 Judge case，超出 70 条 dev 基线。",
                    "在 2~3 条样本上判类别准确率，报出来的是抽样噪声而不是 Judge 质量。",
                    "数据规模上来后用 `--slice-gate-policy enforce` 打开，判据一直在算。",
                ]
            )
        insufficient = slice_gate.get("insufficient_samples")
        if isinstance(insufficient, dict) and insufficient:
            detail = "、".join(
                f"`{name.split(':', 1)[1]}`（n={count}）"
                for name, count in sorted(insufficient.items())
            )
            lines.extend(["", f"样本不足、未判定准确率的类别切片：{detail}。"])
        below = slice_gate.get("below_threshold")
        if isinstance(below, list) and below:
            names = "、".join(f"`{name.split(':', 1)[1]}`" for name in below)
            lines.extend(
                ["", f"样本足够但准确率低于阈值的类别切片：{names}——诊断线索，需人工归因。"]
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


def _label_hint() -> str:
    return "/".join(str(label) for label in LABELS)


def _score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in set(LABELS):
        raise ValueError(f"score 必须是整数 {_label_hint()}")
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
    for line, line_text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
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
    if any(not 0 <= value <= 1 for value in (min_qwk, min_accuracy, min_slice_accuracy)):
        raise ValueError("一致性门槛必须位于 [0,1]")
    if not 0 < ci_level < 1:
        raise ValueError("ci_level 必须位于 (0,1)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线 Judge 校准数据、跑批与门禁")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="从 generation report 导出待标注校准包")
    prepare.add_argument("--generation-report", action="append", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument("--validation-ratio", type=float, default=0.25)
    prepare.add_argument("--min-cases", type=int, default=DEFAULT_JUDGE_CASES)

    freeze = sub.add_parser(
        "freeze", help="calibration 标完后锁死 rubric；validation 只能在此之后标"
    )
    freeze.add_argument("--bundle-dir", type=Path, required=True)
    freeze.add_argument(
        "--labels", type=Path, required=True, help="据以冻结的 calibration 标签文件"
    )
    freeze.add_argument("--note", default="", help="冻结说明，例如 v1->v2 改了什么")

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
    # 不给就用端点默认值，而各档的默认值并不相同：main 开着思考链，heavy 没开。
    # 跨档比 Judge 时这就不是受控对照了——思考链会顶在 JSON 前面，严格解析直接失败，
    # 那测出来的是"能不能闭嘴"，不是"判得准不准"。所以它必须能显式指定。
    run.add_argument(
        "--enable-thinking",
        dest="enable_thinking",
        action="store_true",
        default=None,
    )
    run.add_argument("--no-enable-thinking", dest="enable_thinking", action="store_false")

    import_cmd = sub.add_parser("import", help="校验标签/输出并生成或应用版本化 DB patch")
    import_cmd.add_argument("--examples", type=Path, required=True)
    import_cmd.add_argument("--human-labels", type=Path, required=True)
    import_cmd.add_argument("--judge-predictions", type=Path, required=True)
    import_cmd.add_argument("--output", type=Path, required=True)
    import_cmd.add_argument("--apply", action="store_true")

    calibrate = sub.add_parser("calibrate", help="离线计算一致性并执行 fail-closed 门禁")
    calibrate.add_argument("--examples", type=Path, required=True)
    calibrate.add_argument("--human-labels", type=Path, required=True)
    calibrate.add_argument("--judge-predictions", type=Path, required=True)
    calibrate.add_argument("--output-dir", type=Path, required=True)
    calibrate.add_argument(
        "--rubric-freeze",
        type=Path,
        help="冻结记录路径；给了就校验 rubric/prompt 自冻结后未被改动",
    )
    calibrate.add_argument("--min-samples", type=int, default=DEFAULT_JUDGE_CASES)
    calibrate.add_argument(
        "--min-validation-samples", type=int, default=DEFAULT_MIN_VALIDATION_CASES
    )
    calibrate.add_argument("--min-qwk", type=float, default=0.85)
    calibrate.add_argument("--min-accuracy", type=float, default=0.85)
    calibrate.add_argument("--min-slice-accuracy", type=float, default=0.70)
    calibrate.add_argument("--min-slice-samples", type=int, default=5)
    calibrate.add_argument(
        "--slice-gate-policy",
        choices=list(SLICE_GATE_POLICIES),
        default="report_only",
        help="类别切片是诊断(report_only)还是验收门槛(enforce); 样本量够了再切 enforce",
    )
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
        enable_thinking=args.enable_thinking,
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
    elif args.command == "freeze":
        result = freeze_rubric(args.bundle_dir, note=args.note, labels_path=args.labels)
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
            rubric_freeze=args.rubric_freeze,
            min_samples=args.min_samples,
            min_validation_samples=args.min_validation_samples,
            min_slice_samples=args.min_slice_samples,
            slice_gate_policy=args.slice_gate_policy,
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
