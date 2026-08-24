from pathlib import Path
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_run_bus,
    get_run_queue_dependency,
    get_session_factory,
    require_owner_identity,
)
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import SessionFactory, get_db_session
from app.core.queue import RunQueue
from app.core.run_bus import RunBus
from app.cowork.attachments import CoworkAttachmentError, bind_attachments
from app.cowork.conversation_titles import fallback_conversation_title, is_placeholder_title
from app.cowork.extensions import register_skill_tools
from app.cowork.interactions import (
    cancel_pending_interaction,
    enqueue_steering,
    get_pending_inbox_item,
    resolve_inbox_item,
)
from app.cowork.permissions import (
    CapabilityDeniedError,
    CoworkPermissionError,
    authorize_path,
    ensure_default_session_root,
    list_session_roots,
)
from app.cowork.personas import load_persona_catalog
from app.cowork.runtime import initialize_cowork_state, resume_cowork_after_human
from app.cowork.tools import build_default_cowork_registry
from app.cowork_store.routing import cowork_store, local_run_guard
from app.runstore.conversations import (
    compare_and_set_conversation_title,
    get_conversation,
    get_conversation_kb,
)
from app.runstore.run_stream import parse_last_event_id, stream_run_events
from app.runstore.runs import (
    RunNotFoundError,
    RunRecord,
    append_events,
    append_message,
    create_run,
    ensure_conversation,
    finalize_message,
    finish_run_with_events,
    get_run,
    get_run_for_identity,
    list_events,
    request_cancel,
)
from app.schemas.runs import (
    CoworkInteractionResponseRequest,
    CoworkSteeringRequest,
    CreateCoworkRunRequest,
    CreateRunResponse,
    RunEventListResponse,
    RunStatusResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 关掉 Nginx 缓冲, 否则事件会被攒住直到响应结束, 流式就名存实亡。
    "X-Accel-Buffering": "no",
}


def _run_status_response(run: RunRecord) -> RunStatusResponse:
    # RunRecord 是 services 层的读模型；集中映射避免所有控制类 endpoint 漂移。
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


async def _authorized_workspace_files(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    requested: list[str],
) -> list[str]:
    """把系统选择器回传值收敛成已授权、存在的普通文件路径。"""

    resolved: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        path = Path(raw.strip())
        if not path.is_absolute():
            raise HTTPException(status_code=422, detail="工作文件必须使用系统选择器返回的绝对路径")
        try:
            authorization = await authorize_path(
                session,
                conversation_id=conversation_id,
                target_path=path,
                capability="filesystem.read",
            )
        except (CapabilityDeniedError, ValueError, OSError) as error:
            raise HTTPException(
                status_code=422, detail=f"工作文件未获得目录授权：{path.name}"
            ) from error
        target = authorization.target_path
        if not target.is_file():
            raise HTTPException(
                status_code=422, detail=f"工作文件不存在或不是普通文件：{path.name}"
            )
        value = str(target)
        if value not in seen:
            seen.add(value)
            resolved.append(value)
    return resolved


@router.post("/{run_id}/steering", response_model=RunStatusResponse, status_code=202)
async def steer_cowork_run(
    run_id: UUID,
    request: CoworkSteeringRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    _: Annotated[None, Depends(require_owner_identity)],
) -> RunStatusResponse:
    run = await get_run_for_identity(
        session,
        run_id=run_id,
    )
    if run is None or run.workflow_type != "cowork":
        raise HTTPException(status_code=404, detail="Cowork run 不存在")
    if run.is_terminal:
        raise HTTPException(status_code=409, detail="已结束的 Cowork run 不能再接收 steering")
    steering = await enqueue_steering(
        session,
        run_id=run_id,
        conversation_id=run.conversation_id,
        content=request.message,
    )
    trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
    await append_message(
        session,
        conversation_id=run.conversation_id,
        role="user",
        content=request.message.strip(),
        status="completed",
        run_id=run_id,
        trace_id=trace_id,
    )
    await append_events(
        session,
        run_id=run_id,
        events=[
            (
                "steering.queued",
                {"message_id": str(steering.id), "message": steering.content},
            )
        ],
    )
    await session.commit()
    await bus.publish(run_id)
    refreshed = await get_run_for_identity(
        session,
        run_id=run_id,
    )
    assert refreshed is not None
    return _run_status_response(refreshed)


