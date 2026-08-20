"""把 OAuth 连接器暴露为固定官方域名、凭据不出进程的 Cowork 工具。"""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.cowork_tools import (
    CoworkToolContext,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
)
from app.security.secret_store import LocalSecretStore
from app.services.connectors import (
    ConnectorAccountRecord,
    connector_secrets,
    get_connector_account,
    list_connector_accounts,
)

_BASE_URLS = {
    "github": "https://api.github.com",
    "feishu": "https://open.feishu.cn/open-apis",
    "wecom": "https://qyapi.weixin.qq.com/cgi-bin",
    "wechat_official": "https://api.weixin.qq.com/cgi-bin",
    "tencent_docs": "https://docs.qq.com/openapi",
}
_QUERY_TOKEN_KINDS = frozenset({"wecom", "wechat_official"})


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListConnectorsArgs(_StrictArgs):
    pass


class ConnectorRequestArgs(_StrictArgs):
    account_id: UUID
    path: str = Field(min_length=1, max_length=2048)
    query: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=100)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise ValueError("path 必须是以单个 / 开头、无主机和 fragment 的 API 路径")
        return value


class ConnectorActionArgs(ConnectorRequestArgs):
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    body: dict[str, Any] = Field(default_factory=dict)


def _public_account(account: ConnectorAccountRecord) -> dict[str, Any]:
    return {
        "id": str(account.id),
        "kind": account.kind,
        "name": account.name,
        "status": account.status,
        "scopes": account.scopes,
        "external_account_name": account.external_account_name,
        "enabled": account.enabled,
    }


def _runtime_request(
    account: ConnectorAccountRecord,
    *,
    path: str,
    query: dict[str, str | int | float | bool],
    secret_store: LocalSecretStore,
) -> tuple[str, dict[str, str], dict[str, str | int | float | bool]]:
    if not account.enabled or account.status not in {"connected", "configured"}:
        raise ValueError("连接器未连接或已停用")
    secrets = connector_secrets(account, secret_store)
    access_token = str(secrets.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("连接器缺少 access_token，请先完成 OAuth 或配置令牌")
    headers = {"Accept": "application/json", "User-Agent": "WorkPilot/1.0"}
    request_query = dict(query)
    if account.kind == "tencent_docs":
        if not account.external_account_id:
            raise ValueError("腾讯文档连接器缺少 Open ID，请重新完成 OAuth")
        client_id = str(account.config.get("client_id") or "").strip()
        if not client_id:
            raise ValueError("腾讯文档连接器缺少 Client ID")
        headers.update(
            {
                "Access-Token": access_token,
                "Client-Id": client_id,
                "Open-Id": account.external_account_id,
            }
        )
    elif account.kind in _QUERY_TOKEN_KINDS:
        request_query["access_token"] = access_token
    else:
        headers["Authorization"] = f"Bearer {access_token}"
    if account.kind == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return f"{_BASE_URLS[account.kind]}{path}", headers, request_query


async def _connector_request(
    context: CoworkToolContext,
    args: ConnectorRequestArgs,
    *,
    method: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    account = await get_connector_account(context.session, args.account_id)
    if account is None:
        raise LookupError("连接器不存在")
    url, headers, query = _runtime_request(
        account,
        path=args.path,
        query=args.query,
        secret_store=LocalSecretStore(context.settings.secret_store_key_path),
    )
    try:
        async with httpx.AsyncClient(
            timeout=context.settings.cowork_web_timeout_s,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(method, url, params=query, headers=headers, json=body)
    except httpx.TimeoutException as error:
        raise ValueError("连接器 API 请求超时") from error
    except httpx.HTTPError as error:
        raise ValueError("连接器 API 连接失败") from error
    if 300 <= response.status_code < 400:
        raise ValueError("连接器 API 返回重定向，已按安全策略拒绝跟随")
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"连接器 API 返回 HTTP {response.status_code}")
    text = response.text[: context.settings.cowork_mcp_result_max_chars]
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    return {
        "account": _public_account(account),
        "method": method,
        "path": args.path,
        "status_code": response.status_code,
        "response": payload,
        "truncated": len(response.text) > len(text),
    }


def register_connector_tools(registry: CoworkToolRegistry) -> None:
    async def list_handler(context: CoworkToolContext, _: BaseModel) -> CoworkToolResult:
        accounts = await list_connector_accounts(context.session)
        return CoworkToolResult(output={"connectors": [_public_account(item) for item in accounts]})

    async def read_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = ConnectorRequestArgs.model_validate(raw.model_dump())
        return CoworkToolResult(
            output=await _connector_request(context, args, method="GET", body=None)
        )

    async def action_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = ConnectorActionArgs.model_validate(raw.model_dump())
        output = await _connector_request(
            context,
            args,
            method=args.method,
            body=args.body if args.method != "DELETE" else None,
        )
        return CoworkToolResult(
            output=output,
            effect_ref=f"connector:{args.account_id}:{args.method}:{args.path}",
        )

    registry.register(
        CoworkToolSpec(
            name="list_connectors",
            description="列出已配置的 GitHub、飞书、企业微信、微信公众号和腾讯文档账户，不返回密钥。",
            args_model=ListConnectorsArgs,
            capability="external.action",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=list_handler,
        )
    )
    registry.register(
        CoworkToolSpec(
            name="read_connector_api",
            description=(
                "使用指定连接器读取官方 API。只接受相对 API path，主机由账户类型固定；"
                "例如 GitHub /user 或飞书 /contact/v3/users。"
            ),
            args_model=ConnectorRequestArgs,
            capability="external.action",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=read_handler,
        )
    )
    registry.register(
        CoworkToolSpec(
            name="act_connector_api",
            description=(
                "使用指定连接器调用官方 API 的 POST/PUT/PATCH/DELETE；"
                "任何调用都必须逐次获得用户批准。"
            ),
            args_model=ConnectorActionArgs,
            capability="external.action",
            risk="external",
            effect="external",
            parallel_safe=False,
            handler=action_handler,
            approval_required=True,
        )
    )
    registry.add_system_instructions(
        "连接器凭据不会展示给模型。先调用 list_connectors 获取 account_id；"
        "读取用 read_connector_api，修改外部状态用 act_connector_api 并等待用户批准。"
    )
