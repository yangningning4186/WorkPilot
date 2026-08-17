from dataclasses import dataclass, replace

import httpx

from app.retrieval.dense import DenseSearchHit
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy

# 送给 cross-encoder 的候选文本构造方式。P1-B 表明去掉 title 单独无收益，
# 去掉 heading/结构前缀后才显著改善；先保留三种模式供正式单变量复核。
# 第一项仍是当前生产默认值。
CANDIDATE_TEXT_MODES = ("title_heading_content", "heading_content", "content")


class RerankResponseError(ValueError):
    pass


@dataclass(frozen=True)
class RerankResult:
    hits: list[DenseSearchHit]
    applied: bool
    candidate_count: int
    reason: str
    model: str | None
    provider: str | None


async def rerank_candidates(
    *,
    query: str,
    candidates: list[DenseSearchHit],
    top_k: int,
    base_url: str,
    model: str,
    timeout_s: float = 10.0,
    max_candidate_chars: int = 1200,
    candidate_text_mode: str = CANDIDATE_TEXT_MODES[0],
    client: httpx.AsyncClient | None = None,
    strategy: ChunkStrategy = "heading",
) -> RerankResult:
    strategy = validate_chunk_strategy(strategy)
    if not candidates:
        return RerankResult([], False, 0, "没有候选", None, None)
    if not query.strip():
        raise ValueError("query 不能为空")
    if not 1 <= top_k <= len(candidates):
        raise ValueError("top_k 必须位于 1 到候选数量")
    if not 100 <= max_candidate_chars <= 8000:
        raise ValueError("max_candidate_chars 必须位于 100 到 8000")
    if candidate_text_mode not in CANDIDATE_TEXT_MODES:
        raise ValueError(f"candidate_text_mode 必须是 {CANDIDATE_TEXT_MODES} 之一")
    mismatched = {hit.strategy for hit in candidates if hit.strategy != strategy}
    if mismatched:
        raise ValueError(
            f"rerank 禁止混合 chunk strategy: expected={strategy}, actual={sorted(mismatched)}"
        )

    candidate_map = {f"C{index}": hit for index, hit in enumerate(candidates, start=1)}
    payload = {
        "model": model,
        "query": query.strip(),
        "documents": [
            {
                "id": candidate_id,
                "text": build_candidate_text(
                    hit, max_chars=max_candidate_chars, mode=candidate_text_mode
                ),
            }
            for candidate_id, hit in candidate_map.items()
        ],
        # 要求服务返回完整分数, 客户端再截 Top-K, 便于严格校验漏项和重复项。
        "top_n": len(candidate_map),
    }
    owns_client = client is None
    current_client = client or httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=timeout_s,
        trust_env=False,
    )
    try:
        response = await current_client.post("/v1/rerank", json=payload)
        response.raise_for_status()
        ranked, response_model = parse_cross_encoder_response(
            response.json(),
            allowed_ids=set(candidate_map),
        )
        hits = [
            replace(candidate_map[item_id], rerank_score=score) for item_id, score in ranked[:top_k]
        ]
        return RerankResult(
            hits=hits,
            applied=True,
            candidate_count=len(candidates),
            reason="本地 cross-encoder rerank",
            model=response_model,
            provider="local_cross_encoder",
        )
    except Exception as error:
        # rerank 是增强项。本地服务失败或响应非法时保持 dense/RRF 顺序。
        return RerankResult(
            hits=candidates[:top_k],
            applied=False,
            candidate_count=len(candidates),
            reason=f"rerank 降级: {type(error).__name__}",
            model=None,
            provider=None,
        )
    finally:
        if owns_client:
            await current_client.aclose()


def parse_cross_encoder_response(
    payload: object,
    *,
    allowed_ids: set[str],
) -> tuple[list[tuple[str, float]], str]:
    if not isinstance(payload, dict):
        raise RerankResponseError("rerank 响应必须是 JSON 对象")
    model = payload.get("model")
    results = payload.get("results")
    if not isinstance(model, str) or not model.strip():
        raise RerankResponseError("rerank 响应缺少 model")
    if not isinstance(results, list):
        raise RerankResponseError("rerank 响应缺少 results 数组")

    parsed: list[tuple[str, float]] = []
    seen: set[str] = set()
    previous_score = float("inf")
    for item in results:
        if not isinstance(item, dict):
            raise RerankResponseError("rerank result 必须是对象")
        item_id = item.get("id")
        score = item.get("relevance_score")
        if not isinstance(item_id, str) or item_id not in allowed_ids:
            raise RerankResponseError("rerank 包含未知 id")
        if item_id in seen:
            raise RerankResponseError("rerank id 重复")
        if not isinstance(score, int | float) or isinstance(score, bool) or not 0 <= score <= 1:
            raise RerankResponseError("rerank relevance_score 必须位于 0 到 1")
        score = float(score)
        if score > previous_score:
            raise RerankResponseError("rerank results 未按分数降序排列")
        previous_score = score
        seen.add(item_id)
        parsed.append((item_id, score))
    if seen != allowed_ids:
        raise RerankResponseError("rerank 未覆盖全部候选")
    return parsed, model.strip()


def build_candidate_text(hit: DenseSearchHit, *, max_chars: int, mode: str) -> str:
    """按线上同一口径构造 cross-encoder 的 document 字符串。"""
    prefix = _candidate_prefix(hit, mode=mode)
    content_budget = max(1, max_chars - len(prefix) - 1)
    return f"{prefix}\n{hit.content[:content_budget]}" if prefix else hit.content[:max_chars]


def candidate_content_offset(hit: DenseSearchHit, *, mode: str) -> int:
    """正文首字符在候选字符串中的偏移，供 gold span 可见性审计使用。"""
    prefix = _candidate_prefix(hit, mode=mode)
    return len(prefix) + 1 if prefix else 0


def _candidate_prefix(hit: DenseSearchHit, *, mode: str) -> str:
    if mode == "content":
        parts: tuple[str, ...] = ()
    elif mode == "heading_content":
        parts = (" > ".join(hit.heading_path),)
    elif mode == "title_heading_content":
        parts = (hit.title, " > ".join(hit.heading_path))
    else:
        raise ValueError(f"未知的 rerank_candidate_text_mode: {mode}")
    return "\n".join(part for part in parts if part)


# 兼容既有内部测试与调用；新审计代码使用语义更清楚的公开名称。
_candidate_text = build_candidate_text
