"""gpu_batches: 单价改为可选，成本口径回落到 token 与吞吐

决定不做美元折算：等价云单价是个外部假设，填不同的数会得到不同的"成本"，
而结论（哪个配置在质量-成本前沿上）本来就只取决于 token 与 GPU 时间的相对关系。
与其引入一个不可验证的假设，不如只报可直接测量的量。

单价列保留但可空：将来真要做美元折算时不必再改表，且已有数据不受影响。

Revision ID: 20260817_0017
Revises: 20260816_0016
"""

from alembic import op

revision = "20260817_0017"
down_revision = "20260816_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("gpu_batches", "price_usd_per_hour", nullable=True)
    op.alter_column("gpu_batches", "gpu_model", nullable=True)
    op.alter_column("gpu_batches", "price_source", nullable=True)
    # 原约束要求 price_source 非空; 改成"要么都没有, 要么单价必须带来源"。
    op.drop_constraint("ck_gpu_batches_price_source", "gpu_batches", type_="check")
    op.create_check_constraint(
        "ck_gpu_batches_price_source",
        "gpu_batches",
        "price_usd_per_hour IS NULL OR (price_source IS NOT NULL AND length(price_source) > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_gpu_batches_price_source", "gpu_batches", type_="check")
    # 回滚前必须先补齐历史数据, 否则 NOT NULL 会失败——这是有意的:
    # 静默填一个占位单价会让旧批次的成本数字凭空出现。
    op.create_check_constraint(
        "ck_gpu_batches_price_source", "gpu_batches", "length(price_source) > 0"
    )
    op.alter_column("gpu_batches", "price_source", nullable=False)
    op.alter_column("gpu_batches", "gpu_model", nullable=False)
    op.alter_column("gpu_batches", "price_usd_per_hour", nullable=False)
