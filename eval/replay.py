"""Run 事件的确定性离线验证与回放。

这个模块只读取 replay bundle 并折叠其中的事件。即使事件名是 ``tool.start``，
它也只会更新内存中的审计状态；模块没有工具注册表、网络客户端或生产存储依赖，因而
不会执行工具、恢复 checkpoint 或重放任何副作用。

bundle 的完整性摘要覆盖顶层 ``integrity`` 以外的全部内容。规范 JSON 明确定义为：
UTF-8、对象键排序、无多余空白、保留非 ASCII 字符并拒绝 NaN/Infinity。这个口径由
``CANONICALIZATION`` 版本化，避免以后更换编码规则后把旧摘要静默解释成新摘要。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, NoReturn

BUNDLE_SCHEMA = "workpilot.run-replay-bundle"
BUNDLE_SCHEMA_VERSION = 1
EVENT_PROTOCOL = "workpilot.run-events/v1"
CANONICALIZATION = "workpilot-json-sort-keys-utf8-v1"
INTEGRITY_ALGORITHM = "sha256"

Severity = Literal["error", "warning"]
RunPhase = Literal[
    "connecting",
    "streaming",
    "executing",
    "waiting_human",
    "done",
    "partial",
    "refused",
    "error",
]

# 与 frontend/src/lib/run-protocol.ts 的 RunEventType 对齐。暂时不改变界面的事件也要列出，
# 否则协议新增与拼写错误在离线回归里无法区分。
KNOWN_EVENT_TYPES = frozenset(
    {
        "message.start",
        "message.delta",
        "message.snapshot",
        "message.reset",
        "message.reasoning",
        "citation",
        "citation.validation_failed",
        "message.done",
        "plan",
        "step.update",
        "tool.start",
        "tool.result",
        "tool.error",
        "context.compacted",
        "todo.update",
        "memory.saved",
        "conversation.title",
        "reading.goto",
        "reading.annotated",
        "subagent.progress",
        "team.created",
        "team.worker.started",
        "board.task.created",
        "board.task.review",
        "board.task.failed",
        "board.task.reviewed",
        "board.task.resolved",
        "team.summary",
        "steering.queued",
        "steering.applied",
        "interrupt",
        "approval.waived",
        "run.sleeping",
        "interaction.resolved",
        "artifact",
        "run.done",
        "error",
    }
)
TERMINAL_EVENT_TYPES = frozenset({"message.done", "run.done", "error"})
FINISHED_PHASES = frozenset({"done", "partial", "refused", "error"})
TOOL_FINISH_EVENT_TYPES = frozenset({"tool.result", "tool.error"})

_SEQ_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReplayFormatError(ValueError):
    """输入无法解释成无歧义 JSON 或版本化 replay bundle。"""


@dataclass(frozen=True)
class ReplayIssue:
    """一条可机器判断、也可直接展示给人的验证发现。"""

    severity: Severity
    code: str
    message: str
    case_id: str | None = None
    seq: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.case_id is not None:
            payload["case_id"] = self.case_id
        if self.seq is not None:
            payload["seq"] = self.seq
        return payload


@dataclass(frozen=True)
class ReplayState:
    """与前端 ``RunState`` 同口径的不可变回放状态。"""

    cursor: int = 0
    phase: RunPhase = "connecting"
    message_id: str | None = None
    text: str = ""
    citations: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None
    refusal_reason: str | None = None
    grounded: bool = True
    latency_ms: int | float | None = None
    cost_usd: str | None = None
    agent_plan: tuple[dict[str, Any], ...] = ()
    interrupt: dict[str, Any] | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    recovery_count: int = 0
    notice: str | None = None

    def to_dict(self, *, open_tools: Sequence[str] = ()) -> dict[str, Any]:
        """输出稳定、可写进 JSON 报告的状态快照。"""

        return {
            "cursor": str(self.cursor),
            "phase": self.phase,
            "message_id": self.message_id,
            "text": self.text,
            "citations": [copy.deepcopy(item) for item in self.citations],
            "citation_ids": [str(item.get("citation_id", "")) for item in self.citations],
            "error": copy.deepcopy(self.error),
            "refusal_reason": self.refusal_reason,
            "grounded": self.grounded,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "agent_plan": [copy.deepcopy(item) for item in self.agent_plan],
            "plan_statuses": {str(item.get("id")): item.get("status") for item in self.agent_plan},
            "interrupt": copy.deepcopy(self.interrupt),
            "artifacts": [copy.deepcopy(item) for item in self.artifacts],
            "recovery_count": self.recovery_count,
            "notice": self.notice,
            "open_tools": list(open_tools),
        }


@dataclass(frozen=True)
class ReplayCaseReport:
    """单条 run case 的协议校验与折叠结果。"""

    case_id: str
    run_id: str
    event_count: int
    unique_event_count: int
    duplicate_event_count: int
    terminal_event: str | None
    state: ReplayState
    open_tools: tuple[str, ...]
    issues: tuple[ReplayIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "valid": self.valid,
            "event_count": self.event_count,
            "unique_event_count": self.unique_event_count,
            "duplicate_event_count": self.duplicate_event_count,
            "terminal_event": self.terminal_event,
            "state": self.state.to_dict(open_tools=self.open_tools),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class ReplayReport:
    """一份 bundle 的完整离线验证报告。"""

    source: str
    schema: str | None
    schema_version: int | None
    expected_sha256: str | None
    actual_sha256: str | None
    integrity_valid: bool
    cases: tuple[ReplayCaseReport, ...]
    issues: tuple[ReplayIssue, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return (
            self.integrity_valid
            and not any(issue.severity == "error" for issue in self.issues)
            and bool(self.cases)
            and all(case.valid for case in self.cases)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_version": 1,
            "mode": "offline_validation_only",
            "source": self.source,
            "valid": self.valid,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "integrity": {
                "valid": self.integrity_valid,
                "algorithm": INTEGRITY_ALGORITHM,
                "canonicalization": CANONICALIZATION,
                "expected_sha256": self.expected_sha256,
                "actual_sha256": self.actual_sha256,
            },
            "summary": {
                "case_count": len(self.cases),
                "passed": sum(case.valid for case in self.cases),
                "failed": sum(not case.valid for case in self.cases),
                "errors": _count_issues(self, "error"),
                "warnings": _count_issues(self, "warning"),
            },
            "issues": [issue.to_dict() for issue in self.issues],
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def to_markdown(self) -> str:
        outcome = "通过" if self.valid else "失败"
        integrity = "通过" if self.integrity_valid else "失败"
        lines = [
            "# Run 离线回放验证报告",
            "",
            f"- 结论：**{outcome}**",
            f"- 来源：`{_markdown_code(self.source)}`",
            f"- 协议：`{_markdown_code(str(self.schema))}` v{self.schema_version}",
            f"- SHA256 完整性：**{integrity}**",
            (
                f"- Case：{len(self.cases)}（通过 {sum(case.valid for case in self.cases)}，"
                f"失败 {sum(not case.valid for case in self.cases)}）"
            ),
            "- 模式：仅离线验证；工具与副作用不会执行",
            "",
        ]
        if self.issues:
            lines.extend(["## Bundle 问题", ""])
            lines.extend(_issue_markdown(issue) for issue in self.issues)
            lines.append("")
        lines.extend(
            [
                "## Cases",
                "",
                "| Case | 结论 | 事件（唯一/输入） | 终态 | 正文字符 | 未结束工具 |",
                "|---|---:|---:|---|---:|---:|",
            ]
        )
        for case in self.cases:
            lines.append(
                "| "
                f"`{_markdown_code(case.case_id)}` | "
                f"{'通过' if case.valid else '失败'} | "
                f"{case.unique_event_count}/{case.event_count} | "
                f"{case.state.phase} | {len(case.state.text)} | {len(case.open_tools)} |"
            )
        for case in self.cases:
            if not case.issues:
                continue
            lines.extend(["", f"### {_markdown_code(case.case_id)}", ""])
            lines.extend(_issue_markdown(issue) for issue in case.issues)
        return "\n".join(lines).rstrip() + "\n"


def canonical_json_bytes(value: object) -> bytes:
    """按 v1 规范生成可哈希 JSON；非有限浮点数会被拒绝。"""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ReplayFormatError(f"内容不能编码为规范 JSON: {error}") from error
    return encoded.encode("utf-8")


def canonical_json(value: object) -> str:
    """返回规范 JSON 文本，主要用于测试、审计和跨实现比对。"""

    return canonical_json_bytes(value).decode("utf-8")


def compute_bundle_sha256(bundle: Mapping[str, Any]) -> str:
    """计算 bundle 主体摘要；顶层 ``integrity`` 不参与，避免循环依赖。"""

    body = {key: value for key, value in bundle.items() if key != "integrity"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def seal_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """返回带 v1 SHA256 完整性字段的深拷贝，不修改调用方对象。"""

    sealed = copy.deepcopy(dict(bundle))
    sealed.pop("integrity", None)
    sealed["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "digest": compute_bundle_sha256(sealed),
    }
    return sealed


def initial_replay_state() -> ReplayState:
    """创建与前端 ``initialRunState`` 一致的状态。"""

    return ReplayState()


def apply_envelope(state: ReplayState, envelope: Mapping[str, Any]) -> ReplayState:
    """纯函数地应用一个事件，语义与 ``frontend/src/lib/run-state.ts`` 对齐。"""

    seq = _parse_seq_or_raise(envelope.get("seq"))
    if seq <= state.cursor:
        # SSE 重连可能重投已经消费的事件；与前端一样按 cursor 丢弃。
        return state
    event_type = envelope.get("type")
    data_value = envelope.get("data")
    data = dict(data_value) if isinstance(data_value, Mapping) else {}
    next_state = replace(state, cursor=seq)

    if event_type == "message.start":
        return replace(
            next_state,
            message_id=_optional_string(data.get("message_id")),
            phase="streaming",
        )
    if event_type == "message.delta":
        text = _string_or_empty(data.get("text"))
        return replace(next_state, text=state.text + text, phase="streaming")
    if event_type == "message.snapshot":
        text = _string_or_empty(data.get("text"))
        return replace(next_state, text=text, phase="streaming")
    if event_type == "message.reset":
        return replace(next_state, text="", phase="streaming")
    if event_type == "citation":
        citation_id = data.get("citation_id")
        if any(item.get("citation_id") == citation_id for item in state.citations):
            return next_state
        return replace(next_state, citations=(*state.citations, copy.deepcopy(data)))
    if event_type == "message.done":
        refused = data.get("refused") is True
        grounded = data.get("grounded") is not False
        latency = data.get("latency_ms")
        return replace(
            next_state,
            message_id=_optional_string(data.get("message_id")),
            phase="refused" if refused else "done",
            refusal_reason=_optional_string(data.get("refusal_reason")),
            grounded=grounded,
            latency_ms=latency if isinstance(latency, (int, float)) else None,
            cost_usd=_optional_string(data.get("cost_usd")),
        )
    if event_type == "plan":
        raw_steps = data.get("steps")
        steps = (
            tuple(copy.deepcopy(dict(step)) for step in raw_steps if isinstance(step, Mapping))
            if isinstance(raw_steps, list)
            else ()
        )
        return replace(next_state, agent_plan=steps, phase="executing")
    if event_type == "step.update":
        step_id = data.get("step_id")
        if not isinstance(step_id, str):
            recovery = data.get("recovery_count")
            recovery_count = (
                recovery
                if isinstance(recovery, int) and not isinstance(recovery, bool)
                else state.recovery_count + 1
            )
            return replace(
                next_state,
                recovery_count=recovery_count,
                notice=_optional_string(data.get("summary")),
                phase="executing",
            )
        existing = next((step for step in state.agent_plan if step.get("id") == step_id), None)
        step_idx = data.get("step_idx")
        tool = _optional_string(data.get("tool"))
        summary = _optional_string(data.get("summary"))
        changed: dict[str, Any] = {
            "id": step_id,
            "idx": step_idx
            if isinstance(step_idx, int) and not isinstance(step_idx, bool)
            else len(state.agent_plan),
            "description": summary or f"调用 {tool or 'Cowork 工具'}",
            "tool": tool,
            "depends_on": [],
            "status": data.get("status"),
        }
        if "summary" in data:
            changed["summary"] = summary
        if existing is None:
            plan = (*state.agent_plan, changed)
        else:
            plan = tuple(
                {**step, **changed} if step.get("id") == step_id else step
                for step in state.agent_plan
            )
        return replace(next_state, agent_plan=plan, phase="executing")
    if event_type == "tool.start":
        plan = _update_tool_step(state.agent_plan, data, status="running", summary=None)
        return replace(next_state, agent_plan=plan, phase="executing")
    if event_type == "tool.result":
        summary = "已复用安全执行结果" if data.get("reused") is True else "执行完成"
        plan = _update_tool_step(state.agent_plan, data, status="done", summary=summary)
        return replace(next_state, agent_plan=plan, phase="executing")
    if event_type == "tool.error":
        summary = _optional_string(data.get("error")) or "工具执行失败"
        plan = _update_tool_step(state.agent_plan, data, status="failed", summary=summary)
        return replace(next_state, agent_plan=plan, phase="executing")
    if event_type == "interrupt":
        return replace(next_state, interrupt=copy.deepcopy(data), phase="waiting_human")
    if event_type == "artifact":
        return replace(next_state, artifacts=(*state.artifacts, copy.deepcopy(data)))
    if event_type == "run.done":
        phase: RunPhase = "partial" if data.get("status") == "partial" else "done"
        return replace(next_state, interrupt=None, phase=phase)
    if event_type == "error":
        return replace(next_state, error=copy.deepcopy(data), phase="error")
    return next_state


def fold_envelopes(envelopes: Sequence[Mapping[str, Any]]) -> ReplayState:
    """按输入顺序折叠事件；重复或倒退的 cursor 与前端一样被忽略。"""

    state = initial_replay_state()
    for envelope in envelopes:
        state = apply_envelope(state, envelope)
    return state


def verify_bundle(bundle: object, *, source: str = "<memory>") -> ReplayReport:
    """验证完整性、suite 结构及所有 case，并生成确定性报告。"""

    if not isinstance(bundle, Mapping):
        issue = ReplayIssue("error", "bundle_not_object", "bundle 顶层必须是 JSON 对象")
        return ReplayReport(source, None, None, None, None, False, (), (issue,))

    payload = dict(bundle)
    schema = payload.get("schema") if isinstance(payload.get("schema"), str) else None
    version_value = payload.get("schema_version")
    schema_version = (
        version_value
        if isinstance(version_value, int) and not isinstance(version_value, bool)
        else None
    )
    issues: list[ReplayIssue] = []
    if schema != BUNDLE_SCHEMA:
        issues.append(
            ReplayIssue(
                "error",
                "unsupported_schema",
                f"schema 必须是 {BUNDLE_SCHEMA!r}，实际为 {payload.get('schema')!r}",
            )
        )
    if schema_version != BUNDLE_SCHEMA_VERSION:
        issues.append(
            ReplayIssue(
                "error",
                "unsupported_schema_version",
                f"仅支持 schema_version={BUNDLE_SCHEMA_VERSION}，实际为 {version_value!r}",
            )
        )
    if payload.get("event_protocol") != EVENT_PROTOCOL:
        issues.append(
            ReplayIssue(
                "error",
                "unsupported_event_protocol",
                f"event_protocol 必须是 {EVENT_PROTOCOL!r}",
            )
        )

    expected_sha256, integrity_valid, integrity_issues = _verify_integrity(payload)
    issues.extend(integrity_issues)
    try:
        actual_sha256 = compute_bundle_sha256(payload)
    except ReplayFormatError:
        actual_sha256 = None

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        issues.append(ReplayIssue("error", "cases_missing", "cases 必须是非空数组"))
        raw_cases = []

    case_reports: list[ReplayCaseReport] = []
    seen_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        report = _verify_case(raw_case, index=index)
        if report.case_id in seen_case_ids:
            duplicate_issue = ReplayIssue(
                "error",
                "duplicate_case_id",
                f"case_id 重复: {report.case_id!r}",
                case_id=report.case_id,
            )
            report = replace(report, issues=(*report.issues, duplicate_issue))
        seen_case_ids.add(report.case_id)
        case_reports.append(report)

    return ReplayReport(
        source=source,
        schema=schema,
        schema_version=schema_version,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        integrity_valid=integrity_valid,
        cases=tuple(case_reports),
        issues=tuple(issues),
    )


def load_bundle(path: Path) -> object:
    """读取 JSON，并拒绝重复对象键与非有限数字，避免同一字节流产生歧义语义。"""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReplayFormatError(f"无法读取 {path}: {error}") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ReplayFormatError) as error:
        raise ReplayFormatError(f"{path} 不是无歧义 JSON: {error}") from error


def verify_file(path: Path) -> ReplayReport:
    """读取并验证一个本地 bundle；格式错误也转成结构化失败报告。"""

    try:
        bundle = load_bundle(path)
    except ReplayFormatError as error:
        issue = ReplayIssue("error", "invalid_json", str(error))
        return ReplayReport(str(path), None, None, None, None, False, (), (issue,))
    return verify_bundle(bundle, source=str(path))


def _verify_integrity(
    bundle: Mapping[str, Any],
) -> tuple[str | None, bool, list[ReplayIssue]]:
    issues: list[ReplayIssue] = []
    integrity = bundle.get("integrity")
    if not isinstance(integrity, Mapping):
        return (
            None,
            False,
            [ReplayIssue("error", "integrity_missing", "缺少顶层 integrity 对象")],
        )
    algorithm = integrity.get("algorithm")
    canonicalization = integrity.get("canonicalization")
    digest = integrity.get("digest")
    expected = digest if isinstance(digest, str) else None
    if algorithm != INTEGRITY_ALGORITHM:
        issues.append(
            ReplayIssue(
                "error",
                "integrity_algorithm_invalid",
                f"完整性算法必须是 {INTEGRITY_ALGORITHM!r}",
            )
        )
    if canonicalization != CANONICALIZATION:
        issues.append(
            ReplayIssue(
                "error",
                "canonicalization_invalid",
                f"规范化版本必须是 {CANONICALIZATION!r}",
            )
        )
    if expected is None or _SHA256_PATTERN.fullmatch(expected) is None:
        issues.append(
            ReplayIssue(
                "error",
                "integrity_digest_invalid",
                "integrity.digest 必须是 64 位小写 SHA256",
            )
        )
    try:
        actual = compute_bundle_sha256(bundle)
    except ReplayFormatError as error:
        issues.append(ReplayIssue("error", "canonical_json_invalid", str(error)))
        return expected, False, issues
    if expected is not None and not _constant_time_equal(expected, actual):
        issues.append(
            ReplayIssue(
                "error",
                "integrity_mismatch",
                f"bundle 已被改动：expected={expected}，actual={actual}",
            )
        )
    return expected, not issues, issues


def _verify_case(raw_case: object, *, index: int) -> ReplayCaseReport:
    if not isinstance(raw_case, Mapping):
        case_id = f"<case-{index}>"
        issue = ReplayIssue(
            "error", "case_not_object", f"cases[{index}] 必须是对象", case_id=case_id
        )
        return ReplayCaseReport(case_id, "", 0, 0, 0, None, ReplayState(), (), (issue,))

    case = dict(raw_case)
    case_id_value = case.get("case_id")
    case_id = (
        case_id_value
        if isinstance(case_id_value, str) and case_id_value.strip()
        else f"<case-{index}>"
    )
    run_id_value = case.get("run_id")
    run_id = run_id_value if isinstance(run_id_value, str) else ""
    issues: list[ReplayIssue] = []
    if case_id != case_id_value:
        issues.append(
            ReplayIssue("error", "case_id_invalid", "case_id 必须是非空字符串", case_id=case_id)
        )
    if not run_id:
        issues.append(
            ReplayIssue("error", "run_id_invalid", "run_id 必须是非空字符串", case_id=case_id)
        )
    raw_events = case.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        issues.append(
            ReplayIssue("error", "events_missing", "events 必须是非空数组", case_id=case_id)
        )
        raw_events = []

    seen: dict[int, Mapping[str, Any]] = {}
    unique_events: list[Mapping[str, Any]] = []
    duplicate_count = 0
    highest_seq = 0
    open_tools: dict[str, int] = {}
    terminal_event: str | None = None
    run_done_seq: int | None = None
    citation_payloads: dict[str, Mapping[str, Any]] = {}

    for position, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            issues.append(
                ReplayIssue(
                    "error",
                    "event_not_object",
                    f"events[{position}] 必须是对象",
                    case_id=case_id,
                )
            )
            continue
        event = dict(raw_event)
        seq = _parse_seq(event.get("seq"))
        if seq is None:
            issues.append(
                ReplayIssue(
                    "error",
                    "seq_invalid",
                    f"events[{position}].seq 必须是从 1 开始的十进制字符串",
                    case_id=case_id,
                )
            )
            continue
        previous = seen.get(seq)
        if previous is not None:
            duplicate_count += 1
            if _same_json(previous, event):
                issues.append(
                    ReplayIssue(
                        "warning",
                        "duplicate_event",
                        "完全相同的重复事件已按前端 cursor 语义忽略",
                        case_id=case_id,
                        seq=seq,
                    )
                )
            else:
                issues.append(
                    ReplayIssue(
                        "error",
                        "duplicate_seq_conflict",
                        "同一 seq 出现不同事件，无法确定唯一历史",
                        case_id=case_id,
                        seq=seq,
                    )
                )
            continue

        expected_seq = highest_seq + 1
        if seq != expected_seq:
            code = "seq_gap" if seq > expected_seq else "seq_out_of_order"
            issues.append(
                ReplayIssue(
                    "error",
                    code,
                    f"seq 不连续：期待 {expected_seq}，实际 {seq}",
                    case_id=case_id,
                    seq=seq,
                )
            )
        highest_seq = max(highest_seq, seq)
        seen[seq] = event

        if event.get("run_id") != run_id:
            issues.append(
                ReplayIssue(
                    "error",
                    "run_id_mismatch",
                    f"事件 run_id={event.get('run_id')!r} 与 case run_id={run_id!r} 不一致",
                    case_id=case_id,
                    seq=seq,
                )
            )
            continue
        if event.get("id") != f"{run_id}:{seq}":
            issues.append(
                ReplayIssue(
                    "error",
                    "event_id_mismatch",
                    f"事件 id 必须是 {run_id}:{seq}",
                    case_id=case_id,
                    seq=seq,
                )
            )
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in KNOWN_EVENT_TYPES:
            issues.append(
                ReplayIssue(
                    "error",
                    "event_type_unknown",
                    f"未知事件类型: {event_type!r}",
                    case_id=case_id,
                    seq=seq,
                )
            )
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            issues.append(
                ReplayIssue(
                    "error",
                    "event_data_invalid",
                    "事件 data 必须是对象",
                    case_id=case_id,
                    seq=seq,
                )
            )
            continue
        issues.extend(_payload_issues(event_type, data, case_id=case_id, seq=seq))
        unique_events.append(event)

        if run_done_seq is not None and event_type != "conversation.title":
            issues.append(
                ReplayIssue(
                    "error",
                    "event_after_run_done",
                    f"run.done 之后不应再出现 {event_type}；仅允许异步标题通知",
                    case_id=case_id,
                    seq=seq,
                )
            )
        if event_type in TERMINAL_EVENT_TYPES:
            terminal_event = event_type
        if event_type == "run.done":
            if run_done_seq is not None:
                issues.append(
                    ReplayIssue(
                        "error",
                        "multiple_run_done",
                        f"run.done 已在 seq={run_done_seq} 出现",
                        case_id=case_id,
                        seq=seq,
                    )
                )
            else:
                run_done_seq = seq

        if event_type == "citation":
            citation_id = data.get("citation_id")
            if isinstance(citation_id, str):
                previous_citation = citation_payloads.get(citation_id)
                if previous_citation is not None and not _same_json(previous_citation, data):
                    issues.append(
                        ReplayIssue(
                            "warning",
                            "citation_id_reused",
                            "同一 citation_id 的后续载荷不同；前端会保留第一份",
                            case_id=case_id,
                            seq=seq,
                        )
                    )
                citation_payloads.setdefault(citation_id, data)

        if event_type == "tool.start":
            tool_key = _tool_key(data)
            if tool_key in open_tools:
                issues.append(
                    ReplayIssue(
                        "error",
                        "tool_already_started",
                        f"工具 {tool_key} 尚未结束又被启动",
                        case_id=case_id,
                        seq=seq,
                    )
                )
            else:
                open_tools[tool_key] = seq
        elif event_type in TOOL_FINISH_EVENT_TYPES:
            tool_key = _tool_key(data)
            if tool_key not in open_tools:
                issues.append(
                    ReplayIssue(
                        "error",
                        "tool_finish_without_start",
                        f"工具 {tool_key} 没有对应的 tool.start",
                        case_id=case_id,
                        seq=seq,
                    )
                )
            else:
                del open_tools[tool_key]

    try:
        state = fold_envelopes(unique_events)
    except ReplayFormatError as error:
        issues.append(ReplayIssue("error", "fold_failed", str(error), case_id=case_id))
        state = ReplayState()

    if terminal_event is None:
        issues.append(
            ReplayIssue(
                "error",
                "terminal_event_missing",
                "完整 replay 必须包含 message.done、run.done 或 error 终态事件",
                case_id=case_id,
            )
        )
    if state.phase not in FINISHED_PHASES:
        issues.append(
            ReplayIssue(
                "error",
                "terminal_state_invalid",
                f"折叠后的 phase={state.phase!r} 不是终态",
                case_id=case_id,
            )
        )
    for tool_key, start_seq in sorted(open_tools.items()):
        issues.append(
            ReplayIssue(
                "error",
                "unfinished_tool",
                f"工具 {tool_key} 从 seq={start_seq} 开始后没有 result/error",
                case_id=case_id,
                seq=start_seq,
            )
        )

    expected_state = case.get("expected_state")
    if expected_state is not None:
        if not isinstance(expected_state, Mapping):
            issues.append(
                ReplayIssue(
                    "error",
                    "expected_state_invalid",
                    "expected_state 必须是对象",
                    case_id=case_id,
                )
            )
        else:
            actual_state = state.to_dict(open_tools=tuple(sorted(open_tools)))
            issues.extend(
                _expected_state_issues(
                    expected_state,
                    actual_state,
                    case_id=case_id,
                )
            )

    return ReplayCaseReport(
        case_id=case_id,
        run_id=run_id,
        event_count=len(raw_events),
        unique_event_count=len(unique_events),
        duplicate_event_count=duplicate_count,
        terminal_event=terminal_event,
        state=state,
        open_tools=tuple(sorted(open_tools)),
        issues=tuple(issues),
    )


def _payload_issues(
    event_type: str,
    data: Mapping[str, Any],
    *,
    case_id: str,
    seq: int,
) -> list[ReplayIssue]:
    """只检查折叠与安全审计依赖的最小字段，保持对新增载荷字段前向兼容。"""

    issues: list[ReplayIssue] = []
    string_fields: dict[str, tuple[str, ...]] = {
        "message.start": ("message_id",),
        "message.delta": ("text",),
        "message.snapshot": ("text",),
        "citation": ("citation_id",),
    }
    for field_name in string_fields.get(event_type, ()):
        if not isinstance(data.get(field_name), str):
            issues.append(
                ReplayIssue(
                    "error",
                    "payload_field_invalid",
                    f"{event_type}.data.{field_name} 必须是字符串",
                    case_id=case_id,
                    seq=seq,
                )
            )
    if event_type == "plan":
        steps = data.get("steps")
        if steps is not None and not isinstance(steps, list):
            issues.append(
                ReplayIssue(
                    "error",
                    "payload_field_invalid",
                    "plan.data.steps 必须是数组",
                    case_id=case_id,
                    seq=seq,
                )
            )
    return issues


def _expected_state_issues(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    case_id: str,
    path: str = "expected_state",
) -> list[ReplayIssue]:
    """expected_state 是子集断言；未声明字段不会因报告扩展而让旧 suite 失效。"""

    issues: list[ReplayIssue] = []
    for key, expected_value in expected.items():
        current_path = f"{path}.{key}"
        if key not in actual:
            issues.append(
                ReplayIssue(
                    "error",
                    "expected_state_unknown_field",
                    f"{current_path} 不是可断言的状态字段",
                    case_id=case_id,
                )
            )
            continue
        actual_value = actual[key]
        if isinstance(expected_value, Mapping) and isinstance(actual_value, Mapping):
            issues.extend(
                _expected_state_issues(
                    expected_value,
                    actual_value,
                    case_id=case_id,
                    path=current_path,
                )
            )
        elif expected_value != actual_value:
            issues.append(
                ReplayIssue(
                    "error",
                    "expected_state_mismatch",
                    f"{current_path} 期待 {expected_value!r}，实际 {actual_value!r}",
                    case_id=case_id,
                )
            )
    return issues


def _update_tool_step(
    plan: tuple[dict[str, Any], ...],
    data: Mapping[str, Any],
    *,
    status: str,
    summary: str | None,
) -> tuple[dict[str, Any], ...]:
    step_id = data.get("step_id")
    changed: list[dict[str, Any]] = []
    for step in plan:
        if step.get("id") != step_id:
            changed.append(step)
            continue
        update = {**step, "status": status}
        if summary is not None:
            update["summary"] = summary
        changed.append(update)
    return tuple(changed)


def _tool_key(data: Mapping[str, Any]) -> str:
    step_id = data.get("step_id")
    if isinstance(step_id, str) and step_id:
        return f"step:{step_id}"
    step_idx = data.get("step_idx")
    tool = data.get("tool") if isinstance(data.get("tool"), str) else "<unknown>"
    if isinstance(step_idx, int) and not isinstance(step_idx, bool):
        return f"step_idx:{step_idx}:{tool}"
    return f"tool:{tool}"


def _parse_seq(value: object) -> int | None:
    if not isinstance(value, str) or _SEQ_PATTERN.fullmatch(value) is None:
        return None
    return int(value)


def _parse_seq_or_raise(value: object) -> int:
    seq = _parse_seq(value)
    if seq is None:
        raise ReplayFormatError(f"事件 seq 必须是从 1 开始的十进制字符串，实际为 {value!r}")
    return seq


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _same_json(left: object, right: object) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except ReplayFormatError:
        return False


def _constant_time_equal(left: str, right: str) -> bool:
    return (
        hashlib.sha256(left.encode("ascii", errors="ignore")).digest()
        == hashlib.sha256(right.encode("ascii", errors="ignore")).digest()
    )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayFormatError(f"JSON 对象键重复: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ReplayFormatError(f"JSON 不允许非有限数字: {value}")


def _count_issues(report: ReplayReport, severity: Severity) -> int:
    return sum(issue.severity == severity for issue in report.issues) + sum(
        issue.severity == severity for case in report.cases for issue in case.issues
    )


def _markdown_code(value: str) -> str:
    return value.replace("`", "\\`").replace("|", "\\|")


def _issue_markdown(issue: ReplayIssue) -> str:
    position = f"（seq={issue.seq}）" if issue.seq is not None else ""
    return f"- **{issue.severity.upper()} `{issue.code}`**{position}：{issue.message}"


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.replay",
        description="离线验证并确定性折叠 WorkPilot run replay bundle（绝不执行工具）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="验证一个单 case 或多 case bundle")
    verify.add_argument("file", type=Path, help="replay bundle JSON 文件")
    verify.add_argument(
        "--format",
        "--report-format",
        dest="report_format",
        choices=("json", "markdown"),
        default=None,
        help="标准输出/--output 的格式；省略时按扩展名推断，默认 JSON",
    )
    verify.add_argument("-o", "--output", type=Path, help="可选的报告输出路径")
    verify.add_argument("--json-report", type=Path, help="额外写出 JSON 报告")
    verify.add_argument("--markdown-report", type=Path, help="额外写出 Markdown 报告")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "verify":  # pragma: no cover - argparse 已限制命令集合
        raise AssertionError(f"未处理的命令: {args.command}")
    report = verify_file(args.file)
    report_format = args.report_format
    if report_format is None:
        report_format = (
            "markdown" if args.output and args.output.suffix.lower() == ".md" else "json"
        )
    rendered = report.to_markdown() if report_format == "markdown" else report.to_json()
    if args.output is not None:
        _write_report(args.output, rendered)
    if args.json_report is not None:
        _write_report(args.json_report, report.to_json())
    if args.markdown_report is not None:
        _write_report(args.markdown_report, report.to_markdown())
    sys.stdout.write(rendered)
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
