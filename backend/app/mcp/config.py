"""MCP 服务配置加载。

兼容生态常用的顶层 ``mcpServers``，但工具必须逐个声明策略并显式启用。
stdio 服务还必须声明 trusted=true，因为启动服务进程本身已经越过调用级审批边界。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


class McpConfigurationError(ValueError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpToolPolicy(_StrictModel):
    enabled: bool = False
    # 未声明时按有副作用处理；当前运行时只暴露明确只读的工具。
    side_effect: bool = True
    approval: Literal["always", "never"] = "always"
    data_scope: Literal["deny", "corpus_allowed"] = "deny"
    when_to_use: str = Field(default="", max_length=1_000)
    when_not_to_use: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> McpToolPolicy:
        if self.enabled and (not self.when_to_use.strip() or not self.when_not_to_use.strip()):
            raise ValueError("启用 MCP 工具必须填写 when_to_use 与 when_not_to_use")
        if self.enabled and self.side_effect and self.approval != "always":
            raise ValueError("有副作用的 MCP 工具必须逐次审批")
        return self


class McpServerConfig(_StrictModel):
    enabled: bool = False
    trusted: bool = False
    transport: Literal["stdio", "streamable_http", "http"] = "stdio"
    command: str | None = Field(default=None, min_length=1, max_length=4_096)
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(default=None, max_length=4_096)
    url: str | None = Field(default=None, max_length=8_192)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth_connector_id: UUID | None = None
    catalog_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tools: dict[str, McpToolPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_transport(self) -> McpServerConfig:
        if self.transport == "stdio":
            if self.enabled and not self.trusted:
                raise ValueError("启用 stdio MCP 服务必须显式设置 trusted=true")
            if not self.command:
                raise ValueError("stdio MCP 服务必须配置 command")
        elif not self.url:
            raise ValueError("Streamable HTTP MCP 服务必须配置 url")
        else:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("MCP url 必须是无用户信息和 fragment 的 http/https URL")
            if self.oauth_connector_id is not None and self.headers.get("Authorization"):
                raise ValueError("OAuth connector 与 Authorization header 不能同时配置")
        return self


class McpConfiguration(_StrictModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    source_path: Path | None = None

    def public_status(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path) if self.source_path is not None else None,
            "servers": [
                {
                    "name": name,
                    "enabled": server.enabled,
                    "trusted": server.trusted,
                    "transport": server.transport,
                    "configured_tools": len(server.tools),
                    "eligible_read_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled
                        and not policy.side_effect
                        and policy.data_scope == "corpus_allowed"
                    ),
                    "eligible_action_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled
                        and policy.side_effect
                        and policy.approval == "always"
                        and policy.data_scope == "corpus_allowed"
                    ),
                    "blocked_side_effect_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled
                        and policy.side_effect
                        and policy.approval != "always"
                    ),
                    "blocked_data_scope_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled
                        and not policy.side_effect
                        and policy.data_scope == "deny"
                    ),
                    "catalog_sha256": server.catalog_sha256,
                    "oauth_connector_id": server.oauth_connector_id,
                    "command": server.command,
                    "args": server.args,
                    "cwd": server.cwd,
                    "url": server.url,
                    "env_names": sorted(server.env),
                    "header_names": sorted(server.headers),
                    "tools": {
                        tool_name: policy.model_dump(mode="json")
                        for tool_name, policy in sorted(server.tools.items())
                    },
                }
                for name, server in sorted(self.servers.items())
            ],
        }


def _expand_env(value: object, environment: dict[str, str]) -> object:
    if isinstance(value, str):
        match = _ENV_REF.fullmatch(value)
        if match is None:
            return value
        name = match.group(1)
        if name not in environment:
            raise McpConfigurationError(f"MCP 配置引用了未设置的环境变量 {name}")
        return environment[name]
    if isinstance(value, list):
        return [_expand_env(item, environment) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand_env(item, environment) for key, item in value.items()}
    return value


def load_mcp_configuration(
    path: Path,
    environment: dict[str, str] | None = None,
    *,
    resolve_environment: bool = True,
) -> McpConfiguration:
    if not path.exists():
        return McpConfiguration(source_path=path)
    if not path.is_file() or path.is_symlink():
        raise McpConfigurationError("MCP 配置必须是普通文件")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise McpConfigurationError(f"无法读取 MCP 配置: {error}") from error
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise McpConfigurationError("MCP 配置顶层必须是 object")
    raw_servers = loaded.get("mcpServers", loaded.get("servers", {}))
    _validate_persisted_secrets(raw_servers)
    expanded = (
        _expand_env(raw_servers, dict(os.environ) if environment is None else environment)
        if resolve_environment
        else raw_servers
    )
    try:
        return McpConfiguration.model_validate({"servers": expanded, "source_path": path})
    except ValueError as error:
        raise McpConfigurationError(f"MCP 配置无效: {error}") from error


def save_mcp_configuration(path: Path, configuration: McpConfiguration) -> None:
    """原子保存管理员配置；密钥只能以环境变量引用或 connector id 存在。"""

    target = path.expanduser().resolve()
    if target.exists() and (not target.is_file() or target.is_symlink()):
        raise McpConfigurationError("MCP 配置必须是普通文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mcpServers": {
            name: server.model_dump(mode="json", exclude_none=True)
            for name, server in sorted(configuration.servers.items())
        }
    }
    _validate_persisted_secrets(payload["mcpServers"])
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_name = stream.name
            yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
        temp_name = None
    except OSError as error:
        raise McpConfigurationError(f"无法保存 MCP 配置: {error}") from error
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def _validate_persisted_secrets(raw_servers: object) -> None:
    if not isinstance(raw_servers, dict):
        return
    for server_name, raw_server in raw_servers.items():
        if not isinstance(raw_server, dict):
            continue
        environment = raw_server.get("env")
        if isinstance(environment, dict):
            for env_name, value in environment.items():
                if not isinstance(value, str) or _ENV_REF.fullmatch(value) is None:
                    raise McpConfigurationError(
                        f"MCP 服务 {server_name} 的 env {env_name} 必须使用 ${{ENV_NAME}} 引用"
                    )
        headers = raw_server.get("headers")
        if not isinstance(headers, dict):
            continue
        for header_name, value in headers.items():
            if (
                str(header_name).casefold() in {"authorization", "x-api-key", "api-key"}
                and (not isinstance(value, str) or _ENV_REF.fullmatch(value) is None)
            ):
                raise McpConfigurationError(
                    f"MCP 服务 {server_name} 的敏感 header {header_name} "
                    "必须使用 ${ENV_NAME} 引用"
                )
