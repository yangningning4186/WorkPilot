from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval.artifact_retention import RetentionError, apply_retention, plan_retention


def test_retention_keeps_recent_and_minimum_latest(tmp_path: Path) -> None:
    root = tmp_path / "eval/outputs/nightly"
    root.mkdir(parents=True)
    names = [
        "20260101T000000.000000Z-full-v2",
        "20260102T000000.000000Z-full-v2",
        "20260103T000000.000000Z-full-v2",
        "20260219T000000.000000Z-full-v2",
    ]
    for name in names:
        (root / name).mkdir()
    (root / "notes").mkdir()

    decisions = plan_retention(
        root,
        allowed_root=root,
        now=datetime(2026, 2, 20, tzinfo=UTC),
        older_than_days=30,
        keep_latest=2,
    )

    assert [item.path for item in decisions] == [names[1], names[0]]
    apply_retention(root, decisions)
    assert not (root / names[0]).exists()
    assert not (root / names[1]).exists()
    assert (root / names[2]).is_dir()
    assert (root / names[3]).is_dir()
    assert (root / "notes").is_dir()


def test_retention_rejects_broad_or_symlink_root(tmp_path: Path) -> None:
    allowed = tmp_path / "eval/outputs/nightly"
    allowed.mkdir(parents=True)
    with pytest.raises(RetentionError, match="exactly"):
        plan_retention(
            tmp_path,
            allowed_root=allowed,
            now=datetime.now(UTC),
            older_than_days=30,
            keep_latest=7,
        )

    link = tmp_path / "nightly-link"
    link.symlink_to(allowed, target_is_directory=True)
    with pytest.raises(RetentionError, match="symlink"):
        plan_retention(
            link,
            allowed_root=allowed,
            now=datetime.now(UTC),
            older_than_days=30,
            keep_latest=7,
        )
