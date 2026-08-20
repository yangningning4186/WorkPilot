from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.general_answer import SYSTEM_PROMPT, stream_general_answer
from app.rag.grounded_answer import (
    SYSTEM_PROMPT as GROUNDED_SYSTEM_PROMPT,
)
from app.rag.grounded_answer import (
    _build_user_prompt,
)
from app.rag.memory.prompt import MEMORY_USAGE_POLICY
from app.rag.memory.recall import recall_memory_context
from app.rag.memory.store import apply_memory_operation, get_memory
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


async def _add_manual_memory(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    fact: str,
    pinned: bool = False,
):
    result = await gateway.embed([fact], task_type="memory_test_embedding")
    write = await apply_memory_operation(
        session,
        operation="ADD",
        category="preference",
        fact=fact,
        confidence=1.0,
        valid_from=datetime.now(UTC),
        actor="manual",
        source_message_id=None,
        embedding=result.embeddings[0],
        embedding_model=gateway.embedding_model,
        embedding_provider=gateway.embedding_provider,
        embedding_revision=gateway.embedding_revision,
        pinned=pinned,
    )
    assert write.memory is not None
    return write.memory


async def test_recall_puts_pinned_first_deduplicates_and_marks_usage(
    db_session: AsyncSession,
) -> None:
    provider = DeterministicProvider()
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        embedding_revision="memory-test",
    )
    pinned = await _add_manual_memory(
        db_session,
        gateway,
        fact="回答时先给结论",
        pinned=True,
    )
    relevant = await _add_manual_memory(
        db_session,
        gateway,
        fact="偏好简洁回答",
    )

    recalled = await recall_memory_context(
        db_session,
        gateway,
        query="请简洁回答",
        top_k=5,
        pinned_limit=3,
        max_chars=300,
    )

    assert [memory.id for memory in recalled.memories][:2] == [pinned.id, relevant.id]
    assert recalled.text.startswith("<user_context>\n")
    assert recalled.text.endswith("</user_context>")
    assert "- 回答时先给结论" in recalled.text
    assert "[M" not in recalled.text
    assert "[preference]" not in recalled.text
    assert recalled.text.count(str(pinned.fact)) == 1
    assert len(recalled.text) <= 300
    refreshed = await get_memory(db_session, pinned.id)
    assert refreshed is not None
    assert refreshed.access_count == 1


async def test_memory_context_is_user_data_and_does_not_change_system_prompt() -> None:
    provider = DeterministicProvider(completion_text="通用知识回答")
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        embedding_revision="memory-test",
    )
    context = "<user_context>\n- 偏好简洁回答\n</user_context>"

    chunks = [
        chunk
        async for chunk in stream_general_answer(
            gateway,
            query="解释 RAG",
            memory_context=context,
        )
    ]

    assert "".join(chunks) == "通用知识回答"
    assert provider.last_messages[0].content == SYSTEM_PROMPT
    assert MEMORY_USAGE_POLICY in SYSTEM_PROMPT
    assert MEMORY_USAGE_POLICY in GROUNDED_SYSTEM_PROMPT
    assert "本次回答没有资料库证据" in SYSTEM_PROMPT
    assert "且 user_context 也没有相关信息" in SYSTEM_PROMPT
    assert provider.last_messages[1].content.startswith(context)
    grounded_prompt = _build_user_prompt("解释 RAG", [], memory_context=context)
    assert grounded_prompt.startswith(context)
    assert "问题:\n解释 RAG" in grounded_prompt
