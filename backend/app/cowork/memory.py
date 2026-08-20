"""Cowork 长期记忆：跨会话保留的用户偏好与项目约定。

**为什么不复用 RAG 的 memory。** 除了 `rag ⊥ cowork`（ADR-0011）这条硬约束之外，两者
要解决的问题也不同：RAG 的 memory 做时序有效性建模（ADR-0005），关心"这条事实在哪段
时间成立"；Cowork 需要的是"用户偏好 Markdown 报告"这类当前有效的轻量事实。把后者塞进
前者的模型里，会得到一堆没有时间语义的退化记录。

**为什么不下沉到 agent_core。** 存储要按 ADR-0010 走 `configured_cowork_store()` 双后端
路由，而 `agent_core` 不许依赖 `cowork_store`。等到 RAG 也需要同一套东西时再谈下沉——
现在下沉只会造出一个单一使用者的抽象。

作用域三档，绑定关系由 DB 的 CHECK 约束兜底：
- `global`：跨所有会话的用户偏好
- `workspace`：绑定某个已授权目录的规范化路径，即"这个项目的规矩"
- `conversation`：只在当前会话有效
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.cowork.permissions import list_session_roots
from app.cowork_contracts import (
    CoworkMemoryRecord as CoworkMemoryRecord,
)
from app.cowork_contracts import (
    MemoryNotFoundError as MemoryNotFoundError,
)
from app.cowork_contracts import (
    MemoryScope as MemoryScope,
)
from app.cowork_contracts import (
    MemoryScopeError as MemoryScopeError,
)
from app.cowork_store.routing import configured_cowork_store

MEMORY_SCOPES: frozenset[str] = frozenset({"global", "workspace", "conversation"})
MAX_MEMORY_CONTENT_CHARS = 4000
MAX_MEMORY_KEY_CHARS = 120

_COLUMNS = """
    id, scope, conversation_id, workspace_path, key, content, source,
    created_at, updated_at, forgotten_at
