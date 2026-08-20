"""受支持连接器的 OAuth 授权码流程。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.secret_store import LocalSecretStore
from app.services.connectors import (
    ConnectorAccountRecord,
    connector_secrets,
    set_connector_status,
)

_AUTHORIZE_URLS = {
    "github": "https://github.com/login/oauth/authorize",
    "feishu": "https://accounts.feishu.cn/open-apis/authen/v1/authorize",
    "wecom": "https://open.weixin.qq.com/connect/oauth2/authorize",
    "wechat_official": "https://open.weixin.qq.com/connect/oauth2/authorize",
    "tencent_docs": "https://docs.qq.com/oauth/v2/authorize",
}


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class OAuthIdentity:
    external_id: str | None
    display_name: str | None
    secret_payload: dict[str, Any]
    expires_at: datetime | None


async def begin_oauth(
    session: AsyncSession,
    *,
    account: ConnectorAccountRecord,
    redirect_uri: str | None,
) -> OAuthStart:
    if account.auth_type != "oauth2":
        raise ValueError("该连接器不是 OAuth2 认证")
    client_id = str(account.config.get("client_id") or "").strip()
    callback = str(redirect_uri or account.config.get("redirect_uri") or "").strip()
    if not client_id or not callback:
        raise ValueError("连接器缺少 client_id 或 redirect_uri")
    state = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    await session.execute(
        text(
            """
            DELETE FROM oauth_states
            WHERE connector_account_id = :account_id OR expires_at <= now()
            """
        ),
        {"account_id": account.id},
    )
    await session.execute(
        text(
            """
            INSERT INTO oauth_states (state, connector_account_id, redirect_uri, expires_at)
            VALUES (:state, :account_id, :redirect_uri, :expires_at)
            """
        ),
        {
            "state": state,
            "account_id": account.id,
            "redirect_uri": callback,
            "expires_at": expires_at,
        },
    )
    await set_connector_status(session, account_id=account.id, status="authorizing")
    return OAuthStart(
        authorization_url=_authorization_url(account, callback, state),
        state=state,
        expires_at=expires_at,
    )


async def complete_oauth(
    session: AsyncSession,
    *,
    state: str,
    code: str,
    secret_store: LocalSecretStore,
    timeout_s: float,
    trust_env: bool,
    client: httpx.AsyncClient | None = None,
) -> ConnectorAccountRecord:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT connector_account_id, redirect_uri, expires_at
                    FROM oauth_states WHERE state = :state FOR UPDATE
                    """
                ),
                {"state": state},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("OAuth state 无效或已使用")
    if row["expires_at"] <= datetime.now(UTC):
        await session.execute(
            text("DELETE FROM oauth_states WHERE state = :state"), {"state": state}
        )
        raise ValueError("OAuth state 已过期，请重新发起授权")

    from app.services.connectors import get_connector_account

    account = await get_connector_account(session, row["connector_account_id"])
    if account is None or not account.enabled:
        raise ValueError("连接器不存在或已停用")
    await session.execute(text("DELETE FROM oauth_states WHERE state = :state"), {"state": state})
    try:
        identity = await _exchange_code(
            account,
            code=code,
            redirect_uri=str(row["redirect_uri"]),
            existing=connector_secrets(account, secret_store),
            timeout_s=timeout_s,
            trust_env=trust_env,
            client=client,
        )
    except Exception as error:
        await set_connector_status(
            session,
            account_id=account.id,
            status="error",
            error=f"OAuth 交换失败：{error}"[:1000],
        )
        raise
    ciphertext = secret_store.encrypt(identity.secret_payload)
    await set_connector_status(
        session,
        account_id=account.id,
        status="connected",
        secret_ciphertext=ciphertext,
        external_account_id=identity.external_id,
        external_account_name=identity.display_name,
        expires_at=identity.expires_at,
    )
    updated = await get_connector_account(session, account.id)
    if updated is None:  # pragma: no cover - 同一事务内不可达
        raise RuntimeError("OAuth 完成后连接器丢失")
    return updated


def _authorization_url(account: ConnectorAccountRecord, redirect_uri: str, state: str) -> str:
    client_id = str(account.config["client_id"])
    if account.kind in {"wecom", "wechat_official"}:
        params = {
            "appid": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base" if account.kind == "wecom" else "snsapi_userinfo",
            "state": state,
        }
        if account.kind == "wecom" and account.config.get("agent_id"):
            params["agentid"] = str(account.config["agent_id"])
        return f"{_AUTHORIZE_URLS[account.kind]}?{urlencode(params)}#wechat_redirect"
    scopes = account.scopes or (["read:user"] if account.kind == "github" else [])
    if account.kind == "tencent_docs":
        scopes = scopes or ["all"]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{_AUTHORIZE_URLS[account.kind]}?{urlencode(params)}"


