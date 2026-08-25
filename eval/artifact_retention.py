"""Safely expire local nightly packages without touching promoted baselines.

Only direct children of ``eval/outputs/nightly`` whose names begin with the
runner's UTC timestamp are eligible.  The default mode is a dry run; deletion
requires the explicit ``--apply`` flag.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_PACKAGE_NAME = re.compile(r"^(?P<stamp>20\d{6}T\d{6}(?:\.\d{6})?Z)(?:-|$)")


class RetentionError(ValueError):
    """The requested prune target is unsafe or malformed."""


@dataclass(frozen=True)
class RetentionDecision:
    path: str
    created_at: str
    reason: str


def _package_time(path: Path) -> datetime | None:
    match = _PACKAGE_NAME.match(path.name)
    if match is None:
        return None
    stamp = match.group("stamp")
    pattern = "%Y%m%dT%H%M%S.%fZ" if "." in stamp else "%Y%m%dT%H%M%SZ"
    return datetime.strptime(stamp, pattern).replace(tzinfo=UTC)


def plan_retention(
    root: Path,
    *,
    allowed_root: Path,
    now: datetime,
    older_than_days: int,
    keep_latest: int,
) -> tuple[RetentionDecision, ...]:
    """Return deletion candidates after validating an exact, bounded root."""

    if older_than_days < 1:
        raise RetentionError("older_than_days must be positive")
    if keep_latest < 1:
        raise RetentionError("keep_latest must be positive")
    if now.tzinfo is None:
        raise RetentionError("now must include a timezone")

    resolved = root.resolve()
    expected = allowed_root.resolve()
    if resolved != expected:
        raise RetentionError(f"retention root must be exactly {expected}")
    if root.is_symlink():
        raise RetentionError("retention root must not be a symlink")
    if not root.exists():
        return ()
    if not root.is_dir():
        raise RetentionError("retention root must be a directory")

    packages: list[tuple[datetime, Path]] = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        created_at = _package_time(child)
        if created_at is not None:
            packages.append((created_at, child))
    packages.sort(key=lambda item: (item[0], item[1].name), reverse=True)

    cutoff = now.astimezone(UTC) - timedelta(days=older_than_days)
    decisions: list[RetentionDecision] = []
    for index, (created_at, path) in enumerate(packages):
        if index < keep_latest or created_at >= cutoff:
            continue
        decisions.append(
            RetentionDecision(
                path=path.name,
                created_at=created_at.isoformat(),
                reason=f"older_than_{older_than_days}_days_and_not_latest_{keep_latest}",
            )
        )
    return tuple(decisions)


def apply_retention(root: Path, decisions: Sequence[RetentionDecision]) -> None:
    """Delete only the already validated direct child packages."""

    resolved_root = root.resolve()
    for decision in decisions:
        target = root / decision.path
        if target.is_symlink() or not target.is_dir():
            raise RetentionError(f"candidate changed since planning: {decision.path}")
        resolved_target = target.resolve()
        if resolved_target.parent != resolved_root or _package_time(target) is None:
            raise RetentionError(f"unsafe retention candidate: {decision.path}")
        shutil.rmtree(target)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--keep-latest", type=int, default=7)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    root = _repo_root() / "eval/outputs/nightly"
    decisions = plan_retention(
        root,
        allowed_root=root,
        now=datetime.now(UTC),
        older_than_days=args.older_than_days,
        keep_latest=args.keep_latest,
    )
    if args.apply:
        apply_retention(root, decisions)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "root": "eval/outputs/nightly",
                "deleted_count": len(decisions) if args.apply else 0,
                "candidates": [asdict(item) for item in decisions],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
