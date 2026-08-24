"""建索引与混合检索。

**每一版索引落在自己的目录里**（`versions/<version_id>/`），带着建它时用的 embedding
签名与检索配置。建新版本不碰旧版本——A/B 的前提就是两版同时活着；建到一半失败留下的
半成品目录不在 manifest 的 `versions` 里，因此对检索不可见，旧版本照常服务。

**检索默认是 dense + BM25 两路 RRF。** 稠密召回处理同义改写，BM25 处理精确术语、编号、
人名和公式——中文学术语料上后者经常是唯一能命中的那一路。融合放在排名层而不是分数层：
两套系统的分数既不同尺度也不同形状，排名层不需要校准。走哪几路由版本的
`RetrievalConfig.engine` 决定，于是"同一批文档换个融合方式"是建一版而不是改一行代码。

**加载时比对 embedding 签名。** 换了 embedding 模型之后，旧向量和新查询向量不在同一个
空间里，检索不会报错，只会安静地返回胡说八道的结果。签名不一致就拒绝检索并要求重建——
无声失败和显式失败的区别。版本化之后这条约束没有放松：比对的是**这一版**的签名。
"""

from __future__ import annotations

import asyncio
import math
import shutil
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import faiss
import httpx
import structlog
from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.embeddings import BaseEmbedding, MockEmbedding
from llama_index.core.llms.mock import MockLLM
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import Document, NodeWithScore, QueryBundle
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.vector_stores.faiss import FaissVectorStore

from app.core.config import Settings
from app.knowledge_contracts import KnowledgeUnavailableError
from app.rag.kb.documents import CHUNK_OVERLAP, CHUNK_SIZE
from app.rag.kb.manifest import EmbeddingSignature, KbIndexVersion, RetrievalConfig

# RRF 的平滑常数，取原论文的 60。**这个值现在由 LlamaIndex 的官方融合实现写死**
# （`QueryFusionRetriever._reciprocal_rerank_fusion` 里的 `k = 60.0`），版本清单里那一
# 列因此只是"实际用过的值"的记录，不再是可调项；记着不等于能改，所以对不上时拒绝服务
# 而不是安静地按 60 跑（见 `search_index`）。
DEFAULT_RRF_K = 60

# BM25 索引的落盘目录，就在这一版索引目录下面。跟着索引一起建、一起删，所以不存在
# "索引重建了但词法侧还是旧语料"这种半新半旧的状态。
BM25_DIRNAME = "bm25"

# BM25 的分词正则。
#
# **默认值 `(?u)\b\w\w+\b` 在中文上等于没有分词。** 中文不写空格，`\w\w+` 会把
# 「稠密检索把查询和文档映射到同一个向量空间」整串吞成一个 token，于是查询「向量空间」
# 与它零重叠——实测对逐字包含该词的段落也是 0 分。更糟的是 BM25 并不因此返回空列表，
# 而是照样吐满 k 条 0 分结果，而 RRF 只看名次不看分数，这些噪声就带着真实权重挤进了融合。
#
# 这里改成「CJK 逐字 + 拉丁词」：CJK 单字自成 token，拉丁语仍走原来的词边界规则，两种
# 语言在同一份语料里都能分出词。不引入 jieba 是因为词典版本会变成索引的第二个隐形签名
# ——换个词典版本，旧索引的分词口径就和新查询对不上了，而这件事没有任何迹象。
# 字符类与阅读引擎的 `reading/search.py` 保持同一组区间。
CJK_TOKEN_PATTERN = r"(?u)[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]|\b\w\w+\b"


logger = structlog.get_logger(__name__)

# 每一路默认向下要 `top_k × 2` 条候选。抄的是 DeepTutor 的取法：候选数跟着 top_k 走，
# 而不是一个和 top_k 无关的常数。
DEFAULT_CANDIDATE_MULTIPLIER = 2

