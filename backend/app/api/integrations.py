"""MCP 与本地 Skill 的只读状态接口。"""

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.mcp.client import McpClientManager, McpRemoteTool
from app.mcp.config import (
    McpConfigurationError,
    McpServerConfig,
    McpToolPolicy,
    load_mcp_configuration,
    save_mcp_configuration,
)
from app.mcp.credentials import hydrate_mcp_oauth_credentials
from app.schemas.skills import (
    SkillEnableRequest,
    SkillInstallRequest,
    SkillResourceResponse,
    SkillZipImportRequest,
)
from app.security.secret_store import LocalSecretStore
from app.skills.catalog import SkillCatalogError, load_skill_catalog
from app.skills.distillation_store import (
    get_skill_candidate,
    list_skill_candidates,
    set_candidate_status,
)
from app.skills.lifecycle import (
    import_skill_zip,
    install_auto_distilled_skill,
    install_skill,
    list_managed_skills,
    read_skill_resource,
    remove_skill,
    set_skill_enabled,
)

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_owner_identity)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get("/mcp")
def get_mcp_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        configuration = load_mcp_configuration(
            settings.cowork_mcp_config_path, resolve_environment=False
        )
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return configuration.public_status()


@router.post("/mcp/{server_name}/probe")
async def probe_mcp_server(
    server_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: DbSession,
) -> dict[str, Any]:
    try:
        configuration = load_mcp_configuration(settings.cowork_mcp_config_path)
        configuration = await hydrate_mcp_oauth_credentials(
            session,
            configuration,
            LocalSecretStore(settings.secret_store_key_path),
        )
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


