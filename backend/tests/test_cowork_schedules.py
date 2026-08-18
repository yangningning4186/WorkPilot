from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.api.dependencies import (
    get_run_bus,
    get_run_queue_dependency,
    require_owner_identity,
)
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.run_bus import InMemoryRunBus
from app.main import create_app
from app.services.cowork_interactions import create_inbox_item, list_unattended_inbox
from app.services.cowork_permissions import create_session_root
from app.services.cowork_schedules import (
    ScheduleError,
    compute_next_run,
    create_schedule,
    dispatch_due_schedules,
    get_schedule,
    list_dispatchable_scheduled_runs,
    update_schedule,
)
from app.services.request_identity import RequestIdentity
from app.services.runs import create_run, ensure_conversation, get_run

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.run_ids: list[UUID] = []

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        self.run_ids.append(run_id)


async def _owner_workspace(db_session: AsyncSession, root: Path):
    conversation_id = await ensure_conversation(
        db_session, scope="local_owner", title="自动化测试"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(root),
        access_mode="read_write",
    )
    return conversation_id


def test_compute_next_run_supports_timezone_and_rejects_invalid_cron() -> None:
    base = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)

    next_run = compute_next_run(
        schedule_kind="cron",
        cron_expression="30 9 * * 1-5",
        run_at=None,
        timezone="Asia/Shanghai",
        after=base,
    )

    assert next_run == datetime(2026, 8, 19, 1, 30, tzinfo=UTC)
    with pytest.raises(ScheduleError, match="五段 cron"):
        compute_next_run(
            schedule_kind="cron",
            cron_expression="not cron",
            run_at=None,
            timezone="Asia/Shanghai",
            after=base,
        )


async def test_due_schedule_creates_unattended_run_and_global_inbox(
    db_session, tmp_path: Path
) -> None:
    conversation_id = await _owner_workspace(db_session, tmp_path)
    base = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)
    schedule = await create_schedule(
        db_session,
        conversation_id=conversation_id,
        title="晨报",
        goal="整理今天的项目摘要",
        schedule_kind="once",
        cron_expression=None,
        run_at=base + timedelta(hours=1),
        timezone="Asia/Shanghai",
        now=base,
    )

    run_ids = await dispatch_due_schedules(
        db_session,
        settings=Settings(cowork_skills_path=tmp_path / "missing-skills"),
        trigger="catchup",
        now=base + timedelta(hours=2),
    )

    assert len(run_ids) == 1
    run = await get_run(db_session, run_ids[0])
    assert run is not None
    assert run.schedule_id == schedule.id
    assert run.unattended is True
    assert run.run_trigger == "catchup"
    assert await list_dispatchable_scheduled_runs(db_session) == run_ids
    refreshed = await get_schedule(db_session, schedule_id=schedule.id)
    assert refreshed is not None
    assert refreshed.enabled is False
    assert refreshed.run_count == 1

    step_id = uuid7()
    await db_session.execute(
        text(
            """
            INSERT INTO agent_plan_steps
                (id, run_id, step_idx, description, tool, depends_on, status)
            VALUES (:id, :run_id, 0, '等待回复', 'ask_user', '{}', 'running')
            """
        ),
        {"id": step_id, "run_id": run.id},
    )
    item = await create_inbox_item(
        db_session,
        run_id=run.id,
        conversation_id=conversation_id,
        kind="ask_user",
        tool_call_id="scheduled-call",
        plan_step_id=step_id,
        request={"question": "选择摘要范围"},
    )
    records = await list_unattended_inbox(db_session)

    assert item.unattended is True
    assert records[0].item.id == item.id
    assert records[0].schedule_title == "晨报"


async def test_due_schedule_skips_when_conversation_has_active_run(
    db_session, tmp_path: Path
) -> None:
    conversation_id = await _owner_workspace(db_session, tmp_path)
    base = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)
    await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="正在运行的任务",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=10_000,
        workflow_type="cowork",
    )
    schedule = await create_schedule(
        db_session,
        conversation_id=conversation_id,
        title="不重叠",
        goal="不应该堆叠运行",
        schedule_kind="cron",
        cron_expression="0 * * * *",
        run_at=None,
        timezone="UTC",
        now=base,
    )

    run_ids = await dispatch_due_schedules(
        db_session,
        settings=Settings(),
        trigger="schedule",
        now=base + timedelta(hours=3, minutes=5),
    )

    assert run_ids == []
    refreshed = await get_schedule(db_session, schedule_id=schedule.id)
    assert refreshed is not None
    assert refreshed.skipped_count == 1
    assert refreshed.run_count == 0
    assert refreshed.next_run_at == datetime(2026, 8, 19, 4, 0, tzinfo=UTC)


async def test_schedule_update_rejects_fields_from_other_schedule_kind(
    db_session, tmp_path: Path
) -> None:
    conversation_id = await _owner_workspace(db_session, tmp_path)
    base = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)
    once = await create_schedule(
        db_session,
        conversation_id=conversation_id,
        title="单次",
        goal="只运行一次",
        schedule_kind="once",
        cron_expression=None,
        run_at=base + timedelta(hours=1),
        timezone="UTC",
        now=base,
    )

    with pytest.raises(ScheduleError, match="不能设置 cron_expression"):
        await update_schedule(
            db_session,
            schedule_id=once.id,
            changes={"cron_expression": "0 9 * * *"},
            now=base,
        )
    with pytest.raises(ScheduleError, match="title 不能为 null"):
        await update_schedule(
            db_session,
            schedule_id=once.id,
            changes={"title": None},
            now=base,
        )
    with pytest.raises(ScheduleError, match="缺少 run_at"):
        await update_schedule(
            db_session,
            schedule_id=once.id,
            changes={"enabled": False, "run_at": None},
            now=base,
        )


async def test_automation_api_creates_lists_and_runs_unattended(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_workspace(db_session, tmp_path)
    queue = RecordingQueue()
    bus = InMemoryRunBus()
    settings = Settings(cowork_skills_path=tmp_path / "missing-skills")

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(
        scope="local_owner"
    )
    transport = httpx.ASGITransport(app=app)
    run_at = datetime.now(UTC) + timedelta(days=1)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/automations",
            json={
                "conversation_id": str(conversation_id),
                "title": "一次性整理",
                "goal": "整理工作区摘要",
                "schedule_kind": "once",
                "run_at": run_at.isoformat(),
                "timezone": "Asia/Shanghai",
            },
        )
        listed = await client.get("/api/v1/automations")
        started = await client.post(
            f"/api/v1/automations/{created.json()['id']}/run"
        )
        inbox = await client.get("/api/v1/automations/inbox/items")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert started.status_code == 202
    assert started.json()["unattended"] is True
    assert started.json()["schedule_id"] == created.json()["id"]
    assert queue.run_ids == [UUID(started.json()["run_id"])]
    assert inbox.status_code == 200
    assert inbox.json() == {"items": [], "total": 0}
