from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.review_graph import initialize_review_state
from app.agent.write_note import (
    InvocationInFlightError,
    resolve_note_path,
    resume_review_after_human,
)
from app.api.dependencies import (
    get_demo_session,
    get_run_bus,
    get_run_queue_dependency,
    get_session_factory,
    require_admin_session,
)
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.queue import RunQueue
from app.core.run_bus import RunBus
from app.schemas.runs import (
    CreateReviewRunRequest,
    CreateRunRequest,
    CreateRunResponse,
    ResumeRunRequest,
    RunStatusResponse,
)
from app.services.demo_sessions import DemoSession, consume_question_quota
from app.services.run_stream import parse_last_event_id, stream_run_events
from app.services.runs import (
    RunNotFoundError,
    append_events,
    append_message,
    create_run,
    ensure_conversation,
    finish_run,
    get_run_for_demo_session,
    request_cancel,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 关掉 Nginx 缓冲, 否则事件会被攒住直到响应结束, 流式就名存实亡。
    "X-Accel-Buffering": "no",
}


@router.post("", response_model=CreateRunResponse, status_code=202)
async def create_answer_run(
    request: CreateRunRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreateRunResponse:
    """创建 run 并入队, 立即返回。执行在 worker 进程, 不依附本次 HTTP 连接。"""

    try:
        conversation_id = await ensure_conversation(
            session,
            conversation_id=request.conversation_id,
            scope="demo",
            demo_session_id=demo_session.id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not await consume_question_quota(
        session,
        demo_session_id=demo_session.id,
        limit=settings.demo_session_question_limit,
    ):
        raise HTTPException(status_code=429, detail="本 session 的提问额度已用尽")

    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal=request.query,
        budget_tokens=settings.run_budget_tokens,
        budget_calls=settings.run_budget_calls,
        budget_wall_ms=settings.run_budget_wall_ms,
        answer_mode=request.mode,
    )
    trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
    await append_message(
        session,
        conversation_id=conversation_id,
        role="user",
        content=request.query,
        status="completed",
        run_id=run.id,
        trace_id=trace_id,
    )
    # 先提交再入队: 反过来的话 worker 可能比事务可见更早领到任务, 查不到 run。
    await session.commit()

    try:
        await queue.enqueue_answer_run(run.id, top_k=request.top_k)
    except Exception as error:
        # 入队失败的 run 永远等不到 worker, 也没有租约让 watchdog 回收, 必须当场落终态。
        logger.exception("run 入队失败", run_id=str(run.id))
        await append_events(
            session,
            run_id=run.id,
            events=[
                (
                    "error",
                    {
                        "user_message": "任务未能进入队列, 请稍后重试。",
                        "retryable": True,
                        "code": "enqueue_failed",
                    },
                )
            ],
        )
        await finish_run(session, run_id=run.id, status="failed", error="入队失败")
        await session.commit()
        raise HTTPException(status_code=503, detail="任务队列不可用") from error

    return CreateRunResponse(
        run_id=run.id,
        conversation_id=conversation_id,
        status=run.status,
        workflow_type=run.workflow_type,
    )


@router.post(
    "/reviews",
    response_model=CreateRunResponse,
    status_code=202,
    dependencies=[Depends(require_admin_session)],
)
async def create_review_run(
    request: CreateReviewRunRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreateRunResponse:
    """创建固定综述；只允许已登录的个人 owner 选择文档和写回目标。"""

    if len(set(request.document_ids)) != len(request.document_ids):
        raise HTTPException(status_code=422, detail="document_ids 不能重复")
    try:
        resolve_note_path(settings.agent_output_path, request.output_path)
        conversation_id = await ensure_conversation(
            session,
            conversation_id=request.conversation_id,
            scope="demo",
            demo_session_id=demo_session.id,
        )
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal=request.goal,
        budget_tokens=settings.run_budget_tokens,
        budget_calls=settings.run_budget_calls,
        budget_wall_ms=settings.run_budget_wall_ms,
        workflow_type="literature_review",
    )
    trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
    await append_message(
        session,
        conversation_id=conversation_id,
        role="user",
        content=request.goal,
        status="completed",
        run_id=run.id,
        trace_id=trace_id,
    )
    await initialize_review_state(
        session,
        run_id=run.id,
        document_ids=request.document_ids,
        output_path=request.output_path,
        bus=bus,
    )
    try:
        await queue.enqueue_review_run(run.id)
    except Exception as error:
        logger.exception("固定综述 run 入队失败", run_id=str(run.id))
        await append_events(
            session,
            run_id=run.id,
            events=[
                (
                    "error",
                    {
                        "user_message": "固定综述未能进入队列，请稍后重试。",
                        "retryable": True,
                        "code": "enqueue_failed",
                    },
                )
            ],
        )
        await finish_run(session, run_id=run.id, status="failed", error="入队失败")
        await session.commit()
        await bus.publish(run.id)
        raise HTTPException(status_code=503, detail="任务队列不可用") from error
    return CreateRunResponse(
        run_id=run.id,
        conversation_id=conversation_id,
        status=run.status,
        workflow_type=run.workflow_type,
    )


@router.post(
    "/{run_id}/resume",
    response_model=RunStatusResponse,
    dependencies=[Depends(require_admin_session)],
)
async def resume_review_run(
    run_id: UUID,
    request: ResumeRunRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunStatusResponse:
    run = await get_run_for_demo_session(
        session, run_id=run_id, demo_session_id=demo_session.id
    )
    if run is None or run.workflow_type != "literature_review":
        raise HTTPException(status_code=404, detail="固定综述 run 不存在")
    trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
    try:
        await resume_review_after_human(
            session,
            run_id=run_id,
            resume_token=request.resume_token,
            approved=request.approved,
            output_root=settings.agent_output_path,
            worker_id=f"api:{trace_id}",
            bus=bus,
        )
    except (InvocationInFlightError, LookupError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    refreshed = await get_run_for_demo_session(
        session, run_id=run_id, demo_session_id=demo_session.id
    )
    if refreshed is None:  # pragma: no cover - 同一事务内 run 不会消失
        raise HTTPException(status_code=404, detail="固定综述 run 不存在")
    return RunStatusResponse(
        run_id=refreshed.id,
        conversation_id=refreshed.conversation_id,
        goal=refreshed.goal,
        answer_mode=refreshed.answer_mode,
        workflow_type=refreshed.workflow_type,
        status=refreshed.status,
        cancel_requested=refreshed.cancel_requested,
        used_tokens=refreshed.used_tokens,
        used_calls=refreshed.used_calls,
        next_seq=refreshed.next_seq,
        error=refreshed.error,
    )


@router.get("/{run_id}", response_model=RunStatusResponse)
async def read_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
) -> RunStatusResponse:
    run = await get_run_for_demo_session(session, run_id=run_id, demo_session_id=demo_session.id)
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
    stream_sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
    after_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """先补历史再续实时流。

    `Last-Event-ID` 优先于 `after_seq`: 前者是浏览器自动重连时带的, 代表客户端
    真正收到的最后一个事件; 查询参数只是首次连接或手动回放用的起点。
    """

    if (
        await get_run_for_demo_session(session, run_id=run_id, demo_session_id=demo_session.id)
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


@router.post("/{run_id}/cancel", response_model=RunStatusResponse)
async def cancel_run(
    run_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    demo_session: Annotated[DemoSession, Depends(get_demo_session)],
) -> RunStatusResponse:
    """请求取消。已在执行的 run 由持有租约的 worker 在下一个检查点收尾。"""

    try:
        run = await request_cancel(session, run_id=run_id, demo_session_id=demo_session.id)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail="run 不存在") from error
    await session.commit()
    await bus.publish(run_id)
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
