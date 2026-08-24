from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agent_core.contracts import RunEvent
from eval.replay import verify_bundle
from eval.run_replay_export import (
    RunReplayExportError,
    export_run,
    main,
    write_bundle,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000123")


class _Store:
    def __init__(self, *, status: str = "done") -> None:
        self.run = SimpleNamespace(status=status, workflow_type="cowork")
        self.events = [
            RunEvent(
                RUN_ID,
                1,
                "message.snapshot",
                {"text": "本地敏感回答"},
                datetime(2026, 8, 24, tzinfo=UTC),
            ),
            RunEvent(
                RUN_ID,
                2,
                "run.done",
                {"workflow_type": "cowork", "status": status},
                datetime(2026, 8, 24, 0, 0, 1, tzinfo=UTC),
            ),
        ]

    async def get_run(self, run_id: UUID):
        return self.run if run_id == RUN_ID else None

    async def list_events(
        self, *, run_id: UUID, after_seq: int = 0, limit: int | None = None
    ) -> list[RunEvent]:
        assert run_id == RUN_ID
        assert after_seq == 0
        assert limit is None
        return self.events


@pytest.mark.asyncio
async def test_completed_run_exports_a_sealed_verifiable_bundle(tmp_path: Path) -> None:
    bundle = await export_run(_Store(), run_id=RUN_ID, case_id="production-case")
    report = verify_bundle(bundle)

    assert report.valid is True
    assert bundle["origin"] == "local_production_export"
    assert bundle["sensitive"] is True
    assert bundle["cases"][0]["events"][0]["data"]["text"] == "本地敏感回答"

    output = write_bundle(bundle, tmp_path / "run.json")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["integrity"] == bundle["integrity"]
    with pytest.raises(RunReplayExportError, match="拒绝覆盖"):
        write_bundle(bundle, output)


@pytest.mark.asyncio
async def test_active_or_protocol_invalid_run_is_not_exported() -> None:
    with pytest.raises(RunReplayExportError, match="尚未进入终态"):
        await export_run(_Store(status="executing"), run_id=RUN_ID)

    store = _Store()
    store.events = [store.events[1]]
    store.events[0] = RunEvent(
        RUN_ID,
        2,
        "run.done",
        {"workflow_type": "cowork", "status": "done"},
        datetime(2026, 8, 24, tzinfo=UTC),
    )
    with pytest.raises(RunReplayExportError, match="seq_gap"):
        await export_run(store, run_id=RUN_ID)


def test_cli_requires_explicit_sensitive_output_acknowledgement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--run-id",
            str(RUN_ID),
            "--output",
            str(tmp_path / "must-not-exist.json"),
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "must-not-exist.json").exists()
    assert "--acknowledge-sensitive-output" in capsys.readouterr().err
