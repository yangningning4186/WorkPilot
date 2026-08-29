"""Cowork scheduler 与 unattended inbox API。"""

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.cowork import require_cowork_enabled
from app.api.dependencies import (
    get_run_bus,
    get_run_queue_dependency,
    require_owner_identity,
)
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.core.queue import RunQueue
from app.core.run_bus import RunBus
from app.cowork.interactions import list_unattended_inbox
from app.cowork.permissions import (
    CoworkPermissionError,
    SessionRootRecord,
    create_session_root,
    ensure_default_session_root,
    list_session_roots,
)
from app.cowork.provider_profiles import ensure_default_provider_binding
from app.cowork.schedules import (
    ScheduleError,
    ScheduleNotFoundError,
    ScheduleOverlapError,
    ScheduleRecord,
    ScheduleView,
    create_schedule,
    delete_schedule,
    list_schedules,
    run_schedule_now,
    update_schedule,
)
from app.runstore.runs import ensure_conversation, get_run
from app.schemas.automations import (
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleUpdate,
    UnattendedInboxItemResponse,
    UnattendedInboxListResponse,
)
from app.schemas.runs import RunStatusResponse

router = APIRouter(
    prefix="/api/v1/automations",
    tags=["automations"],
    dependencies=[Depends(require_owner_identity), Depends(require_cowork_enabled)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Owner = Annotated[None, Depends(require_owner_identity)]
logger = structlog.get_logger(__name__)


def _schedule_response(
    schedule: ScheduleRecord,
    *,
    last_run_status: str | None = None,
    pending_inbox_count: int = 0,
    workspace_label: str | None = None,
    workspace_path: str | None = None,
) -> ScheduleResponse:
    return ScheduleResponse(
        **schedule.__dict__,
        last_run_status=last_run_status,
        pending_inbox_count=pending_inbox_count,
        workspace_label=workspace_label,
        workspace_path=workspace_path,
    )


def _schedule_view_response(view: ScheduleView) -> ScheduleResponse:
    return _schedule_response(
        view.schedule,
        last_run_status=view.last_run_status,
        pending_inbox_count=view.pending_inbox_count,
        workspace_label=view.workspace_label,
        workspace_path=view.workspace_path,
    )


@router.get("", response_model=ScheduleListResponse)
async def get_automations(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ScheduleListResponse:
    items = await list_schedules(session, limit=limit)
    return ScheduleListResponse(
        items=[_schedule_view_response(item) for item in items],
        total=len(items),
    )


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def post_automation(
    request: ScheduleCreate,
    session: DbSession,
    _: Owner,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ScheduleResponse:
    try:
        conversation_id = await ensure_conversation(
            session,
            conversation_id=request.conversation_id,
            title=request.title if request.conversation_id is None else None,
        )
        await ensure_default_provider_binding(
            conversation_id=conversation_id,
            settings=settings,
        )
        workspace: SessionRootRecord | None
        if request.workspace_path is not None:
            workspace = await create_session_root(
                session,
                conversation_id=conversation_id,
                requested_path=request.workspace_path,
                access_mode="read_write",
                label=None,
            )
        else:
            workspace = await ensure_default_session_root(
                session,
                conversation_id=conversation_id,
                workspace_path=settings.cowork_default_workspace_path,
            )
        schedule = await create_schedule(
            session,
            conversation_id=conversation_id,
            title=request.title,
            goal=request.goal,
            schedule_kind=request.schedule_kind,
            cron_expression=request.cron_expression,
            run_at=request.run_at,
            timezone=request.timezone,
        )
    except (LookupError, CoworkPermissionError, ScheduleError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return _schedule_response(
        schedule,
        workspace_label=workspace.label if workspace is not None else None,
        workspace_path=workspace.canonical_path if workspace is not None else None,
    )


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def patch_automation(
    schedule_id: UUID,
    request: ScheduleUpdate,
    session: DbSession,
) -> ScheduleResponse:
    try:
        schedule = await update_schedule(
            session,
            schedule_id=schedule_id,
            changes=request.model_dump(exclude_unset=True),
        )
    except ScheduleNotFoundError as error:
        raise HTTPException(status_code=404, detail="自动化不存在") from error
    except ScheduleError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    roots = await list_session_roots(session, conversation_id=schedule.conversation_id)
    workspace = roots[0] if roots else None
    return _schedule_response(
        schedule,
        workspace_label=workspace.label if workspace is not None else None,
        workspace_path=workspace.canonical_path if workspace is not None else None,
    )


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(schedule_id: UUID, session: DbSession) -> Response:
    if not await delete_schedule(session, schedule_id=schedule_id):
        raise HTTPException(status_code=404, detail="自动化不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{schedule_id}/run", response_model=RunStatusResponse, status_code=202)
async def run_automation(
    schedule_id: UUID,
    session: DbSession,
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunStatusResponse:
    try:
        run_id, runnable = await run_schedule_now(
            session, schedule_id=schedule_id, settings=settings
        )
    except ScheduleNotFoundError as error:
        raise HTTPException(status_code=404, detail="自动化不存在") from error
    except ScheduleOverlapError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await session.commit()
    if runnable:
        try:
            await queue.enqueue_cowork_run(run_id)
        except Exception:
            # queued run 会由 scheduler tick 补偿入队，不能把可恢复窗口伪装成终态失败。
            logger.exception("自动化 run 入队失败，等待 scheduler 补偿", run_id=str(run_id))
    await bus.publish(run_id)
    run = await get_run(session, run_id)
    if run is None:  # pragma: no cover - 同事务刚创建
        raise HTTPException(status_code=500, detail="自动化运行创建失败")
    return RunStatusResponse(
        run_id=run.id,
        conversation_id=run.conversation_id,
        goal=run.goal,
        answer_mode=run.answer_mode,
        workflow_type=run.workflow_type,
        status=run.status,
        cancel_requested=run.cancel_requested,
        used_tokens=run.used_tokens,
        used_calls=run.used_calls,
        next_seq=run.next_seq,
        error=run.error,
        schedule_id=run.schedule_id,
        unattended=run.unattended,
        run_trigger=run.run_trigger,
    )


@router.get("/inbox/items", response_model=UnattendedInboxListResponse)
async def get_unattended_inbox(
    session: DbSession,
    include_resolved: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> UnattendedInboxListResponse:
    records = await list_unattended_inbox(session, include_resolved=include_resolved, limit=limit)
    items = [
        UnattendedInboxItemResponse(
            id=record.item.id,
            run_id=record.item.run_id,
            conversation_id=record.item.conversation_id,
            schedule_id=record.schedule_id,
            schedule_title=record.schedule_title,
            run_goal=record.run_goal,
            run_status=record.run_status,
            kind=record.item.kind,
            status=record.item.status,
            resume_token=record.item.resume_token,
            request=record.item.request,
            response=record.item.response,
            created_at=record.item.created_at,
            responded_at=record.item.responded_at,
        )
        for record in records
    ]
    return UnattendedInboxListResponse(items=items, total=len(items))
