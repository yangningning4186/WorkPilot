import argparse
import asyncio
import json

from app.core.db import close_database, session_factory
from sqlalchemy import text
from uuid6 import uuid7

DATASET_NAME = "dense-title-smoke"


async def seed_title_smoke() -> tuple[int, int]:
    async with session_factory() as session, session.begin():
        dataset_id = (
            await session.execute(
                text(
                    """
                        INSERT INTO eval_datasets (id, name, split, version, description)
                        VALUES (:id, :name, 'dev', '1', :description)
                        ON CONFLICT (name) DO UPDATE SET version=EXCLUDED.version
                        RETURNING id
                        """
                ),
                {
                    "id": uuid7(),
                    "name": DATASET_NAME,
                    "description": "由当前文档标题自动生成，只验证 dense 评测工程链路，不作为质量结论",
                },
            )
        ).scalar_one()
        await session.execute(
            text("DELETE FROM eval_items WHERE dataset_id=:dataset_id"),
            {"dataset_id": dataset_id},
        )
        rows = (
            (
                await session.execute(
                    text(
                        """
                            SELECT v.id AS version_id, d.title, b.char_start, b.char_end, b.text
                            FROM documents d
                            JOIN document_versions v ON v.document_id=d.id
                              AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                            JOIN LATERAL (
                                SELECT char_start, char_end, text
                                FROM parsed_blocks
                                WHERE version_id=v.id AND block_type='title'
                                ORDER BY block_idx LIMIT 1
                            ) b ON true
                            WHERE d.deleted_at IS NULL
                            ORDER BY d.title
                            LIMIT 20
                            """
                    )
                )
            )
            .mappings()
            .all()
        )
        items = [
            {
                "id": uuid7(),
                "dataset_id": dataset_id,
                "category": "single_hop",
                "question": f"哪份资料的标题是 {row['title']}？",
                "gold_answer": f"标题为 {row['title']} 的资料。",
                "gold_spans": json.dumps(
                    [
                        {
                            "version_id": str(row["version_id"]),
                            "char_start": row["char_start"],
                            "char_end": row["char_end"],
                            "quote": row["text"],
                            "note": "自动标题 smoke",
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
            for row in rows
        ]
        if items:
            await session.execute(
                text(
                    """
                        INSERT INTO eval_items
                            (id, dataset_id, category, question, gold_answer, gold_spans,
                             difficulty, origin)
                        VALUES
                            (:id, :dataset_id, :category, :question, :gold_answer,
                             CAST(:gold_spans AS jsonb), 1, 'synthetic')
                        """
                ),
                items,
            )
        unanswerable = [
            "哪份资料记录了火星土壤样本的化学成分？",
            "资料库中哪篇文章给出了南极企鹅迁徙的卫星观测数据？",
            "哪份笔记说明了量子计算机的采购预算？",
        ]
        await session.execute(
            text(
                """
                    INSERT INTO eval_items
                        (id, dataset_id, category, question, gold_answer, gold_spans,
                         difficulty, origin)
                    VALUES
                        (:id, :dataset_id, 'unanswerable', :question, NULL, '[]'::jsonb,
                         1, 'synthetic')
                    """
            ),
            [
                {"id": uuid7(), "dataset_id": dataset_id, "question": question}
                for question in unanswerable
            ],
        )
    await close_database()
    return len(rows), len(unanswerable)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成不进入 Git 的标题检索 smoke 样本")
    parser.parse_args()
    answerable, unanswerable = asyncio.run(seed_title_smoke())
    print(f"dataset={DATASET_NAME} answerable={answerable} unanswerable={unanswerable}")


if __name__ == "__main__":
    main()
