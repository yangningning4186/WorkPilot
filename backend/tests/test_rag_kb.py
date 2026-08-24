"""本地知识库（LlamaIndex + FAISS/BM25）的用例。

切分交给了 LlamaIndex 的 `SentenceSplitter`，溯源精度因此降到页级——bbox 精确高亮不在
这套用例的验收范围内，页码必须在。

换存储之后最容易悄悄坏掉的两件事在这里钉住：

1. **页码溯源**——按页喂 Document 是页码能跟着每个片段走的唯一原因，改错了不会报错，
   只会让所有引用都指不到具体位置。
2. **embedding 签名**——换了模型之后旧向量和新查询不在同一空间，检索照样返回结果，
   只是那些结果毫无意义。必须显式失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import replace
from pathlib import Path

import pytest
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from llama_index.retrievers.bm25 import BM25Retriever

from app.core.config import Settings
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument
from app.knowledge_contracts import RagSearchRequest
from app.rag.kb.documents import build_documents
from app.rag.kb.index import (
    BM25_DIRNAME,
    CJK_TOKEN_PATTERN,
    KbHit,
    KbIndexError,
    _PositiveScoreRetriever,
    _WeightedRrfRetriever,
    build_bm25_retriever,
    build_index,
    default_retrieval_config,
    load_index,
    rerank_hits,
    signature_of,
)
from app.rag.kb.manifest import (
    EmbeddingSignature,
    KbIndexVersion,
    KbManifest,
    read_manifest,
    write_manifest,
)
from app.rag.kb.paths import KbNameError, manifest_path, slugify, validate_slug
from app.rag.kb.service import (
    KbNotFoundError,
    LocalKbService,
)


class FakeEmbedding(BaseEmbedding):
    """确定性假 embedding：按词哈希打到固定维度。

    真 embedding 端点会让这套用例变成需要本机跑 Ollama 的集成测试，而这里要验的是索引、
    融合与溯源的行为，跟向量好不好完全无关。
    """

    @classmethod
    def class_name(cls) -> str:
        return "fake"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 1024
        for word in text.lower().split():
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 1024] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture(autouse=True)
def stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """整套用例都用假 embedding，不连本机端点。"""
    from app.rag.kb import index as index_module

    monkeypatch.setattr(index_module, "build_embedding", lambda _settings: FakeEmbedding())


def _settings(model: str = "fake-embed", revision: str = "v1") -> Settings:
    return Settings(
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model=model,
        embedding_revision=revision,
        rerank_enabled=False,
    )


def _block(
    idx: int,
    text: str,
    *,
    page: int | None = None,
    block_type: str = "paragraph",
    heading: tuple[str, ...] = (),
    start: int = 0,
) -> ParsedBlock:
    locations = (
        (BlockLocation(page, 595.0, 842.0, 0, "top_left", (0.1, 0.2, 0.9, 0.3)),)
        if page is not None
        else ()
    )
    return ParsedBlock(
        block_idx=idx,
        block_type=block_type,
        text=text,
        char_start=start,
        char_end=start + len(text),
        heading_path=heading,
        locations=locations,
    )


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


@pytest.fixture
def service(kb_root: Path) -> LocalKbService:
    return LocalKbService(kb_root, settings=_settings())


def _docs(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "docs"
    folder.mkdir(exist_ok=True)
    first = folder / "rag.md"
    first.write_text(
        "# 检索增强生成\n\n## 融合策略\n\nreciprocal rank fusion 不需要校准分数尺度。\n",
        encoding="utf-8",
    )
    second = folder / "notes.md"
    second.write_text(
        "# 工程笔记\n\n## 索引陷阱\n\nHNSW 属性过滤会在候选扫描阶段丢掉候选。\n",
        encoding="utf-8",
    )
    return first, second


def _directory_snapshot(path: Path) -> dict[str, str]:
    """用内容而非 mtime 钉住一版索引；不同文件系统的时间精度不会影响断言。"""
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


# --- 命名与路径 ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "/abs", "Papers", "a" * 64, "", "-lead"])
def test_slug_rejects_anything_that_could_escape_the_root(bad: str) -> None:
    """slug 同时是目录名和路径穿越的防线。"""
    with pytest.raises(KbNameError):
        validate_slug(bad)


def test_slugify_yields_empty_for_pure_cjk_names() -> None:
    """纯中文压不出 slug——与其猜一个音译，不如让调用方显式给。"""
    assert slugify("我的论文库") == ""
    assert slugify("Papers (2026)!") == "papers-2026"


# --- 溯源：页码必须跟着每个片段走 -----------------------------------------------


def _parsed(*blocks: ParsedBlock) -> ParsedDocument:
    return ParsedDocument(
        full_text="\n\n".join(block.text for block in blocks),
        blocks=list(blocks),
        page_count=max((b.locations[0].page_no for b in blocks if b.locations), default=None),
    )


def test_pdf_is_split_into_one_document_per_page() -> None:
    """按页喂 Document 是页码能跟着每个片段走的唯一原因。

    整篇喂进去的话，切分器切出来的节点只知道自己属于哪个文件，不知道在哪一页——
    引用因此指不到任何具体位置，而且不会有任何报错。
    """
    documents = build_documents(
        _parsed(
            _block(0, "第一页正文", page=1),
            _block(1, "第二页正文", page=2),
            _block(2, "第二页还有一段", page=2),
        ),
        doc_id="doc1",
        filename="paper.pdf",
        title="Attention",
    )

    assert [document.metadata["page_no"] for document in documents] == [1, 2]
    assert "第二页还有一段" in documents[1].text
    assert documents[0].metadata["doc_id"] == "doc1"


def test_block_spanning_pages_is_filed_under_the_page_it_starts_on() -> None:
    spanning = ParsedBlock(
        block_idx=0,
        block_type="table",
        text="跨页表格",
        char_start=0,
        char_end=4,
        heading_path=(),
        locations=(
            BlockLocation(2, 595.0, 842.0, 0, "top_left", (0.1, 0.8, 0.9, 0.95)),
            BlockLocation(3, 595.0, 842.0, 0, "top_left", (0.1, 0.05, 0.9, 0.2)),
        ),
    )
    documents = build_documents(_parsed(spanning), doc_id="d", filename="f", title="t")

    assert [document.metadata["page_no"] for document in documents] == [2]


def test_documents_without_page_geometry_fall_back_to_one_document() -> None:
    """Markdown 没有页码，只能整篇一个——此时引用退化到文件级。"""
    documents = build_documents(
        _parsed(_block(0, "纯文本正文")), doc_id="d", filename="f.md", title="t"
    )

    assert len(documents) == 1
    assert "page_no" not in documents[0].metadata


def test_provenance_metadata_never_enters_the_embedded_text() -> None:
    """把文件名和页码混进待编码文本会稀释语义向量。"""
    document = build_documents(
        _parsed(_block(0, "正文", page=1)), doc_id="d", filename="f", title="t"
    )[0]

    for key in ("doc_id", "filename", "page_no"):
        assert key in document.excluded_embed_metadata_keys
        assert key in document.excluded_llm_metadata_keys
    assert "page_no" not in document.get_content(metadata_mode="embed")


def test_empty_document_yields_nothing_rather_than_an_empty_chunk() -> None:
    assert build_documents(_parsed(), doc_id="d", filename="f", title="t") == []


# --- 清单与签名 ---------------------------------------------------------------


def test_manifest_round_trips_and_signature_compares_all_three_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    signature = EmbeddingSignature(model="bge-m3", dimensions=1024, revision="v2")
    version = KbIndexVersion(
        version_id="v1",
        label="默认",
        embedding=signature,
        retrieval=replace(default_retrieval_config(), rrf_lexical_weight=0.75),
    )
    write_manifest(
        path,
        KbManifest(slug="papers", name="论文").with_version(version, activate=True),
    )

    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded.active is not None
    assert loaded.active.embedding == signature
    assert loaded.active.retrieval.rrf_lexical_weight == 0.75
    assert loaded.active_version == "v1"
    assert not signature.matches(EmbeddingSignature("bge-m3", 1024, "v3")), "revision 变了也不兼容"
    assert not signature.matches(EmbeddingSignature("bge-m3", 768, "v2")), "维度变了也不兼容"


def test_unreadable_manifest_returns_none_instead_of_raising(tmp_path: Path) -> None:
    """半截 JSON 不该让整个列表接口炸掉。"""
    broken = tmp_path / "manifest.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert read_manifest(broken) is None


# --- 融合 ---------------------------------------------------------------------


class _StaticRetriever(BaseRetriever):
    def __init__(self, hits: list[NodeWithScore]) -> None:
        self._hits = hits
        super().__init__()

    def _retrieve(self, _query_bundle: QueryBundle) -> list[NodeWithScore]:
        return self._hits

    async def _aretrieve(self, _query_bundle: QueryBundle) -> list[NodeWithScore]:
        return self._hits


def test_weighted_rrf_applies_lexical_weight_without_using_raw_scores() -> None:
    first = TextNode(id_="first", text="first")
    second = TextNode(id_="second", text="second")
    dense = _StaticRetriever(
        [NodeWithScore(node=first, score=0.2), NodeWithScore(node=second, score=0.9)]
    )
    lexical = _StaticRetriever(
        [NodeWithScore(node=second, score=0.1), NodeWithScore(node=first, score=0.8)]
    )

    ranked = _WeightedRrfRetriever(
        dense,
        lexical,
        top_k=2,
        lexical_weight=0.75,
    ).retrieve("query")

    # 每一路都必须先按自己的原始分数排序：second 在 dense 第一、first 在 lexical 第一。
    # lexical 降到 0.75 后，dense 第一的 second 应获胜，原始分数本身不能跨路相加。
    assert [hit.node.node_id for hit in ranked] == ["second", "first"]
    assert ranked[0].score == pytest.approx(1 / 60 + 0.75 / 61)
    assert ranked[1].score == pytest.approx(1 / 61 + 0.75 / 60)


@pytest.mark.asyncio
async def test_local_reranker_reorders_valid_results_and_sends_content_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "results": [
                    {"id": "second", "relevance_score": 0.9},
                    {"id": "first", "relevance_score": 0.2},
                ]
            }

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, path: str, *, json: dict[str, object]) -> Response:
            captured["path"] = path
            captured["json"] = json
            return Response()

    from app.rag.kb import index as index_module

    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    settings = _settings().model_copy(
        update={
            "rerank_enabled": True,
            "rerank_candidate_k": 2,
            "rerank_candidate_text_mode": "content",
            "rerank_max_candidate_chars": 100,
        }
    )
    hits = [
        KbHit("first", "alpha", 0.03, "d1", "a.pdf", "Secret title", 1, "fusion"),
        KbHit("second", "beta!", 0.02, "d2", "b.pdf", "Other title", 2, "fusion"),
    ]

    ranked = await rerank_hits("query", hits, settings=settings, top_n=2)

    assert ranked is not None
    assert [hit.node_id for hit in ranked] == ["second", "first"]
    assert [hit.score_source for hit in ranked] == ["rerank", "rerank"]
    assert [hit.score for hit in ranked] == [0.9, 0.2]
    assert captured["path"] == "/v1/rerank"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["documents"] == [
        {"id": "first", "text": "alpha"},
        {"id": "second", "text": "beta!"},
    ]


@pytest.mark.asyncio
async def test_local_reranker_fails_open_on_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": [{"id": "unknown", "relevance_score": 1.0}]}

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    from app.rag.kb import index as index_module

    monkeypatch.setattr(index_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    settings = _settings().model_copy(update={"rerank_enabled": True, "rerank_candidate_k": 2})
    hits = [
        KbHit("first", "alpha", 0.03, "d1", "a.pdf", "A", 1, "fusion"),
        KbHit("second", "beta", 0.02, "d2", "b.pdf", "B", 2, "fusion"),
    ]

    assert await rerank_hits("query", hits, settings=settings, top_n=1) is None


# --- 词法这一路 ---------------------------------------------------------------

_CHINESE_CORPUS = [
    "引言。检索增强生成把检索器和生成器组合起来，这篇论文不讨论评测方法。",
    "方法。我们使用稠密检索器和词法检索器，并用倒数排名融合把两路结果合起来。",
    "属性过滤应该在向量检索之前完成，否则召回会被污染。",
    "Dense retrieval maps queries and documents into one vector space.",
]


def _bm25_nodes() -> list[TextNode]:
    return [TextNode(id_=str(idx), text=text) for idx, text in enumerate(_CHINESE_CORPUS)]


def test_default_tokenizer_would_score_every_chinese_query_zero() -> None:
    r"""先把病症钉住：库的默认分词在中文上等于没有分词。

    中文不写空格，`(?u)\b\w\w+\b` 会把整句吞成一个 token，于是查询与它零重叠——
    对**逐字包含**该查询的段落也是 0 分。这条用例存在的意义是：哪天有人把
    `token_pattern` 去掉，失败信息会直接指到这里，而不是让中文检索安静地退回噪声。
    """

    retriever = BM25Retriever.from_defaults(nodes=_bm25_nodes(), similarity_top_k=4)

    assert all(hit.score == 0.0 for hit in retriever.retrieve("属性过滤"))


def test_cjk_tokenizer_makes_chinese_queries_score() -> None:
    retriever = BM25Retriever.from_defaults(
        nodes=_bm25_nodes(),
        similarity_top_k=4,
        token_pattern=CJK_TOKEN_PATTERN,
    )

    hits = retriever.retrieve("这篇论文关于属性过滤是怎么说的")

    assert hits[0].node.node_id == "2", "讲属性过滤的那一段该排第一"
    assert hits[0].score > 0.0
    # 拉丁语走的仍是原来的词边界规则，没有被 CJK 那一支吃掉。
    latin = retriever.retrieve("vector space")
    assert latin[0].node.node_id == "3" and latin[0].score > 0.0


def test_zero_score_lexical_hits_never_reach_fusion() -> None:
    """BM25 一个词都没命中时不返回空列表，而是吐满 k 条 0 分结果。

    RRF 只看名次不看分数，这些噪声会拿到和真实命中同等的权重——排第一的 0 分候选得到的
    1/(60+1) 正好等于稠密路排第一的那条。所以它们必须在进融合之前就被挡掉。
    """

    inner = BM25Retriever.from_defaults(
        nodes=_bm25_nodes(),
        similarity_top_k=4,
        token_pattern=CJK_TOKEN_PATTERN,
    )
    # 查询一个词都不在语料里。**中文那侧很难构造这种情况**——CJK 逐字分词之后，任意两段
    # 中文几乎总会共用几个字，这也正是这层过滤不能替代排序质量的原因。
    query = "quantum entanglement superconducting"
    raw = inner.retrieve(query)

    assert len(raw) == 4 and all(hit.score == 0.0 for hit in raw), "先确认噪声真的会被吐出来"
    assert _PositiveScoreRetriever(inner).retrieve(query) == []


def test_candidate_count_scales_with_top_k_instead_of_a_fixed_constant() -> None:
    config = default_retrieval_config("hybrid")

    assert config.candidate_top_k(5, config.vector_top_k_multiplier) == 10
    assert config.candidate_top_k(1, config.bm25_top_k_multiplier) == 2
    # 倍数为 0/负数时不至于要到 0 条候选。
    assert config.candidate_top_k(3, 0) == 3


# --- 端到端 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_index_and_search_produces_the_shared_evidence_contract(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("我的论文库", slug="papers")
    first, second = _docs(tmp_path)
    result = await service.add_documents("papers", [first, second])

    assert len(result.added) == 2
    assert result.skipped == ()
    assert len(result.manifest.documents) == 2
    assert result.manifest.is_indexed
    assert service.index_path("papers").is_dir()

    bundle = await service.search(
        None, RagSearchRequest(query="reciprocal rank fusion", top_k=3, kb_slug="papers")
    )
    assert bundle.backend == "local_kb:papers:v1:hybrid", "证据包自带版本，报告才能自证"
    assert bundle.evidence
    first_segment = bundle.evidence[0]
    # 契约与 pgvector 那条路径同形，Cowork 侧一行不用改。
    assert first_segment.citation_id == "S1"
    assert first_segment.source_uri.endswith(".md")
    assert first_segment.quote.strip()
    assert first_segment.version_id != first_segment.document_id
    # 切分交给了 SentenceSplitter，片段边界与解析块的字符区间对不上，所以字符偏移
    # 只能是 0。写在断言里，免得以后有人以为它坏了。
    assert (first_segment.char_start, first_segment.char_end) == (0, 0)


@pytest.mark.asyncio
async def test_bm25_sidecar_is_written_with_the_index_and_reused_at_query_time(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """词法索引跟着这一版一起落盘，检索时不再重扫全量节点。"""

    service.create("库", slug="papers")
    first, second = _docs(tmp_path)
    await service.add_documents("papers", [first, second])
    version_path = service.index_path("papers", "v1")

    assert (version_path / BM25_DIRNAME / "retriever.json").exists()

    loaded = build_bm25_retriever(load_index(version_path, _settings()), version_path, top_k=2)
    assert loaded is not None
    # 语料是从落盘那份读出来的，不是现场从 docstore 扫出来的。
    assert len(loaded.corpus) > 0


@pytest.mark.asyncio
async def test_repeated_search_reuses_the_loaded_index_storage(
    service: LocalKbService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一不可变版本只从磁盘反序列化一次，后续查询复用只读 StorageContext。"""

    service.create("库", slug="papers")
    first, second = _docs(tmp_path)
    await service.add_documents("papers", [first, second])

    from app.rag.kb import index as index_module

    original = index_module._load_storage_context
    loads = 0

    def counted_load(path: Path):
        nonlocal loads
        loads += 1
        return original(path)

    monkeypatch.setattr(index_module, "_load_storage_context", counted_load)
    request = RagSearchRequest(query="reciprocal rank fusion", top_k=2, kb_slug="papers")
    await service.search(None, request)
    await service.search(None, request)

    assert loads == 1


