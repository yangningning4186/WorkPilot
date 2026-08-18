"""把一次问答拆成可流式产出的事件序列。

worker 只认这里的事件类型, 不认 grounded_answer 的内部结构: 检索、拒答阈值、
证据充分性门控怎么演进, 都不该反过来改动 run/SSE 协议层。
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.retrieval.citations import REFUSAL_TEXT, Citation
from app.services.general_answer import stream_general_answer
from app.services.grounded_answer import (
    GroundedAnswerResult,
    stream_answer_with_settings,
)

# 中文按句号断句最自然; 英文与代码退化为定长切片。
_BREAK_CHARS = "。！？；\n"


@dataclass(frozen=True)
class AnswerDelta:
    text: str


@dataclass(frozen=True)
class AnswerFinished:
    """终止事件只带 run/SSE 层真正要落库和上报的字段。

    刻意不直接传 GroundedAnswerResult: 那个结果对象会随拒答链路(证据充分性门控、
    查询分解……)不断长字段, 每长一次都改一遍协议层和它的测试是没有必要的耦合。
    """

    answer: str
    citations: list[Citation]
    refused: bool
    refusal_reason: str | None
    # 这条回答是否基于资料库。通用知识模式为 False, 前端据此挂免责标识;
    # 默认 True 让既有调用点不必改。
    grounded: bool = True


AnswerStreamEvent = AnswerDelta | AnswerFinished


class AnswerProducer(Protocol):
    def __call__(
        self,
        session: AsyncSession,
        gateway: ModelGateway,
        *,
        query: str,
        top_k: int,
        settings: Settings,
        memory_context: str = "",
        conversation_context: str = "",
        retrieval_query: str | None = None,
    ) -> AsyncIterator[AnswerStreamEvent]: ...


def split_deltas(text: str, *, max_chars: int) -> Iterator[str]:
    """按标点优先切片, 保证每片非空且不超过 max_chars。"""

    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind(char) for char in _BREAK_CHARS)
            if cut > 0:
                end = start + cut + 1
        yield text[start:end]
        start = end


async def produce_answer(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    top_k: int,
    settings: Settings,
    memory_context: str = "",
    conversation_context: str = "",
    retrieval_query: str | None = None,
) -> AsyncIterator[AnswerStreamEvent]:
    """真流式: 模型吐一段就发一段, 首 token 延迟不再等整答生成完。

    判定链在生成之前跑完, 所以门控拒答时一个字都不会流出去。唯一需要防的是
    **模型自己写出拒答句**: 那句话是哨兵不是正文, 泄漏出去前端就会既显示
    "资料库中未找到相关信息。" 又显示拒答卡片。因此开头先攒一段, 确认不是哨兵再放行。
    """

    result: GroundedAnswerResult | None = None
    held = ""
    releasing = False

    async for item in stream_answer_with_settings(
        session,
        gateway,
        query=query,
        top_k=top_k,
        settings=settings,
        memory_context=memory_context,
        conversation_context=conversation_context,
        retrieval_query=retrieval_query,
    ):
        if isinstance(item, GroundedAnswerResult):
            result = item
            continue
        if releasing:
            for piece in split_deltas(item, max_chars=settings.run_delta_flush_chars):
                yield AnswerDelta(text=piece)
            continue
        held += item
        if REFUSAL_TEXT.startswith(held.strip()):
            # 还可能长成整句拒答, 继续攒。
            continue
        releasing = True
        for piece in split_deltas(held.lstrip(), max_chars=settings.run_delta_flush_chars):
            yield AnswerDelta(text=piece)
        held = ""

    if result is None:  # pragma: no cover - 生成器契约保证最后一项是结果
        raise RuntimeError("答案生成没有产出结果对象")
    if held and not result.refused:
        # 短答案整条都在缓冲里, 且确认不是拒答句。
        for piece in split_deltas(held.strip(), max_chars=settings.run_delta_flush_chars):
            yield AnswerDelta(text=piece)
    yield AnswerFinished(
        answer=result.answer,
        citations=result.citations,
        refused=result.refused,
        refusal_reason=result.refusal_reason,
    )


async def produce_general_answer(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    top_k: int,
    settings: Settings,
    memory_context: str = "",
    conversation_context: str = "",
    retrieval_query: str | None = None,
) -> AsyncIterator[AnswerStreamEvent]:
    """通用知识模式: 跳过检索, 产出不可溯源的回答。

    签名与 `produce_answer` 一致(AnswerProducer), worker 因此不需要知道两种模式的差别,
    只按 run 上记录的 answer_mode 选一个 producer。
    """

    del session, top_k, retrieval_query  # 通用知识模式不检索
    parts: list[str] = []
    async for chunk in stream_general_answer(
        gateway,
        query=query,
        memory_context=memory_context,
        conversation_context=conversation_context,
        max_tokens=settings.general_answer_max_tokens,
    ):
        parts.append(chunk)
        for piece in split_deltas(chunk, max_chars=settings.run_delta_flush_chars):
            yield AnswerDelta(text=piece)
    yield AnswerFinished(
        answer="".join(parts).strip(),
        citations=[],
        refused=False,
        refusal_reason=None,
        grounded=False,
    )
