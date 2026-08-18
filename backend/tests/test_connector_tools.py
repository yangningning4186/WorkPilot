from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.agent.cowork_browser_tools import register_browser_tools
from app.agent.cowork_connector_tools import (
    ConnectorRequestArgs,
    _runtime_request,
    register_connector_tools,
)
from app.agent.cowork_tools import build_default_cowork_registry
from app.schemas.connectors import ConnectorKind
from app.security.secret_store import LocalSecretStore
from app.services.connectors import ConnectorAccountRecord
from app.services.oauth_connectors import _exchange_code


def _account(
    kind: ConnectorKind, ciphertext: str, *, external_account_id: str | None = None
) -> ConnectorAccountRecord:
    now = datetime.now(UTC)
    return ConnectorAccountRecord(
        id=uuid4(),
        kind=kind,
        name="测试连接器",
        auth_type="oauth2",
        status="connected",
        config={"client_id": "client-id"},
        secret_ciphertext=ciphertext,
        scopes=[],
        external_account_id=external_account_id,
        external_account_name=None,
        expires_at=None,
        last_checked_at=None,
        last_error=None,
        enabled=True,
        created_at=now,
        updated_at=now,
    )


def test_connector_request_is_pinned_to_official_host_and_hides_token(tmp_path: Path) -> None:
    store = LocalSecretStore(tmp_path / "master.key")
    ciphertext = store.encrypt({"access_token": "top-secret"})
    github = _account("github", ciphertext)
    url, headers, query = _runtime_request(
        github,
        path="/user",
        query={"page": 1},
        secret_store=store,
    )
    assert url == "https://api.github.com/user"
    assert headers["Authorization"] == "Bearer top-secret"
    assert query == {"page": 1}

    wecom = _account("wecom", ciphertext)
    url, headers, query = _runtime_request(
        wecom,
        path="/user/get",
        query={},
        secret_store=store,
    )
    assert url == "https://qyapi.weixin.qq.com/cgi-bin/user/get"
    assert "Authorization" not in headers
    assert query == {"access_token": "top-secret"}

    tencent = _account("tencent_docs", ciphertext, external_account_id="open-user")
    url, headers, query = _runtime_request(
        tencent,
        path="/v1/files",
        query={},
        secret_store=store,
    )
    assert url == "https://docs.qq.com/openapi/v1/files"
    assert headers["Access-Token"] == "top-secret"
    assert headers["Client-Id"] == "client-id"
    assert headers["Open-Id"] == "open-user"
    assert "Authorization" not in headers


@pytest.mark.parametrize(
    "path",
    ["https://evil.example/steal", "//evil.example/steal", "/ok#fragment"],
)
def test_connector_request_rejects_host_override(path: str) -> None:
    with pytest.raises(ValidationError):
        ConnectorRequestArgs(account_id=uuid4(), path=path)


def test_dynamic_tool_catalog_keeps_initial_schema_bounded_and_activates_matches() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)

    initial = {item.name for item in registry.tool_definitions_for("整理本地项目文件")}
    assert "search_tool_catalog" in initial
    assert "act_connector_api" not in initial
    assert "browser_open" not in initial

    browser = {item.name for item in registry.tool_definitions_for("浏览网页并搜索资料")}
    assert {"browser_open", "browser_click", "web_search"} <= browser

    matches = registry.search_tools("连接器写入官方 API", max_results=8)
    assert any(item["name"] == "act_connector_api" for item in matches)
    activated = {item.name for item in registry.tool_definitions_for("继续任务")}
    assert "act_connector_api" in activated


def test_read_only_subagent_catalog_is_bounded_and_excludes_external_actions() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)

    tools = registry.read_only_tool_definitions(
        exclude=frozenset({"explore"}),
        query="搜索网页并查看结果",
        max_tools=12,
    )
    names = {item.name for item in tools}

    assert len(tools) <= 12
    assert {"browser_open", "web_search"} <= names
    assert "act_connector_api" not in names
    assert "read_connector_api" not in names


async def test_tencent_docs_oauth_uses_get_and_persists_open_id() -> None:
    account = _account("tencent_docs", "unused")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/oauth/v2/token"
        assert request.url.params["grant_type"] == "authorization_code"
        assert request.url.params["client_secret"] == "client-secret"
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 259200,
                "user_id": "open-user",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await _exchange_code(
            account,
            code="code",
            redirect_uri="https://example.com/callback",
            existing={"client_secret": "client-secret"},
            timeout_s=5,
            trust_env=False,
            client=client,
        )

    assert identity.external_id == "open-user"
    assert identity.secret_payload["access_token"] == "access"
