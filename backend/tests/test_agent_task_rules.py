import hashlib
from pathlib import Path

from eval.agent_task_rules import evaluate_agent_task


def _case(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    artifact = tmp_path / "artifacts" / "review.md"
    artifact.parent.mkdir()
    artifact.write_text("AGENTBENCH 与 GAIA 对比", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    task: dict[str, object] = {
        "gold_tools": [
            {"name": "list_documents", "arguments": {"document_ids": ["a", "b"]}},
            {"name": "write_note", "arguments": {"output_path": "review.md"}},
        ],
        "constraints": {
            "expected_workflow_type": "literature_review",
            "hitl_decision": "approved",
            "expected_artifact_sha256": digest,
            "must_include": ["AGENTBENCH", "GAIA"],
            "must_not_include": ["外部事实"],
        },
    }
    observed: dict[str, object] = {
        "workflow_type": "literature_review",
        "status": "done",
        "hitl_decision": "approved",
        "tools": [
            {
                "name": "list_documents",
                "arguments": {"document_ids": ["a", "b"], "extra": True},
                "status": "ok",
            },
            {
                "name": "write_note",
                "arguments": {"output_path": "review.md"},
                "status": "ok",
            },
        ],
        "artifact_path": "artifacts/review.md",
        "artifact_sha256": digest,
    }
    return task, observed


def test_agent_task_rule_track_accepts_real_closed_loop(tmp_path: Path) -> None:
    task, observed = _case(tmp_path)

    result = evaluate_agent_task(task, observed, package=tmp_path)

    assert result.passed is True
    assert all(result.to_dict().values())


def test_agent_task_rule_track_rejects_tool_or_hitl_drift(tmp_path: Path) -> None:
    task, observed = _case(tmp_path)
    tools = list(observed["tools"])  # type: ignore[arg-type]
    tools.reverse()
    observed["tools"] = tools
    observed["hitl_decision"] = "rejected"

    result = evaluate_agent_task(task, observed, package=tmp_path)

    assert result.passed is False
    assert result.tool_sequence_match is False
    assert result.hitl_match is False


def test_agent_task_rule_track_rejects_tamper_and_path_escape(tmp_path: Path) -> None:
    task, observed = _case(tmp_path)
    artifact = tmp_path / "artifacts" / "review.md"
    artifact.write_text("被篡改", encoding="utf-8")

    tampered = evaluate_agent_task(task, observed, package=tmp_path)
    assert tampered.passed is False
    assert tampered.artifact_hash_match is False
    assert tampered.artifact_constraints_match is False

    observed["artifact_path"] = "../outside.md"
    escaped = evaluate_agent_task(task, observed, package=tmp_path)
    assert escaped.passed is False
    assert escaped.artifact_path_safe is False
