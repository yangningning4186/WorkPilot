"""本地知识库服务：建库、加文档、检索。

对外同时提供两套面孔：

* **KB 管理**（`create` / `add_documents` / `list_kbs` / `delete`）——这条是新的，
  「命名 KB、用户手建」这个产品形态就落在这里。
* **`RagService` 检索**（`search`）——沿用 `app/knowledge_contracts.py` 里已有的契约，
  所以 Cowork 侧一行不用改就能从 pgvector 切到本地 KB。

加文档是幂等的：内容哈希没变就跳过解析。反复把同一个目录拖进来不会在索引里堆出重复片段。

每次加文档整份重建索引，而不是往 FAISS 里追加：追加本身可以，但 BM25 那一路的语料统计
（idf）会随语料变化，只追加不重算会让词法打分逐渐失真。个人 KB 的规模下整份重建是秒级，
不值得为此维护两套一致性。原始字节按内容哈希复制进 KB 的 ``sources/``，之后所有版本都
从这份不可变快照解析；用户覆盖或移动导入来源不会改变已经入库的语料。

**索引是版本化的。** 一份文档集合上可以并存多版索引，每一版记着建它时的 embedding 签名
与检索配置（引擎、切分、融合常数）。`search` 默认用 active 那一版，也可以按 `version_id`
指定——这正是评测跑批需要的东西：baseline 与 candidate 指向同一个 KB 的两个版本，
语料完全相同，差异只有那一组配置。

加文档只重建 **active** 那一版。其余版本原样留着并被标成 stale：自动重建所有版本会把
"加一篇文档"变成一次几分钟的全量作业，而且会悄悄改掉一个评测报告已经引用过的版本。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

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
from app.rag.kb.index import (
    KbHit,
    KbIndexError,
    build_index,
    default_retrieval_config,
    search_index,
)
from app.rag.kb.manifest import (
    KbDocument,
    KbIndexVersion,
    KbManifest,
    RetrievalConfig,
    RetrievalEngine,
    read_manifest,
    write_manifest,
)
from app.rag.kb.paths import (
    MAX_VERSION_LABEL_CHARS,
    KbNameError,
    iter_slugs,
    kb_dir,
    legacy_index_dir,
    manifest_path,
    slugify,
    sources_dir,
    validate_name,
    validate_slug,
    validate_version_id,
    version_dir,
    versions_dir,
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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

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
                entry = await self._describe(slug, path, content_hash)
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

    async def rebuild(self, slug: str, *, progress: ProgressCallback | None = None) -> KbManifest:
        """按 KB 内的原文快照重新解析，建出一版新的 active 索引。

        换了 embedding 模型、或者要把旧的单索引布局迁到版本化布局，都用它。已经存在的
        版本永不覆盖：重建 v1 会得到 v2，active 只在 v2 完整落盘后原子切换。
        """
        manifest = self.get(slug)
        if not manifest.documents:
            raise KbIndexError("这个知识库还没有文档，没有可重建的内容。")
        updated, _skipped = await self._reindex(
            slug, manifest, manifest.documents, progress=progress
        )
        # 迁移完成：旧布局那个目录已经没有读者了，留着只会让人以为还有一份索引可用。
        legacy = legacy_index_dir(self._root, slug)
        if legacy.is_dir():
            await asyncio.to_thread(shutil.rmtree, legacy, True)
        return updated

    # -- 索引版本 --------------------------------------------------------

    async def create_version(
        self,
        slug: str,
        *,
        label: str = "",
        engine: RetrievalEngine = "hybrid",
        version_id: str | None = None,
        activate: bool = True,
        progress: ProgressCallback | None = None,
    ) -> tuple[KbManifest, KbIndexVersion]:
        """在同一批文档上再建一版索引。**不动任何已有版本。**

        `activate=False` 是评测常用的那一档：先把 candidate 建出来跑分，确认更好再切换。
        默认 True 是给交互用户的——建了一版却不生效，界面上看起来就像什么都没发生。
        """

        manifest = self.get(slug)
        if not manifest.documents:
            raise KbIndexError("这个知识库还没有文档，没有可索引的内容。")
        retrieval = replace(default_retrieval_config(), engine=engine)
        resolved = self._new_version_id(manifest, version_id)
        updated, version, _skipped = await self._build_version(
            slug,
            manifest,
            manifest.documents,
            version_id=resolved,
            label=_version_label(label, resolved),
            retrieval=retrieval,
            activate=activate,
            progress=progress,
        )
        return updated, version

    def activate_version(self, slug: str, version_id: str) -> KbManifest:
        manifest = self.get(slug)
        resolved_id = validate_version_id(version_id)
        if manifest.version(resolved_id) is None:
            raise KbIndexError(
                f"索引版本 {version_id!r} 不存在。现有：{self._version_names(manifest)}"
            )
        if not version_dir(self._root, slug, resolved_id).is_dir():
            raise KbIndexError(f"索引版本 {version_id!r} 的已发布目录丢失，不能激活。")
        updated = replace(manifest, active_version=version_id, updated_at=time.time())
        write_manifest(manifest_path(self._root, slug), updated)
        return updated

    def delete_version(self, slug: str, version_id: str) -> KbManifest:
        """删掉一版索引。**不允许删掉最后一版**，那等于把 KB 变成不可检索。"""

        manifest = self.get(slug)
        if manifest.version(validate_version_id(version_id)) is None:
            raise KbIndexError(
                f"索引版本 {version_id!r} 不存在。现有：{self._version_names(manifest)}"
            )
        if len(manifest.versions) == 1:
            raise KbIndexError(
                "这是最后一版索引，删掉之后这个知识库就无法检索了。"
                "先新建一版，或者直接删掉整个知识库。"
            )
        updated = manifest.without_version(version_id)
        if updated.active_version is None:
            # 删掉的正好是 active 那一版：接手的必须是一个确定的选择，而不是"下一个"。
            # 取最新建的那一版，并且把这件事写进 manifest，检索因此永远有明确的目标。
            updated = replace(updated, active_version=updated.versions[-1].version_id)
        write_manifest(manifest_path(self._root, slug), updated)
        shutil.rmtree(version_dir(self._root, slug, version_id), ignore_errors=True)
        return updated

    def _version_names(self, manifest: KbManifest) -> str:
        return ", ".join(item.version_id for item in manifest.versions) or "（一个都没有）"

    def _new_version_id(self, manifest: KbManifest, requested: str | None) -> str:
        if requested is not None:
            resolved = validate_version_id(requested)
            if (
                manifest.version(resolved) is not None
                or version_dir(self._root, manifest.slug, resolved).exists()
            ):
                raise KbNameError(f"索引版本 {resolved!r} 已存在，换一个标识。")
            return resolved
        # v1, v2, … 按已有的最大序号往后接。用序号而不是时间戳：版本 id 会出现在评测
        # 报告和命令行里，"v3" 比 "20260822T101530" 好读也好说。
        used = {item.version_id for item in manifest.versions}
        versions_root = versions_dir(self._root, manifest.slug)
        if versions_root.is_dir():
            used.update(
                child.name
                for child in versions_root.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            )
        ordinal = len(used) + 1
        while f"v{ordinal}" in used:
            ordinal += 1
        return f"v{ordinal}"

    async def _reindex(
        self,
        slug: str,
        manifest: KbManifest,
        entries: tuple[KbDocument, ...],
        *,
        skip_failures: bool = False,
        progress: ProgressCallback | None = None,
    ) -> tuple[KbManifest, tuple[SkippedSource, ...]]:
        """刷新 active 那一版（没有版本时建出第一版）。

        其余版本原样留着，只是从此覆盖不到新加的文档——`KbIndexVersion.covers` 让界面
        和评测能看见这件事。
        """

        active = manifest.active
        version_id = self._new_version_id(manifest, None)
        label = active.label if active is not None else "默认"
        retrieval = active.retrieval if active is not None else default_retrieval_config()
        updated, _version, skipped = await self._build_version(
            slug,
            manifest,
            entries,
            version_id=version_id,
            label=label,
            retrieval=retrieval,
            activate=True,
            skip_failures=skip_failures,
            progress=progress,
        )
        return updated, skipped

    async def _build_version(
        self,
        slug: str,
        manifest: KbManifest,
        entries: tuple[KbDocument, ...],
        *,
        version_id: str,
        label: str,
        retrieval: RetrievalConfig,
        activate: bool,
        skip_failures: bool = False,
        progress: ProgressCallback | None = None,
    ) -> tuple[KbManifest, KbIndexVersion, tuple[SkippedSource, ...]]:
        if manifest.version(version_id) is not None:
            raise KbNameError(f"索引版本 {version_id!r} 已存在，不能原地覆盖。")
        final_dir = version_dir(self._root, slug, version_id)
        if final_dir.exists():
            raise KbIndexError(
                f"索引版本目录 {version_id!r} 已存在但不在清单中。"
                "这是上次提交中断留下的孤儿目录；请换一个版本标识。"
            )

        chunks: list[Document] = []
        kept: list[KbDocument] = []
        skipped: list[SkippedSource] = []
        for index, entry in enumerate(entries, start=1):
            if progress is not None:
                progress(f"解析 {entry.filename}", index - 1, len(entries))
            try:
                ensured, snapshot = await self._ensure_snapshot(slug, entry)
                parsed, title, _parser = await self._parse(snapshot, display_name=ensured.filename)
                built = build_documents(
                    parsed,
                    doc_id=ensured.doc_id,
                    filename=ensured.filename,
                    title=title or ensured.title,
                )
                if not built:
                    raise KbIndexError(
                        f"{ensured.filename} 抽不出可索引的文字，可能是没有文本层的扫描件。"
                    )
            except (KbIndexError, OSError) as error:
                if not skip_failures:
                    raise
                skipped.append(SkippedSource(filename=entry.filename, reason=str(error)))
                continue
            chunks.extend(built)
            kept.append(ensured)

        if not chunks:
            raise KbIndexError("这批文档一篇也没抽出可索引的文字，索引没有变化。")
        if progress is not None:
            progress("建立索引", len(entries), len(entries))
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = final_dir.parent / f".{version_id}.{uuid4().hex}.staging"
        try:
            signature, node_count = await build_index(
                staging,
                chunks,
                settings=self._settings,
                retrieval=retrieval,
            )
            # staging 完整建好后才进入正式命名空间。final_dir 对检索要么不存在，要么
            # 是一整版完整索引；没有“active 指向正在写的目录”这个中间态。
            await asyncio.to_thread(os.replace, staging, final_dir)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        version = KbIndexVersion(
            version_id=version_id,
            label=label,
            embedding=signature,
            retrieval=retrieval,
            document_hashes=tuple(entry.content_hash for entry in kept),
            node_count=node_count,
        )
        # 先 promotion 再原子写 manifest：反过来会让 active 指向半成品。若进程恰好在两步
        # 之间退出，最多留下一个清单不可见的完整孤儿目录，旧 active 仍照常服务。
        updated = manifest.with_documents(tuple(kept)).with_version(version, activate=activate)
        try:
            write_manifest(manifest_path(self._root, slug), updated)
        except Exception:
            # promotion 已完成但 manifest 没接纳它：目录仍不可见。尽量回收；即使进程在
            # 这里崩溃，旧 manifest/active 也仍是完整的。
            await asyncio.to_thread(shutil.rmtree, final_dir, True)
            raise
        return updated, version, tuple(skipped)

    async def _describe(self, slug: str, path: Path, content_hash: str) -> KbDocument:
        # 先复制并复核 hash，再从快照解析；如果源文件在 hash 与 copy 之间变化，本次导入
        # 显式失败，不会让 manifest 的 content_hash 与真正索引的字节各说各话。
        resolved = await asyncio.to_thread(path.resolve)
        snapshot = await asyncio.to_thread(
            self._snapshot_source, slug, resolved, content_hash, path.suffix.casefold()
        )
        parsed, title, parser = await self._parse(snapshot, display_name=path.name)
        return KbDocument(
            doc_id=content_hash[:16],
            filename=path.name,
            source_path=str(resolved),
            content_hash=content_hash,
            snapshot_path=str(snapshot.relative_to(kb_dir(self._root, slug))),
            title=title,
            parser=parser,
            block_count=len(parsed.blocks),
            char_count=sum(len(block.text) for block in parsed.blocks),
        )

    async def _parse(
        self, path: Path, *, display_name: str | None = None
    ) -> tuple[ParsedDocument, str, str]:
        if not await asyncio.to_thread(path.is_file):
            raise KbIndexError(f"{path} 不存在或不是普通文件。")
        if path.suffix.casefold() == ".pdf":
            try:
                parsed = await parse_pdf(path, pdf_parser_config_from_settings(self._settings))
            except PdfParseError as error:
                raise KbIndexError(f"{path.name} 解析失败：{error}") from error
            fallback = Path(display_name).stem if display_name else path.stem
            return parsed.document, parsed.title or fallback, parsed.parser
        if path.suffix.casefold() not in _MARKDOWN_SUFFIXES:
            raise KbIndexError(
                f"{path.name} 的格式暂不支持入库，目前支持 .pdf/.md/.markdown/.txt。"
            )
        raw = await asyncio.to_thread(path.read_text, "utf-8")
        try:
            document = await asyncio.to_thread(parse_markdown, raw)
        except ValueError as error:
            raise KbIndexError(f"{path.name} 没有可索引内容：{error}") from error
        fallback = Path(display_name).stem if display_name else path.stem
        return document, fallback, "markdown"

    async def _ensure_snapshot(self, slug: str, entry: KbDocument) -> tuple[KbDocument, Path]:
        """返回经过 hash 复核的快照；旧 manifest 在第一次重建时安全迁移。"""

        base = await asyncio.to_thread(kb_dir(self._root, slug).resolve)
        if entry.snapshot_path:
            candidate = await asyncio.to_thread((base / entry.snapshot_path).resolve)
            try:
                candidate.relative_to(base)
            except ValueError as error:
                raise KbIndexError(f"{entry.filename} 的 snapshot_path 越出知识库目录。") from error
            if not await asyncio.to_thread(candidate.is_file):
                raise KbIndexError(
                    f"{entry.filename} 的原文快照丢失，不能重建历史语料。"
                    "请重新导入源文件作为一篇新文档。"
                )
            actual = await asyncio.to_thread(_hash_file, candidate)
            if actual != entry.content_hash:
                raise KbIndexError(f"{entry.filename} 的原文快照校验失败，拒绝用被修改的字节重建。")
            return entry, candidate

        source = Path(entry.source_path)
        if not await asyncio.to_thread(source.is_file):
            raise KbIndexError(
                f"旧知识库里的源文件 {entry.source_path} 已不存在，无法生成不可变快照。"
                "恢复原文件后重建，或重新导入现有文件作为新文档。"
            )
        snapshot = await asyncio.to_thread(
            self._snapshot_source,
            slug,
            source,
            entry.content_hash,
            Path(entry.filename).suffix.casefold(),
        )
        migrated = replace(
            entry,
            snapshot_path=str(snapshot.relative_to(kb_dir(self._root, slug))),
        )
        return migrated, snapshot

    def _snapshot_source(
        self,
        slug: str,
        source: Path,
        expected_hash: str,
        suffix: str,
    ) -> Path:
        """内容寻址地复制原始字节；已存在的快照只校验，绝不覆盖。"""

        if _SHA256_RE.fullmatch(expected_hash) is None:
            raise KbIndexError(f"{source.name} 的 content_hash 非法，拒绝生成快照路径。")
        extension = suffix if suffix in SUPPORTED_SUFFIXES else ".bin"
        target = sources_dir(self._root, slug) / f"{expected_hash}{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _hash_file(target) != expected_hash:
                raise KbIndexError(f"原文快照 {target.name} 已损坏，拒绝覆盖。")
            return target

        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            actual_hash = _hash_file(temporary)
            if actual_hash != expected_hash:
                raise KbIndexError(
                    f"{source.name} 在导入过程中发生了变化，请等文件保存完成后重试。"
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

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
        version = self.resolve_version(manifest, request.kb_version_id)
        hits = await search_index(
            version_dir(self._root, target, version.version_id),
            request.query,
            settings=self._settings,
            version=version,
            top_k=request.top_k,
        )
        version_uuid = _derive_uuid(f"index-version:{target}:{version.version_id}")
        return EvidenceBundle(
            evidence=tuple(
                _segment(hit, index, version_uuid) for index, hit in enumerate(hits, start=1)
            ),
            retrieved_chunks=len(hits),
            # backend 里带上版本：证据包会进评测报告，"这批证据是哪一版检索出来的"
            # 必须能从报告本身答出来，而不是靠跑批当天的记忆。
            backend=f"local_kb:{target}:{version.version_id}:{version.retrieval.engine}",
        )

    def resolve_version(self, manifest: KbManifest, version_id: str | None) -> KbIndexVersion:
        """请求要的那一版，缺省时是 active 那一版。找不到一律显式失败。"""

        if manifest.has_legacy_layout:
            raise KbIndexError(
                f"知识库 {manifest.slug!r} 还是旧的单索引布局，没有版本信息。"
                "运行 `python -m app.cli.kb rebuild "
                f"{manifest.slug}` 迁到版本化布局后再检索。"
            )
        if version_id is not None:
            resolved = manifest.version(validate_version_id(version_id))
            if resolved is None:
                raise KbIndexError(
                    f"知识库 {manifest.slug!r} 没有索引版本 {version_id!r}。"
                    f"现有：{self._version_names(manifest)}"
                )
            return resolved
        active = manifest.active
        if active is None:
            raise KbIndexError(f"知识库 {manifest.slug!r} 还没有可用的索引：先加文档再重建。")
        return active

    def _only_slug(self) -> str:
        manifests = self.list_kbs()
        if not manifests:
            raise KbNotFoundError("还没有任何知识库。先创建一个再检索。")
        if len(manifests) > 1:
            names = ", ".join(item.slug for item in manifests)
            raise KbNotFoundError(f"有多个知识库（{names}），检索时必须指明用哪一个。")
        return manifests[0].slug

    def index_path(self, slug: str, version_id: str | None = None) -> Path:
        manifest = self.get(slug)
        return version_dir(self._root, slug, self.resolve_version(manifest, version_id).version_id)


def _version_label(label: str, version_id: str) -> str:
    cleaned = " ".join((label or "").split())[:MAX_VERSION_LABEL_CHARS]
    return cleaned or version_id


def _segment(hit: KbHit, ordinal: int, version_uuid: UUID) -> EvidenceSegment:
    """`KbHit` → 跨边界的证据契约。

    `block_id` / `version_id` / `document_id` 在契约里是 UUID，而本地 KB 没有数据库行。
    这里用内容派生出确定性 UUID；其中 version_id 明确绑定不可变索引版，而不是像旧实现
    那样错误地复用 document_id。

    **溯源精度到页，不到 bbox。** 切分交给了 LlamaIndex 的 `SentenceSplitter`，它按字符
    窗口切，片段边界和解析块的字符区间对不上，所以 `char_start/char_end` 只能给 0，
    `locations` 只有页码没有矩形框。PDF 预览因此能翻到页，不能圈出那一句。
    """
    node_uuid = _derive_uuid(f"node:{hit.node_id}")
    doc_uuid = _derive_uuid(f"doc:{hit.doc_id}")
    return EvidenceSegment(
        citation_id=f"{CITATION_PREFIX}{ordinal}",
        block_id=node_uuid,
        version_id=version_uuid,
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