@pytest.mark.asyncio
async def test_bm25_only_search_does_not_require_an_embedding_endpoint(
    service: LocalKbService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯词法版本不能因本机 embedding 服务停机而拒绝本可完成的查询。"""

    service.create("库", slug="papers")
    first, second = _docs(tmp_path)
    await service.add_documents("papers", [first, second])
    await service.create_version("papers", version_id="lexical", engine="bm25", activate=True)

    from app.rag.kb import index as index_module

    def unavailable(_settings: Settings):
        raise AssertionError("BM25-only 查询不应创建 embedding client")

    monkeypatch.setattr(index_module, "build_embedding", unavailable)
    bundle = await service.search(
        None,
        RagSearchRequest(query="reciprocal rank fusion", top_k=2, kb_slug="papers"),
    )

    assert bundle.backend == "local_kb:papers:lexical:bm25"
    assert bundle.evidence


@pytest.mark.asyncio
async def test_the_persisted_bm25_keeps_scoring_chinese_after_a_reload(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """落盘那份必须重新设回分词正则，否则中文会安静地退回 0 分。

    `BM25Retriever.persist` 只存 similarity_top_k / verbose / corpus_weight_mask，
    `token_pattern` 不在其中。直接 `from_persist_dir` 拿到的对象会用库的默认分词去切
    查询，而语料是用 CJK 分词切的——两边口径不一致既不报错也没有日志，只会让每一个
    中文查询都命中不到东西。这条用例就是钉住那个缺口。
    """

    service.create("库", slug="papers")
    folder = tmp_path / "zh"
    folder.mkdir()
    source = folder / "filter.md"
    source.write_text(
        "# 索引陷阱\n\n## 属性过滤\n\n属性过滤应该在向量检索之前完成，否则召回会被污染。\n",
        encoding="utf-8",
    )
    await service.add_documents("papers", [source])
    version_path = service.index_path("papers", "v1")

    retriever = build_bm25_retriever(load_index(version_path, _settings()), version_path, top_k=3)

    assert retriever is not None
    assert retriever.token_pattern == CJK_TOKEN_PATTERN
    hits = retriever.retrieve("属性过滤")
    assert hits and hits[0].score > 0.0


@pytest.mark.asyncio
async def test_a_chinese_query_finds_the_right_document_end_to_end(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """走 bm25 引擎的版本，因为它的排序完全由词法这一路决定。

    hybrid 版本在测试里用的是哈希假 embedding，稠密那一路的名次没有意义；只有把稠密
    拿掉，断言才是在验词法检索本身。改回默认分词时这条会红。
    """

    service.create("库", slug="papers")
    folder = tmp_path / "zh"
    folder.mkdir()
    (folder / "a.md").write_text(
        "# 引言\n\n检索增强生成把检索器和生成器组合起来，本节不讨论评测方法。\n",
        encoding="utf-8",
    )
    (folder / "b.md").write_text(
        "# 索引陷阱\n\n属性过滤应该在向量检索之前完成，否则召回会被污染。\n",
        encoding="utf-8",
    )
    await service.add_documents("papers", [folder / "a.md", folder / "b.md"])
    await service.create_version("papers", version_id="lexical", engine="bm25", activate=True)

    bundle = await service.search(
        None, RagSearchRequest(query="属性过滤应该什么时候做", top_k=2, kb_slug="papers")
    )

    assert bundle.backend == "local_kb:papers:lexical:bm25"
    assert bundle.evidence, "中文查询在改分词之前这里是空的（全 0 分被过滤掉）"
    assert bundle.evidence[0].source_uri.endswith("b.md")


# --- 索引版本 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_index_versions_coexist_and_can_be_searched_explicitly(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    manifest, created = await service.create_version(
        "papers", version_id="candidate", label="BM25 candidate", engine="bm25", activate=False
    )

    assert [version.version_id for version in manifest.versions] == ["v1", "candidate"]
    assert manifest.active_version == "v1", "不激活的 candidate 不能悄悄改变线上默认版本"
    assert created.retrieval.engine == "bm25"
    assert service.index_path("papers", "v1").is_dir()
    assert service.index_path("papers", "candidate").is_dir()

    baseline = await service.search(
        None,
        RagSearchRequest(query="融合", top_k=3, kb_slug="papers", kb_version_id="v1"),
    )
    candidate = await service.search(
        None,
        RagSearchRequest(query="融合", top_k=3, kb_slug="papers", kb_version_id="candidate"),
    )
    assert baseline.backend == "local_kb:papers:v1:hybrid"
    assert candidate.backend == "local_kb:papers:candidate:bm25"


@pytest.mark.asyncio
async def test_creating_a_version_does_not_modify_existing_index_files(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])
    original = service.index_path("papers", "v1")
    before = _directory_snapshot(original)

    await service.create_version("papers", version_id="v2", activate=True)

    assert _directory_snapshot(original) == before
    assert service.get("papers").version("v1") is not None
    assert service.get("papers").active_version == "v2"


@pytest.mark.asyncio
async def test_rebuild_uses_immutable_snapshot_and_publishes_a_new_version(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    original = first.read_bytes()
    added = await service.add_documents("papers", [first])
    v1_path = service.index_path("papers", "v1")
    v1_files = _directory_snapshot(v1_path)
    document = added.manifest.documents[0]
    snapshot = service.root / "papers" / document.snapshot_path

    assert snapshot.read_bytes() == original
    first.write_text("# 已被用户覆盖\n\n这不应进入历史 KB。\n", encoding="utf-8")
    first.unlink()

    rebuilt = await service.rebuild("papers")

    assert rebuilt.active_version == "v2"
    assert [item.version_id for item in rebuilt.versions] == ["v1", "v2"]
    assert _directory_snapshot(v1_path) == v1_files
    assert snapshot.read_bytes() == original
    bundle = await service.search(
        None, RagSearchRequest(query="reciprocal rank fusion", top_k=3, kb_slug="papers")
    )
    assert bundle.evidence
    assert "reciprocal rank fusion" in bundle.evidence[0].quote


@pytest.mark.asyncio
async def test_failed_staging_build_keeps_old_active_and_cleans_partial_directory(
    service: LocalKbService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])
    original = _directory_snapshot(service.index_path("papers", "v1"))

    async def fail_after_partial_write(target: Path, *_args: object, **_kwargs: object):
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((target / "partial").write_text, "not ready", encoding="utf-8")
        raise KbIndexError("模拟构建失败")

    monkeypatch.setattr("app.rag.kb.service.build_index", fail_after_partial_write)

    with pytest.raises(KbIndexError, match="模拟构建失败"):
        await service.rebuild("papers")

    manifest = service.get("papers")
    assert manifest.active_version == "v1"
    assert [item.version_id for item in manifest.versions] == ["v1"]
    assert _directory_snapshot(service.index_path("papers", "v1")) == original
    versions = service.index_path("papers", "v1").parent
    assert not any(item.name.endswith(".staging") for item in versions.iterdir())


@pytest.mark.asyncio
async def test_active_pointer_changes_only_after_staging_build_finishes(
    service: LocalKbService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])
    from app.rag.kb import service as service_module

    original_build = service_module.build_index
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_build(target: Path, *args: object, **kwargs: object):
        entered.set()
        await release.wait()
        return await original_build(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module, "build_index", gated_build)
    task = asyncio.create_task(service.rebuild("papers"))
    await entered.wait()

    assert service.get("papers").active_version == "v1"
    release.set()
    rebuilt = await task
    assert rebuilt.active_version == "v2"


@pytest.mark.asyncio
async def test_deleting_the_last_index_version_is_refused(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    with pytest.raises(KbIndexError, match="最后一版"):
        service.delete_version("papers", "v1")

    assert service.get("papers").active_version == "v1"
    assert service.index_path("papers", "v1").is_dir()


@pytest.mark.asyncio
async def test_a_missing_active_pointer_never_falls_back_to_another_version(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])
    await service.create_version("papers", version_id="v2", activate=False)
    broken = replace(service.get("papers"), active_version="already-deleted")
    write_manifest(manifest_path(service.root, "papers"), broken)

    loaded = service.get("papers")
    assert loaded.active is None
    with pytest.raises(KbIndexError, match="还没有可用的索引"):
        await service.search(None, RagSearchRequest(query="融合", kb_slug="papers"))

    # 显式指定仍然可以用；拒绝的只是含糊的默认请求，不能偷偷挑 v1 或 v2。
    explicit = await service.search(
        None,
        RagSearchRequest(query="融合", kb_slug="papers", kb_version_id="v1"),
    )
    assert explicit.backend == "local_kb:papers:v1:hybrid"


@pytest.mark.asyncio
async def test_adding_the_same_file_twice_is_a_no_op(
    service: LocalKbService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容哈希没变就不重建：反复把同一个目录拖进来不该重新解析和 embedding 一遍。"""
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    from app.rag.kb import service as service_module

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("内容没变时不该重建索引")

    monkeypatch.setattr(service_module, "build_index", explode)
    result = await service.add_documents("papers", [first])

    assert result.added == ()
    assert len(result.manifest.documents) == 1


@pytest.mark.asyncio
async def test_rebuild_changes_citation_ids(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """引用编号**不**跨重建稳定。

    片段 id 由 LlamaIndex 的切分器随机生成，重建一次全部换新。目前没有任何地方持久化
    引用（每次回答里的 [S1] 都是当场编号的），所以这没问题；但哪天要把引用存下来——
    比如评测 gold 或用户收藏的一段——就必须先给切分器一个由内容派生的 id_func。
    这条用例把这个前提钉住，免得那时候才发现。
    """
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])
    before = await service.search(None, RagSearchRequest(query="融合", top_k=3, kb_slug="papers"))

    await service.rebuild("papers")
    after = await service.search(None, RagSearchRequest(query="融合", top_k=3, kb_slug="papers"))

    assert before.evidence and after.evidence
    assert {segment.block_id for segment in before.evidence} != {
        segment.block_id for segment in after.evidence
    }
    # 文档级身份仍然稳定：它由内容哈希派生，不受切分影响。
    assert before.evidence[0].document_id == after.evidence[0].document_id


