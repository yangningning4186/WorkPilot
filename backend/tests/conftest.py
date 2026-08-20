import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 集成测试中的既有服务/仓储用例显式覆盖 PostgreSQL 路径；SQLite 发布路径由
# test_local_cowork_store 等套件单独启用，避免依赖产品默认值而漏测任一后端。
os.environ.setdefault("COWORK_STORE_BACKEND", "postgres")

from app.core.config import get_settings

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://workpilot:workpilot@localhost:5432/workpilot_test",
)


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    if not TEST_DATABASE_URL.rsplit("/", maxsplit=1)[-1].endswith("_test"):
        raise RuntimeError("集成测试拒绝使用名称不以 _test 结尾的数据库")

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    yield TEST_DATABASE_URL


@pytest_asyncio.fixture
async def db_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    cowork_attachments, artifacts, capability_grants, session_roots,
                    skill_distillation_jobs, skill_candidate_evidence, skill_candidates,
                    memory_extraction_jobs, memories,
                    feedback, llm_calls, cost_reservations, daily_cost_budgets,
                    eval_results, eval_runs, eval_items, eval_datasets,
                    run_events, messages, agent_runs, conversations, demo_sessions,
                    chunks, parsed_block_locations, parsed_blocks,
                    document_versions, documents, sources
                CASCADE
                """
            )
        )
