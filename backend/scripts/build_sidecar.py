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
ARTIFACT_PYTHON_BASENAME = "workpilot-artifact-python"
PPTX_RENDERER_BASENAME = "workpilot-pptx-renderer"
PPTX_RENDERER_SOURCE = (
    BACKEND_ROOT
    / "app"
    / "cowork"
    / "skills"
    / "builtin"
    / "pptx"
    / "scripts"
    / "pptxgenjs"
)


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


def _run_pyinstaller(*, spec_name: str, executable_name: str, build_name: str) -> Path:
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            "PyInstaller is missing. Run `cd backend && uv sync --group dev` first."
        )

    build_root = BACKEND_ROOT / "build" / build_name
    dist = build_root / "dist"
    work = build_root / "work"
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(build_root / "pyinstaller-config")
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
            str(BACKEND_ROOT / "packaging" / spec_name),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
    )
    extension = ".exe" if sys.platform == "win32" else ""
    output = dist / f"{executable_name}{extension}"
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


def _smoke_test_artifact_python(binary: Path) -> dict[str, object]:
    completed = subprocess.run(
        [str(binary), "--workpilot-selftest"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Artifact Python self-test returned invalid JSON: {completed.stdout[-2000:]}"
        ) from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"Artifact Python self-test failed: {result!r}")
    return result


def _pkg_target(target: str) -> str:
    architecture = (
        "arm64"
        if target.startswith(("aarch64-", "arm64-"))
        else "x64"
        if target.startswith("x86_64-")
        else ""
    )
    platform = (
        "macos"
        if "apple-darwin" in target
        else "win"
        if "windows" in target
        else "linux"
        if "linux" in target
        else ""
    )
    if not architecture or not platform:
        raise RuntimeError(f"PptxGenJS renderer 尚不支持打包目标 {target}")
    return f"node22-{platform}-{architecture}"


def _patch_pptxgenjs_for_pkg(project: Path) -> None:
    """Replace PptxGenJS' Node-only dynamic imports for pkg's snapshot VM.

    PptxGenJS 4.0.1 lazily imports ``node:fs`` and ``node:https`` so its browser
    bundle stays portable.  The pkg snapshot VM does not install a dynamic import
    callback, while static built-in requires are fully supported.  Keep this
    narrow, version-locked transform in the generated build tree and fail closed
    if the pinned upstream source changes.
    """

    source = project / "node_modules" / "pptxgenjs" / "dist" / "pptxgen.cjs.js"
    if not source.is_file():
        raise RuntimeError("PptxGenJS 安装后缺少 CommonJS 入口")
    content = source.read_text(encoding="utf-8")
    replacements = {
        "({ default: fs } = yield import('node:fs'));": "fs = require('node:fs');",
        "({ default: https } = yield import('node:https'));": "https = require('node:https');",
        "const { promises: fs } = yield import('node:fs');": (
            "const { promises: fs } = require('node:fs');"
        ),
    }
    for original, replacement in replacements.items():
        if content.count(original) != 1:
            raise RuntimeError("PptxGenJS 4.0.1 动态导入结构已变化，拒绝生成未验证的桌面 Renderer")
        content = content.replace(original, replacement)
    source.write_text(content, encoding="utf-8")