# FAISS/docstore 与 BM25 都是版本内不可变的落盘产物。逐查询反序列化会让延迟随知识库大小
# 线性增长；缓存只保留少量最近版本，版本 created_at 变化时自然失效。
MAX_CACHED_INDEX_VERSIONS = 4
MAX_CACHED_BM25_RETRIEVERS = 16
_IndexCacheKey = tuple[str, float, tuple[str, ...], int]
_INDEX_CONTEXTS: OrderedDict[_IndexCacheKey, StorageContext] = OrderedDict()
_INDEX_LOADS: dict[_IndexCacheKey, asyncio.Task[StorageContext]] = {}
_BM25_RETRIEVERS: OrderedDict[tuple[_IndexCacheKey, int], BM25Retriever | None] = OrderedDict()
_BM25_LOADS: dict[tuple[_IndexCacheKey, int], asyncio.Task[BM25Retriever | None]] = {}


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
    score_source: str


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


def default_retrieval_config(engine: str = "hybrid") -> RetrievalConfig:
    """默认的一组检索取值。切分参数取自本模块的常量，所以版本记的就是实际用过的值。"""

    return RetrievalConfig(
        engine=cast("Any", engine),
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        rrf_k=DEFAULT_RRF_K,
        rrf_lexical_weight=1.0,
        vector_top_k_multiplier=DEFAULT_CANDIDATE_MULTIPLIER,
        bm25_top_k_multiplier=DEFAULT_CANDIDATE_MULTIPLIER,
    )


def build_bm25_retriever(
    index: VectorStoreIndex,
    persist_dir: Path,
    *,
    top_k: int,
) -> BM25Retriever | None:
    """取这一版的 BM25 检索器：优先读落盘的那份，没有就按 docstore 现建。

    **落盘的那份必须重新设回分词正则。** `BM25Retriever.persist` 只存
    `similarity_top_k / verbose / corpus_weight_mask` 三个字段（见库里的
    `DEFAULT_PERSIST_ARGS`），`token_pattern` 不在其中。直接用 `from_persist_dir`
    拿到的对象会带着**库的默认分词**去切查询，而语料是用 CJK 分词切的——两边口径
    不一致不会报错，只会让所有中文查询安静地退回 0 分。
    """

    target = persist_dir / BM25_DIRNAME
    if (target / "retriever.json").exists():
        try:
            retriever = BM25Retriever.from_persist_dir(str(target))
            retriever.token_pattern = CJK_TOKEN_PATTERN
            retriever.similarity_top_k = _bounded_top_k(top_k, len(retriever.corpus))
            return retriever
        except Exception as error:  # pragma: no cover - 落盘文件损坏时退回现建
            logger.warning("kb.bm25.load_failed", path=str(target), error=str(error))
    nodes = list(index.docstore.docs.values())
    if not nodes:
        return None
    return BM25Retriever.from_defaults(
        nodes=nodes,
        similarity_top_k=_bounded_top_k(top_k, len(nodes)),
        token_pattern=CJK_TOKEN_PATTERN,
    )


def _bounded_top_k(top_k: int, corpus_size: int) -> int:
    """候选数不能超过语料条数。

    `bm25s` 在 `k > 语料条数` 时直接抛错，而候选数现在是 `top_k × 倍数` 算出来的——
    一个只有两个片段的小知识库，任何 top_k ≥ 2 的检索都会撞上它。从 nodes 构造时库
    自己会夹一次，但**从落盘目录加载再改 `similarity_top_k` 绕过了那一次**，所以夹逼
    必须放在这里。
    """

    return max(1, min(int(top_k), max(1, int(corpus_size))))


def persist_bm25_retriever(index: VectorStoreIndex, persist_dir: Path) -> bool:
    """把 BM25 索引落到这一版目录下的 sidecar 里。

    原来每次查询都要 `list(docstore.docs.values())` 再从头建一遍 BM25：语料统计量
    （idf、平均文档长度）是全量扫出来的，于是每一次检索都在为一件建索引时就能做完的事
    付全量语料的开销。建索引本来就要把所有节点过一遍，顺手存下来是免费的。

    失败不阻断建索引：检索侧会退回现建，慢一点但结果一致。
    """

    nodes = list(index.docstore.docs.values())
    if not nodes:
        return False
    target = persist_dir / BM25_DIRNAME
    shutil.rmtree(target, ignore_errors=True)
    try:
        retriever = BM25Retriever.from_defaults(
            nodes=nodes,
            similarity_top_k=DEFAULT_CANDIDATE_MULTIPLIER,
            token_pattern=CJK_TOKEN_PATTERN,
        )
        target.mkdir(parents=True, exist_ok=True)
        retriever.persist(str(target))
        return True
    except Exception as error:  # pragma: no cover - 词法侧不可用不该让建索引失败
        logger.warning("kb.bm25.persist_failed", path=str(target), error=str(error))
        shutil.rmtree(target, ignore_errors=True)
        return False


