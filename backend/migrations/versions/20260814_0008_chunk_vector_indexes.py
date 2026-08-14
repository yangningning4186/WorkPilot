"""按分块策略建立部分 HNSW 向量索引。

Revision ID: 20260814_0008
Revises: 20260814_0007
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0008"
down_revision: str | None = "20260814_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 四策略共用一个 HNSW 再按 strategy 过滤是错的: pgvector 在候选扫描阶段过滤,
# 扫出的候选约 75% 会被丢掉, 很可能凑不满 top-k。更要命的是 W3 的四策略对照会把
# 索引退化的噪声混进结论里(docs/03 §4.1)。因此每策略一个部分索引。
#
# M0 只有 heading 有数据; 其余三个索引是空分区, 不产生写入开销, 建在这里是为了
# W3 建另外三套 chunk 时不必再改 schema。
_STRATEGIES = ("fixed", "heading", "recursive", "semantic")


def upgrade() -> None:
    for strategy in _STRATEGIES:
        # is_searchable 必须进谓词: 它是"版本已激活 + 文档未删除"的唯一开关,
        # 查询也必须显式带上, 否则命不中这个部分索引。
        op.execute(
            f"""
            CREATE INDEX idx_chunk_vec_{strategy}
            ON chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE strategy = '{strategy}' AND is_searchable
            """
        )


def downgrade() -> None:
    for strategy in _STRATEGIES:
        op.execute(f"DROP INDEX idx_chunk_vec_{strategy}")
