"""固定综述图使用的只读数据库/模型工具。"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.budget import CompletionClient
from app.rag.review.state import ReviewCard, ReviewDocument, ReviewGroup
from workpilot_ai.types import Message

CARD_SYSTEM_PROMPT = """你是论文卡片抽取器。只能使用给定文档，不得补充外部知识。
输出一个 JSON 对象，字段固定为：
{"core_problem":"","method_family":"","method":"","findings":[],"limitations":[],"evidence_refs":[]}
findings 为 1-10 条、limitations 为 0-10 条简洁字符串；evidence_refs 为 1-8 个文档中
真实存在的 E 编号。只选择编号，不要复制或改写证据文本；服务端会按编号回填逐字原文。
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
    # `evidence_refs` 是线上协议：模型只做离散选择，服务端负责回填原文。
    # `evidence_quotes` 仅保留给旧 checkpoint / A1、A2 实验回放使用。
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    evidence_quotes: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def require_one_evidence_protocol(self) -> CardPayload:
        if bool(self.evidence_refs) == bool(self.evidence_quotes):
            raise ValueError("evidence_refs 与 evidence_quotes 必须且只能填写一个")
        return self


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
        evidence_catalog = build_evidence_catalog(excerpt)
        messages = [
            Message(role="system", content=self.card_system_prompt),
            Message(
                role="user",
                content=json.dumps(
                    {
                        "title": document["title"],
                        "document": [
                            {"ref": item.ref, "text": item.text} for item in evidence_catalog
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]
        # 逐字校验只对模型真正看过的那段文本成立：拿全文去核对，会把"截断没给它看"
        # 的部分也算成模型编造，冤枉的是工具而不是模型。
        payload = await self._extract_card_payload(
            messages,
            excerpt,
            evidence_catalog={item.ref: item for item in evidence_catalog},
        )
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
        self,
        messages: list[Message],
        source_text: str,
        *,
        evidence_catalog: dict[str, EvidenceAnchor] | None = None,
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
                resolved_quotes = resolve_card_evidence(
                    payload, source_text, evidence_catalog=evidence_catalog
                )
                # 进入图状态前统一降解成既有 ReviewCard 协议，避免新旧字段同时非空。
                payload.evidence_refs = []
                payload.evidence_quotes = resolved_quotes
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

    async def compare_documents(self, cards: list[ReviewCard], groups: list[ReviewGroup]) -> str:
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
    "evidence_refs": "至少 1 个、最多 8 个，只能填写文档中真实存在的 E 编号。",
    "evidence_quotes": "兼容字段；新输出不要填写，改用 evidence_refs。",
}


@dataclass(frozen=True)
class EvidenceAnchor:
    """模型 quote 在原文中的可审计锚点。"""

    quote: str
    char_start: int
    char_end: int
    match_kind: Literal["exact", "layout_normalized"]

    @property
    def ref(self) -> str:
        return f"E{self.char_start:05d}"

    @property
    def text(self) -> str:
        return self.quote


def build_evidence_catalog(source_text: str, *, max_entry_chars: int = 400) -> list[EvidenceAnchor]:
    """把正文切成带稳定字符偏移编号的原文候选，不制造任何新文本。"""

    entries: list[EvidenceAnchor] = []
    cursor = 0
    for line in source_text.splitlines(keepends=True):
        content_end = len(line.rstrip("\r\n"))
        stripped = line[:content_end].strip()
        if not stripped or stripped == "[中间正文因预算截断]":
            cursor += len(line)
            continue
        left_padding = len(line[:content_end]) - len(line[:content_end].lstrip())
        start = cursor + left_padding
        remaining = stripped
        while remaining:
            piece = remaining[:max_entry_chars]
            piece_start = start
            piece_end = piece_start + len(piece)
            entries.append(
                EvidenceAnchor(
                    quote=source_text[piece_start:piece_end],
                    char_start=piece_start,
                    char_end=piece_end,
                    match_kind="exact",
                )
            )
            remaining = remaining[len(piece) :]
            start = piece_end
        cursor += len(line)
    return entries


def _layout_key_with_offsets(value: str) -> tuple[str, list[tuple[int, int]]]:
    """只消除无语义的版式差异，同时保留归一化字符到原文区间的映射。

    NFKC 处理全角/半角等 Unicode 兼容形式；空白和软连字符属于排版产物，
    可以忽略。标点、数字和正文字符仍必须相同，绝不做语义相似匹配。
    """

    normalized: list[str] = []
    offsets: list[tuple[int, int]] = []
    for index, character in enumerate(value):
        if character.isspace() or character == "\u00ad":
            continue
        for normalized_character in unicodedata.normalize("NFKC", character):
            normalized.append(normalized_character)
            offsets.append((index, index + 1))
    return "".join(normalized), offsets


def anchor_evidence_quote(quote: str, source_text: str) -> EvidenceAnchor | None:
    """把一条 quote 映射回模型可见正文的精确字符区间。"""

    candidate = quote.strip()
    if not candidate:
        return None
    exact_start = source_text.find(candidate)
    if exact_start >= 0:
        return EvidenceAnchor(
            quote=source_text[exact_start : exact_start + len(candidate)],
            char_start=exact_start,
            char_end=exact_start + len(candidate),
            match_kind="exact",
        )

    source_key, source_offsets = _layout_key_with_offsets(source_text)
    quote_key, _ = _layout_key_with_offsets(candidate)
    if not quote_key:
        return None
    normalized_start = source_key.find(quote_key)
    if normalized_start < 0:
        return None
    # 归一化匹配若落到多个位置，不能确定模型指向哪一段原文，fail closed。
    if source_key.find(quote_key, normalized_start + 1) >= 0:
        return None
    normalized_end = normalized_start + len(quote_key) - 1
    char_start = source_offsets[normalized_start][0]
    char_end = source_offsets[normalized_end][1]
    return EvidenceAnchor(
        quote=source_text[char_start:char_end],
        char_start=char_start,
        char_end=char_end,
        match_kind="layout_normalized",
    )


def resolve_evidence_quotes(quotes: list[str], source_text: str) -> list[str]:
    """校验并回填原文切片；返回值中的每一条都可用精确区间审计。"""

    anchors = [anchor_evidence_quote(quote, source_text) for quote in quotes]
    missing = [quote for quote, anchor in zip(quotes, anchors, strict=True) if anchor is None]
    if not missing:
        return [anchor.quote for anchor in anchors if anchor is not None]

    listed = "\n".join(f"- {quote}" for quote in missing)
    raise ReviewToolResponseError(
        "卡片 evidence_quotes 不是文档逐字摘录",
        model_message=(
            f"下面 {len(missing)} 条 evidence_quotes 无法锚定到文档原文：\n"
            f"{listed}\n"
            "请只保留能从文档中逐字复制的原样片段：不得改写、合并两处、补全或删节。"
            "空白、换行和全角/半角差异可以由工具还原，除此之外的标点、数字与正文字符"
            "必须一致。如果某条结论找不到原文，就换一条能引用的结论。"
            "其余字段保持不变，重新输出完整 JSON 对象。"
        ),
    )


def resolve_card_evidence(
    payload: CardPayload,
    source_text: str,
    *,
    evidence_catalog: dict[str, EvidenceAnchor] | None = None,
) -> list[str]:
    """解析新编号协议或兼容旧 quote 协议，最终只返回原文切片。"""

    if payload.evidence_refs:
        catalog = evidence_catalog or {}
        unknown = [ref for ref in payload.evidence_refs if ref not in catalog]
        duplicate = len(set(payload.evidence_refs)) != len(payload.evidence_refs)
        if unknown or duplicate:
            details = []
            if unknown:
                details.append("不存在的编号：" + "、".join(unknown))
            if duplicate:
                details.append("编号有重复")
            raise ReviewToolResponseError(
                "卡片 evidence_refs 无法解析",
                model_message=(
                    "；".join(details) + "。请从当前文档给出的 E 编号中选择 1-8 个不同编号，"
                    "不要创造编号，也不要填写 evidence_quotes；重新输出完整 JSON 对象。"
                ),
            )
        return [catalog[ref].quote for ref in payload.evidence_refs]
    return resolve_evidence_quotes(payload.evidence_quotes, source_text)


def _assert_verbatim_quotes(payload: CardPayload, source_text: str) -> None:
    """兼容旧调用点；校验成功时把 quote 回填成精确原文切片。

    这条校验是卡片可审计性的地基：quote 一旦允许改写，卡片结论就没法回到原文核对，
    整条综述链路的溯源承诺随之作废。
    """

    payload.evidence_quotes = resolve_evidence_quotes(payload.evidence_quotes, source_text)


def _bounded_document(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    head = max_chars * 2 // 3
    tail = max_chars - head
    return f"{value[:head]}\n\n[中间正文因预算截断]\n\n{value[-tail:]}"
