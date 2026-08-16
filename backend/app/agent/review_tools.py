"""固定综述图使用的只读数据库/模型工具。"""

from __future__ import annotations

import json
from typing import Literal
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


CardErrorStyle = Literal["none", "generic", "model_facing"]

# 通用错误：只说"错了"，不说错在哪、也不说怎么改。A1 的对照组。
_GENERIC_REPAIR = "上一次输出不合法，请重新输出。"


class ReviewToolResponseError(ValueError):
    """工具校验失败。

    `model_message` 是写给模型看的可执行指令（约束 4）：指出**哪一条**不合法、
    违反了哪条约束、下一步具体怎么做。`str(error)` 仍是给人和日志看的简述。
    """

    def __init__(self, message: str, *, model_message: str | None = None) -> None:
        super().__init__(message)
        self.model_message = model_message or message


def repair_instruction(error: ReviewToolResponseError, style: CardErrorStyle) -> str:
    if style == "model_facing":
        return error.model_message
    return _GENERIC_REPAIR


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
        card_repair_attempts: int = 2,
        card_error_style: CardErrorStyle = "model_facing",
        card_system_prompt: str = CARD_SYSTEM_PROMPT,
        card_max_tokens: int = 2400,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.max_document_chars = max_document_chars
        # A1 实测：1200 会把长文档的卡片截断在半句话上，报出来是"不是 JSON 对象"，
        # 看着像模型不听话，其实是预算不够。与 E6 的 Judge max_tokens 是同一类坑。
        self.card_max_tokens = card_max_tokens
        self.card_system_prompt = card_system_prompt
        # 补救轮数与错误信息风格是 A1 的两个旋钮；线上默认走面向模型的错误信息。
        self.card_repair_attempts = card_repair_attempts
        self.card_error_style = card_error_style
        # 每次 extract_card 的逐轮结果，供实验按轮次统计 recovery_rate。
        self.card_trace: list[list[str]] = []

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
        messages = [
            Message(role="system", content=self.card_system_prompt),
            Message(
                role="user",
                content=json.dumps(
                    {"title": document["title"], "document": excerpt},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]
        # 逐字校验只对模型真正看过的那段文本成立：拿全文去核对，会把"截断没给它看"
        # 的部分也算成模型编造，冤枉的是工具而不是模型。
        payload = await self._extract_card_payload(messages, excerpt)
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

    async def _extract_card_payload(
        self, messages: list[Message], source_text: str
    ) -> CardPayload:
        """抽卡 + 失败后补救。补救信息的风格是 A1 的唯一自变量。"""

        attempts = 0 if self.card_error_style == "none" else self.card_repair_attempts
        trace: list[str] = []
        conversation = list(messages)
        last_error: ReviewToolResponseError | None = None
        for _ in range(attempts + 1):
            result = await self.gateway.complete(
                conversation,
                task_type="agent_extract_card",
                max_tokens=self.card_max_tokens,
                temperature=0.0,
            )
            try:
                payload = parse_card_payload(result.text)
                _assert_verbatim_quotes(payload, source_text)
            except ReviewToolResponseError as error:
                trace.append(str(error))
                last_error = error
                conversation = [
                    *conversation,
                    Message(role="assistant", content=result.text),
                    Message(
                        role="user",
                        content=repair_instruction(error, self.card_error_style),
                    ),
                ]
                continue
            trace.append("ok")
            self.card_trace.append(trace)
            return payload
        self.card_trace.append(trace)
        assert last_error is not None
        raise last_error

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
            raise ReviewToolResponseError(
                f"卡片响应 schema 非法: {error}",
                model_message=_schema_repair_message(error),
            ) from error
    raise ReviewToolResponseError(
        "卡片响应不是 JSON 对象",
        model_message=(
            "上一次输出里没有找到可解析的 JSON 对象。"
            "请只输出一个 JSON 对象，不要加 Markdown 代码围栏、说明文字或前后缀。"
        ),
    )


def _schema_repair_message(error: ValidationError) -> str:
    """把 pydantic 的报错翻译成模型能照做的指令（约束 4）。

    直接把 `ValidationError` 塞回去是最省事也最没用的做法：它讲的是 Python 类型系统，
    而模型需要知道的是"哪个字段、要填什么、填多少条"。
    """

    lines: list[str] = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "(根对象)"
        hint = _FIELD_HINTS.get(str(item["loc"][0]) if item["loc"] else "", "")
        lines.append(f"- {field}：{item['msg']}。{hint}".rstrip())
    listed = "\n".join(lines)
    return (
        f"上一次输出有 {len(error.errors())} 处不符合要求：\n{listed}\n"
        "注意 system 提示里的 JSON 是**字段模板**，不是要照抄的内容——"
        "每个字段都必须换成你从文档中读到的真实内容，不能留空字符串或空数组。"
        "重新输出完整的 JSON 对象。"
    )


_FIELD_HINTS = {
    "core_problem": "用一句话写这篇文档要解决的核心问题。",
    "method_family": "写方法所属的大类，例如“检索增强”“多智能体”。",
    "method": "写具体做法，不要只写方法名。",
    "findings": "至少 1 条、最多 10 条结论。",
    "limitations": "最多 10 条局限；确实没有可以给空数组。",
    "evidence_quotes": "至少 1 条、最多 8 条，逐字摘自文档。",
}


def _assert_verbatim_quotes(payload: CardPayload, source_text: str) -> None:
    """`evidence_quotes` 必须逐字摘自模型看过的正文。

    这条校验是卡片可审计性的地基：quote 一旦允许改写，卡片结论就没法回到原文核对，
    整条综述链路的溯源承诺随之作废。
    """

    missing = [quote for quote in payload.evidence_quotes if quote not in source_text]
    if not missing:
        return
    listed = "\n".join(f"- {quote}" for quote in missing)
    raise ReviewToolResponseError(
        "卡片 evidence_quotes 不是文档逐字摘录",
        model_message=(
            f"下面 {len(missing)} 条 evidence_quotes 在文档里找不到完全相同的文本：\n"
            f"{listed}\n"
            "请只保留能从文档中原样复制的片段：逐字复制，包括标点、空格、大小写与数字格式，"
            "不要改写、不要合并两处、不要补全或删节。"
            "如果某条结论找不到可逐字引用的原文，就换一条能引用的结论，"
            "而不是把它改写成近似的句子。其余字段保持不变，重新输出完整 JSON 对象。"
        ),
    )


def _bounded_document(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{value[:head]}\n\n[中间正文因预算截断]\n\n{value[-tail:]}"

