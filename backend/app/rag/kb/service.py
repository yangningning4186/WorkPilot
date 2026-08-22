"""本地知识库服务：建库、加文档、检索。

对外同时提供两套面孔：

* **KB 管理**（`create` / `add_documents` / `list_kbs` / `delete`）——这条是新的，
  「命名 KB、用户手建」这个产品形态就落在这里。
* **`RagService` 检索**（`search`）——沿用 `app/knowledge_contracts.py` 里已有的契约，
  所以 Cowork 侧一行不用改就能从 pgvector 切到本地 KB。

加文档是幂等的：内容哈希没变就跳过解析。反复把同一个目录拖进来不会在索引里堆出重复片段。

每次加文档整份重建索引，而不是往 FAISS 里追加：追加本身可以，但 BM25 那一路的语料统计
（idf）会随语料变化，只追加不重算会让词法打分逐渐失真。个人 KB 的规模下整份重建是秒级，
不值得为此维护两套一致性。代价是解析结果要留着——所以清单里存的是每篇文档的源路径，
重建时按路径重新解析。
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from llama_index.core.schema import Document

from app.core.config import Settings
from app.ingest.markdown import parse_markdown
from app.ingest.pdf import PdfParseError, parse_pdf
from app.ingest.settings import pdf_parser_config_from_settings
from app.ingest.types import ParsedDocument
from app.knowledge_contracts import (
    EvidenceBundle,
    EvidenceSegment,
    KnowledgeUnavailableError,
    RagSearchRequest,
)
from app.rag.kb.documents import build_documents
from app.rag.kb.index import KbHit, KbIndexError, build_index, search_index
from app.rag.kb.manifest import KbDocument, KbManifest, read_manifest, write_manifest
from app.rag.kb.paths import (
    KbNameError,
    index_dir,
    iter_slugs,
    kb_dir,
    manifest_path,
    slugify,
    validate_name,
    validate_slug,
)

# 每段证据最多截多长。与 pgvector 那条路径的 `max_evidence_chars` 是同一件事，只是这里
# 按段落而不是整包算——本地 KB 的节点本来就短。
MAX_QUOTE_CHARS = 1_600
# 引用编号从 S1 开始，与 RAG 问答页和 `answer-markdown.tsx` 的 `[S1]` chip 是同一套。
CITATION_PREFIX = "S"

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt"})
SUPPORTED_SUFFIXES = frozenset({".pdf", *_MARKDOWN_SUFFIXES})
# 一次导入最多接多少个文件。指到 home 目录的展开会扫出上万个文件，那不是导入而是事故；
# 与其让它跑三小时，不如立刻告诉用户选窄一点。
MAX_SOURCES_PER_IMPORT = 200

# (说明, 已完成数, 总数)。用来给"拖一整个文件夹进来"报进度——解析是分钟级的活，
# 一条不动的进度条和一个卡死的界面在用户看来没有区别。
ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True)
class SkippedSource:
    """跳过的一篇。`reason` 按约束 4 就是可执行的下一步，直接展示给用户。"""

    filename: str
    reason: str


@dataclass(frozen=True)
class AddResult:
    manifest: KbManifest
    added: tuple[KbDocument, ...]
    skipped: tuple[SkippedSource, ...]


def expand_sources(paths: Sequence[Path]) -> list[Path]:
    """把用户给的路径展开成一串待导入文件。

    目录递归展开成里面支持的格式，文件原样保留（**不过滤后缀**：明确指名的一篇失败了
    该报错，悄悄丢掉它只会让用户以为加进去了）。去重并排序，让同一批输入每次得到同一个
    顺序——索引内容因此可复现。

    跳过隐藏目录：`.git`、`.Trash`、`node_modules` 里的东西没有一个是用户想入库的资料。
    """
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if any(part.startswith(".") for part in child.relative_to(path).parts):
                    continue
                if child.is_file() and child.suffix.casefold() in SUPPORTED_SUFFIXES:
                    seen.add(child)
        else:
            seen.add(path)
    if len(seen) > MAX_SOURCES_PER_IMPORT:
        raise KbIndexError(
            f"这次展开出 {len(seen)} 个文件，超过单次上限 {MAX_SOURCES_PER_IMPORT}。"
            "换成更具体的子目录，分几次导入。"
        )
    return sorted(seen)


class KbNotFoundError(KnowledgeUnavailableError):
    """KB 不存在。Cowork 只认 `KnowledgeUnavailableError`，所以继承它而不是 LookupError。"""


class LocalKbService:
    """磁盘上的个人知识库。

    每次调用现读 manifest 而不是在内存里缓存：KB 可能被另一个进程（worker、CLI）改过，
    缓存一份清单只会让「我明明加进去了」变成一个需要重启才能解决的问题。
    """

    def __init__(self, root: Path, *, settings: Settings) -> None:
        self._root = root
        self._settings = settings

    @property
    def root(self) -> Path:
        return self._root

    # -- KB 管理 ---------------------------------------------------------

    def list_kbs(self) -> list[KbManifest]:
        manifests = []
        for slug in iter_slugs(self._root):
            manifest = read_manifest(manifest_path(self._root, slug))
            if manifest is not None:
                manifests.append(manifest)
        return manifests

    def get(self, slug: str) -> KbManifest:
        manifest = read_manifest(manifest_path(self._root, validate_slug(slug)))
        if manifest is None:
            available = ", ".join(item.slug for item in self.list_kbs()) or "（一个都没有）"
            raise KbNotFoundError(f"知识库 {slug!r} 不存在。现有：{available}")
        return manifest

    def create(self, name: str, *, slug: str | None = None, description: str = "") -> KbManifest:
        display = validate_name(name)
        resolved = validate_slug(slug or slugify(display))
        if not resolved:
            raise KbNameError(
                f"从名字 {display!r} 生成不出目录标识（纯中文名会被压成空）。"
                "请另外指定一个由小写字母、数字和连字符组成的 slug。"
            )
        if kb_dir(self._root, resolved).exists():
            raise KbNameError(f"知识库标识 {resolved!r} 已存在，换一个或先删除旧的。")
        manifest = KbManifest(slug=resolved, name=display, description=description.strip())
        write_manifest(manifest_path(self._root, resolved), manifest)
        return manifest

    def delete(self, slug: str) -> bool:
        target = kb_dir(self._root, slug)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    async def add_documents(
        self,
        slug: str,
        paths: list[Path],
        *,
        skip_failures: bool = False,
        progress: ProgressCallback | None = None,
    ) -> AddResult:
        """解析并索引新文档；内容哈希没变的直接跳过。

        `skip_failures=False`（默认）时任何一篇解析失败都会抛错，此时清单不写、索引不动。
        命令行单篇导入要的是这个：你指名道姓要加这一篇，它失败了就该让你知道。

        `skip_failures=True` 是给"拖一整个文件夹进来"用的。三十篇论文里混进一个没有文本层
        的扫描件，把整批都退回去是最没用的行为——用户既拿不到那二十九篇，也不知道是哪一篇
        坏了。跳过并逐条报告，比全有全无有用得多。
        """
        manifest = self.get(slug)
        known = {document.content_hash for document in manifest.documents}
        documents = list(manifest.documents)
        added: list[KbDocument] = []
        skipped: list[SkippedSource] = []

        for index, path in enumerate(paths, start=1):
            if progress is not None:
                progress(f"读取 {path.name}", index - 1, len(paths))
            try:
                content_hash = await asyncio.to_thread(_hash_file, path)
                if content_hash in known:
                    continue
                entry = await self._describe(path, content_hash)
            except (KbIndexError, OSError) as error:
                if not skip_failures:
                    raise
                skipped.append(SkippedSource(filename=path.name, reason=str(error)))
                continue
            documents.append(entry)
            known.add(content_hash)
            added.append(entry)

        if not added:
            return AddResult(manifest=manifest, added=(), skipped=tuple(skipped))
        updated, index_skipped = await self._reindex(
            slug, manifest, tuple(documents), skip_failures=skip_failures, progress=progress
        )
        return AddResult(
            manifest=updated,
            added=tuple(added),
            skipped=tuple([*skipped, *index_skipped]),
        )

    async def rebuild(
        self, slug: str, *, progress: ProgressCallback | None = None
    ) -> KbManifest:
        """按清单里的源路径重新解析并建索引。换了 embedding 模型之后用它。"""
        manifest = self.get(slug)
        if not manifest.documents:
            raise KbIndexError("这个知识库还没有文档，没有可重建的内容。")
        updated, _skipped = await self._reindex(
            slug, manifest, manifest.documents, progress=progress
        )
        return updated

    async def _reindex(
        self,
        slug: str,
        manifest: KbManifest,
        entries: tuple[KbDocument, ...],
        *,
        skip_failures: bool = False,
        progress: ProgressCallback | None = None,
    ) -> tuple[KbManifest, tuple[SkippedSource, ...]]:
        chunks: list[Document] = []
        kept: list[KbDocument] = []
        skipped: list[SkippedSource] = []
        for index, entry in enumerate(entries, start=1):
            if progress is not None:
                progress(f"解析 {entry.filename}", index - 1, len(entries))
            try:
                parsed, title, _parser = await self._parse(Path(entry.source_path))
                built = build_documents(
                    parsed,
                    doc_id=entry.doc_id,
                    filename=entry.filename,
                    title=title or entry.title,
                )
                if not built:
                    raise KbIndexError(
                        f"{entry.filename} 抽不出可索引的文字，可能是没有文本层的扫描件。"
                    )
            except (KbIndexError, OSError) as error:
                if not skip_failures:
                    raise
                skipped.append(SkippedSource(filename=entry.filename, reason=str(error)))
                continue
            chunks.extend(built)
            kept.append(entry)

        if not chunks:
            raise KbIndexError("这批文档一篇也没抽出可索引的文字，索引没有变化。")
        if progress is not None:
            progress("建立索引", len(entries), len(entries))
        signature = await build_index(kb_dir(self._root, slug), chunks, settings=self._settings)
        updated = replace(manifest.with_documents(tuple(kept)), embedding=signature)
        write_manifest(manifest_path(self._root, slug), updated)
        return updated, tuple(skipped)

    async def _describe(self, path: Path, content_hash: str) -> KbDocument:
        parsed, title, parser = await self._parse(path)
        # 存绝对路径：重建时要按它重新解析，相对路径会随进程 cwd 漂移。
        resolved = await asyncio.to_thread(path.resolve)
        return KbDocument(
            doc_id=content_hash[:16],
            filename=path.name,
            source_path=str(resolved),
            content_hash=content_hash,
            title=title,
            parser=parser,
            block_count=len(parsed.blocks),
            char_count=sum(len(block.text) for block in parsed.blocks),
        )

    async def _parse(self, path: Path) -> tuple[ParsedDocument, str, str]:
        if not await asyncio.to_thread(path.is_file):
            raise KbIndexError(f"{path} 不存在或不是普通文件。")
        if path.suffix.casefold() == ".pdf":
            try:
                parsed = await parse_pdf(path, pdf_parser_config_from_settings(self._settings))
            except PdfParseError as error:
                raise KbIndexError(f"{path.name} 解析失败：{error}") from error
            return parsed.document, parsed.title or path.stem, parsed.parser
        if path.suffix.casefold() not in _MARKDOWN_SUFFIXES:
            raise KbIndexError(
                f"{path.name} 的格式暂不支持入库，目前支持 .pdf/.md/.markdown/.txt。"
            )
        raw = await asyncio.to_thread(path.read_text, "utf-8")
        try:
            document = await asyncio.to_thread(parse_markdown, raw)
        except ValueError as error:
            raise KbIndexError(f"{path.name} 没有可索引内容：{error}") from error
        return document, path.stem, "markdown"

    # -- RagService 契约 --------------------------------------------------

    async def search(
        self,
        gateway: object,
        request: RagSearchRequest,
    ) -> EvidenceBundle:
        """在一个 KB 里检索，产出与 pgvector 那条路径同形的 `EvidenceBundle`。

        `request.kb_slug` 缺省时用唯一那个 KB；有多个而调用方没指定，就报错而不是随便
        挑一个——悄悄挑错库，回答会带着看起来很正经的出处，而那些出处来自另一份资料。

        `gateway` 收下但不用：`RagService` 契约里有这个参数，而本地 KB 的 embedding 直连
        本机端点，不经模型网关。保留签名是为了让两种后端可以互换。
        """
        del gateway
        target = request.kb_slug or self._only_slug()
        manifest = self.get(target)
        hits = await search_index(
            kb_dir(self._root, target),
            request.query,
            settings=self._settings,
            stored_signature=manifest.embedding,
            top_k=request.top_k,
        )
        return EvidenceBundle(
            evidence=tuple(_segment(hit, index) for index, hit in enumerate(hits, start=1)),
            retrieved_chunks=len(hits),
            backend="local_faiss_bm25",
        )

    def _only_slug(self) -> str:
        manifests = self.list_kbs()
        if not manifests:
            raise KbNotFoundError("还没有任何知识库。先创建一个再检索。")
        if len(manifests) > 1:
            names = ", ".join(item.slug for item in manifests)
            raise KbNotFoundError(f"有多个知识库（{names}），检索时必须指明用哪一个。")
        return manifests[0].slug

    def index_path(self, slug: str) -> Path:
        return index_dir(self._root, slug)


def _segment(hit: KbHit, ordinal: int) -> EvidenceSegment:
    """`KbHit` → 跨边界的证据契约。

    `block_id` / `version_id` / `document_id` 在契约里是 UUID，而本地 KB 没有数据库行。
    这里用内容派生出**确定性** UUID：同一份文档同一个片段，重建索引后拿到的仍是同一个 id。

    **溯源精度到页，不到 bbox。** 切分交给了 LlamaIndex 的 `SentenceSplitter`，它按字符
    窗口切，片段边界和解析块的字符区间对不上，所以 `char_start/char_end` 只能给 0，
    `locations` 只有页码没有矩形框。PDF 预览因此能翻到页，不能圈出那一句。
    """
    node_uuid = _derive_uuid(f"node:{hit.node_id}")
    doc_uuid = _derive_uuid(f"doc:{hit.doc_id}")
    return EvidenceSegment(
        citation_id=f"{CITATION_PREFIX}{ordinal}",
        block_id=node_uuid,
        version_id=doc_uuid,
        document_id=doc_uuid,
        title=hit.title or hit.filename,
        source_uri=hit.filename,
        quote=hit.text.strip()[:MAX_QUOTE_CHARS],
        char_start=0,
        char_end=0,
        heading_path=[],
        locations=[] if hit.page_no is None else [{"page_no": hit.page_no}],
    )


def _derive_uuid(seed: str) -> UUID:
    return UUID(bytes=hashlib.sha256(seed.encode("utf-8")).digest()[:16], version=5)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CITATION_PREFIX",
    "MAX_SOURCES_PER_IMPORT",
    "SUPPORTED_SUFFIXES",
    "AddResult",
    "KbNotFoundError",
    "LocalKbService",
    "ProgressCallback",
    "SkippedSource",
    "expand_sources",
]
