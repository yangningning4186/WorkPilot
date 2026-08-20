from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.cowork.browser_tools import register_browser_tools
from app.cowork.connector_tools import (
    ConnectorRequestArgs,
    _runtime_request,
    register_connector_tools,
)
from app.cowork.connectors import ConnectorAccountRecord
from app.cowork.oauth_connectors import _exchange_code
from app.cowork.tools import build_default_cowork_registry
from app.schemas.connectors import ConnectorKind
from app.security.secret_store import LocalSecretStore


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

    # 只是被下发过不构成保留理由。否则每轮目录都是上一轮的超集，几轮后等于
    # 无条件注入整个 registry，动态目录就白做了。
    after_topic_switch = {item.name for item in registry.tool_definitions_for("继续整理本地文件")}
    assert not {"browser_open", "browser_click", "web_search"} & after_topic_switch

    # 历史真的调用过才保留：模型上下文里已经有这些 tool_call，schema 不能消失。
    with_history = {
        item.name
        for item in registry.tool_definitions_for(
            "继续整理本地文件", retained_tools={"browser_click"}
        )
    }
    assert "browser_click" in with_history
    assert "browser_open" not in with_history

    matches = registry.search_tools("连接器写入官方 API", max_results=8)
    assert any(item["name"] == "act_connector_api" for item in matches)
    activated = {item.name for item in registry.tool_definitions_for("继续任务")}
    assert "act_connector_api" in activated

    # search_tool_catalog 的显式激活才进快照；单纯下发过的不进。
    snapshot = registry.runtime_snapshot()
    assert "act_connector_api" in snapshot["tool_registry"]["activated_tools"]
    assert "browser_open" not in snapshot["tool_registry"]["activated_tools"]

    resumed = build_default_cowork_registry()
    register_browser_tools(resumed)
    register_connector_tools(resumed)
    resumed.restore_runtime_snapshot(snapshot)
    resumed_tools = {item.name for item in resumed.tool_definitions_for("恢复后继续")}
    assert "act_connector_api" in resumed_tools


def test_dynamic_tool_catalog_does_not_grow_monotonically() -> None:
    """连续换话题不能把目录推向完整 registry。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    turns = (
        "把这个 word 文档整理一下",
        "搜一下最新新闻并打开链接",
        "同步到 github 连接器",
        "写个 shell 脚本",
        "用 mcp 试试",
    )
    sizes = [len(registry.tool_definitions_for(turn)) for turn in turns]

    assert max(sizes) <= 24
    assert len(registry.tool_definitions()) > max(sizes)
    # 单调增长的旧行为下，最后一轮必然 >= 之前每一轮。
    assert sizes[-1] < max(sizes)


def test_retained_tools_survive_even_beyond_max_tools() -> None:
    """保留集宁可超出 max_tools 也不能丢：缺一个 schema 就可能让 provider 拒绝整个请求。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    registered = {item["name"] for item in registry.catalog()}
    retained = {"browser_click", "browser_type", "act_connector_api", "edit_excel"}
    assert retained <= registered

    names = {
        item.name
        for item in registry.tool_definitions_for(
            "继续任务", max_tools=4, retained_tools=retained
        )
    }

    assert retained <= names


def test_runtime_snapshot_ignores_activated_tools_missing_from_new_registry() -> None:
    registry = build_default_cowork_registry()
    registry.restore_runtime_snapshot(
        {"tool_registry": {"activated_tools": ["read_text_file", "removed_extension"]}}
    )

    snapshot = registry.runtime_snapshot()

    assert "read_text_file" in snapshot["tool_registry"]["activated_tools"]
    assert "removed_extension" not in snapshot["tool_registry"]["activated_tools"]


@pytest.mark.parametrize(
    "goal",
    [
        "整理今日 AI 资讯并生成日报",
        "查找最新人工智能新闻",
        "汇总本周 AI 热点",
        "Create a daily AI news briefing",
    ],
)
def test_news_goals_activate_web_tools(goal: str) -> None:
    registry = build_default_cowork_registry()

    names = {item.name for item in registry.tool_definitions_for(goal)}

    assert {"web_search", "fetch_url"} <= names


def test_tool_catalog_matches_english_aliases_for_chinese_web_tools() -> None:
    registry = build_default_cowork_registry()

    matches = registry.search_tools("AI news latest", max_results=8)
    names = {item["name"] for item in matches}

    assert {"web_search", "fetch_url"} <= names
    web_search = next(item for item in matches if item["name"] == "web_search")
    assert "ai news" in web_search["search_aliases"]


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
    assert {"browser_snapshot", "web_search"} <= names
    assert "browser_open" not in names
    assert "search_tool_catalog" not in names
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
