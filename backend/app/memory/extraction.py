from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.gateway import ModelGateway
from app.llm.types import Message
from app.memory.store import (
    MEMORY_CATEGORIES,
    MEMORY_OPERATIONS,
    MemoryCategory,
    MemoryJobSource,
    MemoryOperation,
    MemoryRecord,
    apply_memory_operation,
    search_active_memories,
)

EXTRACTION_SYSTEM_PROMPT = """你是个人助手的长期记忆候选抽取器。
只抽取用户明确表达、未来对其他任务仍有帮助的稳定信息：身份背景、长期项目或兴趣、输出偏好、可复用事实。
不要抽取当前问题本身、一次性要求、寒暄、助手说过的话、从语气推测出的属性、密码令牌或其他敏感凭据。
用户明确否认或改变旧信息时，保留否定/变化语义，交给下一步冲突分类器判断。
只输出 JSON，不要 Markdown：
{"facts":[{"category":"preference|profile|interest|fact","fact":"独立完整的中文事实","confidence":0.0}]}
没有值得长期保存的信息时输出 {"facts":[]}。最多 6 条。"""

CLASSIFICATION_SYSTEM_PROMPT = """你是长期记忆冲突分类器。
比较一条新候选与召回的现有记忆，只能选择一个操作：
ADD：全新且不冲突；UPDATE：新事实替代或修正某条现有事实；DELETE：用户明确否认某条现有事实且没有替代事实；NOOP：语义已存在。
不得因为主题相似就 UPDATE；补充信息通常是 ADD。只有 UPDATE、DELETE、NOOP 可以填写 target_memory_id，且必须来自给定列表。
只输出 JSON，不要 Markdown：
{"operation":"ADD|UPDATE|DELETE|NOOP","target_memory_id":null,"reason":"不超过100字"}"""

REPAIR_PROMPT = "上一条不符合 JSON 契约。请只输出合法 JSON，不要代码围栏或解释。"


class MemoryExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryCandidate:
    category: MemoryCategory
    fact: str
    confidence: float


@dataclass(frozen=True)
class MemoryDecision:
    operation: MemoryOperation
    target_memory_id: UUID | None
    reason: str


