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
from app.retrieval.citations import Citation
from app.services.grounded_answer import answer_with_citations

# 中文按句号断句最自然; 英文与代码退化为定长切片。
_BREAK_CHARS = "。！？；\n"  # noqa: RUF001 - 中文全角标点是断句依据, 不能替换成半角


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
) -> AsyncIterator[AnswerStreamEvent]:
    """默认实现: 复用 answer_with_citations 的完整判定链, 再切片产出。

    刻意不在这里重写一条"流式版检索+拒答+生成": 那会让同一个产品行为有两条
    实现路径, 评测只覆盖其中一条(约束 6)。代价是首 token 延迟等于整答生成延迟;
    等拒答链路稳定后, 把这里换成 gateway.stream() 即可, 对外协议不变。
    """

    result = await answer_with_citations(
        session,
        gateway,
        query=query,
        top_k=top_k,
        refusal_threshold=settings.refusal_threshold,
        refusal_margin_threshold=settings.refusal_margin_threshold,
        max_evidence_chars=settings.answer_max_evidence_chars,
        max_tokens=settings.answer_max_tokens,
    )
    for piece in split_deltas(result.answer, max_chars=settings.run_delta_flush_chars):
        yield AnswerDelta(text=piece)
    yield AnswerFinished(
        answer=result.answer,
        citations=result.citations,
        refused=result.refused,
        refusal_reason=result.refusal_reason,
    )
