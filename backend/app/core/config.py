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
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://workpilot:workpilot@localhost:5432/workpilot"
    redis_url: str = "redis://localhost:6379/0"
    daily_cost_limit_usd: Decimal = Field(default=Decimal("5.00"), ge=0)
    cost_budget_timezone: str = "Asia/Shanghai"
    local_library_path: Path = Path("../data/library")
    tier_main_base_url: str = "http://localhost:8000/v1"
    tier_main_model: str = "local-chat"
    cluster_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "local-embedding"
    embedding_revision: str = "unversioned"
    embedding_dim: Literal[1024] = 1024
    model_timeout_s: float = Field(default=30.0, gt=0)
    model_trust_env: bool = False
    pdf_parse_timeout_s: float = Field(default=120.0, gt=0, le=600)
    pdf_max_pages: int = Field(default=500, ge=1, le=2000)
    pdf_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    pdf_worker_memory_mb: int = Field(default=2048, ge=256, le=16384)
    pdf_worker_cpu_s: int = Field(default=120, ge=1, le=600)
    refusal_threshold: float = Field(default=0.35, ge=-1.0, le=1.0)
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