@pytest.mark.asyncio
async def test_search_refuses_when_the_embedding_model_changed(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """换了 embedding 之后检索不会报错，只会安静地返回胡说八道——所以必须显式拦。"""
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    switched = LocalKbService(service.root, settings=_settings(model="other-embed"))
    with pytest.raises(KbIndexError) as error:
        await switched.search(None, RagSearchRequest(query="融合", top_k=3, kb_slug="papers"))
    message = str(error.value)
    assert "不兼容" in message
    assert "新建一版索引" in message, "错误要告诉调用方下一步做什么（约束 4）"


@pytest.mark.asyncio
async def test_legacy_manifest_is_migrated_only_when_source_still_matches_hash(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """旧库没有 snapshot_path；首次重建验证原路径内容未漂移后才固化。"""
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    current = service.get("papers")
    legacy_document = replace(current.documents[0], snapshot_path="")
    legacy = replace(current, documents=(legacy_document,))
    write_manifest(manifest_path(service.root, "papers"), legacy)

    rebuilt = await service.rebuild("papers")

    assert len(rebuilt.documents) == 1
    assert Path(rebuilt.documents[0].source_path).is_absolute()
    assert rebuilt.documents[0].snapshot_path
    bundle = await service.search(None, RagSearchRequest(query="融合", top_k=3, kb_slug="papers"))
    assert bundle.evidence


@pytest.mark.asyncio
async def test_search_without_a_slug_refuses_to_guess_between_kbs(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """悄悄挑错库，回答会带着看起来很正经的出处，而那些出处来自另一份资料。"""
    service.create("甲", slug="alpha")
    service.create("乙", slug="beta")
    first, _ = _docs(tmp_path)
    await service.add_documents("alpha", [first])

    with pytest.raises(KbNotFoundError) as error:
        await service.search(None, RagSearchRequest(query="融合", top_k=3))
    assert "alpha" in str(error.value) and "beta" in str(error.value)


@pytest.mark.asyncio
async def test_unsupported_format_says_what_is_supported(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    service.create("库", slug="papers")
    binary = tmp_path / "weights.bin"
    binary.write_bytes(b"\x00\x01")

    with pytest.raises(KbIndexError) as error:
        await service.add_documents("papers", [binary])
    assert ".pdf" in str(error.value)


def test_signature_of_reads_all_three_settings_fields() -> None:
    assert signature_of(_settings(model="m", revision="r")) == EmbeddingSignature(
        model="m", dimensions=1024, revision="r"
    )


@pytest.mark.asyncio
async def test_build_index_refuses_an_empty_node_set(kb_root: Path) -> None:
    with pytest.raises(KbIndexError):
        await build_index(
            kb_root / "papers",
            [],
            settings=_settings(),
            retrieval=default_retrieval_config(),
        )
