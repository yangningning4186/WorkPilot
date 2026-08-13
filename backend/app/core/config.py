from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    embedding_dim: Literal[1024] = 1024
    model_timeout_s: float = Field(default=30.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
