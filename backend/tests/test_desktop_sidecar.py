from pathlib import Path

from app import desktop_sidecar


def test_frozen_sidecar_anchors_mutable_paths_to_data_root(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    bundle_root = tmp_path / "bundle"
    data_root = tmp_path / "state"
    monkeypatch.setattr(desktop_sidecar.sys, "frozen", True, raising=False)
    monkeypatch.setitem(desktop_sidecar.sys.__dict__, "_MEIPASS", str(bundle_root))
    monkeypatch.setenv("COWORK_DATA_PATH", str(data_root))
    for name in (
        "ROUTING_CONFIG_PATH",
        "LOCAL_LIBRARY_PATH",
        "AGENT_OUTPUT_PATH",
        "OFFICE_PREVIEW_CACHE_PATH",
        "COWORK_ATTACHMENT_PATH",
        "COWORK_MCP_CONFIG_PATH",
        "COWORK_SKILLS_PATH",
        "SECRET_STORE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    desktop_sidecar._configure_packaged_paths()

    assert desktop_sidecar.os.environ["ROUTING_CONFIG_PATH"] == str(
        bundle_root / "config" / "routing.yaml"
    )
    assert desktop_sidecar.os.environ["LOCAL_LIBRARY_PATH"] == str(data_root / "library")
    assert desktop_sidecar.os.environ["AGENT_OUTPUT_PATH"] == str(data_root / "agent-output")
    assert desktop_sidecar.os.environ["OFFICE_PREVIEW_CACHE_PATH"] == str(
        data_root / "preview-cache"
    )
    assert desktop_sidecar.os.environ["COWORK_ATTACHMENT_PATH"] == str(
        data_root / "cowork-attachments"
    )
    assert desktop_sidecar.os.environ["COWORK_MCP_CONFIG_PATH"] == str(data_root / "mcp.yaml")
    assert desktop_sidecar.os.environ["COWORK_SKILLS_PATH"] == str(data_root / "skills")
    assert desktop_sidecar.os.environ["SECRET_STORE_KEY_PATH"] == str(
        data_root / "secrets" / "master.key"
    )


def test_frozen_sidecar_preserves_explicit_path_overrides(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    custom_output = tmp_path / "custom-output"
    monkeypatch.setattr(desktop_sidecar.sys, "frozen", True, raising=False)
    monkeypatch.setitem(desktop_sidecar.sys.__dict__, "_MEIPASS", str(tmp_path / "bundle"))
    monkeypatch.setenv("COWORK_DATA_PATH", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_OUTPUT_PATH", str(custom_output))

    desktop_sidecar._configure_packaged_paths()

    assert desktop_sidecar.os.environ["AGENT_OUTPUT_PATH"] == str(custom_output)
