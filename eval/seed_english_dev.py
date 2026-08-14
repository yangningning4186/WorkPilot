# ruff: noqa: E501

"""纯英文标注集。

存在的理由(台账 E2): 40 篇语料里 20 篇是英文论文, 但 `core-dev` /
`multihop-test-v1` / `dense-title-smoke` 三个集 49 条题**全部含中文, 0 条纯英文**。
结果是英文检索路径完全没有门禁——E2 修好了英文词法失明(同一条查询的词法 Top-10
从 0 条命中判别词变成 6 条), 却**一个指标都测不出来**。

两条硬约束写进了校验:

1. **问题必须是纯英文**, 出现任何 CJK 字符即拒绝。混了中文就等于没测英文路径。
2. **问题不得从 gold 块里抄词**。连续 4 个词与原文重合即拒绝。
   否则词法臂靠字面重合就能命中, 测出来的是抄袭度不是检索能力。

gold span 锚定在 `parsed_blocks` 的字符区间(约束 8), 种子里只写
(标题, block_idx), 字符区间在入库时从激活版本现取, 重新分块不会让标注失效。

origin='synthetic': 问题与 gold answer 由 AI 依据原文构造, 事实逐条核对过原始块,
但**没有经过作者亲手标注**, 与 `core-dev` 的 origin='human' 不是一个证据等级。
"""

import argparse
import asyncio
import json
import re
from dataclasses import dataclass

from sqlalchemy import text
from uuid6 import uuid7

from app.core.db import close_database, session_factory

DATASET_NAME = "english-dev"

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[a-z0-9]+")
# 4 个词的窗口: 3 个词太容易在英文里自然撞上("the number of"), 5 个词又几乎放行一切。
_LEAK_WINDOW = 4


@dataclass(frozen=True)
class ItemSeed:
    category: str
    difficulty: int
    question: str
    gold_answer: str
    # (文档标题前缀, block_idx); unanswerable 项为空。
    evidence: tuple[tuple[str, int], ...]
    must_include: tuple[str, ...]