@router.post(
    "/{run_id}/interactions/{resume_token}/respond",
    response_model=RunStatusResponse,
    status_code=202,
)
async def respond_to_cowork_interaction(
    run_id: UUID,
    resume_token: UUID,
    request: CoworkInteractionResponseRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    _: Annotated[None, Depends(require_owner_identity)],
) -> RunStatusResponse:
    async with local_run_guard(run_id) as locally_locked:
        run = await get_run_for_identity(
            session,
            run_id=run_id,
        )
        if run is None or run.workflow_type != "cowork":
            raise HTTPException(status_code=404, detail="Cowork run 不存在")
        item = await get_pending_inbox_item(
            session, run_id=run_id, resume_token=resume_token, for_update=not locally_locked
        )
        if item is None:
            raise HTTPException(status_code=404, detail="运行中请求不存在")
        try:
            if item.status == "pending":
                if run.status != "waiting_human":
                    raise ValueError("Cowork run 当前不在等待用户处理")
                item, response = await resolve_inbox_item(
                    session,
                    item=item,
                    approved=request.approved,
                    answer=request.answer,
                    path=request.path,
                    remember=request.remember,
                )
                await resume_cowork_after_human(
                    session, run_id=run_id, item=item, response=response
                )
            elif run.status != "queued":
                raise ValueError("这条运行中请求已经处理")
        except (CoworkPermissionError, LookupError, PermissionError, ValueError) as error:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

        # 同一个 inbox 使用稳定 attempt，HTTP 重试只会命中同一 Arq job id。
        attempt = item.id.int % 2_000_000_000 + 1
        await session.commit()
    await bus.publish(run_id)
    try:
        await queue.enqueue_cowork_run(run_id, attempt=attempt)
    except Exception as error:
        logger.exception("Cowork 人工答复重新入队失败", run_id=str(run_id))
        raise HTTPException(
            status_code=503,
            detail="答复已保存，但任务暂未重新入队；可重试本请求。",
        ) from error
    refreshed = await get_run_for_identity(
        session,
        run_id=run_id,
    )
    assert refreshed is not None
    return _run_status_response(refreshed)


