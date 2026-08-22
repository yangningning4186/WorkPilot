"""知识库管理接口的用例（无需 PostgreSQL：这些端点只碰磁盘）。

管理界面能不能用，全看这几件事：owner 之外的人碰不到、非法 slug 在建库时就被挡住、
导入立刻返回而不是把 HTTP 连接挂住、以及"删了库但会话还挂着它"这个必然会发生的状态
不会把接口打崩。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.main import create_app
from app.rag.kb.jobs import KbIndexingJobs, default_indexing_jobs


@pytest.fixture(autouse=True)
def _clean_jobs() -> AsyncIterator[None]:
    """作业表是进程内单例，用例之间必须清干净，否则"同一个库不能并发建"会串味。"""
    jobs = default_indexing_jobs()
    jobs.__dict__.update(KbIndexingJobs().__dict__)
    yield
    jobs.__dict__.update(KbIndexingJobs().__dict__)


class _NoSession:
    """挂载走 SQLite store 时一行 SQL 都不会碰，但接口仍然会 commit（Postgres 那条要）。"""

    async def commit(self) -> None:
        return None


async def _no_session() -> AsyncIterator[_NoSession]:
    yield _NoSession()


def _client(tmp_path: Path) -> httpx.AsyncClient:
    """owner 身份直接覆盖掉。

    "非 owner 拿 401" 不在这里测：那是整个 cowork router 的依赖，不是知识库端点自己的
    行为，而且不覆盖身份就会让请求落到真的 Postgres 连接池上（`require_owner_identity`
    要查 demo 会话），在没有集成库的单元测试里必然炸。
    """
    app = create_app()
    # embedding 端点显式留空：这些用例验的是接口行为，不该在测试机上真去敲推理服务。
    # 后台作业会立刻拿到"没有配置 EMBEDDING_BASE_URL"这条可执行错误，行为确定且离线。
    app.dependency_overrides[get_settings] = lambda: Settings(
        knowledge_base_path=tmp_path / "kb", embedding_base_url=""
    )
    app.dependency_overrides[require_owner_identity] = lambda: None
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_create_list_and_delete(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        created = await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "papers", "name": "我的论文库", "description": "RAG 相关"},
        )
        assert created.status_code == 201
        assert created.json() == {
            "slug": "papers",
            "name": "我的论文库",
            "description": "RAG 相关",
            "document_count": 0,
            # 刚建的库还没有索引。界面据此显示"未建索引"，而不是等用户提问时才发现。
            "is_indexed": False,
            "embedding": None,
            "documents": [],
        }

        listed = await client.get("/api/v1/cowork/knowledge-bases")
        assert [item["slug"] for item in listed.json()["items"]] == ["papers"]

        assert (await client.delete("/api/v1/cowork/knowledge-bases/papers")).status_code == 204
        assert (await client.get("/api/v1/cowork/knowledge-bases")).json()["items"] == []
        # 删第二次是 404，不是静默成功——界面才敢把"已删除"当成事实。
        assert (await client.delete("/api/v1/cowork/knowledge-bases/papers")).status_code == 404


async def test_illegal_slug_is_refused_with_an_actionable_message(tmp_path: Path) -> None:
    """slug 同时是目录名和路径穿越的防线，必须在建库这一步就挡住。"""
    async with _client(tmp_path) as client:
        response = await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "../escape", "name": "坏库", "description": ""},
        )

    assert response.status_code == 422
    assert "只能用小写字母、数字和连字符" in response.json()["detail"]
    assert not (tmp_path / "kb" / "escape").exists()


async def test_duplicate_slug_is_refused(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        body = {"slug": "papers", "name": "论文", "description": ""}
        assert (await client.post("/api/v1/cowork/knowledge-bases", json=body)).status_code == 201
        again = await client.post("/api/v1/cowork/knowledge-bases", json=body)

    assert again.status_code == 422
    assert "已存在" in again.json()["detail"]


async def test_adding_to_a_missing_kb_is_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.post(
            "/api/v1/cowork/knowledge-bases/ghost/documents",
            json={"paths": [str(tmp_path)]},
        )

    assert response.status_code == 404


async def test_paths_with_nothing_importable_are_refused_before_a_job_starts(
    tmp_path: Path,
) -> None:
    """空导入不该留下一个"完成了但什么都没做"的作业，那只会让人以为加成功了。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    async with _client(tmp_path) as client:
        await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "papers", "name": "论文", "description": ""},
        )
        response = await client.post(
            "/api/v1/cowork/knowledge-bases/papers/documents", json={"paths": [str(empty)]}
        )

        assert response.status_code == 422
        assert "没有可导入的文件" in response.json()["detail"]
        assert (
            await client.get("/api/v1/cowork/knowledge-bases/papers/indexing")
        ).json() is None