ITEMS = (
    # ---------- single_hop ----------
    ItemSeed(
        category="single_hop",
        difficulty=1,
        question="Which three actions did the ReAct authors expose in their Wikipedia API?",
        gold_answer=(
            "search[entity] returns the first five sentences of the entity page or suggests "
            "top-5 similar entities, lookup[string] returns the next sentence containing the "
            "string like Ctrl+F, and finish[answer] ends the task with an answer."
        ),
        evidence=(("REACT", 24),),
        must_include=("search", "lookup", "finish"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=1,
        question="Which frozen language model is prompted in the main ReAct experiments?",
        gold_answer=(
            "A frozen PaLM-540B, prompted with few-shot in-context examples that are human "
            "trajectories of actions, thoughts and environment observations."
        ),
        evidence=(("REACT", 18),),
        must_include=("PaLM-540B", "few-shot"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=2,
        question="How well do people score on GAIA compared with GPT-4, and how big is the benchmark?",
        gold_answer=(
            "Human respondents reach 92% while GPT-4 with plugins reaches 15%. The benchmark "
            "has 466 questions, with answers to 300 of them withheld to power a leaderboard."
        ),
        evidence=(("GAIA", 4),),
        must_include=("92%", "15%", "466", "300"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=2,
        question="What margin does SimpleMem claim over Mem0, and how much cheaper is it?",
        gold_answer=(
            "SimpleMem outperforms Mem0 by 26.4% in F1 while reducing inference token "
            "consumption by 30x compared with full-context models."
        ),
        evidence=(("SimpleMem", 9),),
        must_include=("26.4%", "30", "F1"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=2,
        question="What Pass@1 numbers does MetaGPT report on the two code generation benchmarks?",
        gold_answer=(
            "85.9% on HumanEval and 87.7% on MBPP, both state of the art; MetaGPT also reports "
            "a 100% task completion rate."
        ),
        evidence=(("METAGPT", 15),),
        must_include=("85.9%", "87.7%", "HumanEval", "MBPP"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=2,
        question="How successful is Reflexion on AlfWorld and what does plain ReAct achieve there?",
        gold_answer=(
            "Reflexion reaches a 97% success discovery rate on AlfWorld within 12 autonomous "
            "trials, against 75% for the base ReAct agent."
        ),
        evidence=(("Reflexion", 11),),
        must_include=("97%", "75%", "12"),
    ),
    ItemSeed(
        category="single_hop",
        difficulty=1,
        question="How many REST APIs were collected for ToolBench and where did they come from?",
        gold_answer=(
            "16,464 REST APIs crawled from RapidAPI, spanning 49 diverse categories such as "
            "social media, e-commerce and weather."
        ),
        evidence=(("TOOLLLM", 13),),
        must_include=("16,464", "RapidAPI", "49"),
    ),
    # ---------- table ----------
    ItemSeed(
        category="table",
        difficulty=2,
        question="On HotpotQA with PaLM-540B, what does plain ReAct prompting score and which method wins?",
        gold_answer=(
            "ReAct alone gets 27.4 exact match. The best HotpotQA result is ReAct -> CoT-SC at "
            "35.1, ahead of CoT-SC -> ReAct at 34.2."
        ),
        evidence=(("REACT", 27),),
        must_include=("27.4", "35.1"),
    ),
    ItemSeed(
        category="table",
        difficulty=2,
        question="On the T-REx subset of LAMA, how does Toolformer stack up against the much larger GPT-3?",
        gold_answer=(
            "Toolformer scores 53.5 on T-REx versus 39.8 for GPT-3 (175B), despite being far "
            "smaller; the disabled Toolformer variant only reaches 34.9."
        ),
        evidence=(("Toolformer", 74),),
        must_include=("53.5", "39.8"),
    ),
    ItemSeed(
        category="table",
        difficulty=3,
        question="How many question answering samples survive each filtering threshold in Toolformer?",
        gold_answer=("51,987 examples at a threshold of 0.5, 18,526 at 1.0 and 5,135 at 2.0."),
        evidence=(("Toolformer", 55),),
        must_include=("51,987", "18,526", "5,135"),
    ),
    ItemSeed(
        category="table",
        difficulty=2,
        question="What top-1 accuracy does the VOYAGER skill retrieval reach?",
        gold_answer=("80.2 plus or minus 3.0 at top-1, rising to 96.5 plus or minus 0.3 at top-5."),
        evidence=(("VOYAGER", 367),),
        must_include=("80.2", "96.5"),
    ),
    ItemSeed(
        category="table",
        difficulty=3,
        question="In AgentBench, how do commercial and open-source models differ in completion and invalid actions?",
        gold_answer=(
            "Commercial API-based models complete 61.5% of tasks with 4.6% invalid actions, "
            "while open-sourced models complete 39.1% with 13.6% invalid actions."
        ),
        evidence=(("AGENTBENCH", 493),),
        must_include=("61.5%", "39.1%", "4.6%", "13.6%"),
    ),
    # ---------- multi_hop ----------
    ItemSeed(
        category="multi_hop",
        difficulty=3,
        question="Which model backs the original ReAct experiments, and what does layering self-reflection on top of it achieve in AlfWorld?",
        gold_answer=(
            "ReAct prompts a frozen PaLM-540B with few-shot in-context examples. Adding "
            "Reflexion's self-reflection on top of ReAct reaches a 97% success discovery rate "
            "on AlfWorld in 12 autonomous trials, versus 75% for base ReAct."
        ),
        evidence=(("REACT", 18), ("Reflexion", 11)),
        must_include=("PaLM-540B", "97%", "75%"),
    ),
    ItemSeed(
        category="multi_hop",
        difficulty=3,
        question="Comparing the two agent benchmarks in the library, how many questions does one hold and how many models and environments does the other cover?",
        gold_answer=(
            "GAIA devises 466 questions and withholds answers to 300 of them. AgentBench "
            "evaluates 29 LLMs across 8 distinct environments."
        ),
        evidence=(("GAIA", 4), ("AGENTBENCH", 9)),
        must_include=("466", "29", "8"),
    ),
    ItemSeed(
        category="multi_hop",
        difficulty=3,
        question="What false positive rates are reported for the reasoning-only and the interleaved method, and which failure mode dominates each?",
        gold_answer=(
            "CoT has a 14% false positive rate against 6% for ReAct. Hallucination is CoT's "
            "dominant failure mode at 56% and 0% for ReAct, whereas ReAct's main failures are "
            "reasoning errors at 47% and non-informative search results at 23%."
        ),
        evidence=(("REACT", 34), ("REACT", 36)),
        must_include=("14%", "6%", "56%", "23%"),
    ),
    ItemSeed(
        category="multi_hop",
        difficulty=3,
        question="How do the two tool-use papers differ in the scale of APIs they work with?",
        gold_answer=(
            "ToolLLM gathers 16,464 REST APIs from RapidAPI across 49 categories, while "
            "Toolformer uses a handful of tools whose training examples are filtered per API, "
            "for instance 18,526 question answering examples at a threshold of 1.0."
        ),
        evidence=(("TOOLLLM", 13), ("Toolformer", 55)),
        must_include=("16,464", "RapidAPI", "18,526"),
    ),
    # ---------- unanswerable ----------
    # 同主题 hard negative: 每条都落在语料覆盖的领域内, 但答案确实不在库里。
    ItemSeed(
        category="unanswerable",
        difficulty=2,
        question="What is the monthly subscription price of the ReAct API for commercial users?",
        gold_answer="",
        evidence=(),
        must_include=(),
    ),
    ItemSeed(
        category="unanswerable",
        difficulty=3,
        question="Which Kubernetes version is required to run the AgentBench operating system environment?",
        gold_answer="",
        evidence=(),
        must_include=(),
    ),
    ItemSeed(
        category="unanswerable",
        difficulty=3,
        question="How many kilograms of CO2 were emitted while training Toolformer?",
        gold_answer="",
        evidence=(),
        must_include=(),
    ),
    ItemSeed(
        category="unanswerable",
        difficulty=3,
        question="How many engineers does DeepWisdom employ on the MetaGPT team?",
        gold_answer="",
        evidence=(),
        must_include=(),
    ),
)


def _leaked_ngrams(question: str, block_text: str) -> list[str]:
    """问题里与 gold 原文连续重合的词窗。非空即说明题目是抄的, 会给词法臂送分。"""
    words = _WORD.findall(question.lower())
    haystack = " ".join(_WORD.findall(block_text.lower()))
    windows = (
        " ".join(words[index : index + _LEAK_WINDOW])
        for index in range(len(words) - _LEAK_WINDOW + 1)
    )
    return [window for window in windows if window in haystack]


async def seed_english_dev() -> int:
    async with session_factory() as session, session.begin():
        dataset_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO eval_datasets (id, name, split, version, description)
                    VALUES (:id, :name, 'dev', '1', :description)
                    ON CONFLICT (name) DO UPDATE SET
                        split=EXCLUDED.split,
                        version=EXCLUDED.version,
                        description=EXCLUDED.description
                    RETURNING id
                    """
                ),
                {
                    "id": uuid7(),
                    "name": DATASET_NAME,
                    "description": (
                        "纯英文标注集; 补上 core-dev / multihop-test-v1 全中文留下的英文检索"
                        "盲区 (台账 E2)。AI 辅助构造后按激活版本原文核对, 并强制问题不含 CJK、"
                        "不与 gold 原文连续重合 4 个词"
                    ),
                },
            )
        ).scalar_one()
        await session.execute(
            text("DELETE FROM eval_items WHERE dataset_id=:dataset_id"),
            {"dataset_id": dataset_id},
        )

        prefixes = sorted({prefix for item in ITEMS for prefix, _ in item.evidence})
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT d.title, b.block_idx, b.version_id, b.char_start,
                               b.char_end, b.text, p.prefix
                        FROM unnest(CAST(:prefixes AS text[])) AS p(prefix)
                        JOIN documents d ON d.title LIKE p.prefix || '%'
                        JOIN document_versions v ON v.document_id=d.id
                          AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                        JOIN parsed_blocks b ON b.version_id=v.id
                        WHERE d.deleted_at IS NULL
                        """
                    ),
                    {"prefixes": prefixes},
                )
            )
            .mappings()
            .all()
        )
        blocks = {(str(row["prefix"]), int(row["block_idx"])): row for row in rows}
        ambiguous = {
            prefix
            for prefix in prefixes
            if len({row["title"] for row in rows if row["prefix"] == prefix}) > 1
        }
        if ambiguous:
            raise RuntimeError(f"标题前缀不唯一, 会锚错文档: {sorted(ambiguous)}")

        values: list[dict[str, object]] = []
        for item in ITEMS:
            if _CJK.search(item.question):
                raise RuntimeError(f"英文集不允许出现 CJK: {item.question}")
            if (item.category == "unanswerable") != (not item.evidence):
                raise RuntimeError(f"category 与 gold span 有无不一致: {item.question}")
            missing = [key for key in item.evidence if key not in blocks]
            if missing:
                raise RuntimeError(f"找不到激活版本证据块: {missing}")
            for key in item.evidence:
                leaked = _leaked_ngrams(item.question, str(blocks[key]["text"]))
                if leaked:
                    raise RuntimeError(f"问题与 gold 原文连续重合: {leaked} —— {item.question}")
            spans = [
                {
                    "version_id": str(blocks[key]["version_id"]),
                    "char_start": int(blocks[key]["char_start"]),
                    "char_end": int(blocks[key]["char_end"]),
                    "quote": str(blocks[key]["text"]),
                    "note": "纯英文标注集",
                }
                for key in item.evidence
            ]
            values.append(
                {
                    "id": uuid7(),
                    "dataset_id": dataset_id,
                    "category": item.category,
                    "question": item.question,
                    "gold_answer": item.gold_answer or None,
                    "gold_spans": json.dumps(spans, ensure_ascii=False),
                    "constraints": json.dumps(
                        {"must_include": item.must_include, "must_not_include": []},
                        ensure_ascii=False,
                    ),
                    "difficulty": item.difficulty,
                }
            )
        await session.execute(
            text(
                """
                INSERT INTO eval_items
                   (id, dataset_id, category, question, gold_answer, gold_spans,
                     constraints, difficulty, origin)
                VALUES
                   (:id, :dataset_id, :category, :question, :gold_answer,
                     CAST(:gold_spans AS jsonb), CAST(:constraints AS jsonb),
                     :difficulty, 'synthetic')
                """
            ),
            values,
        )
    await close_database()
    return len(ITEMS)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成纯英文标注集")
    parser.parse_args()
    count = asyncio.run(seed_english_dev())
    print(f"dataset={DATASET_NAME} items={count}")


if __name__ == "__main__":
    main()
