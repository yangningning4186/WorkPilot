"""会话生命周期与按请求身份隔离的消息读取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from uuid6 import uuid7

from app.agent_core.compaction import collect_history_details, deterministic_history_summary
from app.agent_core.session_entries import SessionEntry
from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import ApprovalMode
from app.cowork_contracts import ConversationBusyError as ConversationBusyError
from app.cowork_store.base import SessionLaneNavigation
from app.cowork_store.routing import cowork_store


@dataclass(frozen=True)
class ConversationRecord:
    id: UUID
    title: str | None
    # 会话切走不得取消后台 run；切回时用它从持久化事件游标恢复订阅。
    active_run_id: UUID | None
    message_count: int
    latest_message: str | None
    last_message_at: datetime | None
    provider_profile_id: UUID | None
    # 只记 id 和覆盖值。Provider 的名字、档位、默认模型属于 Cowork 产品层，
    # runstore 认识它们就等于把存储层焊到某一个产品上——同 set_conversation_kb 里
    # 那条注释。解引用由 API 层做。
    model_override: str | None
    unattended: bool
    approval_mode: str
    persona_name: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationMessageRecord:
    id: UUID
    seq: int
    role: str
    content: str
    status: str
    run_id: UUID | None
    citations: list[dict[str, Any]]
    answer_mode: str | None
    attachments: list[dict[str, Any]]
    created_at: datetime


async def _local_conversation_record(
    session: AsyncSession, row: dict[str, Any]
) -> ConversationRecord:
    conversation_id = UUID(str(row["id"]))
    message_count = int(row.get("message_count") or 0)
    latest = row.get("latest_message")
    # content_preview 是 v11 加的。升级前的行没有摘要，只为这些旧会话回读一次 JSONL；
    # 新写入的会话列表完全由一条 SQLite 查询得到。
    if latest is None and message_count:
        from app.cowork_store.factory import local_cowork_stores

        messages = await local_cowork_stores().conversations.read(conversation_id)
        latest = next(
            (
                item.content[:160]
                for item in reversed(messages)
                if item.role in {"user", "assistant"} and item.content
            ),
            None,
        )
    profile_id = (
        None if row["provider_profile_id"] is None else UUID(str(row["provider_profile_id"]))
    )
    return ConversationRecord(
        id=conversation_id,
        title=row["title"],
        active_run_id=(
            None if row.get("active_run_id") is None else UUID(str(row["active_run_id"]))
        ),
        message_count=message_count,
        latest_message=None if latest is None else str(latest),
        last_message_at=(
            None
            if row.get("last_message_at") is None
            else datetime.fromisoformat(str(row["last_message_at"]))
        ),
        provider_profile_id=profile_id,
        model_override=row["model_override"],
        unattended=bool(row["unattended"]),
        approval_mode=str(row.get("approval_mode") or "interactive"),
        persona_name=str(row.get("persona_name") or "general"),
        archived_at=(
            None
            if row.get("archived_at") is None
            else datetime.fromisoformat(str(row["archived_at"]))
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


async def list_conversations(
    session: AsyncSession,
    *,
    archived: bool = False,
    limit: int = 100,
) -> list[ConversationRecord]:
    if not 1 <= limit <= 200:
        raise ValueError("conversation limit 必须位于 1 到 200")
    store = cowork_store()
    return [
        await _local_conversation_record(session, row)
        for row in await store.list_conversation_metadata(archived=archived, limit=limit)
    ]


async def get_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
) -> ConversationRecord | None:
    store = cowork_store()
    rows = await store.list_conversation_metadata(
        conversation_id=conversation_id,
        archived=None,
        limit=1,
    )
    if not rows:
        return None
    return await _local_conversation_record(session, rows[0])


async def compare_and_set_conversation_title(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    expected_title: str | None,
    title: str,
) -> ConversationRecord | None:
    """原子更新标题；标题已被别的路径改过时不覆盖。"""

    changed = await cowork_store().compare_and_set_conversation_title(
        conversation_id=conversation_id,
        expected_title=expected_title,
        title=title,
    )
    if not changed:
        return None
    return await get_conversation(
        session,
        conversation_id=conversation_id,
    )


async def update_conversation_runtime(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    provider_profile_id: UUID | None,
    model_override: str | None,
    unattended: bool,
    approval_mode: str = "interactive",
    persona_name: str = "general",
) -> ConversationRecord | None:
    """更新会话运行时选择。

    `provider_profile_id` 存不存在、停没停用由 API 层校验——Profile 已经不在数据库里，
    这里没有可 JOIN 的东西，也不该为此去 import Cowork 产品层。
    """

    if approval_mode not in {"interactive", "auto"}:
        raise ValueError("approval_mode 只能是 interactive 或 auto")
    if not persona_name or len(persona_name) > 64:
        raise ValueError("persona_name 长度必须位于 1 到 64")
    store = cowork_store()
    changed = await store.update_conversation_runtime(
        conversation_id=conversation_id,
        provider_profile_id=provider_profile_id,
        model_override=model_override.strip() if model_override else None,
        unattended=unattended,
        approval_mode=cast("ApprovalMode", approval_mode),
        persona_name=persona_name,
    )
    if not changed:
        return None
    return await get_conversation(
        session,
        conversation_id=conversation_id,
    )


async def set_conversation_kb(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    kb_slug: str | None,
) -> bool:
    """把一个本地知识库挂到会话上；`kb_slug=None` 表示卸载。

    **演示身份不能挂。** 知识库里是本机所有者的私人资料，挂到一个共享环境的会话上等于
    把它们交出去。这与 `update_conversation_runtime` 拒绝演示身份开无人值守是同一条线。

    这里只写 slug 存不存在的校验交给调用方（API 层持有 `LocalKbService`）：runstore
    不认识知识库长什么样，让它去 import `app.rag` 会把存储层焊到某一个产品上。
    """

    store = cowork_store()
    return await store.set_conversation_kb(conversation_id=conversation_id, kb_slug=kb_slug)


async def get_conversation_kb(
    session: AsyncSession,
    *,
    conversation_id: UUID,
) -> str | None:
    store = cowork_store()
    return await store.get_conversation_kb(conversation_id=conversation_id)


async def set_conversation_archived(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    archived: bool,
) -> ConversationRecord | None:
    """归档或恢复会话；运行中的会话不能从列表隐藏。"""

    store = cowork_store()
    changed = await store.set_conversation_archived(
        conversation_id=conversation_id, archived=archived
    )
    if not changed:
        return None
    return await get_conversation(
        session,
        conversation_id=conversation_id,
    )


async def delete_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
) -> bool:
    """删除归属当前身份的会话；数据库外键负责级联消息与运行记录。

    等待用户、尚未领取及租约已过期的 run 不会再安全写入，可以随会话一并
    删除；只有仍持有效租约的 worker 会阻止操作。已经抽取为 owner 长期记忆的
    事实是独立数据，不随会话删除；其来源消息外键会置空。
    """

    store = cowork_store()
    deleted = await store.delete_conversation(conversation_id=conversation_id)
    if deleted:
        from app.cowork_store.factory import local_cowork_stores

        await local_cowork_stores().conversations.delete(conversation_id)
    return deleted


async def list_conversation_messages(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    lane: str = "main",
    limit: int = 100,
) -> list[ConversationMessageRecord] | None:
    if not 1 <= limit <= 500:
        raise ValueError("message limit 必须位于 1 到 500")
    store = cowork_store()
    if not await store.conversation_exists(conversation_id):
        return None
    from app.cowork_store.factory import local_cowork_stores

    messages = await local_cowork_stores().conversations.read(conversation_id)
    entries = await store.list_session_entries(
        conversation_id=conversation_id,
        lane=lane,
        limit=10_000,
    )
    message_ids = [
        str(entry.payload["record_id"])
        for entry in entries
        if entry.kind == "message" and isinstance(entry.payload.get("record_id"), str)
    ]
    if entries:
        by_id = {str(item.record_id): item for item in messages}
        visible = [
            by_id[record_id]
            for record_id in message_ids
            if record_id in by_id and by_id[record_id].role in {"user", "assistant"}
        ][-limit:]
    else:
        # Databases created before session_entries were introduced have no lane projection.
        visible = [value for value in messages if value.role in {"user", "assistant"}][-limit:]
    runs = {
        run.id: run
        for run in await store.get_runs(
            tuple(item.run_id for item in visible if item.run_id is not None)
        )
    }
    output: list[ConversationMessageRecord] = []
    for item in visible:
        run = None if item.run_id is None else runs.get(item.run_id)
        output.append(
            ConversationMessageRecord(
                id=item.record_id,
                seq=item.seq,
                role=item.role,
                content=item.content,
                status=item.status,
                run_id=item.run_id,
                citations=list(item.citations),
                answer_mode=None if run is None else run.answer_mode,
                attachments=list(item.attachments),
                created_at=datetime.fromisoformat(item.created_at),
            )
        )
    return output


async def navigate_conversation_lane(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    target_entry_id: str,
    position: Literal["before", "after"] = "after",
    summarize: bool = True,
    lane: str = "main",
) -> SessionLaneNavigation:
    """Move a conversation lane and retain the branch being left under a stable name."""

    del session
    store = cowork_store()
    if not await store.conversation_exists(conversation_id):
        raise LookupError("会话不存在")
    current_path = await store.list_session_entries(
        conversation_id=conversation_id,
        lane=lane,
        limit=10_000,
    )
    all_entries = await store.list_session_entries(
        conversation_id=conversation_id,
        lane=None,
        limit=10_000,
    )
    by_id = {entry.id: entry for entry in all_entries}
    target = by_id.get(target_entry_id)
    if target is None:
        raise LookupError("目标 session entry 不存在")
    resolved_target_id = target.parent_id if position == "before" else target.id
    expected_head = current_path[-1].id if current_path else None

    target_ancestry: set[str] = set()
    cursor: str | None = resolved_target_id
    while cursor is not None:
        if cursor in target_ancestry or cursor not in by_id:
            raise ValueError("session entry parent 链损坏")
        target_ancestry.add(cursor)
        cursor = by_id[cursor].parent_id
    abandoned_entries = [entry for entry in current_path if entry.id not in target_ancestry]
    payload: dict[str, Any] | None = None
    if summarize and expected_head != resolved_target_id and expected_head is not None:
        from app.cowork_store.factory import local_cowork_stores

        records = await local_cowork_stores().conversations.read(conversation_id)
        messages = {str(item.record_id): item for item in records}
        abandoned_history: list[dict[str, Any]] = []
        abandoned_message_ids: list[str] = []
        for entry in abandoned_entries:
            if entry.kind != "message":
                continue
            record_id = entry.payload.get("record_id")
            item = messages.get(str(record_id))
            if item is not None and item.role in {"user", "assistant"}:
                abandoned_message_ids.append(str(item.record_id))
                abandoned_history.append({"role": item.role, "content": item.content})
        inherited_details: dict[str, list[str]] = {
            "read_files": [],
            "modified_files": [],
            "artifacts": [],
        }
        for entry in abandoned_entries:
            if entry.kind != "compaction" or not isinstance(entry.payload.get("details"), dict):
                continue
            raw_details = cast("dict[str, Any]", entry.payload["details"])
            for key in inherited_details:
                raw_values = raw_details.get(key)
                if not isinstance(raw_values, list):
                    continue
                for value in raw_values:
                    normalized = str(value).strip()
                    if normalized and normalized not in inherited_details[key]:
                        inherited_details[key].append(normalized)
        details = collect_history_details(abandoned_history, current=inherited_details)
        payload = {
            "reason": "lane_navigation",
            "from_entry_id": expected_head,
            "to_entry_id": resolved_target_id,
            "requested_entry_id": target_entry_id,
            "position": position,
            "abandoned_entry_count": len(abandoned_entries),
            "abandoned_message_ids": abandoned_message_ids,
            "summary": deterministic_history_summary(
                abandoned_history,
                max_chars=6_000,
            ),
            "details": details,
        }

    return await store.navigate_session_lane(
        conversation_id=conversation_id,
        lane=lane,
        target_entry_id=resolved_target_id,
        expected_head_entry_id=expected_head,
        abandoned_lane=f"branch-{uuid7()!s}",
        branch_summary_payload=payload,
    )


async def list_conversation_entries(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    lane: str | None = "main",
    limit: int = 1000,
) -> list[SessionEntry] | None:
    del session
    store = cowork_store()
    if not await store.conversation_exists(conversation_id):
        return None
    return await store.list_session_entries(
        conversation_id=conversation_id,
        lane=lane,
        limit=limit,
    )


async def fork_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    message_id: UUID,
    position: Literal["before", "after"] = "after",
    title: str | None = None,
) -> UUID:
    """从任意可见消息处分叉；源会话与被舍弃分支保持不变。"""

    source = await get_conversation(session, conversation_id=conversation_id)
    if source is None:
        raise LookupError("会话不存在")
    if source.active_run_id is not None:
        raise ConversationBusyError("会话仍有任务在运行，不能从不稳定的消息尾部分叉")
    from app.cowork_store.factory import local_cowork_stores
    from app.runstore.runs import append_message

    messages = await local_cowork_stores().conversations.read(conversation_id)
    target_index = next(
        (index for index, item in enumerate(messages) if item.record_id == message_id),
        None,
    )
    if target_index is None:
        raise LookupError("分叉消息不存在")
    boundary = target_index + (1 if position == "after" else 0)
    retained = messages[:boundary]
    abandoned = messages[boundary:]
    normalized_title = (
        title.strip()
        if title is not None and title.strip()
        else f"{source.title or '新会话'} · 分支"
    )[:120]
    store = cowork_store()
    fork_id = await store.create_conversation(title=normalized_title)
    try:
        await store.update_conversation_runtime(
            conversation_id=fork_id,
            provider_profile_id=source.provider_profile_id,
            model_override=source.model_override,
            unattended=source.unattended,
            approval_mode=cast("ApprovalMode", source.approval_mode),
            persona_name=source.persona_name,
        )
        for message in retained:
            await append_message(
                session,
                conversation_id=fork_id,
                role=message.role,
                content=message.content,
                status="completed",
                run_id=None,
                citations=message.citations,
            )
        await store.append_session_entry(
            conversation_id=fork_id,
            kind="branch_summary",
            payload={
                "source_conversation_id": str(conversation_id),
                "source_message_id": str(message_id),
                "position": position,
                "retained_messages": len(retained),
                "abandoned_messages": len(abandoned),
                "omitted_retained_attachments": sum(len(item.attachments) for item in retained),
                "abandoned_message_ids": [str(item.record_id) for item in abandoned],
                "summary": deterministic_history_summary(
                    [
                        {"role": item.role, "content": item.content}
                        for item in abandoned
                        if item.role in {"user", "assistant"}
                    ],
                    max_chars=6_000,
                ),
                "details": collect_history_details([]),
            },
        )
    except Exception:
        await store.delete_conversation(conversation_id=fork_id)
        await local_cowork_stores().conversations.delete(fork_id)
        raise
    return fork_id
