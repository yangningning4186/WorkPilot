"""gpu_batches: 自建模型的成本摊销批次

自建模型没有 API 账单，成本只能按整批 GPU wall time 摊销（docs/07 §7.2）。
摊销需要三个 llm_calls 里没有、也推不出来的量：批次墙钟、节点数、等价云单价。

**墙钟为什么必须显式存**：`llm_calls.created_at` 的默认值是 `now()`，而 PostgreSQL
的 `now()` 返回的是**事务开始时间**。同一个事务里写入的多条调用会拿到完全相同的
时间戳，`max(created_at) - min(created_at)` 恒等于 0。所以墙钟由进程侧用单调时钟
测量后直接写 `wall_ms`，不依赖数据库时间，也不受应用与数据库时钟漂移影响。

单价来源（`price_source`）是必填的：§7.3 明确要求报告里写出取值与出处，
说不清口径的成本数字一问就露馅。

Revision ID: 20260816_0016
Revises: 20260816_0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260816_0016"
down_revision = "20260816_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpu_batches",
        sa.Column("id", UUID, primary_key=True),
        # 一批只跑一个档位: 混档摊销出来的单任务成本没有意义。
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        # 实验标签, 例如 C4-run2。帕累托实验按它把批次归到配置上。
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("gpu_model", sa.Text(), nullable=False),
        sa.Column("price_usd_per_hour", sa.Numeric(12, 6), nullable=False),
        sa.Column("price_source", sa.Text(), nullable=False),
        # 进程侧单调时钟测得的墙钟, 权威值。下面两个时间戳只用于人看和排序。
        sa.Column("wall_ms", sa.Integer()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "tier IN ('light','main','heavy','external')", name="ck_gpu_batches_tier"
        ),
        sa.CheckConstraint("node_count >= 1", name="ck_gpu_batches_nodes"),
        sa.CheckConstraint("price_usd_per_hour >= 0", name="ck_gpu_batches_price"),
        sa.CheckConstraint("length(price_source) > 0", name="ck_gpu_batches_price_source"),
        sa.CheckConstraint("wall_ms IS NULL OR wall_ms >= 0", name="ck_gpu_batches_wall"),
        # 未收尾的批次(进程被杀)两列都为空, 成本报告会把它整批排除而不是当成 0 秒。
        sa.CheckConstraint(
            "(ended_at IS NULL) = (wall_ms IS NULL)", name="ck_gpu_batches_closed_together"
        ),
    )
    op.create_index("idx_gpu_batches_label", "gpu_batches", ["label", "started_at"])
    op.create_foreign_key(
        "fk_llm_calls_batch",
        "llm_calls",
        "gpu_batches",
        ["batch_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_llm_calls_batch", "llm_calls", type_="foreignkey")
    op.drop_index("idx_gpu_batches_label", table_name="gpu_batches")
    op.drop_table("gpu_batches")
