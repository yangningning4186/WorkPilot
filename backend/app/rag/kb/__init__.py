"""本地个人知识库：命名 KB、LlamaIndex + FAISS/BM25 混合检索。

与 `app/rag/retrieval/`（PostgreSQL + pgvector）是两条**并列**的检索实现，不是替换关系：
这一条面向桌面端，KB 就是磁盘上一个目录，不需要起 Postgres，溯源精度到页；那一条面向
评测，版本激活与可复现性由数据库事务保证，溯源精度到 bbox。两者共同对外满足
`app/knowledge_contracts.py` 的 `RagService`，所以调用方可以互换。

自底向上分层：

* :mod:`.paths`     —— 磁盘布局与命名，不碰内容。
* :mod:`.manifest`  —— KB 清单与 embedding 签名，纯数据 + 原子读写。
* :mod:`.documents` —— 解析结果 → `Document`（PDF 按页一个，页码因此跟着片段走）。
* :mod:`.index`     —— 切分、建索引、dense + BM25 混合检索。
* :mod:`.service`   —— 调用方使用的 KB 增删查改与检索。
"""

from app.core.config import Settings
from app.rag.kb.index import KbIndexError
from app.rag.kb.manifest import EmbeddingSignature, KbDocument, KbManifest
from app.rag.kb.paths import KbNameError, slugify, validate_name, validate_slug
from app.rag.kb.service import KbNotFoundError, LocalKbService


def local_kb_service(settings: Settings) -> LocalKbService:
    """按配置组装本地 KB 服务。

    组装根（`worker/cowork_run.py`）、API 层和 CLI 都从这里拿，免得各自展开一次
    `~` 又各自漏掉——三处对同一个根目录的写法只要有一处不同，用户就会看到"我明明建了库"。
    """
    return LocalKbService(settings.knowledge_base_path.expanduser(), settings=settings)


__all__ = [
    "EmbeddingSignature",
    "KbDocument",
    "KbIndexError",
    "KbManifest",
    "KbNameError",
    "KbNotFoundError",
    "LocalKbService",
    "local_kb_service",
    "slugify",
    "validate_name",
    "validate_slug",
]