class _PositiveScoreRetriever(BaseRetriever):
    """只放行分数为正的命中。

    BM25 在一个词都没命中时**不返回空列表**，而是照样吐满 k 条 0 分结果。RRF 只看名次
    不看分数，这些 0 分候选会带着和真实命中同等的权重进入融合——排第一的噪声拿到的
    1/(60+1) 正好等于稠密路排第一的那条。所以在进融合之前就把它们挡掉：没有信号的
    候选不该参与排名。
    """

    def __init__(self, inner: BaseRetriever) -> None:
        self._inner = inner
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return [node for node in self._inner.retrieve(query_bundle) if (node.score or 0.0) > 0.0]

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        found = await self._inner.aretrieve(query_bundle)
        return [node for node in found if (node.score or 0.0) > 0.0]


async def build_index(
    target: Path,
    documents: list[Document],
    *,
    settings: Settings,
    retrieval: RetrievalConfig,
) -> tuple[EmbeddingSignature, int]:
    """切分、embedding、落盘到 `target`，返回 (embedding 签名, 片段数)。

    `target` 必须是调用方新建的 staging 目录，不得是已经发布的版本目录。这里仍先清理
    staging，方便同一次未完成构建重试；发布由 service 在构建成功后用目录 rename 完成。
    """
    if not documents:
        raise KbIndexError("没有可索引的内容：先往知识库里加至少一篇能抽出文字的文档。")

    # 清目录和建目录都可能很慢（一版索引是几百上千个文件），别占着事件循环。
    await asyncio.to_thread(shutil.rmtree, target, True)
    await asyncio.to_thread(lambda: target.mkdir(parents=True, exist_ok=True))

    # IndexFlatIP + 归一化向量 = 余弦相似度。选 Flat 而不是 HNSW：个人知识库的规模
    # （几百到几万个片段）下精确检索就是毫秒级，而 HNSW 会引入召回率损失和一堆要调的参数。
    store = FaissVectorStore(faiss_index=faiss.IndexFlatIP(settings.embedding_dim))
    context = StorageContext.from_defaults(vector_store=store)
    embedding = build_embedding(settings)
    try:
        try:
            index = VectorStoreIndex.from_documents(
                documents,
                storage_context=context,
                embed_model=embedding,
                transformations=[
                    SentenceSplitter(
                        chunk_size=retrieval.chunk_size or CHUNK_SIZE,
                        chunk_overlap=retrieval.chunk_overlap or CHUNK_OVERLAP,
                    )
                ],
                show_progress=False,
                use_async=True,
            )
        except Exception as error:
            # 建到一半失败会留下一个空的 index/ 目录，而清单里的 embedding 签名不会被更新，
            # 所以下一次检索命中的是"还没建索引"，不是一份半成品。
            raise _embedding_unreachable(error, settings) from error
        index.storage_context.persist(persist_dir=str(target))
    finally:
        await _close_embedding(embedding)
    # 词法侧的 sidecar 跟着这一版一起落盘：语料统计量在这里算一次，检索时就不必每次
    # 重扫全量节点。放在 embedding 成功之后，半成品目录里不会留下一份能被误读的 BM25。
    await asyncio.to_thread(persist_bm25_retriever, index, target)
    return signature_of(settings), len(index.docstore.docs)


def load_index(persist_dir: Path, settings: Settings) -> VectorStoreIndex:
    embedding = build_embedding(settings)
    return _index_from_context(_load_storage_context(persist_dir), embedding)


def _load_storage_context(persist_dir: Path) -> StorageContext:
    if not persist_dir.is_dir():
        raise KbIndexError("这一版索引的文件不在了：重建这个知识库的索引。")
    try:
        store = FaissVectorStore.from_persist_dir(str(persist_dir))
        return StorageContext.from_defaults(vector_store=store, persist_dir=str(persist_dir))
    except KbIndexError:
        raise
    except Exception as error:  # pragma: no cover - 索引损坏的兜底
        raise KbIndexError(f"索引读取失败（{error}）：重建这个知识库的索引。") from error


