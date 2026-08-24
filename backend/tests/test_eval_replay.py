import copy
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from app.schemas.runs import RunEventType
from eval.replay import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    EVENT_PROTOCOL,
    KNOWN_EVENT_TYPES,
    canonical_json,
    main,
    seal_bundle,
    verify_bundle,
    verify_file,
)


def _event(run_id: str, seq: int, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{run_id}:{seq}",
        "run_id": run_id,
        "seq": str(seq),
        "type": event_type,
        "data": data,
    }


def _bundle(
    events: list[dict[str, Any]],
    *,
    run_id: str = "run-replay-1",
    expected_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_id": "case-1",
        "run_id": run_id,
        "events": events,
    }
    if expected_state is not None:
        case["expected_state"] = expected_state
    return seal_bundle(
        {
            "schema": BUNDLE_SCHEMA,
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "event_protocol": EVENT_PROTOCOL,
            "origin": "synthetic",
            "cases": [case],
        }
    )


def _issue_codes(bundle: dict[str, Any]) -> set[str]:
    report = verify_bundle(bundle)
    return {
        issue.code
        for issue in (*report.issues, *(issue for case in report.cases for issue in case.issues))
    }


def test_canonical_json_and_sha256_detect_semantic_tamper() -> None:
    run_id = "run-tamper"
    bundle = _bundle(
        [
            _event(run_id, 1, "message.snapshot", {"text": "原始答案"}),
            _event(run_id, 2, "run.done", {"workflow_type": "cowork", "status": "done"}),
        ],
        run_id=run_id,
    )
    # 键顺序不影响规范 JSON；业务内容的任何变化都会使已封存摘要失效。
    assert canonical_json({"b": 2, "a": "中"}) == '{"a":"中","b":2}'
    tampered = copy.deepcopy(bundle)
    tampered["cases"][0]["events"][0]["data"]["text"] = "篡改答案"

    report = verify_bundle(tampered)

    assert report.valid is False
    assert report.integrity_valid is False
    assert "integrity_mismatch" in _issue_codes(tampered)


def test_replay_event_types_match_backend_and_frontend_contracts() -> None:
    frontend = (Path(__file__).parents[2] / "frontend/src/lib/run-protocol.ts").read_text(
        encoding="utf-8"
    )
    union = frontend.split("export type RunEventType =", 1)[1].split(";", 1)[0]
    frontend_types = set(re.findall(r'\|\s*"([^"]+)"', union))

    assert set(get_args(RunEventType)) == KNOWN_EVENT_TYPES
    assert frontend_types == KNOWN_EVENT_TYPES


def test_sequence_gap_is_rejected_even_when_bundle_digest_is_fresh() -> None:
    run_id = "run-gap"
    bundle = _bundle(
        [
            _event(run_id, 1, "message.delta", {"text": "A"}),
            _event(run_id, 3, "run.done", {"workflow_type": "cowork", "status": "done"}),
        ],
        run_id=run_id,
    )

    report = verify_bundle(bundle)

    assert report.integrity_valid is True
    assert report.valid is False
    assert "seq_gap" in _issue_codes(bundle)


def test_conflicting_duplicate_sequence_is_rejected() -> None:
    run_id = "run-duplicate-conflict"
    bundle = _bundle(
        [
            _event(run_id, 1, "message.delta", {"text": "A"}),
            _event(run_id, 1, "message.delta", {"text": "B"}),
            _event(run_id, 2, "run.done", {"workflow_type": "cowork", "status": "done"}),
        ],
        run_id=run_id,
    )

    report = verify_bundle(bundle)

    assert report.valid is False
    assert report.cases[0].duplicate_event_count == 1
    assert "duplicate_seq_conflict" in _issue_codes(bundle)


def test_exact_delivery_duplicate_is_deduplicated_with_warning() -> None:
    run_id = "run-delivery-duplicate"
    first = _event(run_id, 1, "message.delta", {"text": "只出现一次"})
    bundle = _bundle(
        [
            first,
            copy.deepcopy(first),
            _event(run_id, 2, "message.done", {"message_id": "message-1"}),
        ],
        run_id=run_id,
        expected_state={"text": "只出现一次", "phase": "done"},
    )

    report = verify_bundle(bundle)

    assert report.valid is True
    assert report.cases[0].duplicate_event_count == 1
    assert report.cases[0].state.text == "只出现一次"
    assert "duplicate_event" in _issue_codes(bundle)


