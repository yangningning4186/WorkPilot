import json
from dataclasses import dataclass

from app.llm.gateway import ModelGateway
from app.llm.types import Message

SYSTEM_PROMPT = """你是知识库检索查询规划器。
判断问题是否需要从多个证据片段拼出答案。只有在问题包含多个独立事实、比较项、跨文档关系或明确多跳关系时才分解。
分解后的每条查询必须自包含、适合直接检索, 并保留实体名、版本、指标名等限定词。
不得猜测答案或引入原问题没有的事实。
简单单事实问题不要改写。
只输出一个 JSON 对象, 不要 Markdown, 不要额外解释:
{"decomposed":false,"reason":"简短原因","queries":[]}
需要分解时 decomposed=true, queries 给出 2 到 4 条互补的子查询; 不要把原问题原样放进 queries。"""


class QueryDecompositionError(ValueError):
    pass


@dataclass(frozen=True)
class QueryPlan:
    queries: list[str]
    decomposed: bool
    reason: str
    model: str | None
    provider: str | None


async def plan_retrieval_queries(
    gateway: ModelGateway,
    *,
    query: str,
    max_subqueries: int = 4,
    max_tokens: int = 300,
) -> QueryPlan:
    original = query.strip()
    if not original:
        raise ValueError("query 不能为空")
    if not 2 <= max_subqueries <= 8:
        raise ValueError("max_subqueries 必须位于 2 到 8")
    completion = await gateway.complete(
        [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=json.dumps(
                    {"question": original}, ensure_ascii=False, separators=(",", ":")
                ),
            ),
        ],
        task_type="query_decomposition",
        max_tokens=max_tokens,
        temperature=0.0,
    )
    decomposed, reason, subqueries = parse_query_plan(
        completion.text,
        original_query=original,
        max_subqueries=max_subqueries,
    )
    return QueryPlan(
        queries=[original, *subqueries],
        decomposed=decomposed,
        reason=reason,
        model=completion.model,
        provider=completion.provider,
    )


def fallback_query_plan(query: str, *, reason: str) -> QueryPlan:
    original = query.strip()
    if not original:
        raise ValueError("query 不能为空")
    return QueryPlan(
        queries=[original],
        decomposed=False,
        reason=reason[:500],
        model=None,
        provider=None,
    )


def parse_query_plan(
    value: str,
    *,
    original_query: str,
    max_subqueries: int = 4,
) -> tuple[bool, str, list[str]]:
    payload = _extract_json_object(value)
    decomposed = payload.get("decomposed")
    reason = payload.get("reason")
    queries = payload.get("queries")
    if not isinstance(decomposed, bool):
        raise QueryDecompositionError("查询规划响应缺少布尔 decomposed")
    if not isinstance(reason, str) or not reason.strip():
        raise QueryDecompositionError("查询规划响应缺少 reason")
    if not isinstance(queries, list) or not all(isinstance(item, str) for item in queries):
        raise QueryDecompositionError("查询规划 queries 必须是字符串数组")

    original_key = _query_key(original_query)
    unique: list[str] = []
    seen = {original_key}
    for item in queries:
        normalized = " ".join(item.split()).strip()[:4000]
        key = _query_key(normalized)
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
        if len(unique) >= max_subqueries:
            break

    if decomposed and len(unique) < 2:
        raise QueryDecompositionError("多跳查询至少需要 2 条有效子查询")
    if not decomposed and unique:
        raise QueryDecompositionError("无需分解时 queries 必须为空")
    return decomposed, reason.strip()[:500], unique


def _query_key(value: str) -> str:
    return "".join(value.casefold().split())


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
    raise QueryDecompositionError("查询规划响应不是 JSON 对象")