def _index_from_context(
    context: StorageContext,
    embedding: BaseEmbedding,
) -> VectorStoreIndex:
    try:
        loaded = load_index_from_storage(context, embed_model=embedding)
    except Exception as error:  # pragma: no cover - 索引结构损坏的兜底
        raise KbIndexError(f"索引读取失败（{error}）：重建这个知识库的索引。") from error
    if not isinstance(loaded, VectorStoreIndex):  # pragma: no cover - 防御
        raise KbIndexError("索引类型不符，重建这个知识库的索引。")
    return loaded


def _index_cache_key(persist_dir: Path, version: KbIndexVersion) -> _IndexCacheKey:
    if not persist_dir.is_dir():
        raise KbIndexError("这一版索引的文件不在了：重建这个知识库的索引。")
    return (
        str(persist_dir.resolve()),
        version.created_at,
        version.document_hashes,
        version.node_count,
    )


async def _cached_storage_context(
    persist_dir: Path,
    version: KbIndexVersion,
) -> tuple[_IndexCacheKey, StorageContext]:
    key = await asyncio.to_thread(_index_cache_key, persist_dir, version)
    cached = _INDEX_CONTEXTS.get(key)
    if cached is not None:
        _INDEX_CONTEXTS.move_to_end(key)
        return key, cached

    task = _INDEX_LOADS.get(key)
    if task is None:
        task = asyncio.create_task(
            asyncio.to_thread(_load_storage_context, persist_dir),
            name=f"kb-index-load-{version.version_id}",
        )
        _INDEX_LOADS[key] = task
        task.add_done_callback(lambda completed: _finish_index_load(key, completed))
    context = await asyncio.shield(task)
    _INDEX_CONTEXTS[key] = context
    _INDEX_CONTEXTS.move_to_end(key)
    while len(_INDEX_CONTEXTS) > MAX_CACHED_INDEX_VERSIONS:
        evicted, _ = _INDEX_CONTEXTS.popitem(last=False)
        for bm25_key in [item for item in _BM25_RETRIEVERS if item[0] == evicted]:
            _BM25_RETRIEVERS.pop(bm25_key, None)
    return key, context


def _finish_index_load(key: _IndexCacheKey, task: asyncio.Task[StorageContext]) -> None:
    if _INDEX_LOADS.get(key) is task:
        _INDEX_LOADS.pop(key, None)
    if not task.cancelled():
        task.exception()


async def _cached_bm25_retriever(
    key: _IndexCacheKey,
    index: VectorStoreIndex,
    persist_dir: Path,
    *,
    top_k: int,
) -> BM25Retriever | None:
    cache_key = (key, top_k)
    if cache_key in _BM25_RETRIEVERS:
        cached = _BM25_RETRIEVERS[cache_key]
        _BM25_RETRIEVERS.move_to_end(cache_key)
        return cached
    task = _BM25_LOADS.get(cache_key)
    if task is None:
        task = asyncio.create_task(
            asyncio.to_thread(
                build_bm25_retriever,
                index,
                persist_dir,
                top_k=top_k,
            ),
            name="kb-bm25-load",
        )
        _BM25_LOADS[cache_key] = task
        task.add_done_callback(lambda completed: _finish_bm25_load(cache_key, completed))
    retriever = await asyncio.shield(task)
    _BM25_RETRIEVERS[cache_key] = retriever
    _BM25_RETRIEVERS.move_to_end(cache_key)
    while len(_BM25_RETRIEVERS) > MAX_CACHED_BM25_RETRIEVERS:
        _BM25_RETRIEVERS.popitem(last=False)
    return retriever


def _finish_bm25_load(
    key: tuple[_IndexCacheKey, int],
    task: asyncio.Task[BM25Retriever | None],
) -> None:
    if _BM25_LOADS.get(key) is task:
        _BM25_LOADS.pop(key, None)
    if not task.cancelled():
        task.exception()