@router.put("/mcp/servers/{server_name}")
def put_mcp_server(
    server_name: str,
    request: McpServerConfig,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    _validate_mcp_name(server_name)
    try:
        configuration = load_mcp_configuration(
            settings.cowork_mcp_config_path, resolve_environment=False
        )
        servers = dict(configuration.servers)
        servers[server_name] = request
        updated = configuration.model_copy(update={"servers": servers})
        save_mcp_configuration(settings.cowork_mcp_config_path, updated)
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return updated.public_status()


@router.delete("/mcp/servers/{server_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(
    server_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    _validate_mcp_name(server_name)
    configuration = load_mcp_configuration(
        settings.cowork_mcp_config_path, resolve_environment=False
    )
    if server_name not in configuration.servers:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    servers = dict(configuration.servers)
    del servers[server_name]
    save_mcp_configuration(
        settings.cowork_mcp_config_path,
        configuration.model_copy(update={"servers": servers}),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/mcp/servers/{server_name}/tools/{tool_name}")
def put_mcp_tool_policy(
    server_name: str,
    tool_name: str,
    request: McpToolPolicy,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    _validate_mcp_name(server_name)
    _validate_mcp_name(tool_name)
    configuration = load_mcp_configuration(
        settings.cowork_mcp_config_path, resolve_environment=False
    )
    server = configuration.servers.get(server_name)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    tools = dict(server.tools)
    tools[tool_name] = request
    servers = dict(configuration.servers)
    servers[server_name] = server.model_copy(update={"tools": tools})
    updated = configuration.model_copy(update={"servers": servers})
    save_mcp_configuration(settings.cowork_mcp_config_path, updated)
    return updated.public_status()


@router.post("/mcp/{server_name}/pin")
async def pin_mcp_catalog(
    server_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: DbSession,
) -> dict[str, Any]:
    probe = await probe_mcp_server(server_name, settings, session)
    editable = load_mcp_configuration(settings.cowork_mcp_config_path, resolve_environment=False)
    server = editable.servers.get(server_name)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    servers = dict(editable.servers)
    servers[server_name] = server.model_copy(update={"catalog_sha256": probe["catalog_sha256"]})
    updated = editable.model_copy(update={"servers": servers})
    save_mcp_configuration(settings.cowork_mcp_config_path, updated)
    return {"server": server_name, "catalog_sha256": probe["catalog_sha256"], "pinned": True}


def _validate_mcp_name(name: str) -> None:
    if _MCP_NAME.fullmatch(name) is None:
        raise HTTPException(status_code=422, detail="MCP 名称只允许 1-64 位字母、数字、_、-")


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
        "installed": [
            item.public()
            for item in list_managed_skills(
                settings.cowork_skills_path,
                max_files=settings.cowork_skill_max_files,
                max_bytes=settings.cowork_skill_max_bytes,
            )
        ],
    }


@router.get("/skills/candidates")
async def get_skill_candidates(
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    items = await list_skill_candidates(session)
    return {
        "enabled": settings.skill_distillation_enabled,
        "auto_promotion_enabled": settings.skill_auto_promotion_enabled,
        "min_evidence": settings.skill_promotion_min_evidence,
        "min_confidence": settings.skill_promotion_min_confidence,
        "items": [item.public(include_skill_md=True) for item in items],
    }


@router.post("/skills/candidates/{candidate_id}/promote")
async def promote_skill_candidate(
    candidate_id: UUID,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    candidate = await get_skill_candidate(session, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    if candidate.status == "rejected":
        raise HTTPException(status_code=409, detail="已拒绝的 Skill 候选不能直接晋升")
    try:
        install_auto_distilled_skill(
            settings.cowork_skills_path,
            name=candidate.suggested_name,
            capability_key=candidate.capability_key,
            skill_md=candidate.skill_md,
            max_bytes=settings.cowork_skill_max_bytes,
        )
    except (FileExistsError, OSError, UnicodeError, SkillCatalogError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    updated = await set_candidate_status(
        session,
        candidate_id=candidate.id,
        status="promoted",
        promoted_name=candidate.suggested_name,
    )
    await session.commit()
    return updated.public(include_skill_md=True)


@router.post("/skills/candidates/{candidate_id}/reject")
async def reject_skill_candidate(
    candidate_id: UUID,
    session: DbSession,
) -> dict[str, Any]:
    candidate = await get_skill_candidate(session, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    if candidate.status == "promoted":
        raise HTTPException(status_code=409, detail="已晋升 Skill 请从已安装列表停用或卸载")
    updated = await set_candidate_status(
        session,
        candidate_id=candidate.id,
        status="rejected",
        review_reason="用户已拒绝",
    )
    await session.commit()
    return updated.public(include_skill_md=True)


@router.put("/skills/{skill_name}")
def put_skill(
    skill_name: str,
    request: SkillInstallRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        result = install_skill(
            settings.cowork_skills_path,
            name=skill_name,
            skill_md=request.skill_md,
            enabled=request.enabled,
            max_bytes=settings.cowork_skill_max_bytes,
            replace=request.replace,
        )
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, UnicodeError, SkillCatalogError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.public()


@router.patch("/skills/{skill_name}/enabled")
def patch_skill_enabled(
    skill_name: str,
    request: SkillEnableRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        return set_skill_enabled(
            settings.cowork_skills_path,
            name=skill_name,
            enabled=request.enabled,
            max_bytes=settings.cowork_skill_max_bytes,
        ).public()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (OSError, SkillCatalogError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/skills/{skill_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_name: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    try:
        remove_skill(settings.cowork_skills_path, name=skill_name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (OSError, SkillCatalogError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/skills/import")
def post_skill_import(
    request: SkillZipImportRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        return import_skill_zip(
            settings.cowork_skills_path,
            archive_base64=request.archive_base64,
            enabled=request.enabled,
            max_bytes=settings.cowork_skill_max_bytes,
        ).public()
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, UnicodeError, SkillCatalogError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/skills/{skill_name}/resources/{resource_path:path}", response_model=SkillResourceResponse
)
def get_skill_resource(
    skill_name: str,
    resource_path: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SkillResourceResponse:
    try:
        content, resource = read_skill_resource(
            settings.cowork_skills_path,
            name=skill_name,
            resource=resource_path,
            max_bytes=settings.cowork_skill_max_bytes,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Skill resource 不存在") from error
    except (OSError, UnicodeError, SkillCatalogError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return SkillResourceResponse(name=skill_name, resource=resource, content=content)
