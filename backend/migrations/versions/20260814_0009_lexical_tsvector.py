"""分语言 tsvector: 英文走 english 配置, 中文走 bigram + simple 配置。

Revision ID: 20260814_0009
Revises: 20260814_0008
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0009"
down_revision: str | None = "20260814_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 原 tsv 列自 0001 起就存在, 但从未被写入也从未被查询(实测 2281 行全为 NULL)。
# 它是"以后要做全文检索"的占位, 现在真的要做了, 且必须按语言分列, 所以直接换掉。

# 中文没有 zhparser(pgvector 官方镜像不带), 用 bigram 顶上: 把每个 CJK 连续段
# 切成相邻双字, 空格拼接后交给 simple 配置。bigram 的位置在 tsvector 里是连续的,
# 所以 ts_rank_cd 的 cover density 天然奖励"短语挨在一起", 这正是原覆盖率打分缺的东西。
# 已知取舍: 相邻两个 CJK 段之间没有位置间隔, 跨段的 bigram 会被误判为紧邻。
_ZH_BIGRAMS = """
CREATE OR REPLACE FUNCTION lexical_zh_bigrams(input text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $fn$
  SELECT COALESCE(string_agg(substring(r.run FROM i FOR 2), ' ' ORDER BY r.ord, i), '')
  FROM regexp_matches(lower(input), '[一-鿿]+', 'g') WITH ORDINALITY AS m(parts, ord)
  CROSS JOIN LATERAL (SELECT m.parts[1] AS run, m.ord AS ord) r
  CROSS JOIN LATERAL generate_series(1, greatest(length(r.run) - 1, 1)) AS i;
$fn$
"""

# 英文列要先剥掉 CJK: 中文段在 english 配置下会整段变成一个 token, 既匹配不上任何
# 英文查询, 又会占据位置把英文词之间的 cover 撑宽, 拉低 ts_rank_cd。
_EN_TEXT = """
CREATE OR REPLACE FUNCTION lexical_en_text(input text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $fn$
  SELECT regexp_replace(input, '[一-鿿]+', ' ', 'g');
$fn$
"""

# title / heading / content 分别打 A / B / D 权重。当前查询侧用平权 {1,1,1,1},
# 与换掉的覆盖率打分口径一致(单变量)。权重存在列里而不是查询里, 是为了以后调
# ts_rank_cd 的权重数组时不必重建索引 —— D7 已经暴露 title 加成是个待验证的变量。
_TSV_EXPR = """
    setweight(to_tsvector('{config}', {fn}(d.title)), 'A') ||
    setweight(to_tsvector('{config}', {fn}(
        array_to_string(COALESCE(c.heading_path, ARRAY[]::text[]), ' ')
    )), 'B') ||
    setweight(to_tsvector('{config}', {fn}(c.content)), 'D')
"""


def upgrade() -> None:
    op.execute(_ZH_BIGRAMS)
    op.execute(_EN_TEXT)
    op.execute("DROP INDEX idx_chunk_tsv")
    op.execute("ALTER TABLE chunks DROP COLUMN tsv")
    op.execute("ALTER TABLE chunks ADD COLUMN tsv_en tsvector")
    op.execute("ALTER TABLE chunks ADD COLUMN tsv_zh tsvector")
    # 存量回填。title 在 documents 上, 跨表所以做不成 generated column,
    # 写入路径同样要显式拼(见 services/document_ingestion.py)。
    op.execute(
        f"""
        UPDATE chunks c SET
            tsv_en = {_TSV_EXPR.format(config="english", fn="lexical_en_text")},
            tsv_zh = {_TSV_EXPR.format(config="simple", fn="lexical_zh_bigrams")}
        FROM document_versions v
        JOIN documents d ON d.id = v.document_id
        WHERE v.id = c.version_id
        """
    )
    # is_searchable 进谓词与 HNSW 部分索引同理: 查询恒带这个条件, 索引能小一半。
    # GIN 不像 HNSW 会在候选扫描阶段丢召回, 所以不必按 strategy 再切分。
    op.execute("CREATE INDEX idx_chunk_tsv_en ON chunks USING gin (tsv_en) WHERE is_searchable")
    op.execute("CREATE INDEX idx_chunk_tsv_zh ON chunks USING gin (tsv_zh) WHERE is_searchable")


def downgrade() -> None:
    op.execute("DROP INDEX idx_chunk_tsv_zh")
    op.execute("DROP INDEX idx_chunk_tsv_en")
    op.execute("ALTER TABLE chunks DROP COLUMN tsv_zh")
    op.execute("ALTER TABLE chunks DROP COLUMN tsv_en")
    op.execute("ALTER TABLE chunks ADD COLUMN tsv tsvector")
    op.create_index("idx_chunk_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.execute("DROP FUNCTION lexical_en_text(text)")
    op.execute("DROP FUNCTION lexical_zh_bigrams(text)")
