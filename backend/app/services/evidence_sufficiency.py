import json
from dataclasses import dataclass
from typing import TypedDict

from app.llm.gateway import ModelGateway
from app.llm.types import Message
from app.retrieval.citations import EvidenceSegment

SYSTEM_PROMPT = """你是知识库问答的证据充分性门控器。
判断给定证据是否明确包含回答问题所需的全部事实, 而不是判断证据主题是否相关。
如果问题要求具体数字、版本、实验设置、比较对象或多个子问题, 证据缺少任意一项就判为不足。
不得用外部知识、常识或推测补齐缺失信息。检索分数只能提示排序置信度, 不能替代正文证据。
只输出一个 JSON 对象, 不要 Markdown, 不要附加解释:
{"sufficient":false,"reason":"不超过100字的依据","support_ids":[],"missing_aspects":["缺失项"]}
证据充分时 sufficient=true, support_ids 必须列出直接支撑答案的证据标签, missing_aspects 为空。
证据不足时 sufficient=false, missing_aspects 列出问题要求但证据未明确提供的事实。"""


class EvidenceAssessmentError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceAssessment:
    sufficient: bool
    reason: str
    support_ids: list[str]
    missing_aspects: list[str]
    model: str
    provider: str


class ParsedEvidenceAssessment(TypedDict):
    sufficient: bool
    reason: str
    support_ids: list[str]
    missing_aspects: list[str]


async def assess_evidence_sufficiency(
    gateway: ModelGateway,
    *,
    query: str,
    evidence: list[EvidenceSegment],
    top_score: float,
    second_score: float | None,
    score_margin: float | None,
    low_margin: bool,
    max_tokens: int = 300,
) -> EvidenceAssessment:
    if not evidence:
        raise ValueError("证据不能为空")
    payload = {
        "question": query.strip(),
        "retrieval_signals": {
            "top_score": top_score,
            "second_score": second_score,
            "top1_top2_margin": score_margin,
            "low_margin": low_margin,
        },
        "evidence": [
            {
                "citation_id": item.citation_id,
                "title": item.title,
                "heading_path": item.heading_path,
                "content": item.quote,
            }
            for item in evidence
        ],
    }
    completion = await gateway.complete(
        [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        ],
        task_type="evidence_sufficiency",
        max_tokens=max_tokens,
        temperature=0.0,
    )
    parsed = parse_evidence_assessment(
        completion.text,
        allowed_ids={item.citation_id for item in evidence},
    )
    return EvidenceAssessment(
        **parsed,
        model=completion.model,
        provider=completion.provider,
    )


def parse_evidence_assessment(value: str, *, allowed_ids: set[str]) -> ParsedEvidenceAssessment:
    payload = _extract_json_object(value)
    sufficient = payload.get("sufficient")
    reason = payload.get("reason")
    support_ids = payload.get("support_ids")
    missing_aspects = payload.get("missing_aspects")
    if not isinstance(sufficient, bool):
        raise EvidenceAssessmentError("证据门控响应缺少布尔 sufficient")
    if not isinstance(reason, str) or not reason.strip():
        raise EvidenceAssessmentError("证据门控响应缺少 reason")
    if not isinstance(support_ids, list) or not all(isinstance(item, str) for item in support_ids):
        raise EvidenceAssessmentError("证据门控 support_ids 必须是字符串数组")
    if not isinstance(missing_aspects, list) or not all(
        isinstance(item, str) for item in missing_aspects
    ):
        raise EvidenceAssessmentError("证据门控 missing_aspects 必须是字符串数组")
    unique_support_ids = list(dict.fromkeys(support_ids))
    unknown = [item for item in unique_support_ids if item not in allowed_ids]
    if unknown:
        raise EvidenceAssessmentError(f"证据门控引用未知标签: {', '.join(unknown)}")
    if sufficient and not unique_support_ids:
        raise EvidenceAssessmentError("证据充分时必须给出 support_ids")
    return {
        "sufficient": sufficient,
        "reason": reason.strip()[:500],
        "support_ids": unique_support_ids,
        "missing_aspects": [item.strip()[:200] for item in missing_aspects if item.strip()][:10],
    }


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
    raise EvidenceAssessmentError("证据门控响应不是 JSON 对象")
