"""固定综述图使用的只读数据库/模型工具。"""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import CompletionClient
from app.agent.state import ReviewCard, ReviewDocument, ReviewGroup
from app.llm.types import Message

CARD_SYSTEM_PROMPT = """你是论文卡片抽取器。只能使用给定文档，不得补充外部知识。
输出一个 JSON 对象，字段固定为：
{"core_problem":"","method_family":"","method":"","findings":[],"limitations":[],"evidence_quotes":[]}
findings/limitations 是简洁字符串数组；evidence_quotes 必须逐字摘自文档，用来审计卡片结论。
不要输出 Markdown、代码围栏或额外文字。"""

COMPARE_SYSTEM_PROMPT = """你是严谨的文献比较器。只根据结构化卡片，比较共同问题、方法差异、
实验结论与局限；不得补充卡片外事实。输出中文 Markdown，不要写综述引言或虚构引用。"""

REVIEW_SYSTEM_PROMPT = """你是个人知识库的综述写作者。只使用给定卡片和比较结果，围绕用户目标
生成结构清楚的中文 Markdown 综述。必须保留文档标题，明确共同点、差异和局限；不得补充外部事实。"""


class CardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_problem: str = Field(min_length=1, max_length=1000)
    method_family: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(min_length=1, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    evidence_quotes: list[str] = Field(min_length=1, max_length=8)


class ReviewToolResponseError(ValueError):
    pass


class DatabaseModelReviewTools:
    """真实只读工具；所有模型调用仍经过统一 ModelGateway。

    这里声明的是 `CompletionClient` 而不是 `ModelGateway`，为的是强制注入
    `BudgetedGateway`——工具层拿不到未计量的网关，任何新增节点都自动被熔断覆盖。
    """

    def __init__(
        self,
        session: AsyncSession,
        gateway: CompletionClient,
        *,
        max_document_chars: int = 30_000,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.max_document_chars = max_document_chars

    async def list_documents(self, document_ids: list[str]) -> list[ReviewDocument]:
        ids = [UUID(item) for item in document_ids]
        rows = (
            (
                await self.session.execute(
                    text(
                        """
                        SELECT d.id AS document_id, v.id AS version_id, d.title, d.source_uri
                        FROM documents d
                        JOIN document_versions v ON v.document_id = d.id
                        WHERE d.id = ANY(:document_ids)
                          AND d.deleted_at IS NULL
                          AND v.activated_at IS NOT NULL
                          AND v.invalid_at IS NULL
                          AND v.parse_status = 'done'
                        """
                    ),
                    {"document_ids": ids},
                )
            )
            .mappings()
            .all()
        )
        by_id = {str(row["document_id"]): row for row in rows}
        return [
            {
                "document_id": item,
                "version_id": str(by_id[item]["version_id"]),
                "title": str(by_id[item]["title"]),
                "source_uri": str(by_id[item]["source_uri"]),
            }
            for item in document_ids
            if item in by_id
        ]

    async def extract_card(self, document: ReviewDocument) -> ReviewCard:
        full_text = (
            await self.session.execute(
                text(
                    """
                    SELECT full_text FROM document_versions
                    WHERE id = :version_id AND activated_at IS NOT NULL
                      AND invalid_at IS NULL AND parse_status = 'done'
                    """
                ),
                {"version_id": UUID(document["version_id"])},
            )
        ).scalar_one_or_none()
        if not isinstance(full_text, str) or not full_text.strip():
            raise LookupError(f"文档没有可抽取正文: {document['document_id']}")
        excerpt = _bounded_document(full_text, self.max_document_chars)
        result = await self.gateway.complete(
            [
                Message(role="system", content=CARD_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(
                        {"title": document["title"], "document": excerpt},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ],
            task_type="agent_extract_card",
            max_tokens=1200,
            temperature=0.0,
        )
        payload = parse_card_payload(result.text)
        missing_quotes = [quote for quote in payload.evidence_quotes if quote not in full_text]
        if missing_quotes:
            raise ReviewToolResponseError("卡片 evidence_quotes 不是文档逐字摘录")
        return {
            "document_id": document["document_id"],
            "title": document["title"],
            "core_problem": payload.core_problem.strip(),
            "method_family": payload.method_family.strip(),
            "method": payload.method.strip(),
            "findings": [item.strip() for item in payload.findings if item.strip()],
            "limitations": [item.strip() for item in payload.limitations if item.strip()],
            "evidence_quotes": [item.strip() for item in payload.evidence_quotes if item.strip()],
        }

    async def group_cards(self, cards: list[ReviewCard]) -> list[ReviewGroup]:
        groups: dict[str, list[str]] = {}
        for card in cards:
            name = card["method_family"].strip() or "未分类"
            groups.setdefault(name, []).append(card["document_id"])
        return [
            {"name": name, "document_ids": document_ids}
            for name, document_ids in sorted(groups.items())
        ]

    async def compare_documents(
        self, cards: list[ReviewCard], groups: list[ReviewGroup]
    ) -> str:
        result = await self.gateway.complete(
            [
                Message(role="system", content=COMPARE_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(
                        {"cards": cards, "groups": groups},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ],
            task_type="agent_compare_documents",
            max_tokens=1800,
            temperature=0.0,
        )
        if not result.text.strip():
            raise ReviewToolResponseError("文献比较结果为空")
        return result.text.strip()

    async def generate_review(
        self,
        *,
        goal: str,
        cards: list[ReviewCard],
        groups: list[ReviewGroup],
        comparison: str,
    ) -> str:
        result = await self.gateway.complete(
            [
                Message(role="system", content=REVIEW_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(
                        {
                            "goal": goal,
                            "cards": cards,
                            "groups": groups,
                            "comparison": comparison,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ],
            task_type="agent_generate_review",
            max_tokens=3000,
            temperature=0.0,
        )
        if not result.text.strip():
            raise ReviewToolResponseError("综述生成结果为空")
        return result.text.strip()


def parse_card_payload(value: str) -> CardPayload:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            return CardPayload.model_validate(payload)
        except ValidationError as error:
            raise ReviewToolResponseError(f"卡片响应 schema 非法: {error}") from error
    raise ReviewToolResponseError("卡片响应不是 JSON 对象")


def _bounded_document(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{value[:head]}\n\n[中间正文因预算截断]\n\n{value[-tail:]}"