"""


def _record(row: Any) -> CoworkMemoryRecord:
    return CoworkMemoryRecord(**row)


def _normalize_content(content: str) -> str:
    normalized = content.strip()
    if not 1 <= len(normalized) <= MAX_MEMORY_CONTENT_CHARS:
        raise MemoryScopeError(f"记忆内容长度必须位于 1 到 {MAX_MEMORY_CONTENT_CHARS}")
    return normalized


def _normalize_key(key: str | None) -> str | None:
    if key is None:
        return None
    normalized = key.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_MEMORY_KEY_CHARS:
        raise MemoryScopeError(f"记忆 key 不能超过 {MAX_MEMORY_KEY_CHARS} 个字符")
    return normalized


def resolve_binding(
    scope: MemoryScope,
    *,
    conversation_id: UUID,
    workspace_path: str | None,
) -> tuple[UUID | None, str | None]:
    """把作用域折成 (conversation_id, workspace_path) 这对定位字段。

    调用方传的永远是"当前会话"，不是"要绑哪个会话"——记忆不能跨会话写。
    """

    if scope == "global":
        return None, None
    if scope == "conversation":
        return conversation_id, None
    if scope == "workspace":
        if not workspace_path:
            raise MemoryScopeError("workspace 记忆必须指定一个已授权目录")
        return None, workspace_path
    raise MemoryScopeError(f"未知记忆作用域: {scope}")


async def default_workspace_path(session: AsyncSession, *, conversation_id: UUID) -> str | None:
    """当前会话的首个授权目录——和相对路径解析用的是同一个"当前工作目录"语义。"""

    roots = await list_session_roots(session, conversation_id=conversation_id)
    return roots[0].canonical_path if roots else None


async def remember(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    scope: MemoryScope,
    content: str,
    key: str | None = None,
    workspace_path: str | None = None,
    source: Literal["agent", "user"] = "agent",
) -> tuple[CoworkMemoryRecord, CoworkMemoryRecord | None]:
    """写入一条记忆；带 key 时同作用域内更新而不是再堆一条。

    返回 (当前记录, 被覆盖前的记录)。第二项供客户端的「撤销」还原旧文本——只在 key
    命中已有记忆时才非空。
    """

    normalized = _normalize_content(content)
    normalized_key = _normalize_key(key)
    bound_conversation, bound_workspace = resolve_binding(
        scope, conversation_id=conversation_id, workspace_path=workspace_path
    )
    store = configured_cowork_store()
    if store is not None:
        return await store.remember_cowork_memory(
            scope=scope,
            conversation_id=bound_conversation,
            workspace_path=bound_workspace,
            key=normalized_key,
            content=normalized,
            source=source,
        )

    previous: CoworkMemoryRecord | None = None
    if normalized_key is not None:
        existing = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT {_COLUMNS}
                        FROM cowork_memories
                        WHERE scope = :scope
                          AND conversation_id IS NOT DISTINCT FROM :conversation_id
                          AND workspace_path IS NOT DISTINCT FROM :workspace_path
                          AND key = :key
                          AND forgotten_at IS NULL
                        FOR UPDATE
                        """
                    ),
                    {
                        "scope": scope,
                        "conversation_id": bound_conversation,
                        "workspace_path": bound_workspace,
                        "key": normalized_key,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            previous = _record(existing)
            updated = (
                (
                    await session.execute(
                        text(
                            f"""
                            UPDATE cowork_memories
                            SET content = :content, source = :source, updated_at = now()
                            WHERE id = :id
                            RETURNING {_COLUMNS}
                            """
                        ),
                        {"id": previous.id, "content": normalized, "source": source},
                    )
                )
                .mappings()
                .one()
            )
            return _record(updated), previous

    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO cowork_memories
                        (id, scope, conversation_id, workspace_path, key, content, source)
                    VALUES
                        (:id, :scope, :conversation_id, :workspace_path, :key, :content, :source)
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "scope": scope,
                    "conversation_id": bound_conversation,
                    "workspace_path": bound_workspace,
                    "key": normalized_key,
                    "content": normalized,
                    "source": source,
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row), None


async def update_memory(
    session: AsyncSession,
    *,
    memory_id: UUID,
    content: str | None = None,
    restore: bool = False,
) -> tuple[CoworkMemoryRecord, CoworkMemoryRecord]:
    """改写或恢复一条记忆，返回 (新记录, 旧记录)。

    `restore=True` 用于撤销 forget：清掉 `forgotten_at`。恢复一条 key 已经被新记忆占用的
    记忆会撞唯一索引，这时报错比静默产生两条同 key 记忆好。
    """

    store = configured_cowork_store()
    if store is not None:
        return await store.update_cowork_memory(
            memory_id=memory_id,
            content=None if content is None else _normalize_content(content),
            restore=restore,
        )

    previous_row = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM cowork_memories WHERE id = :id FOR UPDATE"),
                {"id": memory_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if previous_row is None:
        raise MemoryNotFoundError(str(memory_id))
    previous = _record(previous_row)
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE cowork_memories
                    SET content = COALESCE(:content, content),
                        forgotten_at = CASE WHEN :restore THEN NULL ELSE forgotten_at END,
                        updated_at = now()
                    WHERE id = :id
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": memory_id,
                    "content": None if content is None else _normalize_content(content),
                    "restore": restore,
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row), previous


