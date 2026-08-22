"""建索引与混合检索。

**检索是 dense + BM25 两路 RRF。** 稠密召回处理同义改写，BM25 处理精确术语、编号、
人名和公式——中文学术语料上后者经常是唯一能命中的那一路。融合放在排名层而不是分数层：
两套系统的分数既不同尺度也不同形状，排名层不需要校准。

**加载时比对 embedding 签名。** 换了 embedding 模型之后，旧向量和新查询向量不在同一个
空间里，检索不会报错，只会安静地返回胡说八道的结果。签名不一致就拒绝检索并要求重建——
无声失败和显式失败的区别。

重建直接原地覆盖 `index/`：个人 KB 重建是秒级，失败了重跑一次就是，不值得为此维护一套
临时目录加原子换名。代价是重建中途失败会让这个 KB 暂时没有索引，检索会明确报"请重建"。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import httpx
from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.vector_stores.faiss import FaissVectorStore

from app.core.config import Settings
from app.knowledge_contracts import KnowledgeUnavailableError
from app.rag.kb.documents import CHUNK_OVERLAP, CHUNK_SIZE
from app.rag.kb.manifest import EmbeddingSignature
from app.rag.kb.paths import INDEX_DIR

# 每一路召回取多少条送进融合。取得比 top_k 宽，让融合有东西可融——两路各取 top_k 的话，
# 融合退化成"谁排第一谁赢"。
CANDIDATES_PER_ROUTE = 30
# RRF 的平滑常数，取原论文的 60。
DEFAULT_RRF_K = 60


class KbIndexError(KnowledgeUnavailableError):
    """索引不可用。消息按约束 4 写成可执行指令。"""


@dataclass(frozen=True)
class KbHit:
    """一条命中，带页码级溯源。"""

    node_id: str
    text: str
    score: float
    doc_id: str
    filename: str
    title: str
    page_no: int | None


def build_embedding(settings: Settings) -> OpenAIEmbedding:
    """本地 embedding 端点的适配器。

    直连而不经模型网关：这条链路指向本机 Ollama，没有 API 花销也没有配额，网关能提供的
    路由与预算在这里没有可管的东西。代价是 embedding 调用不进成本看板——本地推理免费，
    这个代价是自洽的；哪天换成云端 embedding，就要把它接回网关。
    """
    if not settings.embedding_base_url:
        raise KbIndexError("没有配置 EMBEDDING_BASE_URL，无法建立或检索本地知识库。")
    return OpenAIEmbedding(
        model_name=settings.embedding_model,
        api_base=settings.embedding_base_url,
        # 本地 Ollama 不校验这个值，但 OpenAI 客户端要求非空；指到需要鉴权的端点时
        # 复用集群那把 key，与 routing.yaml 里各档的取法一致。
        api_key=settings.cluster_api_key or "local",
        dimensions=settings.embedding_dim,
        embed_batch_size=16,
        # **必须自己造 client。** httpx 默认 trust_env=True，会读 HTTP_PROXY 把发往
        # 127.0.0.1 的 embedding 请求塞进用户的代理，回来的是 502 而不是向量。开着
        # 代理是常态，所以这不是边缘情况。模型网关早就为此有了 model_trust_env，
        # 这条直连链路沿用同一个开关。
        http_client=httpx.Client(trust_env=settings.model_trust_env),
        async_http_client=httpx.AsyncClient(trust_env=settings.model_trust_env),
    )


def signature_of(settings: Settings) -> EmbeddingSignature:
    return EmbeddingSignature(
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        revision=settings.embedding_revision,
    )


def _embedding_unreachable(error: Exception, settings: Settings) -> KbIndexError:
    """embedding 端点打不通时的可执行错误（约束 4）。

    这是用户最常撞上的一个失败：Ollama 没起、模型没 pull、端口写错。默认情况下它会以
    `openai.InternalServerError: Error code: 502` 加一屏栈的形式冒出来，那既没告诉人
    该去启动什么，也没法原样递给模型。
    """
    return KbIndexError(
        f"embedding 端点 {settings.embedding_base_url} 调用失败（{error}）。"
        f"确认本机推理服务已启动、且模型 {settings.embedding_model} 已就绪，再重试。"
    )


async def build_index(
    kb_path: Path,
    documents: list[Document],
    *,
    settings: Settings,
) -> EmbeddingSignature:
    """切分、embedding、落盘，返回本次使用的 embedding 签名。"""
    if not documents:
        raise KbIndexError("没有可索引的内容：先往知识库里加至少一篇能抽出文字的文档。")

    target = kb_path / INDEX_DIR
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    # IndexFlatIP + 归一化向量 = 余弦相似度。选 Flat 而不是 HNSW：个人知识库的规模
    # （几百到几万个片段）下精确检索就是毫秒级，而 HNSW 会引入召回率损失和一堆要调的参数。
    store = FaissVectorStore(faiss_index=faiss.IndexFlatIP(settings.embedding_dim))
    context = StorageContext.from_defaults(vector_store=store)
    embedding = build_embedding(settings)
    try:
        index = VectorStoreIndex.from_documents(
            documents,
            storage_context=context,
            embed_model=embedding,
            transformations=[
                SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
            ],
            show_progress=False,
            use_async=True,
        )
    except Exception as error:
        # 建到一半失败会留下一个空的 index/ 目录，而清单里的 embedding 签名不会被更新，
        # 所以下一次检索命中的是"还没建索引"，不是一份半成品。
        raise _embedding_unreachable(error, settings) from error
    index.storage_context.persist(persist_dir=str(target))
    return signature_of(settings)


def load_index(kb_path: Path, settings: Settings) -> VectorStoreIndex:
    persist_dir = kb_path / INDEX_DIR
    if not persist_dir.is_dir():
        raise KbIndexError("这个知识库还没有建索引：先加文档再重建。")
    try:
        store = FaissVectorStore.from_persist_dir(str(persist_dir))
        context = StorageContext.from_defaults(vector_store=store, persist_dir=str(persist_dir))
        loaded = load_index_from_storage(context, embed_model=build_embedding(settings))
    except KbIndexError:
        raise
    except Exception as error:  # pragma: no cover - 索引损坏的兜底
        raise KbIndexError(f"索引读取失败（{error}）：重建这个知识库的索引。") from error
    if not isinstance(loaded, VectorStoreIndex):  # pragma: no cover - 防御
        raise KbIndexError("索引类型不符，重建这个知识库的索引。")
    return loaded


async def search_index(
    kb_path: Path,
    query: str,
    *,
    settings: Settings,
    stored_signature: EmbeddingSignature | None,
    top_k: int,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[KbHit]:
    """dense + BM25 两路召回后 RRF 融合。"""
    current = signature_of(settings)
    if stored_signature is None:
        raise KbIndexError("这个知识库还没有建索引：先加文档再重建。")
    if not stored_signature.matches(current):
        # 不是拒绝服务，是拒绝**安静地给错结果**：换了 embedding 模型之后，旧向量和新查询
        # 向量不在同一个空间里，检索照样能返回结果，只是那些结果毫无意义。
        raise KbIndexError(
            f"索引是用 {stored_signature.describe()} 建的，当前是 {current.describe()}，"
            "两者不兼容。重建这个知识库的索引后再检索。"
        )

    index = load_index(kb_path, settings)
    try:
        dense = await index.as_retriever(similarity_top_k=CANDIDATES_PER_ROUTE).aretrieve(query)
    except Exception as error:
        # 稠密这一路失败不能像 BM25 那样降级：查询向量算不出来，剩下的只有词法，
        # 那已经是另一种检索了。宁可显式失败，也不要悄悄换掉检索策略。
        raise _embedding_unreachable(error, settings) from error

    lexical: list[Any] = []
    try:
        nodes = list(index.docstore.docs.values())
        if nodes:
            bm25 = BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=CANDIDATES_PER_ROUTE)
            lexical = await bm25.aretrieve(query)
    except Exception:  # pragma: no cover - BM25 缺分词器等环境问题
        # 词法这一路失败不该让整次检索失败：稠密那一路仍然可用，降级好过报错。
        lexical = []

    return [_hit(scored) for scored in _rrf([dense, lexical], rrf_k=rrf_k)[:top_k]]


def _rrf(rankings: list[list[Any]], *, rrf_k: int) -> list[Any]:
    """倒数排名融合。

    只看名次不看分数，因此两路的分数尺度不需要校准——这正是排名层融合相对分数层融合的
    全部好处。同分时按首次出现顺序稳定排序，结果可复现。
    """
    scores: dict[str, float] = {}
    seen: dict[str, Any] = {}
    # 首次出现的名次单独记一份。不能在排序的 key 里回头去查正在被排序的那个列表——
    # 列表在排序过程中就是乱的，查出来的位置既不稳定也可能根本查不到。
    first_seen: dict[str, int] = {}
    for ranking in rankings:
        for rank, scored in enumerate(ranking, start=1):
            node_id = scored.node.node_id
            if node_id not in seen:
                seen[node_id] = scored
                first_seen[node_id] = len(first_seen)
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (rrf_k + rank)
    order = sorted(seen, key=lambda node_id: (-scores[node_id], first_seen[node_id]))
    for node_id in order:
        seen[node_id].score = scores[node_id]
    return [seen[node_id] for node_id in order]


def _hit(scored: Any) -> KbHit:
    node = scored.node
    meta = getattr(node, "metadata", None) or {}
    raw_page = meta.get("page_no")
    return KbHit(
        node_id=node.node_id,
        text=node.get_content(),
        score=float(scored.score or 0.0),
        doc_id=str(meta.get("doc_id") or ""),
        filename=str(meta.get("filename") or ""),
        title=str(meta.get("title") or ""),
        page_no=int(raw_page) if isinstance(raw_page, int) else None,
    )


__all__ = [
    "CANDIDATES_PER_ROUTE",
    "DEFAULT_RRF_K",
    "KbHit",
    "KbIndexError",
    "build_embedding",
    "build_index",
    "load_index",
    "search_index",
    "signature_of",
]
