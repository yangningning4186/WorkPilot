from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.agent_core.tools import MissingIdentitiesError
from app.core.config import get_settings
from app.cowork.browser_tools import register_browser_tools
from app.cowork.connector_descriptors import (
    get_connector_descriptor,
    list_connector_descriptors,
)
from app.cowork.connector_tools import (
    ConnectorRequestArgs,
    FeishuBaseRecordActionArgs,
    FeishuBaseRecordsArgs,
    FeishuCalendarEventActionArgs,
    FeishuCalendarEventsArgs,
    FeishuDriveFilesArgs,
    FeishuTaskActionArgs,
    _connector_http_error,
    _feishu_base_action_request,
    _feishu_base_list_request,
    _feishu_calendar_action_request,
    _feishu_calendar_list_request,
    _feishu_drive_list_request,
    _feishu_task_action_request,
    _runtime_request,
    connected_connector_kinds,
    register_connector_tools,
)
from app.cowork.connectors import ConnectorAccountRecord, create_connector_account
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


def test_github_app_403_explains_installation_requirement() -> None:
    account = _account("github", "unused")
    response = httpx.Response(
        403,
        json={
            "message": "Resource not accessible by integration",
            "documentation_url": "https://docs.github.com/rest/issues/issues#create-an-issue",
        },
        headers={"X-RateLimit-Remaining": "4999"},
    )

    message = _connector_http_error(
        account,
        response,
        request_headers={"Authorization": "Bearer ghu_do-not-leak"},
    )

    assert "Resource not accessible by integration" in message
    assert "GitHub App" in message
    assert "安装到目标账户/仓库" in message
    assert "Issues: Read and write" in message
    assert "ghu_do-not-leak" not in message


@pytest.mark.parametrize(
    "path",
    ["https://evil.example/steal", "//evil.example/steal", "/ok#fragment"],
)
def test_connector_request_rejects_host_override(path: str) -> None:
    with pytest.raises(ValidationError):
        ConnectorRequestArgs(account_id=uuid4(), path=path)


def test_feishu_calendar_tools_build_fixed_official_paths() -> None:
    account_id = uuid4()
    path, query = _feishu_calendar_list_request(
        FeishuCalendarEventsArgs(
            account_id=account_id,
            calendar_id="feishu.cn_team@group.calendar.feishu.cn",
            start_time=100,
            end_time=200,
            page_token="next",
        )
    )
    assert path == ("/calendar/v4/calendars/feishu.cn_team%40group.calendar.feishu.cn/events")
    assert query == {
        "start_time": "100",
        "end_time": "200",
        "page_size": 100,
        "page_token": "next",
    }

    method, create_path, body = _feishu_calendar_action_request(
        FeishuCalendarEventActionArgs(
            account_id=account_id,
            action="create",
            calendar_id="primary",
            event={"summary": "周会"},
        )
    )
    assert (method, create_path, body) == (
        "POST",
        "/calendar/v4/calendars/primary/events",
        {"summary": "周会"},
    )
    with pytest.raises(ValueError, match="event_id"):
        _feishu_calendar_action_request(
            FeishuCalendarEventActionArgs(
                account_id=account_id,
                action="delete",
                calendar_id="primary",
            )
        )


def test_feishu_base_tools_build_fixed_official_paths() -> None:
    account_id = uuid4()
    path, query = _feishu_base_list_request(
        FeishuBaseRecordsArgs(
            account_id=account_id,
            app_token="app_token",
            table_id="tbl_table",
            field_names=["负责人", "状态"],
            sort=["状态 DESC"],
        )
    )
    assert path == "/bitable/v1/apps/app_token/tables/tbl_table/records"
    assert query["field_names"] == '["负责人","状态"]'
    assert query["sort"] == '["状态 DESC"]'

    method, update_path, body = _feishu_base_action_request(
        FeishuBaseRecordActionArgs(
            account_id=account_id,
            action="update",
            app_token="app_token",
            table_id="tbl_table",
            record_id="rec_record",
            fields={"状态": "完成"},
        )
    )
    assert (method, update_path, body) == (
        "PUT",
        "/bitable/v1/apps/app_token/tables/tbl_table/records/rec_record",
        {"fields": {"状态": "完成"}},
    )


