from pathlib import Path
from typing import Any

import pytest

from app.agent.cowork_extensions import mcp_catalog_sha256, register_mcp_tools
from app.agent.cowork_tools import build_default_cowork_registry
from app.mcp.client import McpRemoteTool
from app.mcp.config import (
    McpConfiguration,
    McpConfigurationError,
    McpServerConfig,
    McpToolPolicy,
    load_mcp_configuration,
)


def test_mcp_config_expands_exact_environment_references(tmp_path: Path) -> None:
    path = tmp_path / "mcp.yaml"
    path.write_text(
        """mcpServers:
  docs:
    enabled: true
    trusted: true
    transport: stdio
    command: ${MCP_COMMAND}
    env:
      ACCESS_TOKEN: ${MCP_TOKEN}
    tools:
      search:
        enabled: true
        side_effect: false
        when_to_use: 查公开文档
        when_not_to_use: 输入包含私密资料
""",
        encoding="utf-8",
    )

    configuration = load_mcp_configuration(
        path, {"MCP_COMMAND": "/usr/bin/example", "MCP_TOKEN": "secret"}
    )

    assert configuration.servers["docs"].command == "/usr/bin/example"
    assert configuration.servers["docs"].env["ACCESS_TOKEN"] == "secret"
    assert "secret" not in str(configuration.public_status())


def test_mcp_stdio_requires_explicit_process_trust(tmp_path: Path) -> None:
    path = tmp_path / "mcp.yaml"
    path.write_text(
        """mcpServers:
  unsafe:
    enabled: true
    transport: stdio
    command: example
""",
        encoding="utf-8",
    )

    with pytest.raises(McpConfigurationError, match="trusted=true"):
        load_mcp_configuration(path, {})


def test_mcp_config_rejects_persisted_literal_environment_secret(tmp_path: Path) -> None:
    path = tmp_path / "mcp.yaml"
    path.write_text(
        """mcpServers:
  unsafe:
    enabled: false
    trusted: true
    transport: stdio
    command: example
    env:
      ACCESS_TOKEN: literal-secret
""",
        encoding="utf-8",
    )
    with pytest.raises(McpConfigurationError, match=r"必须使用.*引用"):
        load_mcp_configuration(path, {})


class _FakeManager:
    def __init__(self, configuration: McpConfiguration, tools: list[McpRemoteTool]) -> None:
        self.configuration = configuration
        self.tools = tools

    async def list_tools(self, _: str) -> list[McpRemoteTool]:
        return self.tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {"server": server_name, "tool": tool_name, "arguments": arguments}


@pytest.mark.asyncio
async def test_mcp_registry_only_exposes_reviewed_read_tools() -> None:
    tools = [
        McpRemoteTool(
            name="search",
            description="Search docs",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        McpRemoteTool(name="publish", description="Publish", input_schema={"type": "object"}),
        McpRemoteTool(name="echo", description="Echo", input_schema={"type": "object"}),
    ]
    configuration = McpConfiguration(
        servers={
            "docs": McpServerConfig(
                enabled=True,
                transport="streamable_http",
                url="https://mcp.example.test",
                catalog_sha256=mcp_catalog_sha256(tools),
                tools={
                    "search": McpToolPolicy(
                        enabled=True,
                        side_effect=False,
                        data_scope="corpus_allowed",
                        when_to_use="查公开文档",
                        when_not_to_use="包含敏感资料",
                    ),
                    "publish": McpToolPolicy(
                        enabled=True,
                        side_effect=True,
                        data_scope="corpus_allowed",
                        when_to_use="发布内容",
                        when_not_to_use="未得到批准",
                    ),
                    "echo": McpToolPolicy(
                        enabled=True,
                        side_effect=False,
                        data_scope="deny",
                        when_to_use="回显非敏感内容",
                        when_not_to_use="包含工作区内容",
                    ),
                },
            )
        }
    )
    registry = build_default_cowork_registry()

    statuses = await register_mcp_tools(registry, _FakeManager(configuration, tools))  # type: ignore[arg-type]

    assert registry.get("mcp__docs__search").capability == "network.read"
    publish = registry.get("mcp__docs__publish")
    assert publish.capability == "external.action"
    assert publish.effect == "external"
    assert publish.approval_required is True
    assert {"name": "echo", "reason": "data_scope_denied"} in statuses["docs"][
        "blocked_tools"
    ]


@pytest.mark.asyncio
async def test_mcp_catalog_drift_fails_closed() -> None:
    tools = [McpRemoteTool(name="search", description="changed", input_schema={"type": "object"})]
    configuration = McpConfiguration(
        servers={
            "docs": McpServerConfig(
                enabled=True,
                transport="streamable_http",
                url="https://mcp.example.test",
                catalog_sha256="0" * 64,
            )
        }
    )
    registry = build_default_cowork_registry()

    statuses = await register_mcp_tools(registry, _FakeManager(configuration, tools))  # type: ignore[arg-type]

    assert statuses["docs"]["status"] == "catalog_drift"
    assert not any(item["name"].startswith("mcp__") for item in registry.catalog())