async def extract_memory_candidates(
    gateway: ModelGateway,
    *,
    user_message: str,
    max_tokens: int = 600,
) -> list[MemoryCandidate]:
    if not user_message.strip():
        return []
    messages = [
        Message(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        Message(
            role="user",
            content=json.dumps(
                {"user_message": user_message.strip()},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    ]
    for attempt in range(2):
        completion = await gateway.complete(
            messages,
            task_type="memory_op",
            max_tokens=max_tokens,
            temperature=0.0,
        )
        try:
            return parse_memory_candidates(completion.text)
        except MemoryExtractionError:
            if attempt == 1:
                raise
            messages.extend(
                (
                    Message(role="assistant", content=completion.text),
                    Message(role="user", content=REPAIR_PROMPT),
                )
            )
    raise MemoryExtractionError("记忆候选抽取没有返回结果")  # pragma: no cover


async def classify_memory_candidate(
    gateway: ModelGateway,
    *,
    candidate: MemoryCandidate,
    existing: list[MemoryRecord],
    max_tokens: int = 300,
) -> MemoryDecision:
    if not existing:
        return MemoryDecision(operation="ADD", target_memory_id=None, reason="没有相近记忆")
    allowed_ids = {item.id for item in existing}
    payload = {
        "candidate": {
            "category": candidate.category,
            "fact": candidate.fact,
            "confidence": candidate.confidence,
        },
        "existing_memories": [
            {
                "memory_id": str(item.id),
                "category": item.category,
                "fact": item.fact,
                "pinned": item.pinned,
            }
            for item in existing
        ],
    }
    messages = [
        Message(role="system", content=CLASSIFICATION_SYSTEM_PROMPT),
        Message(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    ]
    for attempt in range(2):
        completion = await gateway.complete(
            messages,
            task_type="memory_op",
            max_tokens=max_tokens,
            temperature=0.0,
        )
        try:
            return parse_memory_decision(completion.text, allowed_ids=allowed_ids)
        except MemoryExtractionError:
            if attempt == 1:
                raise
            messages.extend(
                (
                    Message(role="assistant", content=completion.text),
                    Message(role="user", content=REPAIR_PROMPT),
                )
            )
    raise MemoryExtractionError("记忆分类没有返回结果")  # pragma: no cover


async def process_memory_job_source(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    source: MemoryJobSource,
) -> list[dict[str, Any]]:
    candidates = await extract_memory_candidates(gateway, user_message=source.content)
    operations: list[dict[str, Any]] = []
    for candidate in candidates:
        embedding_result = await gateway.embed([candidate.fact], task_type="memory_embedding")
        embedding = embedding_result.embeddings[0]
        existing = await search_active_memories(
            session,
            embedding=embedding,
            embedding_model=gateway.embedding_model,
            embedding_provider=gateway.embedding_provider,
            embedding_revision=gateway.embedding_revision,
            top_k=5,
        )
        decision = await classify_memory_candidate(
            gateway,
            candidate=candidate,
            existing=existing,
        )
        protected_target = next(
            (item for item in existing if item.id == decision.target_memory_id), None
        )
        if (
            protected_target is not None
            and protected_target.pinned
            and decision.operation in {"UPDATE", "DELETE"}
        ):
            operations.append(
                {
                    "operation": decision.operation,
                    "applied": False,
                    "target_memory_id": str(protected_target.id),
                    "memory_id": None,
                    "category": candidate.category,
                    "fact": candidate.fact,
                    "confidence": candidate.confidence,
                    "reason": "目标记忆已置顶，自动失效被阻止",
                }
            )
            continue
        write = await apply_memory_operation(
            session,
            operation=decision.operation,
            category=candidate.category,
            fact=candidate.fact,
            confidence=candidate.confidence,
            valid_from=source.message_created_at,
            actor="model",
            source_message_id=source.job.source_message_id,
            embedding=embedding,
            embedding_model=gateway.embedding_model,
            embedding_provider=gateway.embedding_provider,
            embedding_revision=gateway.embedding_revision,
            target_id=decision.target_memory_id,
        )
        operations.append(
            {
                "operation": decision.operation,
                "applied": write.applied,
                "current_changed": write.current_changed,
                "target_memory_id": (
                    None if decision.target_memory_id is None else str(decision.target_memory_id)
                ),
                "memory_id": None if write.memory is None else str(write.memory.id),
                "category": candidate.category,
                "fact": candidate.fact,
                "confidence": candidate.confidence,
                "reason": (
                    decision.reason
                    if write.current_changed or decision.operation == "NOOP"
                    else "事件时间早于当前记忆，未反向覆盖当前状态"
                ),
            }
        )
    return operations


def parse_memory_candidates(value: str) -> list[MemoryCandidate]:
    payload = _extract_json_object(value)
    facts = payload.get("facts")
    if not isinstance(facts, list):
        raise MemoryExtractionError("记忆候选响应缺少 facts 数组")
    if len(facts) > 6:
        raise MemoryExtractionError("单条消息最多抽取 6 条记忆")
    candidates: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()
    for raw in facts:
        if not isinstance(raw, dict):
            raise MemoryExtractionError("facts 元素必须是对象")
        category = raw.get("category")
        fact = raw.get("fact")
        confidence = raw.get("confidence")
        if not isinstance(category, str) or category not in MEMORY_CATEGORIES:
            raise MemoryExtractionError("记忆候选 category 无效")
        if not isinstance(fact, str):
            raise MemoryExtractionError("记忆候选 fact 必须是字符串")
        normalized = " ".join(fact.split())
        if not normalized or len(normalized) > 2000:
            raise MemoryExtractionError("记忆候选 fact 长度无效")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise MemoryExtractionError("记忆候选 confidence 必须是数字")
        score = float(confidence)
        if not 0 <= score <= 1:
            raise MemoryExtractionError("记忆候选 confidence 必须位于 0 到 1")
        key = (category, normalized.casefold())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            MemoryCandidate(
                category=category,  # type: ignore[arg-type]
                fact=normalized,
                confidence=score,
            )
        )
    return candidates


def parse_memory_decision(value: str, *, allowed_ids: set[UUID]) -> MemoryDecision:
    payload = _extract_json_object(value)
    operation = payload.get("operation")
    target_raw = payload.get("target_memory_id")
    reason = payload.get("reason")
    if not isinstance(operation, str) or operation not in MEMORY_OPERATIONS:
        raise MemoryExtractionError("记忆分类 operation 无效")
    if not isinstance(reason, str) or not reason.strip():
        raise MemoryExtractionError("记忆分类缺少 reason")
    target: UUID | None
    if target_raw is None:
        target = None
    elif isinstance(target_raw, str):
        try:
            target = UUID(target_raw)
        except ValueError as error:
            raise MemoryExtractionError("target_memory_id 不是 UUID") from error
    else:
        raise MemoryExtractionError("target_memory_id 必须是字符串或 null")
    typed_operation: MemoryOperation = operation  # type: ignore[assignment]
    if typed_operation == "ADD":
        if target is not None:
            raise MemoryExtractionError("ADD 不能指定 target_memory_id")
    elif target is None or target not in allowed_ids:
        raise MemoryExtractionError(f"{typed_operation} 必须引用给定的现有记忆")
    return MemoryDecision(
        operation=typed_operation,
        target_memory_id=target,
        reason=reason.strip()[:500],
    )


def _extract_json_object(value: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise MemoryExtractionError("记忆响应不是 JSON 对象")
