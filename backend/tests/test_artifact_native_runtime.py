from pathlib import Path

import pytest

from app.artifact_python_runtime import EXPECTED_DISTRIBUTIONS, RUNTIME_PROFILE, main
from app.core.config import Settings
from scripts.build_sidecar import _patch_pptxgenjs_for_pkg, _pkg_target


def test_default_sandbox_uses_native_versioned_runtime() -> None:
    settings = Settings(_env_file=None)

    assert settings.cowork_sandbox_runtime == "auto"
    assert settings.cowork_sandbox_profile == RUNTIME_PROFILE
    assert settings.cowork_sandbox_python_path is None
    assert settings.cowork_sandbox_image == "workpilot-artifact-python:1.0.0"
    assert settings.cowork_sandbox_memory_mb == 512
    assert settings.cowork_sandbox_pids_limit == 128
    assert settings.cowork_sandbox_cpus == 1.0


def test_artifact_runtime_locks_required_format_dependencies() -> None:
    requirements = (
        Path(__file__).parents[2] / "deploy" / "artifact-runtime" / "requirements.lock"
    ).read_text(encoding="utf-8")
    pins = {
        name.casefold(): version
        for line in requirements.splitlines()
        if line and not line.startswith("#")
        for name, version in [line.split("==", maxsplit=1)]
    }

    assert {
        name.casefold(): pins[name.casefold()]
        for name in EXPECTED_DISTRIBUTIONS
    } == {
        name.casefold(): version
        for name, version in EXPECTED_DISTRIBUTIONS.items()
    }


def test_artifact_runtime_info_is_machine_readable(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["--workpilot-runtime-info"]) == 0

    output = capsys.readouterr().out
    assert f'"profile": "{RUNTIME_PROFILE}"' in output
    assert '"python-pptx": "1.0.2"' in output


def test_desktop_bundle_contains_artifact_and_pptx_external_binaries() -> None:
    config = (
        Path(__file__).parents[2] / "frontend" / "src-tauri" / "tauri.bundle.conf.json"
    ).read_text(encoding="utf-8")
    build_script = (
        Path(__file__).parents[1] / "scripts" / "build_sidecar.py"
    ).read_text(encoding="utf-8")
    desktop_shell = (
        Path(__file__).parents[2] / "frontend" / "src-tauri" / "src" / "lib.rs"
    ).read_text(encoding="utf-8")

    assert "binaries/workpilot-artifact-python" in config
    assert "binaries/workpilot-pptx-renderer" in config
    assert "--workpilot-selftest" in build_script
    assert "PPTX_RENDERER_BASENAME" in build_script
    assert "_build_pptx_renderer" in build_script
    assert 'env("COWORK_SANDBOX_PYTHON_PATH", artifact_python)' in desktop_shell
    assert 'env("WORKPILOT_PPTX_RENDERER", renderer)' in desktop_shell
    assert "bundled_pptx_renderer_path" in desktop_shell


def test_pptx_renderer_pkg_patch_is_narrow_and_version_locked(tmp_path: Path) -> None:
    source = (
        tmp_path
        / "node_modules"
        / "pptxgenjs"
        / "dist"
        / "pptxgen.cjs.js"
    )
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            (
                "({ default: fs } = yield import('node:fs'));",
                "({ default: https } = yield import('node:https'));",
                "const { promises: fs } = yield import('node:fs');",
            )
        ),
        encoding="utf-8",
    )

    _patch_pptxgenjs_for_pkg(tmp_path)

    patched = source.read_text(encoding="utf-8")
    assert "import('node:" not in patched
    assert "fs = require('node:fs');" in patched
    assert "https = require('node:https');" in patched


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("aarch64-apple-darwin", "node22-macos-arm64"),
        ("x86_64-apple-darwin", "node22-macos-x64"),
        ("x86_64-pc-windows-msvc", "node22-win-x64"),
        ("aarch64-unknown-linux-gnu", "node22-linux-arm64"),
        ("x86_64-unknown-linux-gnu", "node22-linux-x64"),
    ],
)
def test_pptx_renderer_pkg_target_matches_tauri_target(
    target: str,
    expected: str,
) -> None:
    assert _pkg_target(target) == expected


def test_pptx_renderer_pkg_target_rejects_unknown_platform() -> None:
    with pytest.raises(RuntimeError, match="尚不支持"):
        _pkg_target("wasm32-unknown-unknown")
