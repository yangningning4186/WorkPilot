"""连接器产品描述与运行时适配。

一类连接器只在这里声明一次：展示名、官方 API 主机、OAuth 适配器、请求鉴权、默认
scope、能力和专用工具 registrar。账户存储、HTTP 请求和前端 catalog 都消费同一份描述，
新增中国办公连接器时不再去各处追加 ``if account.kind``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

type ConnectorKind = str

ConnectorAuthStyle = Literal["bearer", "query_token", "tencent_headers"]
OAuthAdapter = Literal["github", "feishu", "wecom", "wechat", "tencent_docs"]
ConnectorAuthType = Literal["oauth2", "token", "app_credentials"]
ConnectorCategory = Literal["china_office", "developer"]


@dataclass(frozen=True)
class ConnectorDescriptor:
    kind: ConnectorKind
    label: str
    blurb: str
    logo: str
    brand_color: str
    category: ConnectorCategory
    api_base_url: str
    authorize_url: str
    oauth_adapter: OAuthAdapter
    auth_types: tuple[ConnectorAuthType, ...] = ("oauth2", "token")
    auth_style: ConnectorAuthStyle = "bearer"
    default_scopes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("openapi",)
    request_headers: tuple[tuple[str, str], ...] = ()
    tool_registrars: tuple[str, ...] = ()
    oauth_fragment: str = ""
    response_code_field: str | None = None
    response_message_field: str = "msg"

    def public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "blurb": self.blurb,
            "logo": self.logo,
            "brand_color": self.brand_color,
            "category": self.category,
            "auth_types": list(self.auth_types),
            "default_scopes": list(self.default_scopes),
            "capabilities": list(self.capabilities),
        }


_DESCRIPTORS = (
    ConnectorDescriptor(
        kind="github",
        label="GitHub",
        blurb="连接仓库、Issue 与 Pull Request，用于代码协作、检索和自动化交付。",
        logo="github",
        brand_color="#24292f",
        category="developer",
        api_base_url="https://api.github.com",
        authorize_url="https://github.com/login/oauth/authorize",
        oauth_adapter="github",
        default_scopes=("read:user", "repo"),
        request_headers=(("X-GitHub-Api-Version", "2022-11-28"),),
    ),
    ConnectorDescriptor(
        kind="feishu",
        label="飞书",
        blurb="覆盖日历、云文档、云盘、多维表格、任务与审批的中国办公主连接器。",
        logo="feishu",
        brand_color="#3370ff",
        category="china_office",
        api_base_url="https://open.feishu.cn/open-apis",
        authorize_url="https://accounts.feishu.cn/open-apis/authen/v1/authorize",
        oauth_adapter="feishu",
        default_scopes=(
            "offline_access",
            "calendar:calendar",
            "bitable:app",
            "docx:document:readonly",
            "drive:drive:readonly",
            "task:task",
            "approval:approval",
        ),
        capabilities=("openapi", "calendar", "base", "docs", "drive", "tasks", "approval"),
        tool_registrars=("app.cowork.connector_tools:register_feishu_tools",),
        response_code_field="code",
    ),
    ConnectorDescriptor(
        kind="wecom",
        label="企业微信",
        blurb="连接企业通讯录与消息能力，在官方 API 边界内完成企业协同。",
        logo="wecom",
        brand_color="#07c160",
        category="china_office",
        api_base_url="https://qyapi.weixin.qq.com/cgi-bin",
        authorize_url="https://open.weixin.qq.com/connect/oauth2/authorize",
        oauth_adapter="wecom",
        auth_style="query_token",
        capabilities=("openapi", "messaging"),
        oauth_fragment="#wechat_redirect",
    ),
    ConnectorDescriptor(
        kind="wechat_official",
        label="微信公众号",
        blurb="连接公众号用户与消息接口，不涉及个人微信模拟登录或非官方自动化。",
        logo="wechat",
        brand_color="#2aae67",
        category="china_office",
        api_base_url="https://api.weixin.qq.com/cgi-bin",
        authorize_url="https://open.weixin.qq.com/connect/oauth2/authorize",
        oauth_adapter="wechat",
        auth_style="query_token",
        capabilities=("openapi", "messaging"),
        oauth_fragment="#wechat_redirect",
    ),
    ConnectorDescriptor(
        kind="tencent_docs",
        label="腾讯文档",
        blurb="读取与管理腾讯文档和云端文件，适合国内团队的在线文档协作。",
        logo="tencent_docs",
        brand_color="#2f80ed",
        category="china_office",
        api_base_url="https://docs.qq.com/openapi",
        authorize_url="https://docs.qq.com/oauth/v2/authorize",
        oauth_adapter="tencent_docs",
        auth_types=("oauth2",),
        auth_style="tencent_headers",
        default_scopes=("all",),
        capabilities=("openapi", "docs", "drive"),
    ),
)

CONNECTOR_DESCRIPTORS = {item.kind: item for item in _DESCRIPTORS}
if len(CONNECTOR_DESCRIPTORS) != len(_DESCRIPTORS):  # pragma: no cover - import-time invariant
    raise RuntimeError("Connector Descriptor kind 重复")


def connector_kinds() -> frozenset[str]:
    return frozenset(CONNECTOR_DESCRIPTORS)


def get_connector_descriptor(kind: str) -> ConnectorDescriptor:
    try:
        return CONNECTOR_DESCRIPTORS[kind]
    except KeyError as error:
        raise ValueError(f"不支持的连接器类型: {kind}") from error


def list_connector_descriptors() -> tuple[ConnectorDescriptor, ...]:
    return _DESCRIPTORS


__all__ = [
    "CONNECTOR_DESCRIPTORS",
    "ConnectorDescriptor",
    "connector_kinds",
    "get_connector_descriptor",
    "list_connector_descriptors",
]
