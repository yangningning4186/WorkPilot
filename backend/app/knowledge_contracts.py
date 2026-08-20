"""RAG ↔ Cowork 的跨产品契约（见 ADR-0011 Step 3）。

`app/rag` 与 `app/cowork` 是两个平级产品包，互不 import。Cowork 需要检索知识库时，
依赖的是这里的 `RagService` Protocol，由 composition root（`worker/cowork_run.py`）
注入 `app.rag.service.PostgresRagService`。

放在 app 根而不是任一产品包内，与既有的 `app/cowork_contracts.py` 同一惯例：
**跨边界的纯数据契约不属于边界的任何一侧**。

本模块只允许出现 dataclass / Protocol / Literal，不许 import 任何产品实现、
ORM 或 SQLAlchemy——它是两个包共同的下游，一旦长出实现就会把两边焊死。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from workpilot_ai.gateway import ModelGateway

ChunkStrategy = Literal["fixed", "heading", "recursive", "semantic"]


class LibraryPathError(ValueError):
    """路径越出了资料库根目录，或文件类型不被允许。

    两个产品都要用：RAG 入库时校验来源路径，Cowork 编辑器校验授权目录边界。
    """


@dataclass(frozen=True)
class EvidenceSegment:
    """一段可溯源证据。

    约束 3：溯源元数据必须完整——`block_id` 锚定解析块（不是 chunk，见 ADR-0006），
    `locations` 每项携带页码、归一化 bbox、页面尺寸、旋转与坐标原点。
    """

    citation_id: str
    block_id: UUID
    version_id: UUID
    document_id: UUID
    title: str
    source_uri: str
    quote: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]


@dataclass(frozen=True)
class RagSearchRequest:
    query: str
    top_k: int = 5
    candidate_k: int = 20
    strategy: ChunkStrategy = "heading"
    max_evidence_chars: int = 12_000


@dataclass(frozen=True)
class EvidenceBundle:
    """跨 Cowork/RAG 边界的稳定证据契约，不泄露 ORM 或裸 chunk。"""

    evidence: tuple[EvidenceSegment, ...]
    retrieved_chunks: int
    backend: str


class RagService(Protocol):
    async def search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
    ) -> EvidenceBundle: ...
