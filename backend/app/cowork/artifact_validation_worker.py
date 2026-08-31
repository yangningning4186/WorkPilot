"""受限 Artifact Validator 子进程入口。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.cowork.artifact_validation import validate_artifact
from app.cowork.process_limits import apply_process_limits


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 6:
        print(json.dumps({"ok": False, "error": "Artifact 校验参数无效"}))
        return 2
    path = Path(arguments[0])
    render_visual = arguments[1] == "1"
    memory_mb = int(arguments[2])
    cpu_seconds = int(arguments[3])
    pids_limit = int(arguments[4])
    max_file_bytes = int(arguments[5])
    apply_process_limits(
        memory_mb=memory_mb,
        pids_limit=pids_limit,
        cpu_seconds=cpu_seconds,
        file_size_bytes=max_file_bytes * 4,
    )
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError("候选 Artifact 必须是普通文件")
        size = path.stat().st_size
        if size < 1 or size > max_file_bytes:
            raise ValueError(f"候选 Artifact 大小 {size} bytes 不在允许范围内")
        report = validate_artifact(path, render_visual=render_visual)
    except Exception as error:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, "report": report.model_dump(mode="json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - 独立子进程入口
    raise SystemExit(main())