async def _await_job(client: httpx.AsyncClient, slug: str) -> dict[str, object]:
    """等作业跑完。作业里有真的 to_thread 调用，让不出控制权的空转是等不到的。"""
    for _ in range(200):
        await asyncio.sleep(0.02)
        job = (await client.get(f"/api/v1/cowork/knowledge-bases/{slug}/indexing")).json()
        if job is not None and job["status"] != "running":
            return job
    raise AssertionError("作业没有在预期时间内结束")


async def test_import_returns_immediately_with_a_job(tmp_path: Path) -> None:
    """解析加 embedding 是分钟级的活，接口必须立刻回来（CLAUDE.md：worker 不依附 HTTP 连接）。"""
    source = tmp_path / "docs"
    source.mkdir()
    (source / "rrf.md").write_text("# RRF\n\n排名层面融合。\n", encoding="utf-8")

    async with _client(tmp_path) as client:
        await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "papers", "name": "论文", "description": ""},
        )
        response = await client.post(
            "/api/v1/cowork/knowledge-bases/papers/documents", json={"paths": [str(source)]}
        )

        assert response.status_code == 202
        body = response.json()
        assert body["slug"] == "papers"
        assert body["status"] == "running"
        assert "1" in body["stage"]

        # 作业跑完（这里必然失败，因为端点是空的）之后，状态要能被轮询到——而不是
        # 变成一个没有人接的异常，让界面永远显示"正在建索引"。
        finished = await _await_job(client, "papers")
        assert finished["status"] == "failed"
        assert "EMBEDDING_BASE_URL" in finished["error"]


async def test_rebuild_without_documents_is_refused(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "papers", "name": "论文", "description": ""},
        )
        response = await client.post("/api/v1/cowork/knowledge-bases/papers/rebuild")

    assert response.status_code == 422
    assert "还没有文档" in response.json()["detail"]


async def test_indexing_status_is_null_for_an_untouched_kb(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        await client.post(
            "/api/v1/cowork/knowledge-bases",
            json={"slug": "papers", "name": "论文", "description": ""},
        )
        response = await client.get("/api/v1/cowork/knowledge-bases/papers/indexing")

    assert response.status_code == 200
    assert response.json() is None


# --- 会话挂载 ------------------------------------------------------------


async def test_mounting_round_trips_and_refuses_a_missing_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """挂载前必须确认库存在。

    数据库那一列没有外键（KB 的事实来源是磁盘上的 manifest，不是表），所以校验只能发生在
    接口这一层。漏掉它，用户会在挂载时看到成功、在下一次提问时才看到检索失败。
    """
    from app.cowork_store.factory import close_local_cowork_stores, initialize_local_cowork_stores
    from app.runstore import conversations as conversations_module

    # conftest 把全局 backend 钉成了 postgres，而挂载读写走的是 `cowork_store()`。
    # 这条用例要验的是接口行为（挂载前校验库存在、卸载能回到 null），不是选后端那段逻辑，
    # 所以直接把 store 换掉，比让整个进程改后端干净。
    stores_holder: dict[str, object] = {}
    monkeypatch.setattr(
        conversations_module, "cowork_store", lambda: stores_holder["state"]
    )

    settings = Settings(
        knowledge_base_path=tmp_path / "kb",
        embedding_base_url="",
        cowork_store_backend="sqlite",
        cowork_data_path=tmp_path / "state",
    )
    stores = await initialize_local_cowork_stores(settings)
    stores_holder["state"] = stores.state
    try:
        conversation_id = await stores.state.create_conversation(title="论文问答")
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[require_owner_identity] = lambda: None
        app.dependency_overrides[get_db_session] = _no_session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            base = f"/api/v1/cowork/sessions/{conversation_id}/knowledge-base"
            assert (await client.get(base)).json() == {"slug": None}

            missing = await client.put(base, json={"slug": "ghost"})
            assert missing.status_code == 404
            assert (await client.get(base)).json() == {"slug": None}

            await client.post(
                "/api/v1/cowork/knowledge-bases",
                json={"slug": "papers", "name": "论文", "description": ""},
            )
            assert (await client.put(base, json={"slug": "papers"})).status_code == 200
            assert (await client.get(base)).json() == {"slug": "papers"}

            # 卸载。
            assert (await client.put(base, json={"slug": None})).json() == {"slug": None}
    finally:
        await close_local_cowork_stores()
