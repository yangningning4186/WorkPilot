from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部运行时设置；环境变量是唯一的部署覆盖入口。"""

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    annotation_tool_enabled: bool = True
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://workpilot:workpilot@localhost:5432/workpilot"
    redis_url: str = "redis://localhost:6379/0"
    daily_cost_limit_usd: Decimal = Field(default=Decimal("5.00"), ge=0)
    cost_budget_timezone: str = "Asia/Shanghai"
    cost_reservation_ttl_s: int = Field(default=900, ge=60, le=7200)
    # 1 字符 = 1 token 是保守上界; 预留偏大只会多占额度, 估小则会穿透每日上限。
    cost_estimate_chars_per_token: float = Field(default=1.0, gt=0, le=8)
    # 每百万 token 单价。默认 0 = 本地自部署模型, 此时网关跳过预留直接调用。
    price_main_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_main_output_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    price_embedding_input_usd_per_mtok: Decimal = Field(default=Decimal("0"), ge=0)
    local_library_path: Path = Path("../data/library")
    tier_main_base_url: str = "http://localhost:8000/v1"
    tier_main_model: str = "local-chat"
    tier_main_enable_thinking: bool | None = None
    cluster_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "local-embedding"
    embedding_revision: str = "unversioned"
    embedding_dim: Literal[1024] = 1024
    model_timeout_s: float = Field(default=30.0, gt=0)
    model_trust_env: bool = False
    pdf_parser_mode: Literal["auto", "pymupdf", "mineru"] = "auto"
    pdf_parse_timeout_s: float = Field(default=120.0, gt=0, le=600)
    pdf_max_pages: int = Field(default=500, ge=1, le=2000)
    pdf_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    pdf_worker_memory_mb: int = Field(default=2048, ge=256, le=16384)
    pdf_worker_cpu_s: int = Field(default=120, ge=1, le=600)
    pdf_mineru_command: Path = Path("../.mineru/.venv/bin/mineru")
    pdf_mineru_revision: str = "3.4.4"
    pdf_mineru_backend: Literal[
        "pipeline",
        "vlm-engine",
        "hybrid-engine",
        "vlm-http-client",
        "hybrid-http-client",
    ] = "hybrid-engine"
    pdf_mineru_effort: Literal["medium", "high"] = "medium"
    pdf_mineru_method: Literal["auto", "txt", "ocr"] = "auto"
    pdf_mineru_timeout_s: float = Field(default=1800.0, gt=0, le=7200)
    pdf_mineru_fallback_enabled: bool = True
    pdf_mineru_processing_window_size: int = Field(default=4, ge=1, le=64)
    # run 预算上限(约束 5): 任一超限即熔断, 防止反思循环烧钱。
    run_budget_tokens: int = Field(default=200_000, ge=0)
    run_budget_calls: int = Field(default=40, ge=0)
    run_budget_wall_ms: int = Field(default=300_000, ge=1_000)
    run_lease_s: int = Field(default=60, ge=5, le=3600)
    # 心跳必须明显短于租约, 否则正常执行中的 run 会被 watchdog 误判为失联。
    run_heartbeat_s: float = Field(default=15.0, gt=0)
    run_delta_flush_ms: int = Field(default=50, ge=10, le=1000)
    run_delta_flush_chars: int = Field(default=120, ge=1, le=4000)
    # pgvector 扫描参数(docs/03 §4.1)。部分索引只覆盖 strategy + is_searchable,
    # 其余过滤(embedding 身份、doc_type)仍在索引内进行, 靠迭代扫描兜底候选不足。
    hnsw_iterative_scan: Literal["off", "relaxed_order", "strict_order"] = "relaxed_order"
    hnsw_max_scan_tuples: int = Field(default=20_000, ge=1_000, le=1_000_000)
    # ef_search 必须不小于 top_k, 否则召回会被候选队列长度截断。
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    refusal_threshold: float = Field(default=0.35, ge=-1.0, le=1.0)
    refusal_margin_threshold: float = Field(default=0.03, ge=0.0, le=2.0)
    evidence_gate_max_chars: int = Field(default=3000, ge=500, le=20000)
    evidence_gate_max_tokens: int = Field(default=300, ge=64, le=2048)
    query_decomposition_enabled: bool = False
    query_decomposition_max_subqueries: int = Field(default=4, ge=2, le=8)
    query_decomposition_max_tokens: int = Field(default=300, ge=64, le=2048)
    rerank_enabled: bool = False
    rerank_candidate_k: int = Field(default=50, ge=2, le=50)
    rerank_batch_size: int = Field(default=10, ge=2, le=25)
    rerank_batch_keep: int = Field(default=3, ge=1, le=25)
    rerank_max_candidate_chars: int = Field(default=600, ge=100, le=4000)
    rerank_max_tokens: int = Field(default=1000, ge=128, le=4096)
    lexical_rrf_enabled: bool = True
    rrf_k: int = Field(default=60, ge=1, le=1000)
    answer_max_evidence_chars: int = Field(default=12000, ge=1000, le=100000)
    answer_max_tokens: int = Field(default=1200, ge=64, le=8192)

    @field_validator("embedding_dim", mode="before")
    @classmethod
    def parse_embedding_dim(cls, value: object) -> object:
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