async def _exchange_code(
    account: ConnectorAccountRecord,
    *,
    code: str,
    redirect_uri: str,
    existing: dict[str, Any],
    timeout_s: float,
    trust_env: bool,
    client: httpx.AsyncClient | None,
) -> OAuthIdentity:
    owns_client = client is None
    runtime_client = client or httpx.AsyncClient(timeout=timeout_s, trust_env=trust_env)
    try:
        if account.kind == "github":
            return await _github_exchange(runtime_client, account, existing, code, redirect_uri)
        if account.kind == "feishu":
            return await _feishu_exchange(runtime_client, account, existing, code, redirect_uri)
        if account.kind == "wecom":
            return await _wecom_exchange(runtime_client, account, existing, code)
        if account.kind == "wechat_official":
            return await _wechat_exchange(runtime_client, account, existing, code)
        return await _tencent_docs_exchange(runtime_client, account, existing, code, redirect_uri)
    finally:
        if owns_client:
            await runtime_client.aclose()


async def _github_exchange(
    client: httpx.AsyncClient,
    account: ConnectorAccountRecord,
    existing: dict[str, Any],
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    payload = await _post_json(
        client,
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": account.config["client_id"],
            "client_secret": _required(existing, "client_secret"),
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
    )
    token = _required(payload, "access_token")
    user = await _get_json(
        client,
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    return OAuthIdentity(
        str(user.get("id") or "") or None,
        str(user.get("login") or "") or None,
        {**existing, **payload},
        None,
    )


async def _feishu_exchange(
    client: httpx.AsyncClient,
    account: ConnectorAccountRecord,
    existing: dict[str, Any],
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    payload = await _post_json(
        client,
        "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": account.config["client_id"],
            "client_secret": _required(existing, "client_secret"),
            "code": code,
            "redirect_uri": redirect_uri,
        },
        form=True,
    )
    token = _required(payload, "access_token")
    user_payload = await _get_json(
        client,
        "https://open.feishu.cn/open-apis/authen/v1/user_info",
        headers={"Authorization": f"Bearer {token}"},
    )
    nested_user = user_payload.get("data")
    user: dict[str, Any] = nested_user if isinstance(nested_user, dict) else user_payload
    return OAuthIdentity(
        str(user.get("open_id") or user.get("union_id") or "") or None,
        str(user.get("name") or user.get("en_name") or "") or None,
        {**existing, **payload},
        _expiry(payload),
    )


async def _wecom_exchange(
    client: httpx.AsyncClient,
    account: ConnectorAccountRecord,
    existing: dict[str, Any],
    code: str,
) -> OAuthIdentity:
    token_payload = await _get_json(
        client,
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={
            "corpid": account.config["client_id"],
            "corpsecret": _required(existing, "client_secret"),
        },
    )
    token = _required(token_payload, "access_token")
    user = await _get_json(
        client,
        "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
        params={"access_token": token, "code": code},
    )
    return OAuthIdentity(
        str(user.get("userid") or user.get("openid") or "") or None,
        str(user.get("userid") or "") or None,
        {**existing, "access_token": token},
        _expiry(token_payload),
    )


async def _wechat_exchange(
    client: httpx.AsyncClient,
    account: ConnectorAccountRecord,
    existing: dict[str, Any],
    code: str,
) -> OAuthIdentity:
    payload = await _get_json(
        client,
        "https://api.weixin.qq.com/sns/oauth2/access_token",
        params={
            "appid": account.config["client_id"],
            "secret": _required(existing, "client_secret"),
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    token = _required(payload, "access_token")
    openid = _required(payload, "openid")
    user = await _get_json(
        client,
        "https://api.weixin.qq.com/sns/userinfo",
        params={"access_token": token, "openid": openid, "lang": "zh_CN"},
    )
    return OAuthIdentity(
        openid, str(user.get("nickname") or "") or None, {**existing, **payload}, _expiry(payload)
    )


async def _tencent_docs_exchange(
    client: httpx.AsyncClient,
    account: ConnectorAccountRecord,
    existing: dict[str, Any],
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    payload = await _get_json(
        client,
        "https://docs.qq.com/oauth/v2/token",
        params={
            "grant_type": "authorization_code",
            "client_id": account.config["client_id"],
            "client_secret": _required(existing, "client_secret"),
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    _required(payload, "access_token")
    open_id = _required(payload, "user_id")
    return OAuthIdentity(open_id, "腾讯文档账户", {**existing, **payload}, _expiry(payload))


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    data: dict[str, Any],
    headers: dict[str, str] | None = None,
    form: bool = False,
) -> dict[str, Any]:
    response = await client.post(
        url, data=data if form else None, json=None if form else data, headers=headers
    )
    response.raise_for_status()
    return _object(response.json())


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.get(url, headers=headers, params=params)
    response.raise_for_status()
    return _object(response.json())


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("OAuth 服务响应不是 object")
    if (
        value.get("error")
        or value.get("errcode") not in {None, 0}
        or value.get("code") not in {None, 0}
    ):
        raise ValueError(str(value.get("error_description") or value.get("errmsg") or value))
    return value


def _required(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key) or "").strip()
    if not result:
        raise ValueError(f"OAuth 数据缺少 {key}")
    return result


def _expiry(payload: dict[str, Any]) -> datetime | None:
    try:
        seconds = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds) if seconds > 0 else None
