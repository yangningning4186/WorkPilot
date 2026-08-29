"""从消息面起一轮 Cowork run。

单独成模块是为了不让 `inbound` 认识预算、队列和工具注册表——那一层只回答"这条消息
该由谁处理"。也不复用 HTTP 的创建路由：那条路带着身份校验、附件绑定和计划模式，
而入站消息一个都用不上，硬凑会让两处都变脏。

**它与 HTTP 创建路径共享同一套 run 语义**：同样的预算、同样的 checkpoint 初始化、
同样的队列。不同的只是触发来源。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.core.queue import RunQueue
from app.core.run_bus import RunBus
from app.cowork.extensions import register_skill_tools
from app.cowork.permissions import ensure_default_session_root, list_session_roots
from app.cowork.runtime import initialize_cowork_state
from app.cowork.tools import build_default_cowork_registry
from app.cowork_store.routing import cowork_store
from app.runstore.conversations import get_conversation
from app.runstore.runs import append_message, create_run, finish_run_with_events


async def active_run_id(session: AsyncSession, *, conversation_id: UUID) -> UUID | None:
    """会话此刻有没有一个还没走完的 run。

    `sleeping` 与 `waiting_human` 都算"还活着"：把一条新消息当成 steering 插进去，
    比另起一轮更接近用户的意思——他是在跟同一件事说话。
    """

    store = cowork_store()
    latest = await store.get_latest_run(conversation_id=conversation_id)
    if latest is None or latest.is_terminal:
        return None
    return latest.id


async def start_cowork_run(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    goal: str,
    settings: Settings,
    queue: RunQueue,
    bus: RunBus,
    source_wake_id: UUID | None = None,
    semantic_review_user_text_source: Literal[
        "local_owner", "external_inbound", "unknown"
    ] = "external_inbound",
) -> UUID:
    await ensure_default_session_root(
        session,
        conversation_id=conversation_id,
        workspace_path=settings.cowork_default_workspace_path,
    )
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal=goal,
        budget_tokens=settings.run_budget_tokens,
        budget_calls=settings.run_budget_calls,
        budget_wall_ms=settings.run_budget_wall_ms,
        workflow_type="cowork",
        run_trigger="manual",
        initializing=True,
        source_wake_id=source_wake_id,
    )
    if run.status != "initializing":
        if run.status == "queued":
            await queue.enqueue_cowork_run(run.id)
        await bus.publish(run.id)
        return run.id
    await append_message(
        session,
        conversation_id=conversation_id,
        role="user",
        content=goal,
        status="completed",
        run_id=run.id,
        trace_id="messaging",
    )
    registry = build_default_cowork_registry()
    roots = await list_session_roots(session, conversation_id=conversation_id)
    muted_skill_names = await cowork_store().list_conversation_skill_mutes(
        conversation_id=conversation_id
    )
    register_skill_tools(
        registry,
        settings,
        project_roots=tuple(Path(item.canonical_path) for item in roots),
        muted_skill_names=muted_skill_names,
    )
    conversation = await get_conversation(session, conversation_id=conversation_id)
    if conversation is None:  # pragma: no cover - run 已创建
        raise LookupError("Cowork 会话不存在")
    try:
        await initialize_cowork_state(
            session,
            run_id=run.id,
            registry=registry,
            bus=bus,
            settings=settings,
            muted_skill_names=muted_skill_names,
            # Connector senders may be authorized to steer a conversation, but their
            # message is untrusted input and can never authorize auto approval. Durable
            # local next_run messages explicitly override this with local_owner.
            semantic_review_user_text_source=semantic_review_user_text_source,
        )
    except Exception as error:
        await finish_run_with_events(
            session,
            run_id=run.id,
            status="failed",
            error=f"messaging run initialization failed: {error}",
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
        raise
    try:
        await queue.enqueue_cowork_run(run.id)
    except Exception:
        # SQLite queued 状态由 dispatcher 补偿；内存通知失败不能把持久化任务判死。
        await bus.publish(run.id)
    return run.id
