"""MCP 与本地 Skill 的状态接口。"""

import json
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.mcp.client import McpClientManager, McpRemoteTool
from app.cowork.mcp.config import (
    McpConfigurationError,
    McpServerConfig,
    McpToolPolicy,
    load_mcp_configuration,
    save_mcp_configuration,
)
from app.cowork.mcp.credentials import hydrate_mcp_oauth_credentials
from app.cowork.permissions import list_session_roots
from app.cowork.skills.candidate_store import (
    SkillCandidateStoreError,
    get_skill_candidate,
    list_skill_candidates,
    set_candidate_status,
)
from app.cowork.skills.catalog import (
    BUILTIN_SKILLS_ROOT,
    SkillCatalogError,
    load_skill_catalog,
)
from app.cowork.skills.lifecycle import (
    AUTO_DISTILLED_PROVENANCE_PURPOSE,
    import_skill_zip,
    install_auto_distilled_skill,
    install_skill,
    list_managed_skills,
    read_skill_resource,
    remove_skill,
    set_skill_enabled,
)
from app.cowork_contracts import ConversationBusyError, ConversationNotFoundError
from app.cowork_store.routing import cowork_store
from app.schemas.skills import (
    SkillEnableRequest,
    SkillInstallRequest,
    SkillResourceResponse,
    SkillSessionMuteRequest,
    SkillZipImportRequest,
)
from app.security.secret_store import LocalSecretStore

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_owner_identity)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SKILL_SESSION_NAME = re.compile(r"^[a-z0-9_-]{1,128}$")
_MCP_SERVER_BODY_MAX_BYTES = 256 * 1024
_INVALID_MCP_SERVER_DETAIL = "MCP 服务配置无效"


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
        configuration = hydrate_mcp_oauth_credentials(
            settings,
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
            try:
                tools = await manager.list_tools(server_name)
            except Exception:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "MCP 探测失败",
                        "health": manager.health_status(server_name),
                    },
                ) from None
            health = manager.health_status(server_name)
        finally:
            await manager.aclose()
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception:
        # SDK/transport 异常可能包含 URL、header 或子进程文本，不能原样进入 HTTP/log。
        raise HTTPException(status_code=502, detail="MCP 探测失败") from None
    # 延迟 import 避免 API 配置查询路径反向依赖整个 Cowork runtime。
    from app.cowork.extensions import mcp_catalog_sha256

    return {
        "server": server_name,
        "catalog_sha256": mcp_catalog_sha256(tools),
        "health": health,
        "tools": [_public_tool(tool, server.tools.get(tool.name)) for tool in tools],
    }


@router.put("/mcp/servers/{server_name}")
async def put_mcp_server(
    server_name: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    _validate_mcp_name(server_name)
    server_config = await _parse_mcp_server_request(request)
    try:
        configuration = load_mcp_configuration(
            settings.cowork_mcp_config_path, resolve_environment=False
        )
        servers = dict(configuration.servers)
        servers[server_name] = server_config
        updated = configuration.model_copy(update={"servers": servers})
        save_mcp_configuration(settings.cowork_mcp_config_path, updated)
    except McpConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return updated.public_status()


async def _parse_mcp_server_request(request: Request) -> McpServerConfig:
    """Parse a secret-bearing config without letting FastAPI echo rejected input.

    FastAPI's default request-validation response includes the offending ``input``.  MCP
    configs can carry credentials when a user makes a mistake, so this endpoint deliberately
    validates a bounded raw body and exposes only a fixed diagnostic.
    """

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            raise HTTPException(status_code=422, detail=_INVALID_MCP_SERVER_DETAIL) from None
        if declared_size < 0:
            raise HTTPException(status_code=422, detail=_INVALID_MCP_SERVER_DETAIL)
        if declared_size > _MCP_SERVER_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail="MCP 服务配置请求过大")

    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MCP_SERVER_BODY_MAX_BYTES:
                raise HTTPException(status_code=413, detail="MCP 服务配置请求过大")
            body.extend(chunk)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail=_INVALID_MCP_SERVER_DETAIL) from None

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("MCP config must be an object")
        return McpServerConfig.model_validate(payload)
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(status_code=422, detail=_INVALID_MCP_SERVER_DETAIL) from None


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
async def get_skills_status(
    settings: Annotated[Settings, Depends(get_settings)],
    session: DbSession,
    conversation_id: UUID | None = None,
) -> dict[str, Any]:
    muted_names: frozenset[str] = frozenset()
    if conversation_id is not None:
        try:
            muted_names = await cowork_store().list_conversation_skill_mutes(
                conversation_id=conversation_id
            )
        except ConversationNotFoundError as error:
            raise HTTPException(status_code=404, detail="会话不存在") from error
    roots = (
        []
        if conversation_id is None
        else await list_session_roots(session, conversation_id=conversation_id)
    )
    project_roots = tuple(Path(item.canonical_path) for item in roots)
    try:
        available_catalog = load_skill_catalog(
            settings.cowork_skills_path,
            max_files=settings.cowork_skill_max_files,
            max_bytes=settings.cowork_skill_max_bytes,
            project_roots=project_roots,
        )
        catalog = load_skill_catalog(
            settings.cowork_skills_path,
            max_files=settings.cowork_skill_max_files,
            max_bytes=settings.cowork_skill_max_bytes,
            project_roots=project_roots,
            muted_names=muted_names,
        )
    except SkillCatalogError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "source_path": str(settings.cowork_skills_path),
        "builtin_path": str(BUILTIN_SKILLS_ROOT),
        "project_paths": [str(root / ".workpilot" / "skills") for root in project_roots],
        "conversation_id": None if conversation_id is None else str(conversation_id),
        "muted_names": sorted(muted_names),
        "snapshot_sha256": catalog.snapshot_sha256,
        "skills": catalog.summaries(),
        "available_skills": available_catalog.summaries(),
        "errors": list(catalog.errors),
        "shadowed": list(catalog.shadowed),
        "installed": [
            item.public()
            for item in list_managed_skills(
                settings.cowork_skills_path,
                max_files=settings.cowork_skill_max_files,
                max_bytes=settings.cowork_skill_max_bytes,
                project_roots=project_roots,
            )
        ],
    }


