"""Cowork 长期记忆：跨会话保留的用户偏好与项目约定。

**为什么不复用 RAG 的 memory。** 除了 `rag ⊥ cowork`（ADR-0011）这条硬约束之外，两者
要解决的问题也不同：RAG 的 memory 做时序有效性建模（ADR-0005），关心"这条事实在哪段
时间成立"；Cowork 需要的是"用户偏好 Markdown 报告"这类当前有效的轻量事实。把后者塞进
前者的模型里，会得到一堆没有时间语义的退化记录。

**为什么不下沉到 agent_core。** 存储要按 ADR-0010 走 `cowork_store()` 双后端
路由，而 `agent_core` 不许依赖 `cowork_store`。等到 RAG 也需要同一套东西时再谈下沉——
现在下沉只会造出一个单一使用者的抽象。

作用域三档，绑定关系由 DB 的 CHECK 约束兜底：
- `global`：跨所有会话的用户偏好
- `workspace`：绑定某个已授权目录的规范化路径，即"这个项目的规矩"
- `conversation`：只在当前会话有效
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork.permissions import list_session_roots
from app.cowork_contracts import (
    CoworkMemoryRecord as CoworkMemoryRecord,
)
from app.cowork_contracts import (
    MemoryCategory as MemoryCategory,
)
from app.cowork_contracts import (
    MemoryExtractionJob as MemoryExtractionJob,
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
from app.cowork_contracts import (
    PinnedMemoryError as PinnedMemoryError,
)
from app.cowork_store.routing import cowork_store

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
    store = cowork_store()
    return await store.remember_cowork_memory(
        scope=scope,
        conversation_id=bound_conversation,
        workspace_path=bound_workspace,
        key=normalized_key,
        content=normalized,
        source=source,
    )


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

    store = cowork_store()
    return await store.update_cowork_memory(
        memory_id=memory_id,
        content=None if content is None else _normalize_content(content),
        restore=restore,
    )


async def forget_memory(
    session: AsyncSession, *, memory_id: UUID
) -> CoworkMemoryRecord | None:
    """软删除。已经删过的返回 None，让重复调用是幂等的而不是报错。"""

    store = cowork_store()
    return await store.forget_cowork_memory(memory_id=memory_id)


async def get_memory(session: AsyncSession, *, memory_id: UUID) -> CoworkMemoryRecord | None:
    store = cowork_store()
    return await store.get_cowork_memory(memory_id=memory_id)


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
    store = cowork_store()
    return await store.list_cowork_memories(
        conversation_id=conversation_id,
        workspace_paths=workspace_paths,
        include_forgotten=include_forgotten,
        limit=limit,
    )


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


# ---- 策展与时序有效性 ----------------------------------------------------------
#
# 这一段是原来 `app/rag/memory/store.py` 合并进来的。两套记忆并存的理由（模块开头
# 那段"为什么不复用 RAG 的 memory"）在 pgvector 与 RAG 主答路径退役之后不成立了：
# RAG 侧剩下的只是时序有效性建模本身，而那正是这一套缺的东西。
#
# 合并后的分工参照两个开源实现：**openworker** 的记忆是扁平的 scope/key/content，
# 模型靠 prompt 里的 `[#id]` 直接改写某一条——没有历史；**DeepTutor** 是三层
# markdown 文档，靠 LLM 定期重新合并，历史体现在文档修订里。WorkPilot 取中间：
# 存储扁平（openworker 那半），但改写走"失效 + 接替"而不是覆盖（ADR-0005），
# 于是记忆面板能给出「当前 / 历史」两个视图，模型改错了看得见它改了什么。
#
# **向量召回没有搬过来。** 原来靠 embedding 找相似记忆做去重判定；pgvector 退役后
# 改成 openworker 的做法：把最近 N 条活跃记忆直接放进 prompt 让模型指名道姓地选。


@dataclass(frozen=True)
class MemoryWrite:
    """一次 ADD/UPDATE/DELETE/NOOP 的落地结果。

    `applied` 是"这次操作有没有写进去"，`current_changed` 是"当前生效的那条有没有变"。
    两者会分叉：一条迟到的旧事实会被记成历史（applied=True），但不该把更新的当前状态
    顶掉（current_changed=False）。
    """

    applied: bool
    current_changed: bool
    memory: CoworkMemoryRecord | None


def _require_store() -> Any:
    store = cowork_store()
    if store is None:  # pragma: no cover - 只有配置成 postgres 后端才可能
        raise MemoryScopeError("长期记忆只在本机 SQLite 存储上可用")
    return store


async def list_curated_memories(*, active: bool, limit: int = 500) -> list[CoworkMemoryRecord]:
    """记忆面板的两个视图：当前生效 / 已失效的历史。"""

    return cast(
        "list[CoworkMemoryRecord]",
        await _require_store().list_cowork_memories_by_validity(active=active, limit=limit),
    )


async def get_curated_memory(memory_id: UUID) -> CoworkMemoryRecord | None:
    return cast(
        "CoworkMemoryRecord | None", await _require_store().get_cowork_memory(memory_id=memory_id)
    )


async def get_active_successor(memory_id: UUID) -> CoworkMemoryRecord | None:
    """顺着 `superseded_by` 一路走到当前仍然生效的那条。

    面板上"这条被什么取代了"要指向活的记忆，而不是链条中间某个同样已失效的版本。
    """

    seen: set[UUID] = set()
    current = await get_curated_memory(memory_id)
    while current is not None and current.superseded_by is not None:
        if current.superseded_by in seen:  # pragma: no cover - 环只可能来自手工改库
            return None
        seen.add(current.superseded_by)
        current = await get_curated_memory(current.superseded_by)
    return None if current is None or not current.active else current


async def set_memory_pinned(*, memory_id: UUID, pinned: bool) -> CoworkMemoryRecord:
    record = await _require_store().set_cowork_memory_pinned(memory_id=memory_id, pinned=pinned)
    if record is None:
        raise MemoryNotFoundError(str(memory_id))
    return cast("CoworkMemoryRecord", record)


async def mark_memories_used(memory_ids: list[UUID]) -> None:
    await _require_store().touch_cowork_memories(memory_ids=memory_ids)


async def apply_memory_operation(
    *,
    operation: Literal["ADD", "UPDATE", "DELETE", "NOOP"],
    category: MemoryCategory,
    fact: str,
    confidence: float,
    valid_from: datetime,
    actor: Literal["model", "manual"],
    source_message_id: UUID | None = None,
    run_id: UUID | None = None,
    target_id: UUID | None = None,
    pinned: bool | None = None,
) -> MemoryWrite:
    """把一次记忆决策落进存储。

    四种操作共用一条纪律：**不覆盖，只失效**。UPDATE 先写新记忆再把旧的标失效并指向
    新的；DELETE 只标失效不写新的；NOOP 只记一次命中。

    乱序处理：如果 `valid_from` 早于目标记忆的生效时刻，说明这是一条迟到的旧事实——
    它照样入库（历史要完整），但当前状态不动，`current_changed=False`。
    """

    store = _require_store()
    if operation == "NOOP":
        if target_id is not None:
            await store.touch_cowork_memories(memory_ids=[target_id])
            return MemoryWrite(True, False, await get_curated_memory(target_id))
        return MemoryWrite(False, False, None)

    target = None if target_id is None else await get_curated_memory(target_id)
    if operation in {"UPDATE", "DELETE"}:
        if target is None:
            raise MemoryNotFoundError(str(target_id))
        if target.pinned:
            raise PinnedMemoryError("置顶记忆不能被自动改写或失效")

    if operation == "DELETE":
        assert target is not None
        if target.valid_from is not None and valid_from < target.valid_from:
            # 迟到的删除不能把一条更新的事实抹掉。
            return MemoryWrite(False, False, target)
        await store.supersede_cowork_memory(
            memory_id=target.id, successor_id=None, invalid_at=valid_from
        )
        return MemoryWrite(True, True, await get_curated_memory(target.id))

    # ADD / UPDATE 都要写一条新记忆。
    stale = (
        target is not None
        and target.valid_from is not None
        and valid_from < target.valid_from
    )
    created, _ = await store.remember_cowork_memory(
        scope="global",
        conversation_id=None,
        workspace_path=None,
        key=None,
        content=fact,
        source="agent" if actor == "model" else "user",
        category=category,
        confidence=confidence,
        pinned=bool(pinned),
        valid_from=valid_from,
        source_message_id=source_message_id,
        run_id=run_id,
    )
    if stale:
        assert target is not None
        # 迟到的旧事实直接以"已失效"的形态入库，接替者就是当前那条。
        await store.supersede_cowork_memory(
            memory_id=created.id,
            successor_id=target.id,
            invalid_at=cast(datetime, target.valid_from),
        )
        return MemoryWrite(True, False, await get_curated_memory(created.id))
    if target is not None:
        await store.supersede_cowork_memory(
            memory_id=target.id, successor_id=created.id, invalid_at=valid_from
        )
    return MemoryWrite(True, True, await get_curated_memory(created.id))


# ---- 抽取作业 ------------------------------------------------------------------


async def schedule_memory_extraction(
    *,
    run_id: UUID,
    conversation_id: UUID | None,
    source_message_id: UUID | None,
    content: str,
    source_created_at: datetime,
) -> MemoryExtractionJob | None:
    return cast(
        "MemoryExtractionJob | None",
        await _require_store().schedule_memory_extraction(
            run_id=run_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            content=content,
            source_created_at=source_created_at,
        ),
    )


async def claim_memory_job(
    *, job_id: UUID, worker_id: str, lease_s: int, max_attempts: int
) -> MemoryExtractionJob | None:
    return cast(
        "MemoryExtractionJob | None",
        await _require_store().claim_memory_job(
            job_id=job_id, worker_id=worker_id, lease_s=lease_s, max_attempts=max_attempts
        ),
    )


async def complete_memory_job(*, job_id: UUID, worker_id: str) -> bool:
    return bool(await _require_store().complete_memory_job(job_id=job_id, worker_id=worker_id))


async def retry_or_fail_memory_job(
    *, job_id: UUID, worker_id: str, error: str, max_attempts: int, retry_delay_s: int
) -> str | None:
    return cast(
        "str | None",
        await _require_store().retry_or_fail_memory_job(
            job_id=job_id,
            worker_id=worker_id,
            error=error,
            max_attempts=max_attempts,
            retry_delay_s=retry_delay_s,
        ),
    )


async def list_dispatchable_memory_jobs(
    *, max_attempts: int, limit: int = 100
) -> list[tuple[UUID, int]]:
    return cast(
        "list[tuple[UUID, int]]",
        await _require_store().list_dispatchable_memory_jobs(
            max_attempts=max_attempts, limit=limit
        ),
    )
