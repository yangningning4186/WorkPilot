import os
from pathlib import Path

import pytest

from app.core.single_instance import SidecarAlreadyRunningError, desktop_sidecar_lock


def test_desktop_sidecar_lock_excludes_a_second_worker(tmp_path: Path) -> None:
    data_path = tmp_path / "cowork-data"

    with desktop_sidecar_lock(data_path) as lock_path:
        assert lock_path.read_text() == f"pid={os.getpid()}"
        assert lock_path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(SidecarAlreadyRunningError, match="已由另一个 WorkPilot sidecar 占用"):
            with desktop_sidecar_lock(data_path):
                raise AssertionError("第二个 sidecar 不应获得锁")

    with desktop_sidecar_lock(data_path):
        pass
