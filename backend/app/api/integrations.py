"""MCP 与本地 Skill 的只读状态接口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.mcp.client import McpClientManager, McpRemoteTool
from app.mcp.config import McpConfigurationError, McpToolPolicy, load_mcp_configuration
from app.skills.catalog import SkillCatalogError, load_skill_catalog

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_owner_identity)],
)


@router.get("/mcp")
def get_mcp_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        configuration = load_mcp_configuration(settings.cowork_mcp_config_path)
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return configuration.public_status()


@router.post("/mcp/{server_name}/probe")
async def probe_mcp_server(
    server_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        configuration = load_mcp_configuration(settings.cowork_mcp_config_path)
        server = configuration.servers.get(server_name)
        if server is None or not server.enabled:
            raise HTTPException(status_code=404, detail="MCP 服务未启用或不存在")
        manager = McpClientManager(
            configuration,
            connect_timeout_s=settings.cowork_mcp_connect_timeout_s,
            call_timeout_s=settings.cowork_mcp_call_timeout_s,
            result_max_chars=settings.cowork_mcp_result_max_chars,
        )
        try:
            tools = await manager.list_tools(server_name)
        finally:
            await manager.aclose()
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"MCP 探测失败: {error}") from error
    # 延迟 import 避免 API 配置查询路径反向依赖整个 Cowork runtime。
    from app.agent.cowork_extensions import mcp_catalog_sha256

    return {
        "server": server_name,
        "catalog_sha256": mcp_catalog_sha256(tools),
        "tools": [_public_tool(tool, server.tools.get(tool.name)) for tool in tools],
    }


def _public_tool(tool: McpRemoteTool, policy: McpToolPolicy | None) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "configured_policy": policy.model_dump(mode="json") if policy is not None else None,
    }


@router.get("/skills")
def get_skills_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        catalog = load_skill_catalog(
            settings.cowork_skills_path,
            max_files=settings.cowork_skill_max_files,
            max_bytes=settings.cowork_skill_max_bytes,
        )
    except SkillCatalogError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "source_path": str(settings.cowork_skills_path),
        "snapshot_sha256": catalog.snapshot_sha256,
        "skills": catalog.summaries(),
        "errors": list(catalog.errors),
    }
