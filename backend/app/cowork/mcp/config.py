"""MCP 服务配置加载。

兼容生态常用的顶层 ``mcpServers``，但工具必须逐个声明策略并显式启用。
stdio 服务还必须声明 trusted=true，因为启动服务进程本身已经越过调用级审批边界。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    model_validator,
)

_ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SENSITIVE_LONG_ARGUMENTS = frozenset(
    {
        "api-key",
        "apikey",
        "access-token",
        "access-key",
        "access-key-id",
        "auth",
        "auth-token",
        "authorization",
        "bearer",
        "bearer-token",
        "token",
        "secret",
        "secret-access-key",
        "session-token",
        "client-secret",
        "connection-string",
        "database-url",
        "dsn",
        "key",
        "pass",
        "passphrase",
        "password",
        "passwd",
        "private-key",
        "credential",
        "credentials",
        "secret-key",
        "user",
        "username",
    }
)
_SENSITIVE_SHORT_ARGUMENTS = frozenset({"-p", "-u"})
_HEADER_ARGUMENTS = frozenset({"--header", "-H"})
_ENV_ARGUMENTS = frozenset({"--env", "-e"})
_SENSITIVE_NAME_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "authenticate",
        "authentication",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "signature",
        "token",
    }
)
_SENSITIVE_COMPACT_SUFFIXES = (
    "authorization",
    "authtoken",
    "accesstoken",
    "apikey",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
    "signature",
    "token",
)
_CREDENTIAL_HEADER_VALUE = re.compile(
    r"^\s*(?:bearer|basic|digest|hmac|token|api[-_ ]?key|signature|aws4-hmac-sha256)\s+",
    re.IGNORECASE,
)


class McpConfigurationError(ValueError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


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
    def validate_transport(self, info: ValidationInfo) -> McpServerConfig:
        runtime_resolved = bool(info.context and info.context.get("runtime_resolved") is True)
        if self.transport == "stdio":
            if self.enabled and not self.trusted:
                raise ValueError("启用 stdio MCP 服务必须显式设置 trusted=true")
            if not self.command:
                raise ValueError("stdio MCP 服务必须配置 command")
            if not runtime_resolved:
                _validate_persisted_stdio_args(self.args)
        elif not self.url:
            raise ValueError("Streamable HTTP MCP 服务必须配置 url")
        elif _ENV_REF.fullmatch(self.url) is None and not _is_safe_http_url(self.url):
            raise ValueError("MCP url 必须是无用户信息、query 和 fragment 的 http/https URL")
        if self.oauth_connector_id is not None and any(
            name.casefold() == "authorization" for name in self.headers
        ):
            raise ValueError("OAuth connector 与 Authorization header 不能同时配置")
        _validate_header_shape(self.headers)
        if not runtime_resolved:
            _validate_persisted_headers(self.headers)
            _validate_persisted_environment(self.env)
        return self


class McpConfiguration(_StrictModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    source_path: Path | None = None
    _runtime_credentials_resolved: bool = PrivateAttr(default=False)

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
                        if policy.enabled and policy.side_effect and policy.approval != "always"
                    ),
                    "blocked_data_scope_tools": sum(
                        1
                        for policy in server.tools.values()
                        if policy.enabled and not policy.side_effect and policy.data_scope == "deny"
                    ),
                    "catalog_sha256": server.catalog_sha256,
                    "oauth_connector_id": server.oauth_connector_id,
                    "command": server.command,
                    "args": _public_stdio_args(server.args),
                    "cwd": server.cwd,
                    "url": _public_mcp_url(server.url),
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


def validate_mcp_runtime_configuration(
    configuration: McpConfiguration,
    *,
    require_resolved_credentials: bool = False,
) -> None:
    """Recheck I/O-critical fields after trusted model copies and environment expansion."""

    for server in configuration.servers.values():
        try:
            _validate_header_shape(server.headers)
        except ValueError as error:
            raise McpConfigurationError(str(error)) from None
        if server.transport == "stdio":
            if server.enabled and not server.trusted:
                raise McpConfigurationError("启用 stdio MCP 服务必须显式设置 trusted=true")
            if not server.command:
                raise McpConfigurationError("stdio MCP 服务必须配置 command")
            if require_resolved_credentials and _stdio_args_require_resolution(server.args):
                if not configuration._runtime_credentials_resolved or any(
                    "${" in argument for argument in server.args
                ):
                    raise McpConfigurationError("MCP runtime credential 参数缺少可信解析来源")
            if require_resolved_credentials and server.env:
                if not configuration._runtime_credentials_resolved or any(
                    _ENV_REF.fullmatch(value) is not None for value in server.env.values()
                ):
                    raise McpConfigurationError("MCP runtime env 缺少可信解析来源")
            continue
        if server.url is None or _ENV_REF.fullmatch(server.url) is not None:
            raise McpConfigurationError("MCP HTTP runtime url 无效")
        if not _is_safe_http_url(server.url):
            raise McpConfigurationError("MCP HTTP runtime url 无效")
        if require_resolved_credentials:
            sensitive_values = [
                value
                for name, value in server.headers.items()
                if _header_contains_credentials(name, value)
            ]
            referenced_values = [
                value for value in server.headers.values() if _ENV_REF.fullmatch(value) is not None
            ]
            if (sensitive_values or referenced_values) and (
                not configuration._runtime_credentials_resolved or referenced_values
            ):
                raise McpConfigurationError("MCP runtime credential header 缺少可信解析来源")


def _expand_env(value: object, environment: dict[str, str]) -> object:
    if isinstance(value, str):
        match = _ENV_REF.fullmatch(value)
        if match is not None:
            return _environment_value(match.group(1), environment)
        embedded = _embedded_argument_env_reference(value)
        if embedded is not None:
            prefix, name = embedded
            return prefix + _environment_value(name, environment)
        return value
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
    except (OSError, UnicodeError, yaml.YAMLError):
        # Parser diagnostics can include the offending scalar, which may itself be a credential.
        raise McpConfigurationError("无法读取 MCP 配置：文件不可读或 YAML 语法无效") from None
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
        configuration = McpConfiguration.model_validate(
            {"servers": expanded, "source_path": path},
            context={"runtime_resolved": resolve_environment},
        )
        configuration._runtime_credentials_resolved = resolve_environment
        if resolve_environment:
            validate_mcp_runtime_configuration(
                configuration,
                require_resolved_credentials=True,
            )
        return configuration
    except ValidationError as error:
        raise McpConfigurationError(_safe_configuration_error(error)) from None


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
    try:
        _validate_persisted_secrets_inner(raw_servers)
    except McpConfigurationError:
        raise
    except ValueError as error:
        raise McpConfigurationError(str(error)) from None


def _validate_persisted_secrets_inner(raw_servers: object) -> None:
    if not isinstance(raw_servers, dict):
        return
    for _server_name, raw_server in raw_servers.items():
        if not isinstance(raw_server, dict):
            continue
        raw_url = raw_server.get("url")
        if (
            isinstance(raw_url, str)
            and _ENV_REF.fullmatch(raw_url) is None
            and not _is_safe_http_url(raw_url)
        ):
            raise ValueError("MCP url 不允许用户信息、query 或 fragment")
        environment = raw_server.get("env")
        if isinstance(environment, dict):
            _validate_persisted_environment(environment)
        args = raw_server.get("args")
        if isinstance(args, list):
            _validate_persisted_stdio_args(args)
        headers = raw_server.get("headers")
        if isinstance(headers, dict):
            _validate_header_shape(headers)
            _validate_persisted_headers(headers)


def _safe_configuration_error(error: ValidationError) -> str:
    messages = [
        str(item.get("msg") or "字段无效")
        for item in error.errors(include_url=False, include_input=False)
    ]
    unique = list(dict.fromkeys(messages))[:3]
    return "MCP 配置无效: " + ("; ".join(unique) if unique else "字段或安全约束不满足")


def _environment_value(name: str, environment: dict[str, str]) -> str:
    if name not in environment:
        raise McpConfigurationError(f"MCP 配置引用了未设置的环境变量 {name}")
    return environment[name]


def _embedded_argument_env_reference(value: str) -> tuple[str, str] | None:
    flag, attached = _argument_flag_and_attached_value(value)
    if attached is not None:
        attached_match = _ENV_REF.fullmatch(attached)
        if attached_match is not None and (
            _is_direct_credential_flag(flag) or flag in _HEADER_ARGUMENTS | _ENV_ARGUMENTS
        ):
            return value[: -len(attached)], attached_match.group(1)
    prefix, separator, raw_reference = value.partition("=")
    if not separator:
        return None
    match = _ENV_REF.fullmatch(raw_reference)
    if match is not None and (_is_direct_credential_flag(prefix) or prefix in _ENV_ARGUMENTS):
        return f"{prefix}=", match.group(1)
    if prefix in _ENV_ARGUMENTS:
        env_name, assignment, assignment_reference = raw_reference.partition("=")
        nested = _ENV_REF.fullmatch(assignment_reference)
        if assignment and _is_sensitive_name(env_name) and nested is not None:
            return f"{prefix}={env_name}=", nested.group(1)
    env_name, assignment, assignment_reference = value.partition("=")
    if assignment and _is_sensitive_name(env_name):
        nested = _ENV_REF.fullmatch(assignment_reference)
        if nested is not None:
            return f"{env_name}=", nested.group(1)
    return None


def _validate_header_shape(headers: object) -> None:
    if not isinstance(headers, dict):
        raise ValueError("MCP headers 必须是 object")
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or _HEADER_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or len(value) > 8_192
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("MCP header 名称或值无效")


def _validate_persisted_headers(headers: Mapping[Any, Any]) -> None:
    for name, value in headers.items():
        if _header_contains_credentials(str(name), value) and (
            not isinstance(value, str) or _ENV_REF.fullmatch(value) is None
        ):
            raise ValueError("MCP 敏感 header 必须使用 ${ENV_NAME} 引用")


def _validate_persisted_environment(environment: Mapping[Any, Any]) -> None:
    for value in environment.values():
        if not isinstance(value, str) or _ENV_REF.fullmatch(value) is None:
            raise ValueError("MCP env 值必须使用 ${ENV_NAME} 引用")


def _validate_persisted_stdio_args(args: Sequence[object]) -> None:
    values = [value for value in args if isinstance(value, str)]
    if len(values) != len(args):
        raise ValueError("MCP stdio args 必须是字符串列表")
    if any(len(value) > 8_192 for value in values):
        raise ValueError("MCP stdio 参数过长")
    index = 0
    while index < len(values):
        argument = values[index]
        if _raw_sensitive_assignment(argument):
            _, _, raw_value = argument.partition("=")
            if _ENV_REF.fullmatch(raw_value) is None:
                raise ValueError("MCP credential env 参数必须使用 ${ENV_NAME} 引用")
        flag, attached = _argument_flag_and_attached_value(argument)
        next_value = values[index + 1] if attached is None and index + 1 < len(values) else None
        candidate = attached if attached is not None else next_value
        consumes_next = attached is None and next_value is not None
        if _is_direct_credential_flag(flag):
            if candidate is not None and _ENV_REF.fullmatch(candidate) is None:
                raise ValueError("MCP credential 参数必须使用 ${ENV_NAME} 引用")
        elif flag in _HEADER_ARGUMENTS and candidate is not None:
            if _ENV_REF.fullmatch(candidate) is None and _header_argument_is_sensitive(candidate):
                raise ValueError("MCP credential header 参数必须使用 ${ENV_NAME} 引用")
        elif flag in _ENV_ARGUMENTS and candidate is not None:
            if not _safe_env_argument_reference(candidate):
                env_name = candidate.partition("=")[0]
                if _is_sensitive_name(env_name):
                    raise ValueError("MCP credential env 参数必须使用 ${ENV_NAME} 引用")
        if consumes_next and flag in (
            _SENSITIVE_SHORT_ARGUMENTS | _HEADER_ARGUMENTS | _ENV_ARGUMENTS
        ) | {f"--{name}" for name in _SENSITIVE_LONG_ARGUMENTS}:
            index += 1
        index += 1


def _argument_flag_and_attached_value(argument: str) -> tuple[str, str | None]:
    if argument.startswith("--"):
        flag, separator, value = argument.partition("=")
        return flag, value if separator else None
    for flag in (*_SENSITIVE_SHORT_ARGUMENTS, *_HEADER_ARGUMENTS, *_ENV_ARGUMENTS):
        if argument == flag:
            return flag, None
        if argument.startswith(flag) and len(argument) > len(flag):
            return flag, argument[len(flag) :].removeprefix("=")
    return argument, None


def _is_direct_credential_flag(flag: str) -> bool:
    if flag in _SENSITIVE_SHORT_ARGUMENTS:
        return True
    if not flag.startswith("--"):
        return False
    normalized = flag[2:].casefold().replace("_", "-")
    return normalized in _SENSITIVE_LONG_ARGUMENTS


def _safe_env_argument_reference(value: str) -> bool:
    if _ENV_REF.fullmatch(value) is not None:
        return True
    env_name, separator, raw_reference = value.partition("=")
    return bool(
        separator and _is_sensitive_name(env_name) and _ENV_REF.fullmatch(raw_reference) is not None
    )


def _header_argument_is_sensitive(value: str) -> bool:
    name, separator, raw_value = value.partition(":")
    return bool(separator and _header_contains_credentials(name.strip(), raw_value.strip()))


def _header_contains_credentials(name: str, value: object) -> bool:
    return _is_sensitive_header_name(name) or (
        isinstance(value, str) and _CREDENTIAL_HEADER_VALUE.match(value) is not None
    )


def _is_sensitive_header_name(name: str) -> bool:
    normalized = name.casefold().replace("_", "-")
    if normalized in {"authorization", "proxy-authorization", "cookie"}:
        return True
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    compact = "".join(character for character in normalized if character.isalnum())
    return (
        bool(parts & _SENSITIVE_NAME_PARTS)
        or "apikey" in parts
        or ("api" in parts and "key" in parts)
        or ("access" in parts and "key" in parts)
        or compact.endswith(_SENSITIVE_COMPACT_SUFFIXES)
    )


def _is_sensitive_name(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    compact = "".join(character for character in normalized if character.isalnum())
    return (
        bool(parts & _SENSITIVE_NAME_PARTS)
        or "apikey" in parts
        or ("api" in parts and "key" in parts)
        or ("access" in parts and "key" in parts)
        or compact.endswith(_SENSITIVE_COMPACT_SUFFIXES)
    )


def _raw_sensitive_assignment(value: str) -> bool:
    if value.startswith("-"):
        return False
    name, separator, _ = value.partition("=")
    return bool(separator and _is_sensitive_name(name))


def _is_safe_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and "?" not in value
            and "#" not in value
            and not any(character.isspace() or ord(character) < 0x20 for character in value)
        )
    except ValueError:
        return False


def _public_mcp_url(value: str | None) -> str | None:
    if value is None:
        return None
    if _ENV_REF.fullmatch(value) is None and not _is_safe_http_url(value):
        return None
    return value


def _public_stdio_args(args: list[str]) -> list[str]:
    public = list(args)
    index = 0
    while index < len(public):
        if _raw_sensitive_assignment(public[index]):
            name, _, _ = public[index].partition("=")
            public[index] = f"{name}=[REDACTED]"
            index += 1
            continue
        flag, attached = _argument_flag_and_attached_value(public[index])
        candidate = (
            attached
            if attached is not None
            else (public[index + 1] if index + 1 < len(public) else None)
        )
        sensitive = (
            _is_direct_credential_flag(flag)
            or (
                flag in _HEADER_ARGUMENTS
                and candidate is not None
                and _header_argument_is_sensitive(candidate)
            )
            or (
                flag in _ENV_ARGUMENTS
                and candidate is not None
                and _is_sensitive_name(candidate.partition("=")[0])
            )
        )
        if sensitive and candidate is not None:
            if attached is None:
                public[index + 1] = "[REDACTED]"
                index += 1
            else:
                public[index] = f"{flag}=[REDACTED]"
        index += 1
    return public


def _stdio_args_require_resolution(args: list[str]) -> bool:
    if _public_stdio_args(args) != args:
        return True
    index = 0
    while index < len(args):
        flag, attached = _argument_flag_and_attached_value(args[index])
        candidate = (
            attached
            if attached is not None
            else (args[index + 1] if index + 1 < len(args) else None)
        )
        if (
            flag in _HEADER_ARGUMENTS | _ENV_ARGUMENTS
            and candidate is not None
            and "${" in candidate
        ):
            return True
        index += 1
    return False


def mcp_runtime_secret_values(server: McpServerConfig) -> tuple[str, ...]:
    """Return resolved credential payloads that diagnostics must never expose."""

    values = [*server.env.values(), *server.headers.values()]
    public_args = _public_stdio_args(server.args)
    for original, public in zip(server.args, public_args, strict=True):
        if original == public:
            continue
        flag, attached = _argument_flag_and_attached_value(original)
        if public == "[REDACTED]":
            candidate = original
        elif attached is not None:
            candidate = attached
        elif _raw_sensitive_assignment(original):
            _, _, candidate = original.partition("=")
        else:
            candidate = original.removeprefix(flag).removeprefix("=")
        values.extend(_credential_payloads(candidate))
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


def _credential_payloads(value: str) -> tuple[str, ...]:
    candidates = {value.strip()}
    for separator in (":", "="):
        _, present, suffix = value.partition(separator)
        if present and suffix.strip():
            candidates.add(suffix.strip())
    for candidate in tuple(candidates):
        scheme = _CREDENTIAL_HEADER_VALUE.match(candidate)
        if scheme is not None and candidate[scheme.end() :].strip():
            candidates.add(candidate[scheme.end() :].strip())
    return tuple(candidates)
