"""建立 M0 基础数据模型。

Revision ID: 20260814_0001
Revises:
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260814_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("sync_cursor", sa.Text()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("sync_status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("sync_error", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('local_dir','obsidian','zotero','web_clip','upload')",
            name="ck_sources_kind",
        ),
        sa.CheckConstraint(
            "sync_status IN ('idle','syncing','failed')",
            name="ck_sources_sync_status",
        ),
    )

    op.create_table(
        "documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "source_id", UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("read_status", sa.Text(), nullable=False, server_default="unread"),
        sa.Column("starred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_opened_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "doc_type IN ('paper','note','book','web','doc','code')",
            name="ck_documents_doc_type",
        ),
        sa.CheckConstraint(
            "read_status IN ('unread','reading','read')",
            name="ck_documents_read_status",
        ),
        sa.UniqueConstraint("source_id", "source_uri", name="uq_documents_source_uri"),
    )
    op.create_index(
        "idx_doc_live",
        "documents",
        ["doc_type", "added_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("idx_doc_tags", "documents", ["tags"], postgresql_using="gin")

    op.create_table(
        "document_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "document_id", UUID, sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("parser", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("full_text", sa.Text()),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("invalid_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("parse_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("parse_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "parse_status IN ('pending','parsing','done','failed','superseded')",
            name="ck_document_versions_parse_status",
        ),
        sa.CheckConstraint(
            "invalid_at IS NULL OR (valid_from IS NOT NULL AND invalid_at > valid_from)",
            name="ck_document_versions_valid_range",
        ),
        sa.CheckConstraint(
            "activated_at IS NULL OR (parse_status = 'done' AND full_text IS NOT NULL)",
            name="ck_document_versions_activation_ready",
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0", name="ck_document_versions_page_count"
        ),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_number"),
    )
    op.create_index(
        "uq_docver_current",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("activated_at IS NOT NULL AND invalid_at IS NULL"),
    )

    op.create_table(
        "parsed_blocks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "version_id",
            UUID,
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("block_idx", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "block_type IN ('title','paragraph','table','list','figure_caption','formula','code')",
            name="ck_parsed_blocks_type",
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name="ck_parsed_blocks_span"
        ),
        sa.UniqueConstraint("version_id", "block_idx", name="uq_parsed_blocks_index"),
    )
    op.create_index("idx_block_span", "parsed_blocks", ["version_id", "char_start", "char_end"])

    op.create_table(
        "parsed_block_locations",
        sa.Column(
            "block_id",
            UUID,
            sa.ForeignKey("parsed_blocks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("location_idx", sa.Integer(), primary_key=True),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("page_width", sa.Float(), nullable=False),
        sa.Column("page_height", sa.Float(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coord_origin", sa.Text(), nullable=False, server_default="top_left"),
        sa.Column("bbox_norm", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "location_idx >= 0 AND page_no >= 1", name="ck_block_locations_position"
        ),
        sa.CheckConstraint("page_width > 0 AND page_height > 0", name="ck_block_locations_size"),
        sa.CheckConstraint("rotation IN (0,90,180,270)", name="ck_block_locations_rotation"),
        sa.CheckConstraint(
            "coord_origin IN ('top_left','bottom_left')",
            name="ck_block_locations_origin",
        ),
    )
    op.create_index("idx_block_location_page", "parsed_block_locations", ["page_no", "block_id"])

    op.create_table(
        "chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "version_id",
            UUID,
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_tokens", sa.Integer(), nullable=False),
        sa.Column("block_start_idx", sa.Integer(), nullable=False),
        sa.Column("block_end_idx", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("dominant_block_type", sa.Text(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text())),
        sa.Column("embedding", Vector(1024)),
        sa.Column("sparse", JSONB),
        sa.Column("tsv", postgresql.TSVECTOR()),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("is_searchable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "strategy IN ('fixed','recursive','semantic','heading')", name="ck_chunks_strategy"
        ),
        sa.CheckConstraint("char_start >= 0 AND char_end > char_start", name="ck_chunks_span"),
        sa.CheckConstraint("block_end_idx >= block_start_idx", name="ck_chunks_block_range"),
        sa.CheckConstraint("content_tokens >= 0", name="ck_chunks_token_count"),
        sa.UniqueConstraint(
            "version_id", "strategy", "chunk_index", name="uq_chunks_strategy_index"
        ),
    )
    op.create_index("idx_chunk_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index("idx_chunk_filter", "chunks", ["strategy", "doc_type", "is_searchable"])
    op.create_index("idx_chunk_ver", "chunks", ["version_id", "strategy", "chunk_index"])
    op.create_index(
        "idx_chunk_span", "chunks", ["version_id", "strategy", "char_start", "char_end"]
    )

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("demo_session_id", UUID),
        sa.Column("title", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_upto", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("scope IN ('local_owner','demo')", name="ck_conversations_scope"),
        sa.CheckConstraint(
            "(scope = 'local_owner' AND demo_session_id IS NULL) OR "
            "(scope = 'demo' AND demo_session_id IS NOT NULL)",
            name="ck_conversations_session_scope",
        ),
    )
    op.create_index(
        "idx_conversation_demo_session",
        "conversations",
        ["demo_session_id", "updated_at"],
        postgresql_where=sa.text("scope = 'demo'"),
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("worker_id", sa.Text()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("budget_tokens", sa.Integer(), nullable=False, server_default="200000"),
        sa.Column("budget_calls", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("budget_wall_ms", sa.Integer(), nullable=False, server_default="300000"),
        sa.Column("used_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_seq", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued','planning','awaiting_approval','executing','reflecting',"
            "'waiting_human','done','failed','cancelled','budget_exceeded')",
            name="ck_agent_runs_status",
        ),
        sa.CheckConstraint(
            "budget_tokens >= 0 AND budget_calls >= 0 AND budget_wall_ms >= 0",
            name="ck_agent_runs_budget",
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="completed"),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("citations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("trace_id", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('user','assistant','tool','system')", name="ck_messages_role"),
        sa.CheckConstraint(
            "status IN ('streaming','completed','failed','cancelled')",
            name="ck_messages_status",
        ),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )
    op.create_index(
        "idx_msg_run", "messages", ["run_id"], postgresql_where=sa.text("run_id IS NOT NULL")
    )

    op.create_table(
        "run_events",
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_run_events_time", "run_events", ["run_id", "created_at"])

    op.create_table(
        "eval_datasets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("split IN ('dev','test','regression')", name="ck_eval_datasets_split"),
    )

    op.create_table(
        "eval_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "dataset_id",
            UUID,
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("gold_answer", sa.Text()),
        sa.Column("gold_spans", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("gold_tools", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("constraints", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("temporal_ctx", sa.DateTime(timezone=True)),
        sa.Column("difficulty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "category IN ('single_hop','multi_hop','table','temporal',"
            "'unanswerable','global','agent_task')",
            name="ck_eval_items_category",
        ),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 3", name="ck_eval_items_difficulty"),
        sa.CheckConstraint(
            "origin IN ('human','synthetic','badcase')", name="ck_eval_items_origin"
        ),
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "dataset_id",
            UUID,
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("fallback_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("actual_models", JSONB),
        sa.Column("metrics", JSONB),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "eval_results",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "item_id", UUID, sa.ForeignKey("eval_items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("answer", sa.Text()),
        sa.Column("retrieved", JSONB),
        sa.Column("tool_calls", JSONB),
        sa.Column("scores", JSONB, nullable=False),
        sa.Column("judge_raw", JSONB),
        sa.Column("human_label", JSONB),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("tokens", sa.Integer()),
        sa.UniqueConstraint("run_id", "item_id", name="uq_eval_results_run_item"),
    )

    op.create_table(
        "daily_cost_budgets",
        sa.Column("budget_date", sa.Date(), primary_key=True),
        sa.Column("limit_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("spent_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("limit_usd >= 0", name="ck_daily_cost_budgets_limit"),
        sa.CheckConstraint(
            "reserved_usd >= 0 AND spent_usd >= 0", name="ck_daily_cost_budgets_nonnegative"
        ),
        sa.CheckConstraint(
            "spent_usd + reserved_usd <= limit_usd", name="ck_daily_cost_budgets_cap"
        ),
    )

    op.create_table(
        "cost_reservations",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column(
            "budget_date",
            sa.Date(),
            sa.ForeignKey("daily_cost_budgets.budget_date"),
            nullable=False,
        ),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("estimated_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("actual_usd", sa.Numeric(12, 6)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("estimated_usd >= 0", name="ck_cost_reservations_estimate"),
        sa.CheckConstraint(
            "actual_usd IS NULL OR (actual_usd >= 0 AND actual_usd <= estimated_usd)",
            name="ck_cost_reservations_actual",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','settled','released','charged_estimate')",
            name="ck_cost_reservations_status",
        ),
        sa.CheckConstraint(
            "(status IN ('reserved','released') AND actual_usd IS NULL) OR "
            "(status IN ('settled','charged_estimate') AND actual_usd IS NOT NULL)",
            name="ck_cost_reservations_status_amount",
        ),
    )
    op.create_index(
        "idx_cost_reservation_expiry",
        "cost_reservations",
        ["expires_at"],
        postgresql_where=sa.text("status = 'reserved'"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("eval_run_id", UUID, sa.ForeignKey("eval_runs.id", ondelete="SET NULL")),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("was_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_type", sa.Text()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("ttft_ms", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column("batch_id", UUID),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("tier IN ('light','main','heavy','external')", name="ck_llm_calls_tier"),
        sa.CheckConstraint(
            "cache_type IS NULL OR cache_type IN ('exact','semantic','prompt')",
            name="ck_llm_calls_cache_type",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_llm_calls_measurements",
        ),
    )
    op.create_index("idx_llm_task", "llm_calls", ["task_type", "tier", "created_at"])
    op.create_index(
        "idx_llm_batch", "llm_calls", ["batch_id"], postgresql_where=sa.text("batch_id IS NOT NULL")
    )

    op.create_table(
        "feedback",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "message_id", UUID, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("comment", sa.Text()),
        sa.Column("promoted_to_eval", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("rating IN (-1,1)", name="ck_feedback_rating"),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN "
            "('factual','citation','should_refuse','format','slow','other')",
            name="ck_feedback_reason",
        ),
    )


def downgrade() -> None:
    for table_name in (
        "feedback",
        "llm_calls",
        "cost_reservations",
        "daily_cost_budgets",
        "eval_results",
        "eval_runs",
        "eval_items",
        "eval_datasets",
        "run_events",
        "messages",
        "agent_runs",
        "conversations",
        "chunks",
        "parsed_block_locations",
        "parsed_blocks",
        "document_versions",
        "documents",
        "sources",
    ):
        op.drop_table(table_name)
