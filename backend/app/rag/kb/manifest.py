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
from typing import Any


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


@dataclass(frozen=True)
class KbDocument:
    """KB 里的一篇文档。

    `content_hash` 让重复加入同一个文件变成幂等：内容没变就不重新解析、不重新 embedding。
    `source_path` 只作展示与重建用——KB 自己持有解析后的节点，原文件挪走了索引照样能查。
    """

    doc_id: str
    filename: str
    source_path: str
    content_hash: str
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
    embedding: EmbeddingSignature | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def node_count(self) -> int:
        return sum(document.node_count for document in self.documents)

    @property
    def is_indexed(self) -> bool:
        return self.embedding is not None and bool(self.documents)

    def with_documents(self, documents: tuple[KbDocument, ...]) -> KbManifest:
        return replace(self, documents=documents, updated_at=time.time())

    def summary(self) -> str:
        return (
            f"{self.name}（{len(self.documents)} 篇文档，{self.node_count} 个片段"
            f"{'，尚未建索引' if not self.is_indexed else ''}）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "documents": [document.to_dict() for document in self.documents],
            "embedding": None if self.embedding is None else self.embedding.to_dict(),
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
        return cls(
            slug=str(data.get("slug") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            documents=documents,
            embedding=EmbeddingSignature.from_dict(data.get("embedding")),
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
    "KbManifest",
    "read_manifest",
    "write_manifest",
]