def test_connector_descriptors_are_the_single_catalog_and_mount_feishu_domains() -> None:
    descriptors = list_connector_descriptors()

    assert len({item.kind for item in descriptors}) == len(descriptors) == 5
    feishu = get_connector_descriptor("feishu")
    assert {"docs", "drive", "tasks", "approval"} <= set(feishu.capabilities)
    assert feishu.public()["logo"] == "feishu"
    assert feishu.public()["brand_color"] == "#3370ff"
    assert feishu.public()["auth_types"] == ["oauth2", "token"]
    assert feishu.tool_registrars == ("app.cowork.connector_tools:register_feishu_tools",)
    assert get_connector_descriptor("tencent_docs").auth_types == ("oauth2",)

    registry = build_default_cowork_registry()
    register_connector_tools(registry)
    names = {item["name"] for item in registry.catalog()}
    assert {
        "feishu_document_read",
        "feishu_drive_files",
        "feishu_task_read",
        "feishu_task_action",
        "feishu_approval_instance",
        "feishu_approval_submit",
    } <= names


def test_feishu_drive_and_task_requests_use_fixed_domain_paths() -> None:
    account_id = uuid4()
    path, query = _feishu_drive_list_request(
        FeishuDriveFilesArgs(account_id=account_id, folder_token="fld_demo", page_token="next")
    )
    assert path == "/drive/v1/files"
    assert query["folder_token"] == "fld_demo"
    assert query["page_token"] == "next"

    method, task_path, body = _feishu_task_action_request(
        FeishuTaskActionArgs(
            account_id=account_id,
            action="update",
            task_guid="task_demo",
            task={"summary": "更新周报"},
        )
    )
    assert (method, task_path, body) == (
        "PATCH",
        "/task/v2/tasks/task_demo",
        {"summary": "更新周报"},
    )


def test_tool_catalog_defers_extensions_until_explicitly_loaded() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    registered = {item["name"] for item in registry.catalog()}

    initial = {item.name for item in registry.tool_definitions_for("整理本地项目文件")}
    assert "load_tools" in initial
    assert {"act_connector_api", "browser_open"}.isdisjoint(initial)
    assert initial < registered

    browser = {item.name for item in registry.tool_definitions_for("浏览网页并搜索资料")}
    assert browser == initial

    after_topic_switch = {item.name for item in registry.tool_definitions_for("继续整理本地文件")}
    assert after_topic_switch == initial

    with_history = {
        item.name
        for item in registry.tool_definitions_for(
            "继续整理本地文件", retained_tools={"browser_click"}
        )
    }
    assert with_history == initial | {"browser_click"}

    # 搜索只负责发现，不会因为关键词命中就悄悄扩张 schema。
    matches = registry.search_tools("连接器写入官方 API", max_results=8)
    assert any(item["name"] == "act_connector_api" for item in matches)
    assert {item.name for item in registry.tool_definitions_for("继续任务")} == initial

    loaded = registry.load_deferred_tools(
        ["act_connector_api", "browser_open"], allowed=registry.names()
    )
    assert loaded["loaded"] == ["act_connector_api", "browser_open"]
    activated = {item.name for item in registry.tool_definitions_for("继续任务")}
    assert activated == initial | {"act_connector_api", "browser_open"}

    snapshot = registry.runtime_snapshot()
    assert "act_connector_api" in snapshot["tool_registry"]["activated_tools"]

    resumed = build_default_cowork_registry()
    register_browser_tools(resumed)
    register_connector_tools(resumed)
    resumed.restore_runtime_snapshot(snapshot)
    resumed_tools = {item.name for item in resumed.tool_definitions_for("恢复后继续")}
    assert resumed_tools == activated


