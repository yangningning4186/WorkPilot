from app.agent.cowork_browser_tools import register_browser_tools
from app.agent.cowork_tools import build_default_cowork_registry


def test_browser_mutations_are_individually_approved_and_leased() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)

    for name in (
        "browser_open",
        "browser_click",
        "browser_back",
        "browser_type",
        "browser_select",
        "browser_upload",
        "browser_download",
        "browser_screenshot",
    ):
        spec = registry.get(name)
        assert spec.approval_required
        assert spec.effect != "none"

    assert registry.get("browser_snapshot").effect == "none"
    assert registry.get("browser_find").effect == "none"


def test_readonly_subagent_cannot_receive_browser_actions() -> None:
    registry = build_default_cowork_registry()
    register_browser_tools(registry)
    names = {
        definition.name
        for definition in registry.read_only_tool_definitions(
            exclude=frozenset(), query="浏览网页并填写表单"
        )
    }

    assert "browser_snapshot" in names
    assert "browser_click" not in names
    assert "browser_type" not in names
    assert "browser_upload" not in names