def _build_pptx_renderer(*, target: str) -> Path:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        raise RuntimeError("PptxGenJS renderer 打包需要 Node.js 与 npm")
    if not (PPTX_RENDERER_SOURCE / "package-lock.json").is_file():
        raise RuntimeError("PptxGenJS renderer 缺少 package-lock.json")

    build_root = BACKEND_ROOT / "build" / "pptx-renderer"
    shutil.rmtree(build_root, ignore_errors=True)
    project = build_root / "project"
    shutil.copytree(
        PPTX_RENDERER_SOURCE,
        project,
        ignore=shutil.ignore_patterns("node_modules", "dist", ".DS_Store"),
    )
    subprocess.run(
        [npm, "ci", "--ignore-scripts"],
        cwd=project,
        check=True,
        timeout=300,
    )
    _patch_pptxgenjs_for_pkg(project)
    cli = project / "node_modules" / "@yao-pkg" / "pkg" / "lib-es5" / "bin.js"
    if not cli.is_file():
        raise RuntimeError("@yao-pkg/pkg 安装后缺少 CLI")
    extension = ".exe" if "windows" in target else ""
    output = build_root / "dist" / f"{PPTX_RENDERER_BASENAME}{extension}"
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    # Keep the downloaded base Node runtime outside the renderer's clean build tree.
    # Repeated desktop builds can then reuse it without touching any user-level cache.
    environment["PKG_CACHE_PATH"] = str(BACKEND_ROOT / "build" / "pkg-cache")
    subprocess.run(
        [
            node,
            str(cli),
            ".",
            "--target",
            _pkg_target(target),
            "--output",
            str(output),
        ],
        cwd=project,
        env=environment,
        check=True,
        timeout=900,
    )
    if not output.is_file():
        raise RuntimeError(f"PptxGenJS renderer 打包未生成 {output}")
    output.chmod(output.stat().st_mode | 0o111)
    return output


def _smoke_test_pptx_renderer(binary: Path) -> dict[str, object]:
    completed = subprocess.run(
        [str(binary), "--workpilot-selftest"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"PptxGenJS renderer self-test returned invalid JSON: {completed.stdout[-2000:]}"
        ) from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"PptxGenJS renderer self-test failed: {result!r}")
    return result


def _stage_external_binary(binary: Path, *, basename: str, target: str) -> Path:
    extension = ".exe" if "windows" in target else ""
    target_dir = TAURI_ROOT / "binaries"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / f"{basename}-{target}{extension}"
    shutil.copy2(binary, staged)
    staged.chmod(staged.stat().st_mode | 0o111)
    return staged


def main() -> int:
    args = _parser().parse_args()
    host = _rust_host()
    target = args.target or host
    if target != host:
        raise RuntimeError(
            f"Frozen Python sidecars cannot be cross-compiled: requested {target}, host is {host}."
        )
    built = _run_pyinstaller(
        spec_name="workpilot_sidecar.spec",
        executable_name=SIDECAR_BASENAME,
        build_name="sidecar",
    )
    _smoke_test(built)
    artifact_python = _run_pyinstaller(
        spec_name="artifact_python_runtime.spec",
        executable_name=ARTIFACT_PYTHON_BASENAME,
        build_name="artifact-python",
    )
    artifact_runtime = _smoke_test_artifact_python(artifact_python)
    pptx_renderer = _build_pptx_renderer(target=target)
    pptx_runtime = _smoke_test_pptx_renderer(pptx_renderer)

    staged = _stage_external_binary(built, basename=SIDECAR_BASENAME, target=target)
    staged_artifact_python = _stage_external_binary(
        artifact_python,
        basename=ARTIFACT_PYTHON_BASENAME,
        target=target,
    )
    staged_pptx_renderer = _stage_external_binary(
        pptx_renderer,
        basename=PPTX_RENDERER_BASENAME,
        target=target,
    )
    target_dir = TAURI_ROOT / "binaries"
    browsers = [] if args.skip_browser else _stage_browser()
    manifest = {
        "schema_version": 1,
        "target": target,
        "filename": staged.name,
        "sha256": _sha256(staged),
        "size_bytes": staged.stat().st_size,
        "python": sys.version.split()[0],
        "playwright_browsers": browsers,
        "artifact_python": {
            "filename": staged_artifact_python.name,
            "sha256": _sha256(staged_artifact_python),
            "size_bytes": staged_artifact_python.stat().st_size,
            **artifact_runtime,
        },
        "pptx_renderer": {
            "filename": staged_pptx_renderer.name,
            "sha256": _sha256(staged_pptx_renderer),
            "size_bytes": staged_pptx_renderer.stat().st_size,
            **pptx_runtime,
        },
    }
    (target_dir / "sidecar-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
