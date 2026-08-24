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
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from workpilot_ai.gateway import ModelGateway

ChunkStrategy = Literal["fixed", "heading", "recursive", "semantic"]


class LibraryPathError(ValueError):
    """路径越出了资料库根目录，或文件类型不被允许。

    两个产品都要用：RAG 入库时校验来源路径，Cowork 编辑器校验授权目录边界。
    """


class KnowledgeUnavailableError(RuntimeError):
    """检索这次做不了：库不存在、还没建索引、或索引与当前 embedding 对不上。

    存在的理由是 Cowork 需要 catch 它，而 Cowork 不能 import `app.rag`。各后端的具体
    异常（`KbNotFoundError` / `KbIndexError`）继承这个类，Cowork 侧只认这一个类型。

    消息按约束 4 写成可执行指令，可以原样递给模型或用户。
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
    # 在哪个命名知识库里搜。None = 由后端自己决定（本地 KB 在只有一个库时用那一个，
    # 有多个则报错而不是随便挑）。
    #
    # 放在请求里而不是加进 `RagService.search` 的签名，是为了让不支持命名库的后端不必
    # 长出一个它兑现不了的参数。但**不支持不等于可以忽略**：拿到非空 slug 却当没看见，
    # 回答会带着看起来很正经的出处，而那些出处来自另一份资料。这类后端必须显式抛
    # `KnowledgeUnavailableError`。
    kb_slug: str | None = None
    # 用这个 KB 的哪一版索引。None = active 那一版。
    #
    # 存在的理由是评测：baseline 与 candidate 指向同一个 KB 的两个版本，语料完全相同，
    # 差异只有那一组 (embedding, 引擎, 切分, 融合常数)。复制一遍语料再各建一个 KB 也能
    # 做 A/B，但那样两边的文档集合是"看起来一样"，不是同一份。
    #
    # 与 `kb_slug` 同一条纪律：不支持版本的后端拿到非空值必须显式报错，不能当没看见。
    kb_version_id: str | None = None


@dataclass(frozen=True)
class EvidenceBundle:
    """跨 Cowork/RAG 边界的稳定证据契约，不泄露 ORM 或裸 chunk。"""

    evidence: tuple[EvidenceSegment, ...]
    retrieved_chunks: int
    backend: str


@runtime_checkable
class RagService(Protocol):
    """`runtime_checkable` 是给组装根用的：worker 要判断 ctx 里注入的那个对象是不是
    一个 rag。只查方法名存不存在，不查签名——够用，且不需要为此在 ctx 里另立一个标记。
    """

    async def search(
        self,
        gateway: ModelGateway,
        request: RagSearchRequest,
    ) -> EvidenceBundle: ...
