"""受支持连接器的 OAuth 授权码流程。

**state 不落盘。** 它是一次性的、10 分钟就过期的 CSRF 令牌，只在"用户点了授权"和
"浏览器带着 code 跳回来"之间活着——这段时间短于任何一次进程重启的间隔。参照
openworker `coworker/mcp/oauth.py` 的单槽 pending future：那里更进一步，一次只允许
一个交互式登录。这里保留按 state 索引的字典，因为多个连接器可以同时授权；但同一个
账户重新发起授权会作废它上一条 state，对应原来那句
`DELETE ... WHERE connector_account_id = :account_id`。

落盘的代价不只是多一张表：state 一旦持久化，重启后那些悬着的授权流会一直留着，
而它们对应的浏览器标签早就没了。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.cowork.connector_descriptors import ConnectorDescriptor, get_connector_descriptor
from app.cowork.connectors import (
    ConnectorAccountRecord,
    connector_secrets,
    get_connector_account,
    set_connector_status,
)
from app.security.secret_store import LocalSecretStore


@dataclass(frozen=True)
class _PendingAuthorization:
    account_id: Any
    redirect_uri: str
    expires_at: datetime


_pending: dict[str, _PendingAuthorization] = {}
_pending_lock = threading.Lock()


def _sweep(now: datetime) -> None:
    for state, pending in list(_pending.items()):
        if pending.expires_at <= now:
            del _pending[state]


def reset_pending_authorizations() -> None:
    """测试用：清空进程内的授权流，避免用例之间互相看见对方的 state。"""

    with _pending_lock:
        _pending.clear()


def reject_oauth(*, settings: Settings, state: str, reason: str) -> bool:
    """消费一次被用户/服务方拒绝的 flow，并让客户端轮询立即得到终态。"""

    now = datetime.now(UTC)
    with _pending_lock:
        _sweep(now)
        pending = _pending.pop(state, None)
    if pending is None:
        return False
    set_connector_status(
        settings,
        account_id=pending.account_id,
        status="error",
        error=f"OAuth 授权未完成：{reason}"[:1000],
    )
    return True


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
    *,
    settings: Settings,
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
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=10)
    with _pending_lock:
        _sweep(now)
        # 同一账户重新发起授权作废上一条：旧标签页再回来也换不到 token。
        for existing, pending in list(_pending.items()):
            if pending.account_id == account.id:
                del _pending[existing]
        _pending[state] = _PendingAuthorization(
            account_id=account.id, redirect_uri=callback, expires_at=expires_at
        )
    set_connector_status(settings, account_id=account.id, status="authorizing")
    return OAuthStart(
        authorization_url=_authorization_url(account, callback, state),
        state=state,
        expires_at=expires_at,
    )


async def complete_oauth(
    *,
    settings: Settings,
    state: str,
    code: str,
    secret_store: LocalSecretStore,
    timeout_s: float,
    trust_env: bool,
    client: httpx.AsyncClient | None = None,
) -> ConnectorAccountRecord:
    now = datetime.now(UTC)
    with _pending_lock:
        _sweep(now)
        # 弹出即作废：一条 state 只能换一次 token，重放拿到的是"无效或已使用"。
        pending = _pending.pop(state, None)
    if pending is None:
        raise ValueError("OAuth state 无效或已使用")
    account = get_connector_account(settings, pending.account_id)
    if account is None or not account.enabled:
        raise ValueError("连接器不存在或已停用")
    try:
        identity = await _exchange_code(
            account,
            code=code,
            redirect_uri=pending.redirect_uri,
            existing=connector_secrets(account, secret_store),
            timeout_s=timeout_s,
            trust_env=trust_env,
            client=client,
        )
    except Exception as error:
        set_connector_status(
            settings,
            account_id=account.id,
            status="error",
            error=f"OAuth 交换失败：{error}"[:1000],
        )
        raise
    ciphertext = secret_store.encrypt(identity.secret_payload)
    set_connector_status(
        settings,
        account_id=account.id,
        status="connected",
        secret_ciphertext=ciphertext,
        external_account_id=identity.external_id,
        external_account_name=identity.display_name,
        expires_at=identity.expires_at,
    )
    updated = get_connector_account(settings, account.id)
    if updated is None:  # pragma: no cover - 刚写完就读不到只可能是磁盘故障
        raise RuntimeError("OAuth 完成后连接器丢失")
    return updated


def _authorization_url(account: ConnectorAccountRecord, redirect_uri: str, state: str) -> str:
    descriptor = get_connector_descriptor(account.kind)
    builder = {
        "github": _standard_authorization_url,
        "feishu": _standard_authorization_url,
        "tencent_docs": _standard_authorization_url,
        "wecom": _wechat_authorization_url,
        "wechat": _wechat_authorization_url,
    }[descriptor.oauth_adapter]
    return builder(descriptor, account, redirect_uri, state)


def _wechat_authorization_url(
    descriptor: ConnectorDescriptor,
    account: ConnectorAccountRecord,
    redirect_uri: str,
    state: str,
) -> str:
    client_id = str(account.config["client_id"])
    params = {
        "appid": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "snsapi_base" if descriptor.oauth_adapter == "wecom" else "snsapi_userinfo",
        "state": state,
    }
    if descriptor.oauth_adapter == "wecom" and account.config.get("agent_id"):
        params["agentid"] = str(account.config["agent_id"])
    return f"{descriptor.authorize_url}?{urlencode(params)}{descriptor.oauth_fragment}"


def _standard_authorization_url(
    descriptor: ConnectorDescriptor,
    account: ConnectorAccountRecord,
    redirect_uri: str,
    state: str,
) -> str:
    client_id = str(account.config["client_id"])
    scopes = account.scopes or list(descriptor.default_scopes)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{descriptor.authorize_url}?{urlencode(params)}{descriptor.oauth_fragment}"


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
        descriptor = get_connector_descriptor(account.kind)
        exchange = {
            "github": _github_exchange,
            "feishu": _feishu_exchange,
            "wecom": _wecom_exchange,
            "wechat": _wechat_exchange,
            "tencent_docs": _tencent_docs_exchange,
        }[descriptor.oauth_adapter]
        return await exchange(runtime_client, account, existing, code, redirect_uri)
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
    redirect_uri: str,
) -> OAuthIdentity:
    del redirect_uri
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
    redirect_uri: str,
) -> OAuthIdentity:
    del redirect_uri
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
