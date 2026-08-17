from dataclasses import replace
from uuid import UUID

from app.retrieval.dense import DenseSearchHit
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy

MAX_RRF_CANDIDATES = 200


def reciprocal_rank_fusion(
    rankings: list[list[DenseSearchHit]],
    *,
    top_k: int = 50,
    rrf_k: int = 60,
    strategy: ChunkStrategy = "heading",
) -> list[DenseSearchHit]:
    if not 1 <= top_k <= MAX_RRF_CANDIDATES:
        raise ValueError(f"top_k 必须位于 1 到 {MAX_RRF_CANDIDATES}")
    if rrf_k < 1:
        raise ValueError("rrf_k 必须为正数")
    strategy = validate_chunk_strategy(strategy)
    mismatched = {
        hit.strategy for ranking in rankings for hit in ranking if hit.strategy != strategy
    }
    if mismatched:
        raise ValueError(
            f"RRF 禁止混合 chunk strategy: expected={strategy}, actual={sorted(mismatched)}"
        )
    fused: dict[UUID, tuple[DenseSearchHit, float]] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            current = fused.get(hit.chunk_id)
            fusion_score = 1.0 / (rrf_k + rank)
            if current is None:
                fused[hit.chunk_id] = (hit, fusion_score)
                continue
            base, score = current
            dense_score = base.dense_score if base.dense_score is not None else hit.dense_score
            lexical_score = (
                base.lexical_score if base.lexical_score is not None else hit.lexical_score
            )
            if base.dense_score is None and hit.dense_score is not None:
                base = hit
            fused[hit.chunk_id] = (
                replace(base, dense_score=dense_score, lexical_score=lexical_score),
                score + fusion_score,
            )
    ordered = sorted(
        fused.values(),
        key=lambda item: (-item[1], str(item[0].chunk_id)),
    )
    return [replace(hit, fusion_score=score) for hit, score in ordered[:top_k]]


def rerank_candidate_union(
    dense_hits: list[DenseSearchHit],
    lexical_hits: list[DenseSearchHit],
    *,
    rrf_k: int,
    strategy: ChunkStrategy = "heading",
) -> list[DenseSearchHit]:
    """构造交给 cross-encoder 的两臂并集，RRF 只提供降级时的稳定顺序。

    旧链路先把 dense/lexical 各 50 条融合并截断到 RRF Top-50，再送 rerank。这样
    只被单臂命中的 gold chunk 会在 cross-encoder 看见它之前就被 RRF 截掉，等于让
    词法/语义单臂的弱点充当精排的硬门。这里保留 RRF 的完整排序作为 rerank 失败时的
    fallback，但不在 rerank 之前按 RRF 截断：输出恰好是两个臂的去重并集（最多 200 条）。

    正常 rerank 成功时，初始 RRF 顺序只影响 cross-encoder 同分的稳定 tie-break；最终
    Top-K、拒答和 evidence gate 的输入深度都不在这里改变。
    """
    unique_count = len({hit.chunk_id for hit in [*dense_hits, *lexical_hits]})
    if unique_count > MAX_RRF_CANDIDATES:
        raise ValueError(
            f"rerank 两臂并集超过上限 {MAX_RRF_CANDIDATES}: actual={unique_count}"
        )
    if unique_count == 0:
        return []
    return reciprocal_rank_fusion(
        [dense_hits, lexical_hits],
        top_k=unique_count,
        rrf_k=rrf_k,
        strategy=strategy,
    )


def apply_document_cap(
    hits: list[DenseSearchHit], *, cap: int
) -> list[DenseSearchHit]:
    """按 version_id 限制每篇文档在头部占用的名额，超额的降级到尾部。

    跨文档题里语义最近的那篇会吃掉几乎全部名额，题目真正需要的第二篇被饿死
    （台账 E7 B 组：Socratic-SWE 28/28，ASP 一条没有）。RRF 与 cross-encoder
    都只按单点相关性排序，没有任何按文档去冗的机制。

    **降级而不是丢弃**：超额候选移到尾部而不是删掉，
    这样漏召回归因与 budget recall 仍能看到 Top-K 之外的深度（与 dense_baseline
    保留完整排序同一个理由），且 cap 调小不会不可逆地损失候选。

    实测依据（100 篇语料，dense/lexical 双臂深排探针）：被挤掉的第二篇文档
    其 gold chunk 常常在**某一臂**里排得很靠前（词法 3 / 8 名），
    属于"在候选池里但被霸榜挤出头部"，正是这一刀能救的形状；
    但另有两条 gold chunk 两臂都不可达（>400 名且词法无匹配），
    重排救不回来，需要子查询分解或改写。
    """
    if cap < 1:
        raise ValueError("每文档名额上限必须为正数")
    head: list[DenseSearchHit] = []
    overflow: list[DenseSearchHit] = []
    used: dict[UUID, int] = {}
    for hit in hits:
        count = used.get(hit.version_id, 0)
        if count < cap:
            used[hit.version_id] = count + 1
            head.append(hit)
        else:
            overflow.append(hit)
    return head + overflow