async def forget_memory(
    session: AsyncSession, *, memory_id: UUID
) -> CoworkMemoryRecord | None:
    """软删除。已经删过的返回 None，让重复调用是幂等的而不是报错。"""

    store = configured_cowork_store()
    if store is not None:
        return await store.forget_cowork_memory(memory_id=memory_id)
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE cowork_memories
                    SET forgotten_at = now(), updated_at = now()
                    WHERE id = :id AND forgotten_at IS NULL
                    RETURNING {_COLUMNS}
                    """
                ),
                {"id": memory_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _record(row)


async def get_memory(session: AsyncSession, *, memory_id: UUID) -> CoworkMemoryRecord | None:
    store = configured_cowork_store()
    if store is not None:
        return await store.get_cowork_memory(memory_id=memory_id)
    row = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM cowork_memories WHERE id = :id"),
                {"id": memory_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _record(row)


async def list_memories(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    workspace_paths: list[str],
    include_forgotten: bool = False,
    limit: int = 200,
) -> list[CoworkMemoryRecord]:
    """列出当前会话可见的记忆：global + 本会话授权目录的 workspace + 本会话。

    别的会话的 conversation 记忆和别的目录的 workspace 记忆一律不可见——记忆是上下文
    注入，作用域漏了就是把无关的事实喂给模型。
    """

    if not 1 <= limit <= 500:
        raise MemoryScopeError("记忆条数上限必须位于 1 到 500")
    store = configured_cowork_store()
    if store is not None:
        return await store.list_cowork_memories(
            conversation_id=conversation_id,
            workspace_paths=workspace_paths,
            include_forgotten=include_forgotten,
            limit=limit,
        )
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS}
                    FROM cowork_memories
                    WHERE (:include_forgotten OR forgotten_at IS NULL)
                      AND (
                        scope = 'global'
                        OR (scope = 'conversation' AND conversation_id = :conversation_id)
                        OR (
                            scope = 'workspace'
                            AND :has_paths
                            AND workspace_path = ANY(CAST(:workspace_paths AS TEXT[]))
                        )
                      )
                    ORDER BY updated_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "workspace_paths": workspace_paths,
                    "has_paths": bool(workspace_paths),
                    "include_forgotten": include_forgotten,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    return [_record(row) for row in rows]


async def load_visible_memories(
    session: AsyncSession, *, conversation_id: UUID, limit: int = 200
) -> list[CoworkMemoryRecord]:
    """runtime 用的便捷入口：自己解析当前会话的授权目录再查。"""

    roots = await list_session_roots(session, conversation_id=conversation_id)
    return await list_memories(
        session,
        conversation_id=conversation_id,
        workspace_paths=[root.canonical_path for root in roots],
        limit=limit,
    )


def render_memory_block(
    memories: list[CoworkMemoryRecord],
    *,
    max_chars: int,
    preview_chars: int,
) -> str:
    """把记忆钉进 system prompt。

    单条超长就截断并标注，让模型用 `memory_read` 取全文——全量注入会让一条几千字的
    记忆吃掉整个上下文预算。总长超限时丢最久没更新的：最近更新过的更可能仍然相关。
    """

    active = [item for item in memories if item.forgotten_at is None]
    if not active or max_chars <= 0:
        return ""
    header = (
        "<known_memories>\n"
        "这些是你在以往会话中记下的长期事实，可以直接当作已知前提使用。\n"
        "发现某条已经过时就用 memory_update 改写、用 memory_forget retire，"
        "不要在旁边再记一条新的。\n"
    )
    footer = "</known_memories>"
    lines: list[str] = []
    used = len(header) + len(footer)
    for item in active:
        body = item.content
        truncated = len(body) > preview_chars
        if truncated:
            body = body[:preview_chars] + f"…（已截断，用 memory_read 取全文，共 {len(item.content)} 字）"
        line = f"[{item.scope}] [#{item.id}] {body}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return header + "\n".join(lines) + "\n" + footer


def memory_payload(record: CoworkMemoryRecord) -> dict[str, Any]:
    """事件与工具结果共用的序列化形状。"""

    return {
        "id": str(record.id),
        "scope": record.scope,
        "key": record.key,
        "content": record.content,
        "source": record.source,
        "workspace_path": record.workspace_path,
        "forgotten": record.forgotten_at is not None,
        "updated_at": _iso(record.updated_at),
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def normalize_scope(value: str) -> MemoryScope:
    if value not in MEMORY_SCOPES:
        raise MemoryScopeError(f"未知记忆作用域: {value}")
    return cast("MemoryScope", value)
