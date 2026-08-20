"""受约束 Agentic RAG 的纯规划与候选选择组件。

这里只放无数据库副作用的状态转换，供 P1-L 离线实验复用。生产问答链路不导入本模块。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.rag.retrieval.dense import DenseSearchHit
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import Message

PLAN_SYSTEM_PROMPT = """你是知识库多跳检索规划器。
判断问题是否需要多个独立事实才能回答。需要时拆成 2 到 4 个可独立验证的子事实；
每个子事实必须保留实体名、版本、指标、比较对象等限定词，并列出问题中明确出现的实体和文档名提示。
不得猜测答案，不得引入原问题没有的实体或文档。
简单单事实问题不要拆分。
只输出一个 JSON 对象，不要 Markdown：
{"decomposed":true,"reason":"简短原因","requirements":[
  {"id":"R1","query":"可直接检索的自包含问题","entities":["实体"],"document_hints":["文档名"]}
]}
无需拆分时输出 requirements=[]。"""


class AgenticPlanError(ValueError):
    pass


@dataclass(frozen=True)
class RetrievalRequirement:
    id: str
    query: str
    entities: tuple[str, ...]
    document_hints: tuple[str, ...]

    @property
    def retrieval_query(self) -> str:
        suffix = [*self.entities, *self.document_hints]
        if not suffix:
            return self.query
        return f"{self.query} {' '.join(suffix)}"


@dataclass(frozen=True)
class AgenticPlan:
    decomposed: bool
    reason: str
    requirements: tuple[RetrievalRequirement, ...]
    model: str | None
    provider: str | None


async def plan_agentic_retrieval(
    gateway: ModelGateway,
    *,
    query: str,
    max_requirements: int = 4,
    max_tokens: int = 500,
) -> AgenticPlan:
    if not query.strip():
        raise ValueError("query 不能为空")
    if not 2 <= max_requirements <= 8:
        raise ValueError("max_requirements 必须位于 2 到 8")
    completion = await gateway.complete(
        [
            Message(role="system", content=PLAN_SYSTEM_PROMPT),
            Message(
                role="user",
                content=json.dumps(
                    {"question": query.strip()},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ],
        task_type="query_decomposition",
        max_tokens=max_tokens,
        temperature=0.0,
    )
    decomposed, reason, requirements = parse_agentic_plan(
        completion.text,
        original_query=query,
        max_requirements=max_requirements,
    )
    return AgenticPlan(
        decomposed=decomposed,
        reason=reason,
        requirements=requirements,
        model=completion.model,
        provider=completion.provider,
    )


def fallback_agentic_plan(query: str, *, reason: str) -> AgenticPlan:
    if not query.strip():
        raise ValueError("query 不能为空")
    return AgenticPlan(
        decomposed=False,
        reason=reason[:500],
        requirements=(),
        model=None,
        provider=None,
    )


def parse_agentic_plan(
    value: str,
    *,
    original_query: str,
    max_requirements: int = 4,
) -> tuple[bool, str, tuple[RetrievalRequirement, ...]]:
    payload = _extract_json_object(value)
    decomposed = payload.get("decomposed")
    reason = payload.get("reason")
    raw_requirements = payload.get("requirements")
    if not isinstance(decomposed, bool):
        raise AgenticPlanError("规划响应缺少布尔 decomposed")
    if not isinstance(reason, str) or not reason.strip():
        raise AgenticPlanError("规划响应缺少 reason")
    if not isinstance(raw_requirements, list):
        raise AgenticPlanError("规划响应 requirements 必须是数组")
    if not decomposed:
        if raw_requirements:
            raise AgenticPlanError("无需分解时 requirements 必须为空")
        return False, reason.strip()[:500], ()

    requirements: list[RetrievalRequirement] = []
    seen_queries = {_key(original_query)}
    for index, raw in enumerate(raw_requirements):
        if not isinstance(raw, dict):
            raise AgenticPlanError("requirement 必须是对象")
        query = _text(raw.get("query"), field="requirement.query", max_chars=4000)
        query_key = _key(query)
        if query_key in seen_queries:
            continue
        seen_queries.add(query_key)
        requirement_id = raw.get("id")
        if not isinstance(requirement_id, str) or not requirement_id.strip():
            requirement_id = f"R{index + 1}"
        requirements.append(
            RetrievalRequirement(
                id=requirement_id.strip()[:32],
                query=query,
                entities=_string_tuple(raw.get("entities"), max_items=8),
                document_hints=_string_tuple(raw.get("document_hints"), max_items=6),
            )
        )
        if len(requirements) >= max_requirements:
            break
    if len(requirements) < 2:
        raise AgenticPlanError("多跳规划至少需要 2 个有效子事实")
    return True, reason.strip()[:500], tuple(requirements)


def rank_documents_with_hints(
    ranked_documents: list[DenseSearchHit],
    requirement: RetrievalRequirement,
) -> list[DenseSearchHit]:
    """只对明确文档名做稳定前置，不拿泛实体硬改文档排序。"""

    hints = tuple(_key(value) for value in requirement.document_hints if _key(value))
    if not hints:
        return list(ranked_documents)
    indexed = list(enumerate(ranked_documents))
    indexed.sort(
        key=lambda row: (
            0 if any(hint in _key(row[1].title) for hint in hints) else 1,
            row[0],
        )
    )
    return [hit for _, hit in indexed]


def select_documents_for_requirements(
    requirement_documents: list[list[DenseSearchHit]],
    original_documents: list[DenseSearchHit],
    *,
    max_documents: int = 3,
) -> list[DenseSearchHit]:
    """先给每个子事实一个文档名额，再按原问题文档排名补满。"""

    if max_documents < 1:
        raise ValueError("max_documents 必须为正数")
    selected: list[DenseSearchHit] = []
    seen: set[UUID] = set()
    max_rank = max((len(items) for items in requirement_documents), default=0)
    for rank in range(max_rank):
        for documents in requirement_documents:
            if rank >= len(documents):
                continue
            hit = documents[rank]
            if hit.version_id in seen:
                continue
            selected.append(hit)
            seen.add(hit.version_id)
            if len(selected) >= max_documents:
                return selected
    for hit in original_documents:
        if hit.version_id in seen:
            continue
        selected.append(hit)
        seen.add(hit.version_id)
        if len(selected) >= max_documents:
            break
    return selected


def section_navigation_candidates(
    local_ranking: list[DenseSearchHit],
    *,
    top_n: int = 10,
    section_seed_k: int = 2,
    neighbor_radius: int = 1,
) -> list[DenseSearchHit]:
    """围绕局部 dense 头部的章节与相邻 chunk 扩展，再按局部排名补满。"""

    if top_n < 1 or section_seed_k < 1 or neighbor_radius < 0:
        raise ValueError("章节导航参数非法")
    if not local_ranking:
        return []
    by_position = sorted(local_ranking, key=lambda hit: (hit.char_start, str(hit.chunk_id)))
    position = {hit.chunk_id: index for index, hit in enumerate(by_position)}
    seeds: list[DenseSearchHit] = []
    seen_sections: set[tuple[str, ...]] = set()
    for hit in local_ranking:
        section = tuple(hit.heading_path)
        if section in seen_sections:
            continue
        seen_sections.add(section)
        seeds.append(hit)
        if len(seeds) >= section_seed_k:
            break

    selected: list[DenseSearchHit] = []
    selected_ids: set[UUID] = set()

    def add(hit: DenseSearchHit) -> None:
        if hit.chunk_id not in selected_ids and len(selected) < top_n:
            selected.append(hit)
            selected_ids.add(hit.chunk_id)

    for seed in seeds:
        add(seed)
        index = position[seed.chunk_id]
        for offset in range(1, neighbor_radius + 1):
            if index - offset >= 0:
                add(by_position[index - offset])
            if index + offset < len(by_position):
                add(by_position[index + offset])
        section = tuple(seed.heading_path)
        for hit in local_ranking:
            if tuple(hit.heading_path) == section:
                add(hit)
            if len(selected) >= top_n:
                return selected
    for hit in local_ranking:
        add(hit)
        if len(selected) >= top_n:
            break
    return selected


def round_robin_candidates(
    rankings: list[list[DenseSearchHit]], *, max_candidates: int
) -> list[DenseSearchHit]:
    if max_candidates < 1:
        raise ValueError("max_candidates 必须为正数")
    selected: list[DenseSearchHit] = []
    seen: set[UUID] = set()
    max_rank = max((len(ranking) for ranking in rankings), default=0)
    for rank in range(max_rank):
        for ranking in rankings:
            if rank >= len(ranking):
                continue
            hit = ranking[rank]
            if hit.chunk_id in seen:
                continue
            selected.append(hit)
            seen.add(hit.chunk_id)
            if len(selected) >= max_candidates:
                return selected
    return selected


def evidence_ledger_top_k(
    requirement_rankings: list[list[DenseSearchHit]],
    fallback_ranking: list[DenseSearchHit],
    *,
    top_k: int = 5,
) -> list[DenseSearchHit]:
    """每个子事实先占一个不同证据，再用原问题排序补满。"""

    if top_k < 1:
        raise ValueError("top_k 必须为正数")
    selected: list[DenseSearchHit] = []
    seen: set[UUID] = set()
    for ranking in requirement_rankings:
        chosen = next((hit for hit in ranking if hit.chunk_id not in seen), None)
        if chosen is None:
            continue
        selected.append(chosen)
        seen.add(chosen.chunk_id)
        if len(selected) >= top_k:
            return selected
    for hit in fallback_ranking:
        if hit.chunk_id in seen:
            continue
        selected.append(hit)
        seen.add(hit.chunk_id)
        if len(selected) >= top_k:
            break
    return selected


def build_missing_requirement(
    original_query: str,
    missing_aspects: list[str],
    *,
    entities: tuple[str, ...] = (),
    document_hints: tuple[str, ...] = (),
) -> RetrievalRequirement:
    missing = [" ".join(value.split()).strip() for value in missing_aspects if value.strip()]
    if not missing:
        raise ValueError("缺失子事实不能为空")
    query = f"{original_query.strip()}；仅检索以下缺失事实：{'；'.join(missing[:6])}"
    return RetrievalRequirement(
        id="R_missing",
        query=query[:4000],
        entities=tuple(dict.fromkeys(entities))[:8],
        document_hints=tuple(dict.fromkeys(document_hints))[:6],
    )


def _extract_json_object(value: str) -> dict[str, Any]:
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
    raise AgenticPlanError("规划响应不是 JSON 对象")


def _text(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgenticPlanError(f"{field} 不能为空")
    return " ".join(value.split()).strip()[:max_chars]


def _string_tuple(value: object, *, max_items: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgenticPlanError("entities/document_hints 必须是字符串数组")
    normalized = (" ".join(item.split()).strip()[:200] for item in value)
    return tuple(dict.fromkeys(item for item in normalized if item))[:max_items]


def _key(value: str) -> str:
    return "".join(value.casefold().split())
