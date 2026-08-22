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

import hashlib
import math
from pathlib import Path

import pytest
from llama_index.core.embeddings import BaseEmbedding

from app.core.config import Settings
from app.ingest.types import BlockLocation, ParsedBlock, ParsedDocument
from app.knowledge_contracts import RagSearchRequest
from app.rag.kb.documents import build_documents
from app.rag.kb.index import KbIndexError, _rrf, build_index, signature_of
from app.rag.kb.manifest import EmbeddingSignature, KbManifest, read_manifest, write_manifest
from app.rag.kb.paths import KbNameError, slugify, validate_slug
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
    write_manifest(path, KbManifest(slug="papers", name="论文", embedding=signature))

    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded.embedding == signature
    assert not signature.matches(EmbeddingSignature("bge-m3", 1024, "v3")), "revision 变了也不兼容"
    assert not signature.matches(EmbeddingSignature("bge-m3", 768, "v2")), "维度变了也不兼容"


def test_unreadable_manifest_returns_none_instead_of_raising(tmp_path: Path) -> None:
    """半截 JSON 不该让整个列表接口炸掉。"""
    broken = tmp_path / "manifest.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert read_manifest(broken) is None


# --- 融合 ---------------------------------------------------------------------


class _Node:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


class _Scored:
    def __init__(self, node_id: str) -> None:
        self.node = _Node(node_id)
        self.score = 0.0


def test_rrf_rewards_appearing_in_both_routes() -> None:
    dense = [_Scored("a"), _Scored("b")]
    lexical = [_Scored("c"), _Scored("a")]

    fused = _rrf([dense, lexical], rrf_k=60)

    assert fused[0].node.node_id == "a", "两路都命中的该排最前"
    assert len(fused) == 3


def test_rrf_is_stable_when_scores_tie() -> None:
    """同分时按首次出现顺序，结果可复现——评测对比要靠这个。"""
    dense = [_Scored("x"), _Scored("y")]
    assert [item.node.node_id for item in _rrf([dense], rrf_k=60)] == ["x", "y"]


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
    assert bundle.backend == "local_faiss_bm25"
    assert bundle.evidence
    first_segment = bundle.evidence[0]
    # 契约与 pgvector 那条路径同形，Cowork 侧一行不用改。
    assert first_segment.citation_id == "S1"
    assert first_segment.source_uri.endswith(".md")
    assert first_segment.quote.strip()
    # 切分交给了 SentenceSplitter，片段边界与解析块的字符区间对不上，所以字符偏移
    # 只能是 0。写在断言里，免得以后有人以为它坏了。
    assert (first_segment.char_start, first_segment.char_end) == (0, 0)


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
    assert "重建" in str(error.value), "错误要告诉调用方下一步做什么（约束 4）"


@pytest.mark.asyncio
async def test_rebuild_reparses_from_the_recorded_source_paths(
    service: LocalKbService,
    tmp_path: Path,
) -> None:
    """换 embedding 模型之后要能重建。清单里存的绝对路径就是重建的全部依据。"""
    service.create("库", slug="papers")
    first, _ = _docs(tmp_path)
    await service.add_documents("papers", [first])

    rebuilt = await service.rebuild("papers")

    assert len(rebuilt.documents) == 1
    assert Path(rebuilt.documents[0].source_path).is_absolute()
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
        await build_index(kb_root / "papers", [], settings=_settings())