@router.put("/skills/session/{conversation_id}/{skill_name}")
async def put_session_skill_mute(
    conversation_id: UUID,
    skill_name: str,
    request: SkillSessionMuteRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: DbSession,
) -> dict[str, Any]:
    if _SKILL_SESSION_NAME.fullmatch(skill_name) is None:
        raise HTTPException(status_code=422, detail="Skill name 非法")
    try:
        await cowork_store().list_conversation_skill_mutes(conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise HTTPException(status_code=404, detail="会话不存在") from error
    # Validate against the globally enabled, workspace-resolved catalog before persisting a
    # deny.  Unmute deliberately permits a stale name so uninstall/reinstall cannot trap a row.
    roots = await list_session_roots(session, conversation_id=conversation_id)
    project_roots = tuple(Path(item.canonical_path) for item in roots)
    try:
        available = load_skill_catalog(
            settings.cowork_skills_path,
            max_files=settings.cowork_skill_max_files,
            max_bytes=settings.cowork_skill_max_bytes,
            project_roots=project_roots,
        )
    except SkillCatalogError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if request.muted and skill_name not in {skill.name for skill in available.skills}:
        raise HTTPException(status_code=404, detail="Skill 不存在或已全局停用")
    try:
        await cowork_store().set_conversation_skill_muted(
            conversation_id=conversation_id,
            skill_name=skill_name,
            muted=request.muted,
        )
    except ConversationNotFoundError as error:
        raise HTTPException(status_code=404, detail="会话不存在") from error
    except ConversationBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return await get_skills_status(settings, session, conversation_id)


@router.get("/skills/candidates")
def get_skill_candidates(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return {
        "enabled": settings.skill_distillation_enabled,
        "auto_promotion_enabled": settings.skill_auto_promotion_enabled,
        "min_evidence": settings.skill_promotion_min_evidence,
        "min_confidence": settings.skill_promotion_min_confidence,
        "source_path": str(settings.cowork_skill_candidates_path),
        "items": [
            item.public(include_skill_md=True)
            for item in list_skill_candidates(settings.cowork_skill_candidates_path)
        ],
    }


@router.post("/skills/candidates/{capability_key}/promote")
def promote_skill_candidate(
    capability_key: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """晋升 = 把候选目录里的 SKILL.md 装进已安装目录。

    正文是现读现装的，所以用户可以先用编辑器改候选的 SKILL.md 再点晋升——
    这是候选从数据库文本列搬到文件之后白拿的能力。
    """

    try:
        candidate = get_skill_candidate(settings.cowork_skill_candidates_path, capability_key)
    except SkillCandidateStoreError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
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
            provenance_signing_key=LocalSecretStore(
                settings.secret_store_key_path
            ).derive_signing_key(AUTO_DISTILLED_PROVENANCE_PURPOSE),
        )
    except (FileExistsError, OSError, UnicodeError, SkillCatalogError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    updated = set_candidate_status(
        settings.cowork_skill_candidates_path,
        capability_key=candidate.capability_key,
        status="promoted",
        promoted_name=candidate.suggested_name,
    )
    return updated.public(include_skill_md=True)


@router.post("/skills/candidates/{capability_key}/reject")
def reject_skill_candidate(
    capability_key: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        candidate = get_skill_candidate(settings.cowork_skill_candidates_path, capability_key)
    except SkillCandidateStoreError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if candidate is None:
        raise HTTPException(status_code=404, detail="Skill 候选不存在")
    if candidate.status == "promoted":
        raise HTTPException(status_code=409, detail="已晋升 Skill 请从已安装列表停用或卸载")
    updated = set_candidate_status(
        settings.cowork_skill_candidates_path,
        capability_key=candidate.capability_key,
        status="rejected",
        review_reason="用户已拒绝",
    )
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
    except SkillCatalogError as error:
        # 出厂 Skill 不可删：不是请求不合法（422），是目标状态不允许（409）。
        raise HTTPException(status_code=409, detail=str(error)) from error
    except OSError as error:
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
