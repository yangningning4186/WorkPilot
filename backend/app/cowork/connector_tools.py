"""把 OAuth 连接器暴露为固定官方域名、凭据不出进程的 Cowork 工具。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.cowork.connector_descriptors import (
    get_connector_descriptor,
    list_connector_descriptors,
)
from app.cowork.connectors import (
    ConnectorAccountRecord,
    connector_secrets,
    get_connector_account,
    list_connector_accounts,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
)
from app.security.secret_store import LocalSecretStore


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _external_mutation_capability(
    raw: BaseModel,
) -> Literal["external.write", "external.destructive"]:
    action = str(getattr(raw, "action", getattr(raw, "method", ""))).casefold()
    return "external.destructive" if action == "delete" else "external.write"


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


class FeishuCalendarEventsArgs(_StrictArgs):
    account_id: UUID
    calendar_id: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_@.\-]+$")
    start_time: int = Field(ge=0, description="查询起点，Unix 秒")
    end_time: int = Field(ge=0, description="查询终点，Unix 秒")
    page_size: int = Field(default=100, ge=1, le=500)
    page_token: str | None = Field(default=None, max_length=1024)

    @field_validator("end_time")
    @classmethod
    def validate_time_range(cls, value: int, info: Any) -> int:
        start = info.data.get("start_time")
        if isinstance(start, int) and value <= start:
            raise ValueError("end_time 必须晚于 start_time")
        return value


class FeishuCalendarEventActionArgs(_StrictArgs):
    account_id: UUID
    action: Literal["create", "update", "delete"]
    calendar_id: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_@.\-]+$")
    event_id: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_@.\-]+$")
    event: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def normalize_event_id(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @field_validator("event")
    @classmethod
    def bound_event(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False)) > 100_000:
            raise ValueError("event 请求体不能超过 100000 字符")
        return value

    @model_validator(mode="after")
    def validate_action_payload(self) -> FeishuCalendarEventActionArgs:
        if self.action == "create":
            if self.event_id is not None:
                raise ValueError("创建日程时不要提供 event_id")
            if not self.event:
                raise ValueError("创建日程必须提供 event 请求体")
        elif not self.event_id:
            raise ValueError(f"{self.action} 日程必须提供 event_id")
        elif self.action == "update" and not self.event:
            raise ValueError("更新日程必须提供 event 请求体")
        elif self.action == "delete" and self.event:
            raise ValueError("删除日程时不要提供 event 请求体")
        return self


class FeishuBaseRecordsArgs(_StrictArgs):
    account_id: UUID
    app_token: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    table_id: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    view_id: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    filter: str | None = Field(default=None, max_length=5000)
    sort: list[str] = Field(default_factory=list, max_length=100)
    field_names: list[str] = Field(default_factory=list, max_length=100)
    page_size: int = Field(default=100, ge=1, le=500)
    page_token: str | None = Field(default=None, max_length=1024)


class FeishuBaseRecordActionArgs(_StrictArgs):
    account_id: UUID
    action: Literal["create", "update", "delete"]
    app_token: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    table_id: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    record_id: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_\-]+$")
    fields: dict[str, Any] = Field(default_factory=dict, max_length=500)

    @model_validator(mode="after")
    def validate_action_payload(self) -> FeishuBaseRecordActionArgs:
        if self.action == "create":
            if self.record_id is not None:
                raise ValueError("创建记录时不要提供 record_id")
            if not self.fields:
                raise ValueError("创建记录必须提供 fields")
        elif not self.record_id:
            raise ValueError(f"{self.action} 记录必须提供 record_id")
        elif self.action == "update" and not self.fields:
            raise ValueError("更新记录必须提供 fields")
        elif self.action == "delete" and self.fields:
            raise ValueError("删除记录时不要提供 fields")
        return self


_FEISHU_ID_PATTERN = r"^[A-Za-z0-9_@.\-]+$"


class FeishuDocumentArgs(_StrictArgs):
    account_id: UUID
    document_id: str = Field(min_length=1, max_length=512, pattern=_FEISHU_ID_PATTERN)
    lang: Literal["zh", "en"] = "zh"


class FeishuDriveFilesArgs(_StrictArgs):
    account_id: UUID
    folder_token: str | None = Field(default=None, max_length=512, pattern=_FEISHU_ID_PATTERN)
    page_size: int = Field(default=100, ge=1, le=200)
    page_token: str | None = Field(default=None, max_length=1024)
    order_by: Literal["EditedTime", "CreatedTime"] = "EditedTime"
    direction: Literal["ASC", "DESC"] = "DESC"


class FeishuTaskArgs(_StrictArgs):
    account_id: UUID
    task_guid: str = Field(min_length=1, max_length=512, pattern=_FEISHU_ID_PATTERN)


class FeishuTaskActionArgs(_StrictArgs):
    account_id: UUID
    action: Literal["create", "update", "delete"]
    task_guid: str | None = Field(default=None, max_length=512, pattern=_FEISHU_ID_PATTERN)
    task: dict[str, Any] = Field(default_factory=dict, max_length=200)

    @model_validator(mode="after")
    def validate_action_payload(self) -> FeishuTaskActionArgs:
        if self.action == "create":
            if self.task_guid is not None or not self.task:
                raise ValueError("创建任务必须提供 task，且不要提供 task_guid")
        elif not self.task_guid:
            raise ValueError(f"{self.action} 任务必须提供 task_guid")
        elif self.action == "update" and not self.task:
            raise ValueError("更新任务必须提供 task")
        elif self.action == "delete" and self.task:
            raise ValueError("删除任务时不要提供 task")
        return self


class FeishuApprovalInstanceArgs(_StrictArgs):
    account_id: UUID
    instance_code: str = Field(min_length=1, max_length=512, pattern=_FEISHU_ID_PATTERN)


class FeishuApprovalSubmitArgs(_StrictArgs):
    account_id: UUID
    approval_code: str = Field(min_length=1, max_length=512, pattern=_FEISHU_ID_PATTERN)
    user_id: str = Field(min_length=1, max_length=512, pattern=_FEISHU_ID_PATTERN)
    form: str = Field(min_length=2, max_length=100_000)
    uuid: str | None = Field(default=None, max_length=512, pattern=_FEISHU_ID_PATTERN)
    node_approver_user_id_list: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    node_cc_user_id_list: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @field_validator("form")
    @classmethod
    def validate_form_json(cls, value: str) -> str:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("form 必须是 JSON 字符串") from error
        if not isinstance(loaded, list):
            raise ValueError("form 必须编码飞书审批表单数组")
        return value


def _public_account(account: ConnectorAccountRecord) -> dict[str, Any]:
    descriptor = get_connector_descriptor(account.kind)
    return {
        "id": str(account.id),
        "kind": account.kind,
        "name": account.name,
        "status": account.status,
        "scopes": account.scopes,
        "external_account_name": account.external_account_name,
        "enabled": account.enabled,
        "capabilities": list(descriptor.capabilities),
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
    descriptor = get_connector_descriptor(account.kind)
    headers.update(dict(descriptor.request_headers))
    if descriptor.auth_style == "tencent_headers":
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
    elif descriptor.auth_style == "query_token":
        request_query["access_token"] = access_token
    else:
        headers["Authorization"] = f"Bearer {access_token}"
    return f"{descriptor.api_base_url}{path}", headers, request_query


def _connector_http_error(
    account: ConnectorAccountRecord,
    response: httpx.Response,
    *,
    request_headers: dict[str, str],
) -> str:
    """保留上游的可执行错误信息，但绝不回显请求头或凭据。"""

    message = ""
    documentation_url = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        documentation_url = str(payload.get("documentation_url") or "").strip()
    elif response.text:
        message = " ".join(response.text.split())
    detail = f"连接器 API 返回 HTTP {response.status_code}"
    if message:
        detail = f"{detail}：{message[:1000]}"

    if account.kind == "github" and response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        token = request_headers.get("Authorization", "").removeprefix("Bearer ")
        if remaining == "0":
            detail += "。GitHub API 调用额度已耗尽，请等待 RateLimit-Reset 后重试"
        elif token.startswith("ghu_"):
            detail += (
                "。当前凭据是 GitHub App 用户令牌；请把该 GitHub App 安装到目标账户/仓库，"
                "并授予 Issues: Read and write。若刚修改过 App 权限，还需要在安装页批准新权限"
            )
    if documentation_url.startswith("https://docs.github.com/"):
        detail += f"（GitHub 文档：{documentation_url[:1000]}）"
    return detail


async def _connector_request(
    context: CoworkToolContext,
    args: ConnectorRequestArgs,
    *,
    method: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    account = get_connector_account(context.settings, args.account_id)
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
        raise ValueError(_connector_http_error(account, response, request_headers=headers))
    text = response.text[: context.settings.cowork_mcp_result_max_chars]
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    descriptor = get_connector_descriptor(account.kind)
    if descriptor.response_code_field is not None and isinstance(payload, dict):
        code = payload.get(descriptor.response_code_field)
        if isinstance(code, int) and code != 0:
            message = str(payload.get(descriptor.response_message_field) or "未知错误")[:1000]
            raise ValueError(f"{descriptor.label} API 返回 code={code}：{message}")
    return {
        "account": _public_account(account),
        "method": method,
        "path": args.path,
        "status_code": response.status_code,
        "response": payload,
        "truncated": len(response.text) > len(text),
    }


def _feishu_calendar_list_request(
    args: FeishuCalendarEventsArgs,
) -> tuple[str, dict[str, str | int | float | bool]]:
    path = f"/calendar/v4/calendars/{quote(args.calendar_id, safe='')}/events"
    query: dict[str, str | int | float | bool] = {
        "start_time": str(args.start_time),
        "end_time": str(args.end_time),
        "page_size": args.page_size,
    }
    if args.page_token:
        query["page_token"] = args.page_token
    return path, query


def _feishu_calendar_action_request(
    args: FeishuCalendarEventActionArgs,
) -> tuple[str, str, dict[str, Any] | None]:
    base = f"/calendar/v4/calendars/{quote(args.calendar_id, safe='')}/events"
    if args.action == "create":
        if args.event_id is not None:
            raise ValueError("创建日程时不要提供 event_id")
        if not args.event:
            raise ValueError("创建日程必须提供 event 请求体")
        return "POST", base, args.event
    if not args.event_id:
        raise ValueError(f"{args.action} 日程必须提供 event_id")
    path = f"{base}/{quote(args.event_id, safe='')}"
    if args.action == "update":
        if not args.event:
            raise ValueError("更新日程必须提供 event 请求体")
        return "PATCH", path, args.event
    if args.event:
        raise ValueError("删除日程时不要提供 event 请求体")
    return "DELETE", path, None


def _feishu_base_list_request(
    args: FeishuBaseRecordsArgs,
) -> tuple[str, dict[str, str | int | float | bool]]:
    path = (
        f"/bitable/v1/apps/{quote(args.app_token, safe='')}/tables/"
        f"{quote(args.table_id, safe='')}/records"
    )
    query: dict[str, str | int | float | bool] = {"page_size": args.page_size}
    if args.page_token:
        query["page_token"] = args.page_token
    if args.view_id:
        query["view_id"] = args.view_id
    if args.filter:
        query["filter"] = args.filter
    if args.sort:
        query["sort"] = json.dumps(args.sort, ensure_ascii=False, separators=(",", ":"))
    if args.field_names:
        query["field_names"] = json.dumps(
            args.field_names, ensure_ascii=False, separators=(",", ":")
        )
    return path, query


def _feishu_base_action_request(
    args: FeishuBaseRecordActionArgs,
) -> tuple[str, str, dict[str, Any] | None]:
    base = (
        f"/bitable/v1/apps/{quote(args.app_token, safe='')}/tables/"
        f"{quote(args.table_id, safe='')}/records"
    )
    if args.action == "create":
        if args.record_id is not None:
            raise ValueError("创建记录时不要提供 record_id")
        if not args.fields:
            raise ValueError("创建记录必须提供 fields")
        return "POST", base, {"fields": args.fields}
    if not args.record_id:
        raise ValueError(f"{args.action} 记录必须提供 record_id")
    path = f"{base}/{quote(args.record_id, safe='')}"
    if args.action == "update":
        if not args.fields:
            raise ValueError("更新记录必须提供 fields")
        return "PUT", path, {"fields": args.fields}
    if args.fields:
        raise ValueError("删除记录时不要提供 fields")
    return "DELETE", path, None


def _feishu_drive_list_request(
    args: FeishuDriveFilesArgs,
) -> tuple[str, dict[str, str | int | float | bool]]:
    query: dict[str, str | int | float | bool] = {
        "page_size": args.page_size,
        "order_by": args.order_by,
        "direction": args.direction,
    }
    if args.folder_token:
        query["folder_token"] = args.folder_token
    if args.page_token:
        query["page_token"] = args.page_token
    return "/drive/v1/files", query


def _feishu_task_action_request(
    args: FeishuTaskActionArgs,
) -> tuple[str, str, dict[str, Any] | None]:
    if args.action == "create":
        return "POST", "/task/v2/tasks", args.task
    if not args.task_guid:
        raise ValueError(f"{args.action} 任务必须提供 task_guid")
    path = f"/task/v2/tasks/{quote(args.task_guid, safe='')}"
    if args.action == "update":
        return "PATCH", path, args.task
    return "DELETE", path, None


def _feishu_approval_submit_body(args: FeishuApprovalSubmitArgs) -> dict[str, Any]:
    body: dict[str, Any] = {
        "approval_code": args.approval_code,
        "user_id": args.user_id,
        "form": args.form,
    }
    if args.uuid:
        body["uuid"] = args.uuid
    if args.node_approver_user_id_list:
        body["node_approver_user_id_list"] = args.node_approver_user_id_list
    if args.node_cc_user_id_list:
        body["node_cc_user_id_list"] = args.node_cc_user_id_list
    return body


def _require_feishu_account(context: CoworkToolContext, account_id: UUID) -> None:
    account = get_connector_account(context.settings, account_id)
    if account is None:
        raise LookupError("飞书连接器不存在")
    if account.kind != "feishu":
        raise ValueError("该工具只接受 kind=feishu 的连接器账户")


async def _list_handler(context: CoworkToolContext, _: BaseModel) -> CoworkToolResult:
    accounts = list_connector_accounts(context.settings)
    return CoworkToolResult(output={"connectors": [_public_account(item) for item in accounts]})


async def _read_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = ConnectorRequestArgs.model_validate(raw.model_dump())
    return CoworkToolResult(output=await _connector_request(context, args, method="GET", body=None))


async def _action_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = ConnectorActionArgs.model_validate(raw.model_dump())
    output = await _connector_request(
        context, args, method=args.method, body=args.body if args.method != "DELETE" else None
    )
    return CoworkToolResult(
        output=output, effect_ref=f"connector:{args.account_id}:{args.method}:{args.path}"
    )


async def _feishu_read(
    context: CoworkToolContext,
    *,
    account_id: UUID,
    path: str,
    query: dict[str, str | int | float | bool] | None = None,
) -> CoworkToolResult:
    _require_feishu_account(context, account_id)
    request = ConnectorRequestArgs(account_id=account_id, path=path, query=query or {})
    return CoworkToolResult(
        output=await _connector_request(context, request, method="GET", body=None)
    )


async def _feishu_write(
    context: CoworkToolContext,
    *,
    account_id: UUID,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    effect_ref: str,
) -> CoworkToolResult:
    _require_feishu_account(context, account_id)
    request = ConnectorRequestArgs(account_id=account_id, path=path)
    output = await _connector_request(context, request, method=method, body=body)
    return CoworkToolResult(output=output, effect_ref=effect_ref)


async def _calendar_list_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuCalendarEventsArgs.model_validate(raw.model_dump())
    path, query = _feishu_calendar_list_request(args)
    return await _feishu_read(context, account_id=args.account_id, path=path, query=query)


async def _calendar_action_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuCalendarEventActionArgs.model_validate(raw.model_dump())
    method, path, body = _feishu_calendar_action_request(args)
    return await _feishu_write(
        context,
        account_id=args.account_id,
        method=method,
        path=path,
        body=body,
        effect_ref=f"feishu-calendar:{args.account_id}:{args.action}:{args.event_id or 'new'}",
    )


async def _base_list_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuBaseRecordsArgs.model_validate(raw.model_dump())
    path, query = _feishu_base_list_request(args)
    return await _feishu_read(context, account_id=args.account_id, path=path, query=query)


async def _base_action_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuBaseRecordActionArgs.model_validate(raw.model_dump())
    method, path, body = _feishu_base_action_request(args)
    return await _feishu_write(
        context,
        account_id=args.account_id,
        method=method,
        path=path,
        body=body,
        effect_ref=f"feishu-base:{args.account_id}:{args.action}:{args.record_id or 'new'}",
    )


async def _document_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuDocumentArgs.model_validate(raw.model_dump())
    path = f"/docx/v1/documents/{quote(args.document_id, safe='')}/raw_content"
    return await _feishu_read(
        context, account_id=args.account_id, path=path, query={"lang": args.lang}
    )


async def _drive_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuDriveFilesArgs.model_validate(raw.model_dump())
    path, query = _feishu_drive_list_request(args)
    return await _feishu_read(context, account_id=args.account_id, path=path, query=query)


async def _task_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuTaskArgs.model_validate(raw.model_dump())
    path = f"/task/v2/tasks/{quote(args.task_guid, safe='')}"
    return await _feishu_read(context, account_id=args.account_id, path=path)


async def _task_action_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuTaskActionArgs.model_validate(raw.model_dump())
    method, path, body = _feishu_task_action_request(args)
    return await _feishu_write(
        context,
        account_id=args.account_id,
        method=method,
        path=path,
        body=body,
        effect_ref=f"feishu-task:{args.account_id}:{args.action}:{args.task_guid or 'new'}",
    )


async def _approval_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuApprovalInstanceArgs.model_validate(raw.model_dump())
    path = f"/approval/v4/instances/{quote(args.instance_code, safe='')}"
    return await _feishu_read(context, account_id=args.account_id, path=path)


async def _approval_submit_handler(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = FeishuApprovalSubmitArgs.model_validate(raw.model_dump())
    return await _feishu_write(
        context,
        account_id=args.account_id,
        method="POST",
        path="/approval/v4/instances",
        body=_feishu_approval_submit_body(args),
        effect_ref=f"feishu-approval:{args.account_id}:submit:{args.uuid or args.approval_code}",
    )


def _register_all(registry: CoworkToolRegistry, specs: tuple[CoworkToolSpec, ...]) -> None:
    for spec in specs:
        registry.register_deferred(spec, group="连接器")


def _feishu_calendar_specs() -> tuple[CoworkToolSpec, ...]:
    return (
        CoworkToolSpec(
            name="feishu_calendar_events",
            description="读取飞书日历指定时间范围内的日程；固定调用 calendar/v4。",
            args_model=FeishuCalendarEventsArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_calendar_list_handler,
            search_aliases=("飞书日历", "日程", "calendar", "会议", "agenda"),
        ),
        CoworkToolSpec(
            name="feishu_calendar_event_action",
            description="创建、更新或删除飞书日程；外部写操作需要用户批准。",
            args_model=FeishuCalendarEventActionArgs,
            capability_resolver=_external_mutation_capability,
            risk="external",
            effect="external",
            parallel_safe=False,
            handler=_calendar_action_handler,
            approval_required=True,
            approval_target_fields=("account_id", "action", "calendar_id", "event_id"),
            search_aliases=("飞书日历", "创建日程", "修改日程", "删除日程"),
        ),
    )


def _feishu_content_specs() -> tuple[CoworkToolSpec, ...]:
    return (
        CoworkToolSpec(
            name="feishu_base_records",
            description="读取飞书多维表格记录，支持视图、筛选、排序、字段裁剪和分页。",
            args_model=FeishuBaseRecordsArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_base_list_handler,
            search_aliases=("飞书多维表格", "多维表", "bitable", "base", "记录"),
        ),
        CoworkToolSpec(
            name="feishu_base_record_action",
            description="创建、更新或删除一条飞书多维表格记录；外部写操作需要用户批准。",
            args_model=FeishuBaseRecordActionArgs,
            capability_resolver=_external_mutation_capability,
            risk="external",
            effect="external",
            parallel_safe=False,
            handler=_base_action_handler,
            approval_required=True,
            approval_target_fields=("account_id", "action", "app_token", "table_id", "record_id"),
            search_aliases=("飞书多维表格", "写入多维表", "bitable", "base record"),
        ),
        CoworkToolSpec(
            name="feishu_document_read",
            description="读取飞书新版文档的纯文本正文；固定调用 docx/v1，不需要拼 API path。",
            args_model=FeishuDocumentArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_document_handler,
            search_aliases=("飞书文档", "云文档", "docx", "document"),
        ),
        CoworkToolSpec(
            name="feishu_drive_files",
            description="列出飞书云盘或指定文件夹中的文件，支持分页与排序。",
            args_model=FeishuDriveFilesArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_drive_handler,
            search_aliases=("飞书云盘", "云空间", "drive", "文件夹"),
        ),
    )


def _feishu_workflow_specs() -> tuple[CoworkToolSpec, ...]:
    return (
        CoworkToolSpec(
            name="feishu_task_read",
            description="按 task_guid 读取一条飞书任务。",
            args_model=FeishuTaskArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_task_handler,
            search_aliases=("飞书任务", "待办", "task"),
        ),
        CoworkToolSpec(
            name="feishu_task_action",
            description="创建、更新或删除飞书任务；外部写操作需要用户批准。",
            args_model=FeishuTaskActionArgs,
            capability_resolver=_external_mutation_capability,
            risk="external",
            effect="external",
            parallel_safe=False,
            handler=_task_action_handler,
            approval_required=True,
            approval_target_fields=("account_id", "action", "task_guid"),
            search_aliases=("飞书任务", "创建待办", "更新任务", "task"),
        ),
        CoworkToolSpec(
            name="feishu_approval_instance",
            description="按 instance_code 读取飞书审批实例详情。",
            args_model=FeishuApprovalInstanceArgs,
            capability="external.read",
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_approval_handler,
            search_aliases=("飞书审批", "审批单", "approval"),
        ),
        CoworkToolSpec(
            name="feishu_approval_submit",
            description="发起一条飞书审批实例；表单用官方 form JSON，提交前需要用户批准。",
            args_model=FeishuApprovalSubmitArgs,
            capability="external.write",
            risk="external",
            effect="external",
            parallel_safe=False,
            handler=_approval_submit_handler,
            approval_required=True,
            approval_target_fields=("account_id", "approval_code", "user_id"),
            search_aliases=("飞书审批", "发起审批", "submit approval"),
        ),
    )


def register_feishu_tools(registry: CoworkToolRegistry) -> None:
    """由 Feishu Descriptor 装配；通用注册函数不认识任何飞书工具名。"""

    _register_all(
        registry,
        (*_feishu_calendar_specs(), *_feishu_content_specs(), *_feishu_workflow_specs()),
    )


def _load_registrar(reference: str) -> Callable[[CoworkToolRegistry], None]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"无效的 Connector tool registrar: {reference}")
    candidate = getattr(importlib.import_module(module_name), attribute)
    if not callable(candidate):
        raise TypeError(f"Connector tool registrar 不可调用: {reference}")
    return cast("Callable[[CoworkToolRegistry], None]", candidate)


def register_connector_tools(registry: CoworkToolRegistry) -> None:
    """注册通用连接器工具，再按 Descriptor 装配各平台的专用域工具。"""

    platform_aliases = tuple(
        dict.fromkeys(
            (
                "连接器",
                "connector",
                "oauth",
                *(
                    alias
                    for descriptor in list_connector_descriptors()
                    for alias in (descriptor.kind, descriptor.label)
                ),
            )
        )
    )
    _register_all(
        registry,
        (
            CoworkToolSpec(
                name="list_connectors",
                description="列出已配置连接器账户与 Descriptor 声明的能力，不返回密钥。",
                args_model=ListConnectorsArgs,
                capability="external.read",
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=_list_handler,
                search_aliases=(*platform_aliases, "账户", "account", "已连接"),
            ),
            CoworkToolSpec(
                name="read_connector_api",
                description="使用连接器读取固定官方主机上的相对 API path。",
                args_model=ConnectorRequestArgs,
                capability="external.read",
                risk="read",
                effect="none",
                parallel_safe=True,
                handler=_read_handler,
                search_aliases=(*platform_aliases, "读取 API", "read api"),
            ),
            CoworkToolSpec(
                name="act_connector_api",
                description="调用固定官方主机上的写 API；任何调用都必须逐次获得用户批准。",
                args_model=ConnectorActionArgs,
                capability_resolver=_external_mutation_capability,
                risk="external",
                effect="external",
                parallel_safe=False,
                handler=_action_handler,
                approval_required=True,
                approval_target_fields=("account_id", "method", "path"),
                search_aliases=(
                    *platform_aliases,
                    "写入 API",
                    "write api",
                    "创建 issue",
                    "pull request",
                ),
            ),
        ),
    )
    for descriptor in list_connector_descriptors():
        for registrar in descriptor.tool_registrars:
            _load_registrar(registrar)(registry)
    registry.add_system_instructions(
        "连接器凭据不会展示给模型。先调用 list_connectors 获取 account_id；"
        "飞书文档、云盘、日历、多维表、任务与审批优先使用 feishu_* 专用工具；"
        "其他读取用 read_connector_api，外部写入必须等待用户批准。"
    )
