"""KB 清单：这个知识库是什么、装了哪些文档、索引是用什么 embedding 建的。

**embedding 签名是这里最要紧的字段。** 一份 FAISS 索引只对建它的那个 embedding
模型有意义：换了模型、换了维度、甚至同一模型换了 revision，旧向量和新查询向量就不在
同一个空间里——检索不会报错，只会**安静地返回胡说八道的结果**。Postgres 那边靠
`document_versions` 的版本切换把这件事挡住了；换到文件系统之后，只能靠每次加载时比对
签名，不一致就拒绝检索并要求重建。

这是无声失败和显式失败的区别，也是这个文件存在的全部理由。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast


@dataclass(frozen=True)
class EmbeddingSignature:
    """决定一份索引还能不能用的那三个值。"""

    model: str
    dimensions: int
    revision: str

    def matches(self, other: EmbeddingSignature) -> bool:
        return (
            self.model == other.model
            and self.dimensions == other.dimensions
            and self.revision == other.revision
        )

    def describe(self) -> str:
        return f"{self.model}({self.dimensions}维, rev={self.revision})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> EmbeddingSignature | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                model=str(data["model"]),
                dimensions=int(data["dimensions"]),
                revision=str(data.get("revision") or "unversioned"),
            )
        except (KeyError, TypeError, ValueError):
            return None


#: 检索引擎。三者共用同一份 FAISS + docstore 落盘产物，区别只在检索时走哪几路：
#: hybrid = dense + BM25 后 RRF；dense/bm25 各走一路。分成配置而不是三套代码，
#: 是因为"同一批文档换个融合方式跑一遍"正是版本化要服务的那个动作。
RetrievalEngine = Literal["hybrid", "dense", "bm25"]


@dataclass(frozen=True)
class RetrievalConfig:
    """一版索引的检索侧取值。

    切分参数放在这里而不是全局设置里：改了 chunk_size 就是另一份索引，把它记在版本上，
    "这一版是怎么建出来的"才是可回答的问题。评测报告引用一个 version_id 就等于引用了
    这一整组取值。
    """

    engine: RetrievalEngine = "hybrid"
    chunk_size: int = 0
    chunk_overlap: int = 0
    rrf_k: int = 60
    # dense 路固定为 1.0；这个值只缩放 BM25 的倒数排名贡献。1.0 是标准等权 RRF。
    rrf_lexical_weight: float = 1.0
    # 每一路向下要多少候选：`top_k × 倍数`。固定条数（原来是写死的 30）在 top_k=1 时
    # 是在为一条结果融合三十条，在 top_k=10 时又只剩三倍余量——同一个常数在两端都不对。
    # 按 top_k 缩放让"融合有东西可融"这件事在任何 top_k 下都成立。
    vector_top_k_multiplier: int = 2
    bm25_top_k_multiplier: int = 2

    def candidate_top_k(self, top_k: int, multiplier: int) -> int:
        """这一路要向下取多少条候选。"""

        requested = max(1, int(top_k))
        return max(requested, requested * max(1, int(multiplier)))

    def describe(self) -> str:
        if self.engine == "hybrid":
            fusion = (
                f", rrf_k={self.rrf_k}"
                f", lexical_weight={self.rrf_lexical_weight:g}"
                f", cand={self.vector_top_k_multiplier}x/{self.bm25_top_k_multiplier}x"
            )
        else:
            fusion = ""
        return f"{self.engine}(chunk={self.chunk_size}/{self.chunk_overlap}{fusion})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> RetrievalConfig:
        if not isinstance(data, dict):
            return cls()
        engine = str(data.get("engine") or "hybrid")
        return cls(
            engine=cast("RetrievalEngine", engine if engine in _ENGINES else "hybrid"),
            chunk_size=int(data.get("chunk_size") or 0),
            chunk_overlap=int(data.get("chunk_overlap") or 0),
            rrf_k=int(data.get("rrf_k") or 60),
            rrf_lexical_weight=float(data.get("rrf_lexical_weight") or 1.0),
            # 倍数是后加的字段：老 manifest 里没有，按默认值读出来即可——它描述的是检索
            # 时怎么取候选，不像 embedding 签名那样决定索引本身能不能用。
            vector_top_k_multiplier=int(data.get("vector_top_k_multiplier") or 2),
            bm25_top_k_multiplier=int(data.get("bm25_top_k_multiplier") or 2),
        )


_ENGINES = frozenset({"hybrid", "dense", "bm25"})


@dataclass(frozen=True)
class KbIndexVersion:
    """一份文档集合上的一版索引。

    `document_hashes` 记的是**建这一版时**的文档集合。之后往 KB 里加了文档，这一版
    并不会自动跟上——它仍然可用，只是覆盖面变窄了，`is_stale` 让界面和评测能看见这件事。
    自动重建所有旧版本会让"加一篇文档"变成一次几分钟的全量作业，而且会悄悄改掉一个
    评测已经引用过的版本，那才是真正不能接受的。
    """

    version_id: str
    label: str
    embedding: EmbeddingSignature
    retrieval: RetrievalConfig
    document_hashes: tuple[str, ...] = ()
    node_count: int = 0
    created_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        return f"{self.version_id}｜{self.embedding.describe()}｜{self.retrieval.describe()}"

    def covers(self, hashes: tuple[str, ...]) -> bool:
        return set(hashes) <= set(self.document_hashes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "label": self.label,
            "embedding": self.embedding.to_dict(),
            "retrieval": self.retrieval.to_dict(),
            "document_hashes": list(self.document_hashes),
            "node_count": self.node_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> KbIndexVersion | None:
        if not isinstance(data, dict):
            return None
        embedding = EmbeddingSignature.from_dict(data.get("embedding"))
        version_id = str(data.get("version_id") or "")
        if embedding is None or not version_id:
            return None
        raw_hashes = data.get("document_hashes")
        return cls(
            version_id=version_id,
            label=str(data.get("label") or version_id),
            embedding=embedding,
            retrieval=RetrievalConfig.from_dict(data.get("retrieval")),
            document_hashes=tuple(
                str(item) for item in (raw_hashes if isinstance(raw_hashes, list) else [])
            ),
            node_count=int(data.get("node_count") or 0),
            created_at=float(data.get("created_at") or 0.0),
        )


@dataclass(frozen=True)
class KbDocument:
    """KB 里的一篇文档。

    `content_hash` 让重复加入同一个文件变成幂等：内容没变就不重新解析、不重新 embedding。
    `source_path` 只记录导入来源；`snapshot_path` 指向 KB 内按内容哈希保存的原始字节快照。
    所有后续版本只从快照解析，因此用户改名、移动或覆盖源文件都不会悄悄改写历史语料。
    """

    doc_id: str
    filename: str
    source_path: str
    content_hash: str
    snapshot_path: str = ""
    title: str = ""
    parser: str = ""
    block_count: int = 0
    node_count: int = 0
    char_count: int = 0
    added_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KbDocument:
        return cls(
            doc_id=str(data.get("doc_id") or ""),
            filename=str(data.get("filename") or ""),
            source_path=str(data.get("source_path") or ""),
            content_hash=str(data.get("content_hash") or ""),
            snapshot_path=str(data.get("snapshot_path") or ""),
            title=str(data.get("title") or ""),
            parser=str(data.get("parser") or ""),
            block_count=int(data.get("block_count") or 0),
            node_count=int(data.get("node_count") or 0),
            char_count=int(data.get("char_count") or 0),
            added_at=float(data.get("added_at") or 0.0),
        )


@dataclass(frozen=True)
class KbManifest:
    slug: str
    name: str
    description: str = ""
    documents: tuple[KbDocument, ...] = ()
    versions: tuple[KbIndexVersion, ...] = ()
    active_version: str | None = None
    #: **只在读旧库时非空。** 旧布局把唯一那份签名直接挂在库上；留着它是为了让
    #: `has_legacy_layout` 能认出"这是个需要重建的老库"并给出可执行的错误，
    #: 而不是把一个没有检索配置的版本硬认领进 `versions`——那样 A/B 的两边从此不可比。
    legacy_embedding: EmbeddingSignature | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def node_count(self) -> int:
        return sum(document.node_count for document in self.documents)

    @property
    def document_hashes(self) -> tuple[str, ...]:
        return tuple(document.content_hash for document in self.documents)

    @property
    def is_indexed(self) -> bool:
        return self.active is not None and bool(self.documents)

    @property
    def has_legacy_layout(self) -> bool:
        return not self.versions and self.legacy_embedding is not None

    @property
    def active(self) -> KbIndexVersion | None:
        """当前服役的那一版。

        `active_version` 指向一个已被删掉的版本时返回 None 而不是回落到"最新那一版"：
        回落会让检索安静地换一个索引，而用户以为自己搜的还是原来那一版。
        """

        if self.active_version is None:
            return None
        return self.version(self.active_version)

    def version(self, version_id: str) -> KbIndexVersion | None:
        for item in self.versions:
            if item.version_id == version_id:
                return item
        return None

    def with_documents(self, documents: tuple[KbDocument, ...]) -> KbManifest:
        return replace(self, documents=documents, updated_at=time.time())

    def with_version(self, version: KbIndexVersion, *, activate: bool) -> KbManifest:
        others = tuple(item for item in self.versions if item.version_id != version.version_id)
        return replace(
            self,
            versions=(*others, version),
            active_version=version.version_id if activate else self.active_version,
            legacy_embedding=None,
            updated_at=time.time(),
        )

    def without_version(self, version_id: str) -> KbManifest:
        remaining = tuple(item for item in self.versions if item.version_id != version_id)
        active = None if self.active_version == version_id else self.active_version
        return replace(self, versions=remaining, active_version=active, updated_at=time.time())

    def summary(self) -> str:
        if not self.is_indexed:
            state = "，尚未建索引"
        elif len(self.versions) > 1:
            state = f"，{len(self.versions)} 个索引版本，当前 {self.active_version}"
        else:
            state = ""
        return f"{self.name}（{len(self.documents)} 篇文档，{self.node_count} 个片段{state}）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "documents": [document.to_dict() for document in self.documents],
            "versions": [version.to_dict() for version in self.versions],
            "active_version": self.active_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KbManifest:
        raw_documents = data.get("documents")
        documents = tuple(
            KbDocument.from_dict(item)
            for item in (raw_documents if isinstance(raw_documents, list) else [])
            if isinstance(item, dict)
        )
        raw_versions = data.get("versions")
        versions = tuple(
            version
            for version in (
                KbIndexVersion.from_dict(item)
                for item in (raw_versions if isinstance(raw_versions, list) else [])
            )
            if version is not None
        )
        versions = tuple(sorted(versions, key=lambda item: item.created_at))
        return cls(
            slug=str(data.get("slug") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            documents=documents,
            versions=versions,
            active_version=(str(data["active_version"]) if data.get("active_version") else None),
            legacy_embedding=(
                None if versions else EmbeddingSignature.from_dict(data.get("embedding"))
            ),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


def write_manifest(path: Path, manifest: KbManifest) -> None:
    """原子写。

    清单是「这个 KB 里有什么」的唯一事实来源；写到一半断电会让整个 KB 变成不可读的
    JSON 残片，而它旁边那份索引其实完好无损。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_manifest(path: Path) -> KbManifest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return KbManifest.from_dict(data) if isinstance(data, dict) else None


__all__ = [
    "EmbeddingSignature",
    "KbDocument",
    "KbIndexVersion",
    "KbManifest",
    "RetrievalConfig",
    "RetrievalEngine",
    "read_manifest",
    "write_manifest",
]
