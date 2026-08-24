from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.cowork.connector_descriptors import ConnectorKind as ConnectorKind
from app.cowork.connector_descriptors import connector_kinds

ConnectorAuthType = Literal["oauth2", "token", "app_credentials"]
ConnectorStatus = Literal["configured", "authorizing", "connected", "expired", "error"]


class ConnectorAccountCreate(BaseModel):
    kind: ConnectorKind
    name: str = Field(min_length=1, max_length=100)
    auth_type: ConnectorAuthType = "oauth2"
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=8192)
    access_token: str | None = Field(default=None, max_length=16384)
    refresh_token: str | None = Field(default=None, max_length=16384)
    redirect_uri: str | None = Field(default=None, max_length=2048)
    scopes: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value not in connector_kinds():
            raise ValueError(f"不支持的连接器类型: {value}")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("连接器名称不能为空")
        return normalized

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("redirect_uri 必须是完整的 http(s) URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("redirect_uri 不能包含凭据或 fragment")
        return value

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_credentials(self) -> "ConnectorAccountCreate":
        if self.auth_type == "oauth2" and not (self.client_id or "").strip():
            raise ValueError("OAuth 连接器必须填写 client_id")
        if self.auth_type == "token" and not (self.access_token or "").strip():
            raise ValueError("Token 连接器必须填写 access_token")
        if self.auth_type == "app_credentials" and not (
            (self.client_id or "").strip() and (self.client_secret or "").strip()
        ):
            raise ValueError("应用凭据连接器必须填写 client_id 与 client_secret")
        return self


class ConnectorAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=8192)
    access_token: str | None = Field(default=None, max_length=16384)
    refresh_token: str | None = Field(default=None, max_length=16384)
    redirect_uri: str | None = Field(default=None, max_length=2048)
    scopes: list[str] | None = Field(default=None, max_length=100)
    config: dict[str, Any] | None = None
    clear_secrets: bool = False
    enabled: bool | None = None


class ConnectorAccountResponse(BaseModel):
    id: UUID
    kind: ConnectorKind
    name: str
    auth_type: ConnectorAuthType
    status: ConnectorStatus
    config: dict[str, Any]
    scopes: list[str]
    capabilities: list[str] = Field(default_factory=list)
    external_account_id: str | None
    external_account_name: str | None
    expires_at: datetime | None
    last_checked_at: datetime | None
    last_error: str | None
    enabled: bool
    has_secrets: bool
    created_at: datetime
    updated_at: datetime


class ConnectorAccountListResponse(BaseModel):
    items: list[ConnectorAccountResponse]


class ConnectorDescriptorResponse(BaseModel):
    kind: ConnectorKind
    label: str
    blurb: str
    logo: str
    brand_color: str
    category: Literal["china_office", "developer"]
    auth_types: list[ConnectorAuthType]
    default_scopes: list[str]
    capabilities: list[str]


class ConnectorDescriptorListResponse(BaseModel):
    items: list[ConnectorDescriptorResponse]


class OAuthStartRequest(BaseModel):
    redirect_uri: str | None = Field(default=None, max_length=2048)


class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str
    expires_at: datetime
