"""资料库读模型。

重点不是"能列出文档", 而是**候选版本解析失败时列表说了实话**:
约束 10 保证旧版继续服务, 于是失败会变成一个没人看见的沉默降级——
资料库页存在的意义就是把它显示出来。
"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.db import get_db_session
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway
from app.main import create_app
from app.services.library import get_library_overview
from app.services.markdown_ingestion import ingest_markdown_file
from tests.fakes import DeterministicProvider

pytestmark = pytest.mark.integration


async def _ingest_one(session: AsyncSession, tmp_path: Path, name: str = "dense.md") -> None:
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024, audit_sink=SqlLlmCallAudit(session))
    library = tmp_path / "library"
    library.mkdir(exist_ok=True)
    (library / name).write_text(
        "# 检索\n\n## 稠密检索\n\n稠密检索通过向量相似度召回语义相关内容。\n",
        encoding="utf-8",
    )
    await ingest_markdown_file(session, gateway, path=Path(name), library_root=library)


async def test_ready_document_reports_chunk_and_block_counts(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await _ingest_one(db_session, tmp_path)

    overview = await get_library_overview(db_session)

    document = next(item for item in overview.documents if item.source_uri.endswith("dense.md"))
    assert document.state == "ready"
    assert document.block_count > 0
    assert document.searchable_chunk_count > 0
    assert document.chunk_count >= document.searchable_chunk_count
    # markdown 没有 bbox, 前端据此提示"仅文本"。
    assert document.locatable is False
    assert overview.totals.documents >= 1
    assert overview.totals.failed == 0


async def test_failed_candidate_version_is_surfaced_while_old_version_still_serves(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await _ingest_one(db_session, tmp_path)
    document_id = (await db_session.execute(text("SELECT id FROM documents LIMIT 1"))).scalar_one()

    # 模拟"新版本解析失败": 候选版本入库但没有激活, 旧版本仍然是 active。
    await db_session.execute(
        text(
            """
            INSERT INTO document_versions
                (id, document_id, version_no, content_hash, parser, parser_version,
                 parse_status, parse_error, embedding_model, embedding_provider,
                 embedding_revision)
            VALUES (:id, :document_id, 2, 'hash-v2', 'mineru', '3.4.4',
                    'failed', 'MinerU 子进程超时', 'fake-embedding', 'deterministic_test',
                    'unversioned')
            """
        ),
        {"id": uuid7(), "document_id": document_id},
    )

    overview = await get_library_overview(db_session)
    document = next(item for item in overview.documents if item.document_id == document_id)

    assert document.state == "failed"
    assert document.parse_error == "MinerU 子进程超时"
    # 关键: 旧版本还在, 检索没断——版本号和 chunk 数都还是 v1 的。
    assert document.version_no == 1
    assert document.searchable_chunk_count > 0
    assert overview.totals.failed == 1


async def test_library_endpoint_is_readable_without_admin_and_hides_local_root(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await _ingest_one(db_session, tmp_path)
    await db_session.commit()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 没有 admin cookie: 资料库是只读产品页, 不该要求 admin。
        response = await client.get("/api/v1/library")

    assert response.status_code == 200
    payload = response.json()
    assert payload["documents"]
    assert payload["sources"]
    # 本地绝对路径属于 owner 环境信息, 不进浏览器。
    assert "config" not in payload["sources"][0]
    assert "root" not in payload["sources"][0]