async def _close_embedding(embedding: BaseEmbedding) -> None:
    """关闭传给 LlamaIndex 的自建 httpx clients；Fake/Mock embedding 无需处理。"""

    private = getattr(embedding, "__pydantic_private__", None)
    if not isinstance(private, dict):
        return
    async_client = private.get("_async_http_client")
    sync_client = private.get("_http_client")
    try:
        if isinstance(async_client, httpx.AsyncClient):
            await async_client.aclose()
        if isinstance(sync_client, httpx.Client):
            await asyncio.to_thread(sync_client.close)
    except Exception:  # pragma: no cover - 清理失败不应反转成功的检索
        logger.warning("kb.embedding.close_failed", exc_info=True)


async def search_index(
    persist_dir: Path,
    query: str,
    *,
    settings: Settings,
    version: KbIndexVersion,
    top_k: int,
) -> list[KbHit]:
    """在**指定的一版**索引上检索；走哪几路由这一版的 engine 决定。"""
    current = signature_of(settings)
    if not version.embedding.matches(current):
        # 不是拒绝服务，是拒绝**安静地给错结果**：换了 embedding 模型之后，旧向量和新查询
        # 向量不在同一个空间里，检索照样能返回结果，只是那些结果毫无意义。
        raise KbIndexError(
            f"索引版本 {version.version_id} 是用 {version.embedding.describe()} 建的，"
            f"当前是 {current.describe()}，两者不兼容。"
            "用当前 embedding 新建一版索引，或把 embedding 配置改回去。"
        )

    retrieval = version.retrieval
    if retrieval.engine == "hybrid" and (retrieval.rrf_k or DEFAULT_RRF_K) != DEFAULT_RRF_K:
        # 融合改用 LlamaIndex 官方实现之后，RRF 的平滑常数由库里写死为 60。这一版记的
        # 是别的值，就说明它是按另一套融合参数建/评过的——按 60 跑等于给出一个和台账
        # 对不上的结果，而报告里没有任何地方看得出来。
        raise KbIndexError(
            f"索引版本 {version.version_id} 记的 rrf_k={retrieval.rrf_k}，"
            f"而当前融合实现固定为 {DEFAULT_RRF_K}。新建一版索引，或改用 dense/bm25 引擎。"
        )
    if retrieval.engine == "hybrid" and retrieval.rrf_lexical_weight <= 0:
        raise KbIndexError("hybrid 版本的 rrf_lexical_weight 必须大于 0。")

    engine = retrieval.engine
    bounded_top_k = max(1, int(top_k))
    # Rerank 只扩大最终融合池，不扩大 dense/BM25 各自的候选深度：这样 reranker
    # 不可用时，前 ``top_k`` 条仍与未开启 rerank 的基线逐位一致。默认 Top-5 会从
    # RRF Top-10 中重排出 5 条，而不是恢复历史上延迟约 4 秒的 Top-50 精排。
    rerank_pool_k = (
        max(bounded_top_k, int(settings.rerank_candidate_k))
        if settings.rerank_enabled
        else bounded_top_k
    )

    cache_key, context = await _cached_storage_context(persist_dir, version)
    embedding: BaseEmbedding = (
        MockEmbedding(embed_dim=settings.embedding_dim)
        if engine == "bm25"
        else build_embedding(settings)
    )
    try:
        index = _index_from_context(context, embedding)
        lexical: BaseRetriever | None = None
        if engine in {"hybrid", "bm25"}:
            try:
                built = await _cached_bm25_retriever(
                    cache_key,
                    index,
                    persist_dir,
                    top_k=retrieval.candidate_top_k(bounded_top_k, retrieval.bm25_top_k_multiplier),
                )
                lexical = None if built is None else _PositiveScoreRetriever(built)
            except Exception as error:  # pragma: no cover - BM25 缺分词器等环境问题
                if engine == "bm25":
                    # 这一版**只有**词法这一路。降级等于返回空结果并假装检索成功了——
                    # 而 hybrid 那一版降级还剩稠密可用，两种情况不能用同一段代码处理。
                    raise KbIndexError(
                        f"词法检索不可用（{error}），而版本 {version.version_id} 只走 BM25。"
                        "换一个 hybrid 或 dense 版本再检索。"
                    ) from error
                # hybrid：稠密那一路仍然可用，降级好过报错。
                lexical = None

        if engine == "bm25":
            if lexical is None:
                raise KbIndexError(
                    f"版本 {version.version_id} 只走 BM25，但这一版索引里没有可检索的片段。"
                    "重建这个知识库的索引。"
                )
            retriever: BaseRetriever = lexical
        elif lexical is None:
            # 只剩稠密一路（dense 版本，或 hybrid 降级）：没有东西可融，多取的候选只会被截掉。
            retriever = index.as_retriever(similarity_top_k=rerank_pool_k)
        else:
            dense = index.as_retriever(
                similarity_top_k=retrieval.candidate_top_k(
                    bounded_top_k, retrieval.vector_top_k_multiplier
                )
            )
            retriever = _fusion(
                dense,
                lexical,
                top_k=rerank_pool_k,
                lexical_weight=retrieval.rrf_lexical_weight,
            )

        try:
            fused = await retriever.aretrieve(query)
        except Exception as error:
            if engine == "bm25":
                # 这一版没有稠密那一路，别把词法侧的故障报成 embedding 不可达。
                raise KbIndexError(
                    f"词法检索失败（{error}）。重建这个知识库的索引，或改用 hybrid/dense 版本。"
                ) from error
            # 走到这里时 BM25 已经在内存里了（构建期的失败在上面就分流过），还能失败的只剩
            # 稠密那一路的 embedding 调用。它不能像 BM25 那样降级：查询向量算不出来，剩下的
            # 只有词法，那已经是另一种检索了。宁可显式失败，也不要悄悄换掉检索策略。
            raise _embedding_unreachable(error, settings) from error
    finally:
        await _close_embedding(embedding)
    score_source = (
        "fusion"
        if engine == "hybrid" and lexical is not None
        else ("lexical" if engine == "bm25" else "dense")
    )
    candidates = [_hit(scored, score_source=score_source) for scored in fused[:rerank_pool_k]]
    reranked = await rerank_hits(
        query,
        candidates,
        settings=settings,
        top_n=bounded_top_k,
    )
    return (reranked or candidates)[:bounded_top_k]