def test_github_account_task_keeps_generic_api_as_named_advanced_fallback() -> None:
    """GitHub URL 会同时命中 web/git；通用 API 不进常规目录但仍可准确加载。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    goal = (
        "使用已连接的 GitHub 账户，在 "
        "https://github.com/yangningning4186/yangningning4186.github.io 创建一个测试 Issue"
    )
    names = {
        item.name
        for item in registry.tool_definitions_for(
            goal,
            capability_tools={
                "run_shell",
                "load_skill",
            },
        )
    }

    assert {"list_connectors", "read_connector_api", "act_connector_api"}.isdisjoint(names)

    manifest = registry.deferred_tools_manifest()
    assert "list_connectors" in manifest
    assert "read_connector_api" not in manifest
    assert "act_connector_api" not in manifest
    instructions = registry.system_instructions()
    assert "read_connector_api/act_connector_api" in instructions
    assert "GitHub" in instructions

    searched = {item["name"] for item in registry.search_tools("github", max_results=8)}
    assert {"list_connectors", "read_connector_api", "act_connector_api"} <= searched


def test_feishu_calendar_and_base_are_first_class_catalog_capabilities() -> None:
    registry = build_default_cowork_registry()
    register_connector_tools(registry)

    calendar = {item["name"] for item in registry.search_tools("飞书日历安排会议", max_results=8)}
    base = {item["name"] for item in registry.search_tools("写入飞书多维表格", max_results=8)}

    assert {"feishu_calendar_events", "feishu_calendar_event_action"} <= calendar
    assert {"feishu_base_records", "feishu_base_record_action"} <= base
    specs = {item["name"]: item for item in registry.catalog()}
    assert specs["feishu_calendar_event_action"]["approval_required"] is True
    assert specs["feishu_base_record_action"]["approval_required"] is True
    assert "external_approval" in specs["feishu_calendar_event_action"]["description"]
    instructions = registry.system_instructions()
    assert "account_id 时直接使用" in instructions
    assert "不要提前用 ask_user" in instructions
    assert "属于高级 fallback" in instructions


def test_connected_accounts_control_connector_surface(tmp_path: Path) -> None:
    settings = get_settings().model_copy(update={"cowork_data_path": tmp_path})
    store = LocalSecretStore(tmp_path / "master.key")
    create_connector_account(
        settings,
        kind="feishu",
        name="飞书主账号",
        auth_type="token",
        client_id=None,
        client_secret=None,
        access_token="test-token",
        refresh_token=None,
        redirect_uri=None,
        scopes=[],
        config={},
        enabled=True,
        secret_store=store,
    )

    kinds = connected_connector_kinds(settings)
    registry = build_default_cowork_registry()
    register_connector_tools(registry, enabled_kinds=kinds)

    assert kinds == frozenset({"feishu"})
    assert "feishu_calendar_events" in registry.names()
    assert "read_connector_api" in registry.names()  # 可按准确名称加载的高级 fallback
    manifest = registry.deferred_tools_manifest()
    assert "feishu_calendar_events" in manifest
    assert "read_connector_api" not in manifest
    assert "act_connector_api" not in manifest

    empty = build_default_cowork_registry()
    register_connector_tools(empty, enabled_kinds=frozenset())
    assert "list_connectors" not in empty.names()
    assert "feishu_calendar_events" not in empty.names()


def test_initial_tool_catalog_is_stable_across_topic_switches() -> None:
    """基础 schema 不随关键词抖动，扩展目录也保持逐字稳定。"""

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

    assert len(set(sizes)) == 1
    assert sizes[0] < len(registry.tool_definitions())
    assert registry.deferred_tools_manifest() == registry.deferred_tools_manifest()


def test_load_tools_has_no_schema_count_cap_and_keeps_manifest_byte_stable() -> None:
    """一次可加载整个允许集合；加载状态不应改写 system prompt 里的目录。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    deferred = sorted(registry.deferred_tool_names())
    manifest_before = registry.deferred_tools_manifest()

    result = registry.load_deferred_tools(deferred, allowed=registry.names())

    assert result == {
        "loaded": deferred,
        "already_loaded": [],
        "unavailable": [],
    }
    assert registry.deferred_tools_manifest() == manifest_before
    assert set(deferred) <= {item.name for item in registry.tool_definitions_for("继续完成长任务")}


def test_load_tools_reports_already_loaded_without_mutating_state() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    first = registry.load_deferred_tools(["browser_click"], allowed=registry.names())
    snapshot = registry.runtime_snapshot()
    second = registry.load_deferred_tools(["browser_click"], allowed=registry.names())
    core = registry.load_deferred_tools(["web_search"], allowed=frozenset())

    assert first["loaded"] == ["browser_click"]
    assert second == {
        "loaded": [],
        "already_loaded": ["browser_click"],
        "unavailable": [],
    }
    assert core == {
        "loaded": [],
        "already_loaded": ["web_search"],
        "unavailable": [],
    }
    assert registry.tools_already_loaded(["browser_click", "web_search"])
    assert registry.runtime_snapshot() == snapshot