@router.post(
    "/cowork",
    response_model=CreateRunResponse,
    status_code=202,
)
async def create_cowork_run(
    request: CreateCoworkRunRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    _: Annotated[None, Depends(require_owner_identity)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreateRunResponse:
    """创建已授权目录上的 Cowork 动态工具任务；执行期间不逐操作确认。"""

    if not settings.cowork_enabled:
        raise HTTPException(status_code=404, detail="Cowork 功能尚未启用")
    try:
        conversation_id = await ensure_conversation(
            session,
            conversation_id=request.conversation_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail="Cowork 会话不存在") from error
    try:
        await ensure_default_session_root(
            session,
            conversation_id=conversation_id,
            workspace_path=settings.cowork_default_workspace_path,
        )
    except CoworkPermissionError as error:
        await session.rollback()
        raise HTTPException(status_code=503, detail=str(error)) from error
    workspace_files = await _authorized_workspace_files(
        session,
        conversation_id=conversation_id,
        requested=request.workspace_files,
    )
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal=request.goal,
        budget_tokens=settings.run_budget_tokens,
        budget_calls=settings.run_budget_calls,
        budget_wall_ms=settings.run_budget_wall_ms,
        workflow_type="cowork",
        initializing=True,
    )
    conversation = await get_conversation(session, conversation_id=conversation_id)
    conversation_title = None if conversation is None else conversation.title
    if conversation is not None and is_placeholder_title(conversation.title):
        titled = await compare_and_set_conversation_title(
            session,
            conversation_id=conversation_id,
            expected_title=conversation.title,
            title=fallback_conversation_title(request.goal),
        )
        if titled is not None:
            conversation_title = titled.title
    trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
    message_id = await append_message(
        session,
        conversation_id=conversation_id,
        role="user",
        content=request.goal,
        status="completed",
        run_id=run.id,
        trace_id=trace_id,
    )
    try:
        await bind_attachments(
            session,
            conversation_id=conversation_id,
            attachment_ids=request.attachment_ids,
            message_id=message_id,
            run_id=run.id,
            max_count=settings.cowork_attachment_max_count,
        )
    except CoworkAttachmentError as error:
        await finish_run_with_events(
            session,
            run_id=run.id,
            status="failed",
            error=f"attachment initialization failed: {error}",
            events=[
                (
                    "error",
                    {
                        "code": "attachment_initialization_failed",
                        "retryable": False,
                        "user_message": str(error),
                    },
                )
            ],
        )
        await bus.publish(run.id)
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        registry = build_default_cowork_registry()
        session_roots = await list_session_roots(session, conversation_id=conversation_id)
        register_skill_tools(
            registry,
            settings,
            project_roots=tuple(Path(item.canonical_path) for item in session_roots),
        )
        conversation = await get_conversation(session, conversation_id=conversation_id)
        if conversation is None:  # pragma: no cover - ensure_conversation 已校验
            raise LookupError("Cowork 会话不存在")
        persona = load_persona_catalog(
            settings,
            project_roots=tuple(Path(item.canonical_path) for item in session_roots),
        ).get(conversation.persona_name)
        await initialize_cowork_state(
            session,
            run_id=run.id,
            registry=registry,
            bus=bus,
            plan_mode=request.plan_mode,
            work_mode=request.work_mode,
            reading_path=request.reading_path,
            # 客户端在发送这一刻读一次阅读器的当前视口带上来。它只进提示词，读哪份
            # 文件仍由每次工具调用上的目录授权决定，这里填什么都越不过去。
            reading_viewport=(
                None
                if request.reading_viewport is None
                else request.reading_viewport.model_dump(exclude_none=True)
            ),
            workspace_files=workspace_files,
            # 挂载在会话上、不在请求里：用户挂一次，之后每一轮都用同一个库。读到 state
            # 里冻住，中途改绑定影响的是下一个 run，不会让这一个 run 换语料。
            kb_slug=await get_conversation_kb(
                session,
                conversation_id=conversation_id,
            ),
            settings=settings,
            persona=persona,
        )
        refreshed_run = await get_run(session, run.id)
        if refreshed_run is None or refreshed_run.status != "queued":  # pragma: no cover
            raise RuntimeError("Cowork run 初始化后未进入 queued")
        run = refreshed_run
    except (RuntimeError, ValueError) as error:
        await finish_run_with_events(
            session,
            run_id=run.id,
            status="failed",
            error=f"run initialization failed: {error}",
            events=[
                (
                    "error",
                    {
                        "code": "run_initialization_failed",
                        "retryable": True,
                        "user_message": f"任务初始化失败：{error}",
                    },
                )
            ],
        )
        await bus.publish(run.id)
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        await queue.enqueue_cowork_run(run.id)
    except Exception:
        # 内存队列只是低延迟提示，SQLite queued 才是任务真相。这里不能把已经完整初始化
        # 的 run 反向判失败；dispatcher 会在下一个轮询周期重新发现并投递。
        logger.exception("Cowork run 即时入队失败，等待 dispatcher 补偿", run_id=str(run.id))
        await bus.publish(run.id)
    return CreateRunResponse(
        run_id=run.id,
        conversation_id=conversation_id,
        conversation_title=conversation_title,
        status=run.status,
        workflow_type=run.workflow_type,
    )


@router.get("/{run_id}", response_model=RunStatusResponse)
async def read_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: Annotated[None, Depends(require_owner_identity)],
) -> RunStatusResponse:
    run = await get_run_for_identity(
        session,
        run_id=run_id,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
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
    )


