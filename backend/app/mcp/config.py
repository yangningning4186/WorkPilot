"""MCP 服务配置加载。

兼容生态常用的顶层 ``mcpServers``，但工具必须逐个声明策略并显式启用。
stdio 服务还必须声明 trusted=true，因为启动服务进程本身已经越过调用级审批边界。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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
    data_scope: Literal["deny", "corpus_allowed"] = "deny"
    when_to_use: str = Field(default="", max_length=1_000)
    when_not_to_use: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> McpToolPolicy:
        if self.enabled and (not self.when_to_use.strip() or not self.when_not_to_use.strip()):
            raise ValueError("启用 MCP 工具必须填写 when_to_use 与 when_not_to_use")
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
                    "blocked_side_effect_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled and policy.side_effect
                    ),
                    "blocked_data_scope_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled
                        and not policy.side_effect
                        and policy.data_scope == "deny"
                    ),
                    "catalog_sha256": server.catalog_sha256,
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
    path: Path, environment: dict[str, str] | None = None
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
    expanded = _expand_env(raw_servers, dict(os.environ) if environment is None else environment)
    try:
        return McpConfiguration.model_validate({"servers": expanded, "source_path": path})
    except ValueError as error:
        raise McpConfigurationError(f"MCP 配置无效: {error}") from error