def test_reset_snapshot_and_citation_dedup_match_frontend_fold() -> None:
    run_id = "run-reset-snapshot"
    citation = {"citation_id": "cite-1", "title": "证据一", "quote": "原文"}
    bundle = _bundle(
        [
            _event(run_id, 1, "message.start", {"message_id": "message-1"}),
            _event(run_id, 2, "message.delta", {"text": "第一轮草稿"}),
            _event(run_id, 3, "citation", citation),
            _event(run_id, 4, "citation", copy.deepcopy(citation)),
            _event(run_id, 5, "message.reset", {}),
            _event(run_id, 6, "message.delta", {"text": "第二轮仍非终稿"}),
            _event(run_id, 7, "message.snapshot", {"text": "最终答案"}),
            _event(run_id, 8, "message.done", {"message_id": "message-1"}),
            _event(run_id, 9, "run.done", {"workflow_type": "cowork", "status": "done"}),
        ],
        run_id=run_id,
        expected_state={
            "cursor": "9",
            "phase": "done",
            "text": "最终答案",
            "citation_ids": ["cite-1"],
        },
    )

    report = verify_bundle(bundle)

    assert report.valid is True
    state = report.cases[0].state
    assert state.text == "最终答案"
    assert [item["citation_id"] for item in state.citations] == ["cite-1"]


def test_plan_tool_interrupt_error_and_run_done_are_folded_without_execution() -> None:
    run_id = "run-plan-tool"
    step = {
        "id": "step-1",
        "idx": 0,
        "description": "读取输入",
        "tool": "read_text_file",
        "depends_on": [],
        "status": "pending",
    }
    bundle = _bundle(
        [
            _event(run_id, 1, "plan", {"workflow_type": "cowork", "steps": [step]}),
            _event(
                run_id,
                2,
                "tool.start",
                {"step_id": "step-1", "tool": "read_text_file", "command": "绝不执行"},
            ),
            _event(
                run_id,
                3,
                "tool.result",
                {"step_id": "step-1", "tool": "read_text_file", "reused": True},
            ),
            _event(
                run_id,
                4,
                "interrupt",
                {"kind": "ask_user", "resume_token": "offline", "payload": {}},
            ),
            _event(
                run_id,
                5,
                "error",
                {"code": "cancelled", "retryable": True, "user_message": "已取消"},
            ),
            _event(
                run_id,
                6,
                "run.done",
                {"workflow_type": "cowork", "status": "cancelled"},
            ),
        ],
        run_id=run_id,
        expected_state={
            "phase": "done",
            "interrupt": None,
            "open_tools": [],
            "plan_statuses": {"step-1": "done"},
        },
    )

    report = verify_bundle(bundle)

    assert report.valid is True
    assert report.cases[0].state.error == {
        "code": "cancelled",
        "retryable": True,
        "user_message": "已取消",
    }
    assert report.cases[0].state.agent_plan[0]["summary"] == "已复用安全执行结果"


def test_terminal_run_with_unfinished_tool_is_rejected() -> None:
    run_id = "run-unfinished-tool"
    bundle = _bundle(
        [
            _event(
                run_id,
                1,
                "tool.start",
                {"step_id": "step-1", "tool": "write_text_file"},
            ),
            _event(run_id, 2, "run.done", {"workflow_type": "cowork", "status": "failed"}),
        ],
        run_id=run_id,
    )

    report = verify_bundle(bundle)

    assert report.valid is False
    assert report.cases[0].open_tools == ("step:step-1",)
    assert "unfinished_tool" in _issue_codes(bundle)


def test_committed_synthetic_protocol_suite_verifies() -> None:
    path = Path(__file__).parents[2] / "eval" / "replays" / "run-protocol-v1.json"

    report = verify_file(path)

    assert report.valid is True
    assert len(report.cases) == 3


def test_multi_case_suite_and_cli_emit_json_and_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_run = "run-suite-1"
    second_run = "run-suite-2"
    raw = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "event_protocol": EVENT_PROTOCOL,
        "origin": "synthetic",
        "cases": [
            {
                "case_id": "one",
                "run_id": first_run,
                "events": [_event(first_run, 1, "message.done", {"message_id": "m-1"})],
            },
            {
                "case_id": "two",
                "run_id": second_run,
                "events": [
                    _event(
                        second_run,
                        1,
                        "error",
                        {"code": "offline", "retryable": False, "user_message": "失败"},
                    )
                ],
            },
        ],
    }
    bundle_path = tmp_path / "suite.json"
    bundle_path.write_text(
        json.dumps(seal_bundle(raw), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = tmp_path / "report.md"

    assert main(["verify", str(bundle_path)]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["summary"]["case_count"] == 2
    assert main(["verify", str(bundle_path), "--output", str(markdown_path)]) == 0
    capsys.readouterr()

    assert verify_file(bundle_path).valid is True
    assert "# Run 离线回放验证报告" in markdown_path.read_text(encoding="utf-8")
    assert "仅离线验证" in markdown_path.read_text(encoding="utf-8")