async def rerank_hits(
    query: str,
    hits: list[KbHit],
    *,
    settings: Settings,
    top_n: int,
) -> list[KbHit] | None:
    """用本机 cross-encoder 重排候选；服务不可用或响应非法时保留原排序。

    这是质量增强而不是可用性依赖。请求只发往配置的 reranker endpoint，候选正文按字符
    上限截断；返回 id、数量、分数任一不可信就整批丢弃，不能拼出一半新排序一半旧排序。
    """

    if not settings.rerank_enabled or len(hits) < 2:
        return None
    requested = min(max(1, int(top_n)), len(hits))
    candidates = hits[: max(requested, min(len(hits), int(settings.rerank_candidate_k)))]
    by_id = {hit.node_id: hit for hit in candidates}
    if len(by_id) != len(candidates):
        logger.warning("kb.rerank.duplicate_candidate_id", candidate_count=len(candidates))
        return None
    documents = [
        {
            "id": hit.node_id,
            "text": _rerank_candidate_text(hit, settings=settings),
        }
        for hit in candidates
    ]
    try:
        async with httpx.AsyncClient(
            base_url=settings.reranker_base_url.rstrip("/"),
            timeout=settings.reranker_timeout_s,
            trust_env=settings.model_trust_env,
        ) as client:
            response = await client.post(
                "/v1/rerank",
                json={
                    "model": settings.reranker_model,
                    "query": query,
                    "documents": documents,
                    "top_n": requested,
                },
            )
            response.raise_for_status()
            payload = response.json()
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list) or len(raw_results) != requested:
            raise ValueError("reranker results 数量不符")
        ranked: list[KbHit] = []
        seen: set[str] = set()
        for raw in raw_results:
            if not isinstance(raw, dict):
                raise TypeError("reranker result 必须是 object")
            node_id = str(raw.get("id") or "")
            if node_id not in by_id or node_id in seen:
                raise ValueError("reranker 返回未知或重复候选")
            raw_score = raw.get("relevance_score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
                raise TypeError("reranker relevance_score 必须是数值")
            score = float(raw_score)
            if not math.isfinite(score):
                raise ValueError("reranker 返回非有限分数")
            seen.add(node_id)
            ranked.append(replace(by_id[node_id], score=score, score_source="rerank"))
        return ranked
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        logger.warning(
            "kb.rerank.failed",
            candidate_count=len(candidates),
            error=f"{type(error).__name__}: {error}",
        )
        return None


def _rerank_candidate_text(hit: KbHit, *, settings: Settings) -> str:
    content = hit.text[: settings.rerank_max_candidate_chars]
    if settings.rerank_candidate_text_mode == "content":
        return content
    # 当前文件系统 KB 没有独立 heading 字段；heading_content 因而自然退化为正文。
    if settings.rerank_candidate_text_mode == "heading_content":
        return content
    title = hit.title.strip()
    return f"{title}\n{content}" if title else content


class _WeightedRrfRetriever(BaseRetriever):
    """两路加权 RRF；dense 权重固定 1，lexical 权重由实验变量给出。"""

    def __init__(
        self,
        dense: BaseRetriever,
        lexical: BaseRetriever,
        *,
        top_k: int,
        lexical_weight: float,
    ) -> None:
        self._dense = dense
        self._lexical = lexical
        self._top_k = top_k
        self._route_weights = (1.0, lexical_weight)
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        routes = (
            self._dense.retrieve(query_bundle),
            self._lexical.retrieve(query_bundle),
        )
        return self._fuse(routes)[: self._top_k]

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        routes = await asyncio.gather(
            self._dense.aretrieve(query_bundle),
            self._lexical.aretrieve(query_bundle),
        )
        return self._fuse(routes)[: self._top_k]

    def _fuse(
        self,
        routes: tuple[list[NodeWithScore], list[NodeWithScore]],
    ) -> list[NodeWithScore]:
        scores: dict[str, float] = {}
        nodes: dict[str, NodeWithScore] = {}
        for route, weight in zip(routes, self._route_weights, strict=True):
            for rank, hit in enumerate(
                sorted(route, key=lambda item: item.score or 0.0, reverse=True)
            ):
                node_hash = hit.node.hash
                nodes[node_hash] = hit
                scores[node_hash] = scores.get(node_hash, 0.0) + weight / (rank + DEFAULT_RRF_K)
        ranked = sorted(scores, key=scores.__getitem__, reverse=True)
        for node_hash in ranked:
            nodes[node_hash].score = scores[node_hash]
        return [nodes[node_hash] for node_hash in ranked]


def _fusion(
    dense: BaseRetriever,
    lexical: BaseRetriever,
    *,
    top_k: int,
    lexical_weight: float,
) -> BaseRetriever:
    """两路的倒数排名融合，用 LlamaIndex 官方的实现。

    只看名次不看分数，因此两路的分数尺度不需要校准——这正是排名层融合相对分数层融合的
    全部好处。

    `num_queries=1` 关掉它自带的查询改写：那一步要额外一次 LLM 调用，而这条链路要么跑在
    模型网关之外（本地 embedding），要么挂在预检索上，多一次调用的代价和收益都还没量过。
    因此这里的 `MockLLM` 永远不会被调用，只是满足构造签名。
    """

    if not math.isclose(lexical_weight, 1.0):
        return _WeightedRrfRetriever(
            dense,
            lexical,
            top_k=top_k,
            lexical_weight=lexical_weight,
        )
    return QueryFusionRetriever(
        [dense, lexical],
        llm=MockLLM(),
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=top_k,
        num_queries=1,
        use_async=True,
    )


def _hit(scored: Any, *, score_source: str) -> KbHit:
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
        score_source=score_source,
    )


__all__ = [
    "BM25_DIRNAME",
    "CJK_TOKEN_PATTERN",
    "DEFAULT_CANDIDATE_MULTIPLIER",
    "DEFAULT_RRF_K",
    "KbHit",
    "KbIndexError",
    "build_bm25_retriever",
    "build_embedding",
    "build_index",
    "default_retrieval_config",
    "load_index",
    "persist_bm25_retriever",
    "rerank_hits",
    "search_index",
    "signature_of",
]