def test_legacy_max_tools_argument_does_not_truncate_the_catalog() -> None:
    """兼容参数仍可传入，但不能再截断 schema。"""

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    initial = {item.name for item in registry.tool_definitions_for("继续任务")}
    retained = {"browser_click", "browser_type", "act_connector_api", "run_shell"}

    names = {
        item.name
        for item in registry.tool_definitions_for("继续任务", max_tools=4, retained_tools=retained)
    }

    assert names == initial | retained


def test_tool_catalog_order_does_not_depend_on_retained_set_iteration_order() -> None:
    """同一组工具必须排出同一个顺序。

    `retained_tools` 常常是 frozenset，而字符串哈希逐进程随机化——不排序的话，同一个
    会话在不同 worker 进程里会拿到顺序不同的 tool schema 数组。provider 的 prompt
    cache 按前缀命中，数组一变前缀就作废，`prompt_cache_key` 也会落到另一个分区。
    """

    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)
    retained = ["browser_click", "act_connector_api", "run_shell", "browser_type"]

    forward = [
        item.name for item in registry.tool_definitions_for("继续任务", retained_tools=retained)
    ]
    backward = [
        item.name
        for item in registry.tool_definitions_for(
            "继续任务", retained_tools=list(reversed(retained))
        )
    ]

    assert forward == backward
    assert set(retained) <= set(forward)


def test_runtime_snapshot_rejects_activated_tools_missing_from_new_registry() -> None:
    registry = build_default_cowork_registry()
    with pytest.raises(MissingIdentitiesError, match="removed_extension"):
        registry.restore_runtime_snapshot(
            {"tool_registry": {"activated_tools": ["read_text_file", "removed_extension"]}}
        )


@pytest.mark.parametrize(
    "goal",
    [
        "整理今日 AI 资讯并生成日报",
        "查找最新人工智能新闻",
        "汇总本周 AI 热点",
        "Create a daily AI news briefing",
    ],
)
def test_news_goals_discover_web_tools_without_query_based_schema_growth(goal: str) -> None:
    registry = build_default_cowork_registry()

    names = {item.name for item in registry.tool_definitions_for(goal)}

    assert {"web_search", "fetch_url"} <= names
    manifest = registry.deferred_tools_manifest()
    assert "web_search" not in manifest
    assert "fetch_url" not in manifest


def test_default_tool_surface_keeps_core_tools_and_defers_admin_routes() -> None:
    registry = build_default_cowork_registry()
    from app.cowork.extensions import register_skill_tools

    register_skill_tools(registry, get_settings())
    core = {
        "web_search",
        "fetch_url",
        "load_skill",
        "list_files",
        "search_files",
        "read_file",
        "write_file",
        "replace_in_file",
        "run_shell",
    }
    on_demand = {"run_sandbox"}
    hidden_compatibility = {
        "read_text_file",
        "write_text_file",
        "create_artifact",
        "read_pdf",
        "list_skills",
    }
    initial = {item.name for item in registry.tool_definitions_for("任意任务")}
    deferred = registry.deferred_tool_names()

    assert core <= initial
    assert core.isdisjoint(deferred)
    assert on_demand.isdisjoint(initial)
    assert on_demand <= deferred
    assert hidden_compatibility.isdisjoint(initial | deferred)


def test_tool_catalog_matches_english_aliases_for_chinese_web_tools() -> None:
    registry = build_default_cowork_registry()

    matches = registry.search_tools("AI news latest", max_results=8)
    names = {item["name"] for item in matches}

    assert {"web_search", "fetch_url"} <= names
    web_search = next(item for item in matches if item["name"] == "web_search")
    assert "ai news" in web_search["search_aliases"]


def test_read_only_subagent_catalog_is_unbounded_but_excludes_external_actions() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    register_connector_tools(registry)

    tools = registry.read_only_tool_definitions(
        exclude=frozenset({"explore"}),
        query="搜索网页并查看结果",
        max_tools=1,
    )
    names = {item.name for item in tools}

    assert len(tools) > 1
    assert {"browser_snapshot", "web_search"} <= names
    assert "browser_open" not in names
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
