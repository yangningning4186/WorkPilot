
import argparse
import asyncio
import json
from dataclasses import dataclass

from sqlalchemy import text
from uuid6 import uuid7

from app.core.db import close_database, session_factory

DATASET_NAME = "multihop-test-v1"


@dataclass(frozen=True)
class ItemSeed:
    question: str
    gold_answer: str
    evidence: tuple[tuple[str, int], ...]
    must_include: tuple[str, ...]


ITEMS = (
    ItemSeed(
        question=(
            "MemEvolve 为什么认为固定记忆架构不适合开放式演化，EvolveLab 又用什么统一接口让架构可演化？"
        ),
        gold_answer=(
            "不同任务需要不同的记忆能力，固定的摄取、抽象和检索管线无法随任务适配；EvolveLab "
            "以 BaseMemoryProvider 统一 Encode、Store、Retrieve、Manage 四个组件。"
        ),
        evidence=(("MemEvolve: Meta-Evolution of Agent Memory Systems", 11), ("MemEvolve: Meta-Evolution of Agent Memory Systems", 41)),
        must_include=("不同任务", "BaseMemoryProvider", "Encode", "Store", "Retrieve", "Manage"),
    ),
    ItemSeed(
        question=(
            "EvolveLab 的四组件接口是什么，其中 take_in_memory 主要覆盖哪些阶段，Manage 通常如何实现？"
        ),
        gold_answer=(
            "接口由 Encode、Store、Retrieve、Manage 构成；take_in_memory 主要整合 Encode 和 Store，"
            "Manage 的离线整合或选择性遗忘通常由 provider 辅助方法实现，或在特定生命周期事件触发。"
        ),
        evidence=(("MemEvolve: Meta-Evolution of Agent Memory Systems", 41), ("MemEvolve: Meta-Evolution of Agent Memory Systems", 192)),
        must_include=("Encode", "Store", "Retrieve", "Manage", "take_in_memory", "生命周期"),
    ),
    ItemSeed(
        question=(
            "现有自改进记忆为何被认为收益不稳定，MemEvolve 的跨基准收益与成本表现如何？"
        ),
        gold_answer=(
            "DILU、Dynamic Cheatsheet 和 ExpeL 会在不同基准上退化，说明固定设计缺少任务适配；"
            "MemEvolve 在三个基准上稳定提升 3.54%–5.0%，同时 API 成本与 No-Memory 基线相近。"
        ),
        evidence=(("MemEvolve: Meta-Evolution of Agent Memory Systems", 92), ("MemEvolve: Meta-Evolution of Agent Memory Systems", 94)),
        must_include=("收益不稳定", "3.54%", "5.0%", "No-Memory"),
    ),
    ItemSeed(
        question=(
            "EvolveLab 的 MemoryItem 能承载哪些内容，SkillWeaverProvider 如何生成、存储并向 agent 提供技能？"
        ),
        gold_answer=(
            "MemoryItem 可表示原始文本、蒸馏洞见或可执行代码，并携带时间、置信度和来源元数据；"
            "SkillWeaverProvider 用 LLM 从成功轨迹合成可复用 Python 函数，以代码仓库存储，再通过统一 "
            "MemoryItem 接口动态检索并注入 agent 动作空间。"
        ),
        evidence=(("MemEvolve: Meta-Evolution of Agent Memory Systems", 197), ("MemEvolve: Meta-Evolution of Agent Memory Systems", 203)),
        must_include=("MemoryItem", "Python", "成功轨迹", "动作空间"),
    ),
    ItemSeed(
        question=(
            "MemEvolve 的 TaskCraft 工作子集如何分配到元演化轮次，候选记忆变体可以在哪些维度不同？"
        ),
        gold_answer=(
            "研究从 TaskCraft 收集 300 条工作子集，其中 120 条用于三轮元演化，每轮 40 条；"
            "候选变体可在编码策略、存储规则、检索约束和管理策略上变化。"
        ),
        evidence=(("MemEvolve: Meta-Evolution of Agent Memory Systems", 210), ("MemEvolve: Meta-Evolution of Agent Memory Systems", 71)),
        must_include=("300", "120", "三轮", "40", "编码", "存储", "检索", "管理"),
    ),
    ItemSeed(
        question=(
            "长时间运行的 coding agent 为什么容易退化成贪心局部搜索，SWARMRESEARCH 相对 EvoX 和 CORAL 的任务结果如何？"
        ),
        gold_answer=(
            "长对话历史和单一可编辑程序不保留替代方向，使 agent 围绕一个方案微调；"
            "SWARMRESEARCH 在 15 个任务中超过 EvoX 13 个、持平 1 个，超过 CORAL 10 个、持平 2 个。"
        ),
        evidence=(("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 10), ("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 43)),
        must_include=("替代方向", "13/15", "10/15"),
    ),
    ItemSeed(
        question=(
            "SWARMRESEARCH 的固定扩展对照用了什么实验范围，Signal Processing 任务两种扩展的配置和分数分别是多少，整体结论是什么？"
        ),
        gold_answer=(
            "实验以 Minimax-M2.5 为主模型，只测 5 个任务。Signal Processing 的最优固定扩展为 "
            "15×4、得分 0.6369，编排器引导扩展平均为 6.78×8、得分 0.6787；总体上较短的并行运行"
            "优于较长的串行运行，但并行度并非越高越好。"
        ),
        evidence=(("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 53), ("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 54)),
        must_include=("Minimax-M2.5", "5", "15×4", "0.6369", "6.78×8", "0.6787"),
    ),
    ItemSeed(
        question=(
            "开放式发现技术的跨运行方差为什么难以消除、重复运行代价多大，作者为何仍建议 SWARMRESEARCH 只运行一次？"
        ),
        gold_answer=(
            "高温采样本就是发现能力与方差的来源，稳定估计可能需运行五次以上且次数因任务而异；"
            "三种方法在 15 个任务上单次运行约耗费 1700 美元 Opus 4.6 额度。作者认为独立搜索 agent "
            "本身提供并行扩展并改善稳定性，因此在现实预算下建议运行一次。"
        ),
        evidence=(("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 129), ("SWARMRESEARCH: Orchestrating Coding Agents for Open-Ended Discovery", 131)),
        must_include=("高温采样", "五次", "1700", "独立搜索", "一次"),
    ),
)


async def seed_multihop_test() -> int:
    async with session_factory() as session, session.begin():
        dataset_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO eval_datasets (id, name, split, version, description)
                    VALUES (:id, :name, 'test', '1', :description)
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
                        "独立于 core-dev 的 PDF multi-hop 留出集；只使用 MemEvolve 与 "
                        "SWARMRESEARCH，AI 辅助构造后按激活版本原文严格校验"
                    ),
                },
            )
        ).scalar_one()
        await session.execute(
            text("DELETE FROM eval_items WHERE dataset_id=:dataset_id"),
            {"dataset_id": dataset_id},
        )

        documents = sorted({title for item in ITEMS for title, _ in item.evidence})
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT d.title, b.block_idx, b.version_id, b.char_start,
                               b.char_end, b.text
                        FROM documents d
                        JOIN document_versions v ON v.document_id=d.id
                          AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                        JOIN parsed_blocks b ON b.version_id=v.id
                        WHERE d.deleted_at IS NULL AND d.title=ANY(:titles)
                        """
                    ),
                    {"titles": documents},
                )
            )
            .mappings()
            .all()
        )
        blocks = {(str(row["title"]), int(row["block_idx"])): row for row in rows}

        values: list[dict[str, object]] = []
        for item in ITEMS:
            missing = [key for key in item.evidence if key not in blocks]
            if missing:
                raise RuntimeError(f"找不到激活版本证据块: {missing}")
            spans = [
                {
                    "version_id": str(blocks[key]["version_id"]),
                    "char_start": int(blocks[key]["char_start"]),
                    "char_end": int(blocks[key]["char_end"]),
                    "quote": str(blocks[key]["text"]),
                    "note": "独立 PDF multi-hop 留出集",
                }
                for key in item.evidence
            ]
            values.append(
                {
                    "id": uuid7(),
                    "dataset_id": dataset_id,
                    "question": item.question,
                    "gold_answer": item.gold_answer,
                    "gold_spans": json.dumps(spans, ensure_ascii=False),
                    "constraints": json.dumps(
                        {"must_include": item.must_include, "must_not_include": []},
                        ensure_ascii=False,
                    ),
                }
            )
        await session.execute(
            text(
                """
                INSERT INTO eval_items
                    (id, dataset_id, category, question, gold_answer, gold_spans,
                     constraints, difficulty, origin)
                VALUES
                    (:id, :dataset_id, 'multi_hop', :question, :gold_answer,
                     CAST(:gold_spans AS jsonb), CAST(:constraints AS jsonb), 3, 'synthetic')
                """
            ),
            values,
        )
    await close_database()
    return len(ITEMS)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成独立 PDF multi-hop 留出集")
    parser.parse_args()
    count = asyncio.run(seed_multihop_test())
    print(f"dataset={DATASET_NAME} items={count}")


if __name__ == "__main__":
    main()
