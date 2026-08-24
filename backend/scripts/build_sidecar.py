"""Build and stage the frozen Python sidecar expected by Tauri externalBin."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
TAURI_ROOT = PROJECT_ROOT / "frontend" / "src-tauri"
SIDECAR_BASENAME = "workpilot-sidecar"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="Rust target triple; defaults to rustc host")
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Do not bundle the installed Playwright headless Chromium runtime",
    )
    return parser


def _rust_host() -> str:
    completed = subprocess.run(
        ["rustc", "-vV"],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"^host:\s*(\S+)\s*$", completed.stdout, re.MULTILINE)
    if match is None:
        raise RuntimeError("rustc -vV did not report a host triple")
    return match.group(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_pyinstaller() -> Path:
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            "PyInstaller is missing. Run `cd backend && uv sync --group dev` first."
        )

    build_root = BACKEND_ROOT / "build" / "sidecar"
    dist = build_root / "dist"
    work = build_root / "work"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            str(BACKEND_ROOT / "packaging" / "workpilot_sidecar.spec"),
        ],
        cwd=BACKEND_ROOT,
        check=True,
    )
    extension = ".exe" if sys.platform == "win32" else ""
    output = dist / f"{SIDECAR_BASENAME}{extension}"
    if not output.is_file():
        raise RuntimeError(f"PyInstaller completed without producing {output}")
    return output


def _stage_browser() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        cwd=BACKEND_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    locations = [
        Path(value.strip())
        for value in re.findall(r"^\s*Install location:\s*(.+?)\s*$", completed.stdout, re.MULTILINE)
    ]
    selected = {
        path
        for path in locations
        if path.name.startswith("chromium_headless_shell-") or path.name.startswith("ffmpeg-")
    }
    missing = [path for path in selected if not path.is_dir()]
    if not selected or missing:
        rendered = ", ".join(str(path) for path in missing) or "no compatible runtime reported"
        raise RuntimeError(
            "Playwright browser runtime is not installed ("
            f"{rendered}). Run `cd backend && uv run playwright install chromium`."
        )

    destination = TAURI_ROOT / "resources" / "ms-playwright"
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for source in sorted(selected):
        shutil.copytree(source, destination / source.name, symlinks=True)
        staged.append(source.name)
    return staged


def _smoke_test(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="workpilot-sidecar-smoke-") as directory:
        data_root = Path(directory)
        launch_token = "sidecar-build-smoke-token-00000000000000000000000000000000"
        environment = os.environ.copy()
        environment.update(
            {
                "COWORK_DATA_PATH": str(data_root / "data"),
                "KNOWLEDGE_BASE_PATH": str(data_root / "kb"),
                "COWORK_ATTACHMENT_PATH": str(data_root / "attachments"),
                "COWORK_SKILL_CANDIDATES_PATH": str(data_root / "skill-candidates"),
                "SECRET_STORE_KEY_PATH": str(data_root / "secrets" / "master.key"),
                "OFFICE_PREVIEW_CACHE_PATH": str(data_root / "preview-cache"),
                "DESKTOP_MODE_ENABLED": "true",
                "DESKTOP_LAUNCH_TOKEN": launch_token,
                "LOG_LEVEL": "WARNING",
            }
        )
        subprocess.run([str(binary), "migrate"], env=environment, check=True, timeout=120)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log_path = data_root / "sidecar-smoke.log"
        with log_path.open("w", encoding="utf-8") as log:
            # 构建机常设 HTTP(S)_PROXY；localhost 健康检查绝不能被送去公司代理。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            process = subprocess.Popen(
                [str(binary), "api", "--port", str(port)],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 120
                url = f"http://127.0.0.1:{port}/health/ready"
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.flush()
                        details = log_path.read_text("utf-8")[-8_000:]
                        raise RuntimeError(
                            "Frozen sidecar API exited during smoke test with "
                            f"{process.returncode}:\n{details}"
                        )
                    request = urllib.request.Request(
                        url,
                        headers={"x-workpilot-launch-token": launch_token},
                    )
                    try:
                        with opener.open(request, timeout=2) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)
                else:
                    log.flush()
                    details = log_path.read_text("utf-8")[-8_000:]
                    raise RuntimeError(
                        "Frozen sidecar API did not become ready within 120 seconds:\n" + details
                    )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def main() -> int:
    args = _parser().parse_args()
    host = _rust_host()
    target = args.target or host
    if target != host:
        raise RuntimeError(
            f"Frozen Python sidecars cannot be cross-compiled: requested {target}, host is {host}."
        )
    built = _run_pyinstaller()
    _smoke_test(built)

    extension = ".exe" if "windows" in target else ""
    target_dir = TAURI_ROOT / "binaries"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / f"{SIDECAR_BASENAME}-{target}{extension}"
    shutil.copy2(built, staged)
    staged.chmod(staged.stat().st_mode | 0o111)
    browsers = [] if args.skip_browser else _stage_browser()
    manifest = {
        "schema_version": 1,
        "target": target,
        "filename": staged.name,
        "sha256": _sha256(staged),
        "size_bytes": staged.stat().st_size,
        "python": sys.version.split()[0],
        "playwright_browsers": browsers,
    }
    (target_dir / "sidecar-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