@router.get("/{run_id}/events")
async def stream_events(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    stream_sessions: Annotated[SessionFactory, Depends(get_session_factory)],
    _: Annotated[None, Depends(require_owner_identity)],
    after_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """先补历史再续实时流。

    `Last-Event-ID` 优先于 `after_seq`: 前者是浏览器自动重连时带的, 代表客户端
    真正收到的最后一个事件; 查询参数只是首次连接或手动回放用的起点。
    """

    if (
        await get_run_for_identity(
            session,
            run_id=run_id,
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="run 不存在")

    cursor = parse_last_event_id(last_event_id)
    return StreamingResponse(
        stream_run_events(
            stream_sessions,
            bus,
            run_id=run_id,
            after_seq=cursor if cursor is not None else after_seq,
            heartbeat_s=get_settings().run_heartbeat_s,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/{run_id}/event-log", response_model=RunEventListResponse)
async def read_event_log(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: Annotated[None, Depends(require_owner_identity)],
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> RunEventListResponse:
    """SSE 的有界 JSON 镜像；Tauri webview 可带启动 header 增量轮询。"""

    if (
        await get_run_for_identity(
            session,
            run_id=run_id,
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="run 不存在")
    events = await list_events(
        session,
        run_id=run_id,
        after_seq=after_seq,
        limit=limit,
    )
    return RunEventListResponse(items=[event.envelope() for event in events])


@router.post("/{run_id}/cancel", response_model=RunStatusResponse)
async def cancel_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    _: Annotated[None, Depends(require_owner_identity)],
) -> RunStatusResponse:
    """请求取消。已在执行的 run 由持有租约的 worker 在下一个检查点收尾。"""

    # 请求取消和补终态事件必须在同一把 run 锁里。否则两个并发请求都可能先读到 queued，
    # 虽然状态更新是幂等的，随后却各写一组 error/run.done，重放端就会看到两个终态。
    async with local_run_guard(run_id):
        previous = await get_run_for_identity(
            session,
            run_id=run_id,
        )
        try:
            run = await request_cancel(
                session,
                run_id=run_id,
            )
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail="run 不存在") from error
        # queued run 不会再被 worker 领取，因此取消接口必须自己补齐终态事件；否则
        # 已经连上 SSE / event-log 的前端只会看到状态字段改变，却永远等不到流终止。
        # 仅在 queued -> cancelled 的首次转换写事件，重复点击取消保持幂等。
        if (
            previous is not None
            and previous.status in {"queued", "waiting_human"}
            and run.status == "cancelled"
        ):
            if previous.status == "waiting_human" and previous.workflow_type == "cowork":
                await cancel_pending_interaction(session, run_id=run_id)
                for message_id in await cowork_store().list_streaming_message_ids(run_id=run_id):
                    await finalize_message(
                        session,
                        message_id=message_id,
                        status="cancelled",
                        content="Cowork 任务已停止。",
                    )
            await append_events(
                session,
                run_id=run_id,
                events=[
                    (
                        "error",
                        {
                            "code": "cancelled",
                            "retryable": True,
                            "user_message": "任务已取消。",
                        },
                    ),
                    (
                        "run.done",
                        {"workflow_type": run.workflow_type, "status": "cancelled"},
                    ),
                ],
            )
            refreshed = await get_run_for_identity(
                session,
                run_id=run_id,
            )
            if refreshed is not None:
                run = refreshed
        await session.commit()
    await bus.publish(run_id)
    return _run_status_response(run)
