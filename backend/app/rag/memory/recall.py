from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.memory.prompt import (
    MEMORY_CONTEXT_PREFIX,
    MEMORY_CONTEXT_SUFFIX,
    escape_memory_fact,
)
from app.rag.memory.store import (
    MemoryRecord,
    list_pinned_memories,
    mark_memories_used,
    search_active_memories,
)
from workpilot_ai.gateway import ModelGateway


@dataclass(frozen=True)
class RecalledMemoryContext:
    memories: list[MemoryRecord]
    text: str


async def recall_memory_context(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    top_k: int = 5,
    pinned_limit: int = 3,
    max_chars: int = 2000,
) -> RecalledMemoryContext:
    if not query.strip():
        return RecalledMemoryContext(memories=[], text="")
    if max_chars < 200:
        raise ValueError("memory context max_chars 不能小于 200")
    pinned = await list_pinned_memories(session, limit=pinned_limit)
    embedding_result = await gateway.embed([query], task_type="memory_recall_embedding")
    relevant = await search_active_memories(
        session,
        embedding=embedding_result.embeddings[0],
        embedding_model=gateway.embedding_model,
        embedding_provider=gateway.embedding_provider,
        embedding_revision=gateway.embedding_revision,
        top_k=top_k,
    )
    candidates = [*pinned, *relevant]
    unique: list[MemoryRecord] = []
    seen = set()
    for memory in candidates:
        if memory.id in seen:
            continue
        seen.add(memory.id)
        unique.append(memory)

    lines: list[str] = []
    selected: list[MemoryRecord] = []
    used_chars = len(MEMORY_CONTEXT_PREFIX) + len(MEMORY_CONTEXT_SUFFIX)
    for memory in unique:
        # 类别和编号只是内部检索元数据；放进 prompt 会诱导模型
        # 向用户复述“根据记忆 [M1]”，又没有任何产品价值。
        line = f"- {escape_memory_fact(memory.fact)}\n"
        if used_chars + len(line) > max_chars:
            continue
        lines.append(line)
        selected.append(memory)
        used_chars += len(line)
    if not selected:
        return RecalledMemoryContext(memories=[], text="")
    await mark_memories_used(session, [memory.id for memory in selected])
    return RecalledMemoryContext(
        memories=selected,
        text=MEMORY_CONTEXT_PREFIX + "".join(lines) + MEMORY_CONTEXT_SUFFIX,
    )
