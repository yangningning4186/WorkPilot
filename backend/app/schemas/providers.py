from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ProviderKind = Literal[
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "qwen",
    "ollama",
    "openai_compatible",
]


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url 必须是完整的 http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url 不能内嵌用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url 不能包含 query 或 fragment")
    return normalized


class ProviderProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: ProviderKind
    base_url: str = Field(min_length=1, max_length=2048)
    default_model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=8192)
    context_window_tokens: int = Field(default=128_000, ge=1024, le=2_000_000)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "default_model")
    @classmethod
    def strip_required(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validate_base_url(value)

    @model_validator(mode="after")
    def require_key_for_remote_provider(self) -> "ProviderProfileCreate":
        if self.provider != "ollama" and not (self.api_key or "").strip():
            raise ValueError("远程 Provider 必须填写 API Key")
        return self


class ProviderProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    default_model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=8192)
    clear_api_key: bool = False
    context_window_tokens: int | None = Field(default=None, ge=1024, le=2_000_000)
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("name", "default_model")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_optional_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_base_url(value)

    @model_validator(mode="after")
    def validate_secret_change(self) -> "ProviderProfileUpdate":
        if self.clear_api_key and self.api_key is not None:
            raise ValueError("api_key 与 clear_api_key 不能同时设置")
        return self


class ProviderProfileResponse(BaseModel):
    id: UUID
    name: str
    provider: ProviderKind
    base_url: str
    default_model: str
    context_window_tokens: int
    enabled: bool
    has_api_key: bool
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProviderProfileListResponse(BaseModel):
    items: list[ProviderProfileResponse]


class ProviderProbeResponse(BaseModel):
    ok: bool
    provider: ProviderKind
    models: list[str]
    latency_ms: int
    message: str
