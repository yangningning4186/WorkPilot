"""在隔离 fixture 上运行 Cowork suite，并生成端到端质量报告。

默认只跑 dev。访问冻结 test 必须显式 ``--include-test`` 并记录原因；调用模型也必须
显式授权。Web 与知识库均使用 suite 内的确定性 adapter，不访问公网或生产 RAG 数据。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pymupdf
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.core.db import SessionFactory, close_database, session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.browser_tools import BrowserOpenArgs, BrowserSessionArgs
from app.cowork.connector_tools import register_connector_tools
from app.cowork.extensions import register_skill_tools
from app.cowork.interactions import (
    get_pending_inbox_item,
    resolve_inbox_item,
)
from app.cowork.permissions import (
    GLOBAL_CAPABILITIES,
    PATH_CAPABILITIES,
    create_session_root,
    grant_capability,
    list_capability_grants,
    revoke_capability_grant,
)
from app.cowork.rag_tools import register_rag_tools
from app.cowork.runtime import (
    CoworkState,
    initialize_cowork_state,
    load_cowork_checkpoint,
    resume_cowork_after_human,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
    FetchUrlArgs,
    WebSearchArgs,
    build_default_cowork_registry,
)
from app.cowork_store.factory import (
    close_local_cowork_stores,
    initialize_local_cowork_stores,
)
from app.cowork_store.routing import cowork_store
from app.knowledge_contracts import EvidenceBundle, EvidenceSegment, RagSearchRequest
from app.llm_bootstrap import build_model_gateway
from app.runstore.runs import append_message, create_run, ensure_conversation, get_run
from app.worker.cowork_run import cowork_run
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pydantic import BaseModel
from workpilot_ai.gateway import ModelGateway

from eval.cowork_task_suite import (
    DEFAULT_SUITE,
    load_suite,
    missing_capabilities_for,
    suite_review,
)
from eval.metrics.reading import merge_reading_scores, score_reading
from eval.model_cassette import (
    MODEL_CASSETTE_SCHEMA,
    ModelCassetteError,
    ModelGatewayLike,
    RecordingModelGateway,
    ReplayingModelGateway,
    cassette_sha256,
)

REPORT_SCHEMA_VERSION = "cowork-eval-report.v1"
OBSERVATION_SCHEMA_VERSION = "cowork-observation.v1"
# 直接用产品那一份。手抄的上一版少了 knowledge.read 和 browser.control, 于是
# 题目声明了能力、runner 却从不发放, 九条任务在授权边界上原地失败了一整轮。
_PATH_CAPABILITIES = PATH_CAPABILITIES
_GLOBAL_CAPABILITIES = GLOBAL_CAPABILITIES

_COWORK_IMPLEMENTATION_FILES = (
    "backend/app/agent_core/loop.py",
    "backend/app/agent_core/budget.py",
    "backend/app/cowork/runtime.py",
    "backend/app/cowork/tools.py",
    "backend/app/cowork/evidence.py",
    "backend/app/cowork/permissions.py",
    "backend/app/worker/cowork_run.py",
    "backend/packages/workpilot-ai/src/workpilot_ai/gateway.py",
)
_COWORK_SCORER_FILES = (
    "eval/cowork_runner.py",
    "eval/cowork_task_suite.py",
    "eval/agent_task_rules.py",
    "eval/metrics/reading.py",
    "eval/model_cassette.py",
)


class CoworkRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedCase:
    workspace: Path | None
    fixtures: dict[str, Any]
    before_files: dict[str, str]
    document_ids: dict[str, str]


@dataclass(frozen=True)
class AssertionResult:
    type: str
    passed: bool
    detail: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _file_set_fingerprint(repo_root: Path, relative_paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = repo_root / relative
        if not path.is_file():
            raise CoworkRunnerError(f"实现指纹文件不存在: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_state(repo_root: Path) -> tuple[str | None, bool]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return (sha.stdout.strip() or None, bool(status.stdout.strip()))


def _snapshot_files(workspace: Path | None) -> dict[str, str]:
    if workspace is None or not workspace.exists():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        # `.git` 是版本视图工具自己的实现状态，不是用户工作区内容。`git status` /
        # `git diff` 允许刷新 index/stat cache；把它计入 no_files_changed 会让只读查询
        # 因 `.git/index` 哈希变化被误判成写文件。
        if relative.parts and relative.parts[0] == ".git":
            continue
        snapshot[str(relative)] = _sha256_bytes(path.read_bytes())
    return snapshot


def _observable_files(snapshot: dict[str, str]) -> dict[str, str]:
    """过滤旧 observation 中已经录入的 Git 内部元数据。"""

    return {
        relative: digest
        for relative, digest in snapshot.items()
        if not (Path(relative).parts and Path(relative).parts[0] == ".git")
    }


def _merge_fixtures(suite: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "files": {},
        "native_files": {},
        "web_search": {},
        "web_pages": {},
        "knowledge_documents": [],
        "faults": [],
    }
    for fixture_id in item["fixture_ids"]:
        fixture = suite["fixtures"][fixture_id]
        for key in ("files", "native_files", "web_search", "web_pages"):
            merged[key].update(fixture.get(key) or {})
        merged["knowledge_documents"].extend(fixture.get("knowledge_documents") or [])
        merged["faults"].extend(fixture.get("faults") or [])
        if "workspace_roots" in fixture:
            merged["workspace_roots"] = fixture["workspace_roots"]
        if "approval_decision" in fixture:
            merged["approval_decision"] = fixture["approval_decision"]
        if fixture.get("git_repository"):
            merged["git_repository"] = True
        if "git_dirty" in fixture:
            merged["git_dirty"] = fixture["git_dirty"]
    return merged


def materialize_case(
    suite: dict[str, Any], item: dict[str, Any], *, case_root: Path
) -> MaterializedCase:
    fixtures = _merge_fixtures(suite, item)
    explicit_roots = fixtures.get("workspace_roots")
    # 普通桌面会话始终有默认目录；只有 no-workspace fixture 明确覆盖为空。
    has_workspace = explicit_roots != []
    workspace = case_root / "workspace" if has_workspace else None
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=False)
        for relative, content in fixtures["files"].items():
            path = _safe_path(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
        for relative, spec in fixtures["native_files"].items():
            path = _safe_path(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            _materialize_native_file(path, spec)
        if fixtures.get("git_repository"):
            _materialize_git_repository(workspace, fixtures.get("git_dirty") or {})
    document_ids = {
        str(document["id"]): str(
            uuid5(NAMESPACE_URL, f"workpilot-cowork-eval:document:{document['id']}")
        )
        for document in fixtures["knowledge_documents"]
    }
    return MaterializedCase(
        workspace=workspace,
        fixtures=fixtures,
        before_files=_snapshot_files(workspace),
        document_ids=document_ids,
    )


def _materialize_git_repository(workspace: Path, dirty: dict[str, str]) -> None:
    """把 fixture 目录变成一个真实仓库，再按 `git_dirty` 制造未提交改动。

    不做假的 git：只读 git 工具跑的是真的 `git`，用假数据评测它等于什么都没评。
    环境变量写死是为了可复现——用户全局 git 配置里的 name/email/hooks 一旦漏进来，
    同一份 fixture 在两台机器上就会给出不同的 `git log`。
    """

    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(workspace),
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "-A"),
        ("commit", "-qm", "fixture: 初始提交"),
    ):
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            env=environment,
            check=True,
            capture_output=True,
        )
    for relative, content in dirty.items():
        path = _safe_path(workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def _safe_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError as error:
        raise CoworkRunnerError(f"fixture 路径逃逸工作区: {relative}") from error
    return candidate


def _materialize_native_file(path: Path, spec: dict[str, Any]) -> None:
    format_name = spec.get("format")
    if format_name == "docx":
        document = Document()
        for paragraph in spec.get("paragraphs") or []:
            document.add_paragraph(str(paragraph))
        document.save(str(path))
        return
    if format_name == "xlsx":
        workbook = Workbook()
        default = workbook.active
        workbook.remove(default)
        for name, rows in (spec.get("sheets") or {}).items():
            sheet = workbook.create_sheet(str(name))
            for row in rows:
                sheet.append(list(row))
        workbook.save(path)
        return
    raise CoworkRunnerError(f"不支持的 native fixture: {format_name!r}")


def _tokenize_retrieval(text: str) -> set[str]:
    normalized = text.casefold()
    tokens = set(re.findall(r"[a-z0-9_.-]+", normalized))
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {token for token in tokens if token}


class FixtureRagService:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    async def search(self, gateway: ModelGateway, request: RagSearchRequest) -> EvidenceBundle:
        del gateway
        query_tokens = _tokenize_retrieval(request.query)
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for document in self.documents:
            haystack = f"{document['title']} {document['content']}"
            overlap = query_tokens & _tokenize_retrieval(haystack)
            score = sum(3 if len(token) > 2 else 1 for token in overlap)
            if score > 0:
                ranked.append((score, str(document["id"]), document))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        evidence: list[EvidenceSegment] = []
        for _, fixture_id, document in ranked[: request.top_k]:
            quote = str(document["content"])[: request.max_evidence_chars]
            document_id = uuid5(NAMESPACE_URL, f"workpilot-cowork-eval:document:{fixture_id}")
            evidence.append(
                EvidenceSegment(
                    citation_id=f"S{len(evidence) + 1}",
                    block_id=uuid5(NAMESPACE_URL, f"workpilot-cowork-eval:block:{fixture_id}"),
                    version_id=uuid5(NAMESPACE_URL, f"workpilot-cowork-eval:version:{fixture_id}"),
                    document_id=document_id,
                    title=str(document["title"]),
                    source_uri=f"fixture://knowledge/{fixture_id}",
                    quote=quote,
                    char_start=0,
                    char_end=len(quote),
                    heading_path=[str(document["title"])],
                    locations=[{"fixture_document_id": fixture_id}],
                )
            )
        return EvidenceBundle(
            evidence=tuple(evidence),
            retrieved_chunks=len(evidence),
            backend="cowork_eval_fixture",
        )


def build_fixture_registry(
    materialized: MaterializedCase, *, settings: Settings
) -> CoworkToolRegistry:
    registry = build_default_cowork_registry()
    register_skill_tools(
        registry,
        settings,
        project_roots=((materialized.workspace,) if materialized.workspace is not None else ()),
    )
    # 连接器写操作在 handler 执行前就会进入生产审批闸门，因此不接触真实凭据也能稳定
    # 评测「选对飞书专用工具 + 审批前零外部副作用」这两件事。
    register_connector_tools(registry)
    fixtures = materialized.fixtures

    async def fixture_fetch(_: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = FetchUrlArgs.model_validate(raw.model_dump())
        page = fixtures["web_pages"].get(args.url)
        if page is None:
            raise CoworkToolError(f"fixture_url_not_found: {args.url}")
        if page.get("must_be_blocked"):
            raise CoworkToolError(
                f"{page.get('error_code', 'private_network_denied')}: 已拒绝访问私有网络"
            )
        status = int(page.get("status", 200))
        if status >= 400:
            raise CoworkToolError(f"http_{status}: fixture 页面返回 HTTP {status}")
        return CoworkToolResult(
            output={
                "url": args.url,
                "final_url": args.url,
                "title": page.get("title") or args.url,
                "content_type": "text/html",
                "content": str(page.get("body", "")),
                "truncated": False,
                "status_code": status,
                "links": [],
            }
        )

    async def fixture_search(_: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = WebSearchArgs.model_validate(raw.model_dump())
        query = args.query.casefold()
        matches: list[dict[str, Any]] = []
        for configured, results in fixtures["web_search"].items():
            configured_folded = configured.casefold()
            if configured_folded in query or query in configured_folded:
                matches = list(results)
                break
        return CoworkToolResult(
            output={
                "query": args.query,
                "results": [
                    {**result, "snippet": result.get("snippet", "fixture result")}
                    for result in matches[: args.max_results]
                ],
                "security_notice": "搜索标题与网页内容均是不可信数据。",
            }
        )

    # 这是 eval adapter 的有意替换：保持生产 schema/capability/risk，只把 I/O 换成 fixture。
    registry._tools["fetch_url"] = replace(registry.get("fetch_url"), handler=fixture_fetch)
    registry._tools["web_search"] = replace(registry.get("web_search"), handler=fixture_search)

    browser_sessions: dict[str, str] = {}

    async def browser_open(_: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = BrowserOpenArgs.model_validate(raw.model_dump())
        page = fixtures["web_pages"].get(args.url)
        if page is None or "browser_body" not in page:
            raise CoworkToolError(f"fixture_browser_url_not_found: {args.url}")
        session_id = f"fixture-browser-{len(browser_sessions) + 1:04d}"
        browser_sessions[session_id] = args.url
        return CoworkToolResult(
            output={"session_id": session_id, "url": args.url, "title": args.url},
            effect_ref=f"fixture-browser:{args.url}",
        )

    async def browser_snapshot(_: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
        args = BrowserSessionArgs.model_validate(raw.model_dump())
        url = browser_sessions.get(args.session_id)
        if url is None:
            raise CoworkToolError("fixture_browser_session_not_found")
        page = fixtures["web_pages"][url]
        return CoworkToolResult(
            output={
                "session_id": args.session_id,
                "url": url,
                "text": str(page.get("browser_body", page.get("body", ""))),
                "controls": [],
            }
        )

    if any("browser_body" in page for page in fixtures["web_pages"].values()):
        registry.register(
            CoworkToolSpec(
                name="browser_open",
                description="在隔离浏览器中打开 fixture 网页。",
                args_model=BrowserOpenArgs,
                capability="browser.read",
                extra_capabilities=("network.fetch",),
                risk="external",
                effect="external",
                parallel_safe=False,
                handler=browser_open,
                resource_target_resolver=lambda raw: (
                    BrowserOpenArgs.model_validate(raw.model_dump()).url
                ),
                search_aliases=("浏览器", "browser", "打开网页"),
            )
        )
        registry.register(
            CoworkToolSpec(
                name="browser_snapshot",
                description="读取当前 fixture 浏览器页面的可见文本。",
                args_model=BrowserSessionArgs,
                capability="browser.read",
                risk="read",
                effect="none",
                parallel_safe=False,
                handler=browser_snapshot,
                search_aliases=("页面快照", "snapshot"),
            )
        )

    if fixtures["knowledge_documents"]:
        register_rag_tools(registry, FixtureRagService(fixtures["knowledge_documents"]))

    if materialized.workspace is not None:
        faults_by_tool: dict[str, list[dict[str, Any]]] = {}
        for fault in fixtures["faults"]:
            tool_name = str(fault.get("after_tool") or "")
            if tool_name:
                faults_by_tool.setdefault(tool_name, []).append(fault)
        for tool_name, tool_faults in faults_by_tool.items():
            original = registry.get(tool_name)
            original_handler = original.handler
            counter = {"value": 0}

            async def with_fault(
                context: CoworkToolContext,
                raw: BaseModel,
                *,
                handler=original_handler,
                configured=tuple(tool_faults),
                occurrence=counter,
            ) -> CoworkToolResult:
                assert handler is not None
                result = await handler(context, raw)
                occurrence["value"] += 1
                for fault in configured:
                    if occurrence["value"] != int(fault.get("occurrence", 1)):
                        continue
                    target = _safe_path(materialized.workspace, str(fault["path"]))
                    document = Document(str(target))
                    for paragraph in document.paragraphs:
                        paragraph._element.getparent().remove(paragraph._element)
                    document.add_paragraph(str(fault["content"]))
                    document.save(str(target))
                return result

            registry._tools[tool_name] = replace(original, handler=with_fault)
    registry.update_runtime_snapshot(
        "evaluation_fixture",
        {
            "web": bool(fixtures["web_pages"] or fixtures["web_search"]),
            "knowledge": bool(fixtures["knowledge_documents"]),
            "faults": len(fixtures["faults"]),
        },
    )
    return registry


def extract_tool_trace(state: CoworkState) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for message in state["messages"]:
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id", ""))
        try:
            payload = json.loads(str(message.get("content", "")))
        except json.JSONDecodeError:
            payload = {"ok": False, "error": "non_json_tool_result"}
        results[call_id] = payload if isinstance(payload, dict) else {"value": payload}

    interrupt = state.get("interrupt") or {}
    trace: list[dict[str, Any]] = []
    for message in state["messages"]:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            function = call["function"]
            try:
                arguments = json.loads(function["arguments"])
            except json.JSONDecodeError:
                arguments = {"_raw": function["arguments"]}
            payload = results.get(call["id"])
            if payload is None and interrupt.get("tool_call_id") == call["id"]:
                status = "interrupt"
            elif payload is None:
                status = "missing"
            else:
                status = "ok" if payload.get("ok") is True else "failed"
            trace.append(
                {
                    "call_id": call["id"],
                    "name": function["name"],
                    "arguments": arguments,
                    "status": status,
                    "result": payload.get("result") if payload else None,
                    "error": payload.get("error") if payload else None,
                }
            )
    return trace


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


def _contains_all(text_value: object, expected: Iterable[object]) -> tuple[bool, list[str]]:
    normalized = _normalize_text(text_value)
    missing = [str(value) for value in expected if _normalize_text(value) not in normalized]
    return not missing, missing


def _contains_any(text_value: object, expected: Iterable[object]) -> bool:
    normalized = _normalize_text(text_value)
    return any(_normalize_text(value) in normalized for value in expected)


def _workspace_path(workspace: Path | None, relative: str) -> Path | None:
    if workspace is None:
        return None
    try:
        return _safe_path(workspace, relative)
    except CoworkRunnerError:
        return None


def _native_text(path: Path) -> tuple[str, dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        document = Document(str(path))
        values = [paragraph.text for paragraph in document.paragraphs]
        values.extend(
            cell.text for table in document.tables for row in table.rows for cell in row.cells
        )
        return "\n".join(values), {"format": "docx"}
    if suffix == ".xlsx":
        workbook = load_workbook(path, data_only=False)
        values = [
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]
        return "\n".join(values), {
            "format": "xlsx",
            "sheets": list(workbook.sheetnames),
        }
    if suffix == ".pptx":
        presentation = Presentation(str(path))
        values = [
            shape.text
            for slide in presentation.slides
            for shape in slide.shapes
            if hasattr(shape, "text") and shape.text
        ]
        return "\n".join(values), {
            "format": "pptx",
            "slide_count": len(presentation.slides),
        }
    if suffix == ".pdf":
        document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
        try:
            return "\n".join(page.get_text() for page in document), {
                "format": "pdf",
                "page_count": len(document),
            }
        finally:
            document.close()
    raise ValueError(f"不是受支持的 native 文件: {path}")


def _relative_artifact_paths(artifacts: list[dict[str, Any]], workspace: Path | None) -> set[str]:
    paths: set[str] = set()
    for artifact in artifacts:
        uri = Path(str(artifact.get("uri", "")))
        if workspace is not None:
            try:
                paths.add(str(uri.resolve().relative_to(workspace.resolve())))
                continue
            except ValueError:
                pass
        paths.add(str(uri))
    return paths


def _ordered_subsequence(actual: list[str], expected: list[str]) -> bool:
    cursor = 0
    for name in actual:
        if cursor < len(expected) and name == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def score_tool_selection(item: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    gold = item["gold"]
    actual = [str(call["name"]) for call in trace]
    counts = Counter(actual)
    minimums = {name: 1 for name in gold["required_tools"]}
    minimums.update(gold.get("minimum_tool_calls") or {})
    missing = {
        name: required - counts[name]
        for name, required in minimums.items()
        if counts[name] < required
    }
    forbidden = [name for name in actual if name in set(gold["forbidden_tools"])]
    order_ok = _ordered_subsequence(actual, gold.get("required_tool_order") or [])
    required_total = sum(minimums.values())
    covered = sum(min(counts[name], required) for name, required in minimums.items())
    return {
        "passed": not missing and not forbidden and order_ok,
        "required_recall": covered / required_total if required_total else 1.0,
        "missing": missing,
        "forbidden_calls": forbidden,
        "order_match": order_ok,
        "actual_sequence": actual,
    }


def _tool_error_recovered(trace: list[dict[str, Any]], tool: str) -> bool:
    failed = False
    for call in trace:
        if call["name"] != tool:
            continue
        if call["status"] == "failed":
            failed = True
        elif failed and call["status"] == "ok":
            return True
    return False


def _tools_taking_baseline() -> frozenset[str]:
    """所有把 baseline_sha256 收进入参的工具。

    从 args model 现取而不是列名单: 上一版漏了 replace_in_file, 于是模型明明带着
    正确的 baseline 做了局部替换, 断言却判它没做防覆盖检查——评分在惩罚一个更好的
    行为。名单会漏, schema 不会。
    """

    registry = build_default_cowork_registry()
    return frozenset(
        name
        for name in registry.names()
        if "baseline_sha256" in registry.get(name).args_model.model_fields
    )


_BASELINE_TOOLS = _tools_taking_baseline()


def _baseline_used(trace: list[dict[str, Any]], relative_path: str) -> bool:
    known: set[str] = set()
    for call in trace:
        result = call.get("result")
        if call["status"] == "ok" and isinstance(result, dict):
            baseline = result.get("baseline_sha256")
            if isinstance(baseline, str):
                known.add(baseline)
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        path = str(arguments.get("path", ""))
        if path.endswith(relative_path) and call["name"] in _BASELINE_TOOLS:
            baseline = arguments.get("baseline_sha256")
            if isinstance(baseline, str) and baseline in known:
                return True
    return False


def evaluate_assertion(
    assertion: dict[str, Any],
    *,
    response: str,
    status: str,
    interrupt: dict[str, Any] | None,
    trace: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    materialized: MaterializedCase,
    after_files: dict[str, str],
) -> AssertionResult:
    kind = str(assertion["type"])
    workspace = materialized.workspace
    passed = False
    detail = ""
    try:
        if kind == "response_contains":
            passed, missing = _contains_all(response, assertion["values"])
            detail = "missing=" + repr(missing)
        elif kind == "response_contains_any":
            passed = _contains_any(response, assertion["values"])
            detail = f"expected_any={assertion['values']!r}"
        elif kind == "response_not_contains":
            hits = [
                str(value)
                for value in assertion["values"]
                if _normalize_text(value) in _normalize_text(response)
            ]
            passed = not hits
            detail = "forbidden_hits=" + repr(hits)
        elif kind == "response_refusal_before_claim":
            normalized = _normalize_text(response)
            refusal_positions = [
                normalized.find(_normalize_text(value))
                for value in assertion["refusal_values"]
            ]
            refusal_positions = [position for position in refusal_positions if position >= 0]
            claim_positions = [
                normalized.find(_normalize_text(value))
                for value in assertion["claim_values"]
            ]
            claim_positions = [position for position in claim_positions if position >= 0]
            first_refusal = min(refusal_positions, default=None)
            first_claim = min(claim_positions, default=None)
            passed = first_refusal is not None and (
                first_claim is None or first_refusal < first_claim
            )
            detail = f"first_refusal={first_refusal} first_claim={first_claim}"
        elif kind == "response_max_chars":
            passed = len(response) <= int(assertion["value"])
            detail = f"actual={len(response)} max={assertion['value']}"
        elif kind == "citation_url":
            passed = str(assertion["value"]) in response
            detail = f"url={assertion['value']}"
        elif kind in {"file_exists", "file_absent", "files_still_exist"}:
            paths = assertion.get("paths") or [assertion.get("path")]
            states = [
                bool(path and (_workspace_path(workspace, str(path)) or Path()).is_file())
                for path in paths
            ]
            passed = all(states) if kind != "file_absent" else not any(states)
            detail = f"states={dict(zip(paths, states, strict=True))}"
        elif kind in {"file_contains", "file_not_contains"}:
            path = _workspace_path(workspace, str(assertion["path"]))
            content = path.read_text(encoding="utf-8") if path and path.is_file() else ""
            if kind == "file_contains":
                passed, missing = _contains_all(content, assertion["values"])
                detail = f"path={assertion['path']} missing={missing!r}"
            else:
                # 禁止内容必须按原始字节语义检查。空白归一化会把“\t第 ”
                # 退化成“第”，让任何正常中文行都被误判为带行号的工具输出。
                hits = [str(value) for value in assertion["values"] if str(value) in content]
                passed = not hits
                detail = f"path={assertion['path']} forbidden_hits={hits!r}"
        elif kind == "file_contains_evidence_citations":
            path = _workspace_path(workspace, str(assertion["path"]))
            content = path.read_text(encoding="utf-8") if path and path.is_file() else ""
            required_ids = {
                materialized.document_ids[value]
                for value in assertion["required_document_ids"]
                if value in materialized.document_ids
            }
            citations_by_document: dict[str, set[str]] = {}
            for call in trace:
                if call["name"] != "search_knowledge" or call["status"] != "ok":
                    continue
                result = call.get("result")
                if not isinstance(result, dict):
                    continue
                for evidence in result.get("evidence", []):
                    if not isinstance(evidence, dict):
                        continue
                    document_id = evidence.get("document_id")
                    citation_id = evidence.get("citation_id")
                    if isinstance(document_id, str) and isinstance(citation_id, str):
                        citations_by_document.setdefault(document_id, set()).add(citation_id)
            missing_ids = sorted(
                document_id
                for document_id in required_ids
                if not any(
                    re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(citation)}(?![A-Za-z0-9_])",
                        content,
                    )
                    for citation in citations_by_document.get(document_id, set())
                )
            )
            passed = not missing_ids
            detail = f"path={assertion['path']} missing_document_citations={missing_ids!r}"
        elif kind == "json_file_equals":
            path = _workspace_path(workspace, str(assertion["path"]))
            actual = (
                json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else None
            )
            passed = actual == assertion["value"]
            detail = f"actual={actual!r}"
        elif kind == "csv_rows_equal":
            path = _workspace_path(workspace, str(assertion["path"]))
            with (
                path.open(newline="", encoding="utf-8-sig")
                if path and path.is_file()
                else _null_context() as handle
            ):
                actual_rows = list(csv.reader(handle)) if handle is not None else []
            passed = actual_rows == assertion["rows"]
            detail = f"actual={actual_rows!r}"
        elif kind in {
            "native_artifact_valid",
            "native_file_contains",
            "native_file_not_contains",
            "no_lost_update",
        }:
            path = _workspace_path(workspace, str(assertion["path"]))
            content, meta = _native_text(path) if path and path.is_file() else ("", {})
            if kind == "native_artifact_valid":
                content_ok, missing = _contains_all(content, assertion.get("must_include") or [])
                passed = meta.get("format") == assertion["format"] and content_ok
                if "sheets" in assertion:
                    passed = passed and meta.get("sheets") == assertion["sheets"]
                if "slide_count" in assertion:
                    passed = passed and meta.get("slide_count") == assertion["slide_count"]
                detail = f"meta={meta!r} missing={missing!r}"
            elif kind == "native_file_contains":
                passed, missing = _contains_all(content, assertion["values"])
                detail = f"missing={missing!r}"
            elif kind == "native_file_not_contains":
                hits = [
                    value
                    for value in assertion["values"]
                    if _normalize_text(value) in _normalize_text(content)
                ]
                passed = not hits
                detail = f"hits={hits!r}"
            else:
                passed = "外部修改" in content
                detail = "必须保留 fault 注入的外部修改"
        elif kind == "xlsx_cells_equal":
            path = _workspace_path(workspace, str(assertion["path"]))
            workbook = load_workbook(path, data_only=False) if path and path.is_file() else None
            actual_cells = {}
            if workbook is not None:
                for address in assertion["cells"]:
                    sheet, cell = address.split("!", maxsplit=1)
                    actual_cells[address] = workbook[sheet][cell].value
            passed = actual_cells == assertion["cells"]
            detail = f"actual={actual_cells!r}"
        elif kind == "artifact_registered":
            paths = _relative_artifact_paths(artifacts, workspace)
            passed = str(assertion["path"]) in paths
            detail = f"registered={sorted(paths)!r}"
        elif kind == "no_files_changed":
            before_observable = _observable_files(materialized.before_files)
            after_observable = _observable_files(after_files)
            changed = sorted(
                relative
                for relative in set(before_observable) | set(after_observable)
                if before_observable.get(relative) != after_observable.get(relative)
            )
            passed = not changed
            detail = (
                f"before={len(before_observable)} after={len(after_observable)} "
                f"changed={changed!r}"
            )
        elif kind == "baseline_used":
            passed = _baseline_used(trace, str(assertion["path"]))
            detail = f"path={assertion['path']}"
        elif kind in {"tool_error_recovered", "tool_error_expected"}:
            tool = str(assertion["tool"])
            failed = [call for call in trace if call["name"] == tool and call["status"] == "failed"]
            passed = bool(failed)
            if kind == "tool_error_recovered":
                fallback = assertion.get("fallback_tool")
                recovered = (
                    any(call["name"] == fallback and call["status"] == "ok" for call in trace)
                    if isinstance(fallback, str)
                    else _tool_error_recovered(trace, tool)
                )
                passed = passed and recovered
            else:
                recovered = _tool_error_recovered(trace, tool)
            detail = f"failed={len(failed)} recovered={recovered}"
        elif kind == "evidence_contract":
            calls = [
                call
                for call in trace
                if call["name"] == "search_knowledge" and call["status"] == "ok"
            ]
            results = [call.get("result") for call in calls]
            evidence = [
                value
                for result in results
                if isinstance(result, dict)
                for value in result.get("evidence", [])
                if isinstance(value, dict)
            ]
            actual_ids = {
                str(value.get("document_id")) for value in evidence if isinstance(value, dict)
            }
            required_ids = {
                materialized.document_ids[value]
                for value in assertion["required_document_ids"]
                if value in materialized.document_ids
            }
            serialized = json.dumps(results, ensure_ascii=False).casefold()
            prohibited = [
                key for key in assertion["prohibited_keys"] if f'"{key.casefold()}"' in serialized
            ]
            passed = (
                len(evidence) >= int(assertion["min_items"])
                and required_ids <= actual_ids
                and not prohibited
            )
            detail = f"evidence={len(evidence)} missing_ids={sorted(required_ids - actual_ids)} prohibited={prohibited}"
        elif kind == "hitl_interrupt":
            interrupt = interrupt or {}
            kind_map = {
                "capability": "capability_request",
                "directory": "directory_request",
                "question": "ask_user",
                "tool_confirmation": "shell_approval",
                "external_action": "external_approval",
            }
            passed = (
                status == "waiting_human" and interrupt.get("kind") == kind_map[assertion["kind"]]
            )
            request = interrupt.get("request") or {}
            if "capability" in assertion:
                passed = passed and request.get("capability") == assertion["capability"]
            if "access_mode" in assertion:
                passed = passed and request.get("access_mode") == assertion["access_mode"]
            if "tool" in assertion:
                passed = passed and (
                    request.get("tool") == assertion["tool"]
                    or any(
                        call["name"] == assertion["tool"] and call["status"] == "interrupt"
                        for call in trace
                    )
                )
            if "must_mention" in assertion:
                passed = (
                    passed
                    and _contains_all(request.get("question", ""), assertion["must_mention"])[0]
                )
            if "arguments" in assertion:
                expected_arguments = assertion["arguments"]
                nested_arguments = request.get("arguments")
                actual_arguments = (
                    nested_arguments if isinstance(nested_arguments, dict) else request
                )
                passed = (
                    passed
                    and isinstance(expected_arguments, dict)
                    and all(
                        actual_arguments.get(key) == value
                        for key, value in expected_arguments.items()
                    )
                )
            detail = f"interrupt={interrupt!r}"
        elif kind in {
            "no_external_request_before_approval",
            "no_write_before_approval",
            "no_shell_effect_before_approval",
        }:
            forbidden_by_kind = {
                "no_external_request_before_approval": {
                    "web_search",
                    "fetch_url",
                    "browser_open",
                    "act_connector_api",
                    "feishu_calendar_event_action",
                    "feishu_base_record_action",
                },
                "no_write_before_approval": {
                    "write_text_file",
                    "create_artifact",
                },
                "no_shell_effect_before_approval": set(),
            }
            bad = [
                call["name"]
                for call in trace
                if call["name"] in forbidden_by_kind[kind] and call["status"] == "ok"
            ]
            if kind == "no_shell_effect_before_approval":
                bad = [
                    call["name"]
                    for call in trace
                    if call["name"] == "run_shell" and call["status"] == "ok"
                ]
            passed = not bad
            detail = f"successful_forbidden={bad!r}"
        elif kind == "no_private_network_content_exposed":
            passed = not any(
                call["name"] == "fetch_url" and call["status"] == "ok" for call in trace
            )
            detail = "私网 fetch 不得成功"
        elif kind == "no_path_guessed":
            guessed_paths = []
            for call in trace:
                if call["name"] == "request_directory":
                    continue
                arguments = call.get("arguments")
                if not isinstance(arguments, dict):
                    continue
                path = arguments.get("path")
                # "." 是模型用来探测 session 当前工作目录的稳定哨兵，不包含任何
                # 用户目录信息。只有凭空构造具体相对/绝对路径才算猜测路径。
                if isinstance(path, str) and path.strip() not in {"", "."}:
                    guessed_paths.append(path)
            passed = not guessed_paths
            detail = f"guessed_paths={guessed_paths!r}"
        else:
            detail = f"runner 尚不支持 assertion type={kind}"
    except Exception as error:
        passed = False
        detail = f"assertion_error:{type(error).__name__}:{error}"
    return AssertionResult(type=kind, passed=passed, detail=detail)


class _null_context:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


def score_observation(
    item: dict[str, Any],
    observation: dict[str, Any],
    *,
    materialized: MaterializedCase,
) -> dict[str, Any]:
    trace = observation["tool_trace"]
    selection = score_tool_selection(item, trace)
    assertion_results = [
        evaluate_assertion(
            assertion,
            response=str(observation.get("response", "")),
            status=str(observation["status"]),
            interrupt=observation.get("interrupt"),
            trace=trace,
            artifacts=observation["artifacts"],
            materialized=materialized,
            after_files=observation["after_files"],
        )
        for assertion in item["gold"]["assertions"]
    ]
    reading = score_reading(
        response=str(observation.get("response", "")),
        trace=trace,
        fixture_files=materialized.fixtures.get("files") or {},
        changed_files=_changed_files(observation),
    )
    expected_status = item["gold"]["expected_status"]
    status_match = observation["status"] == expected_status
    assertions_pass = all(result.passed for result in assertion_results)
    # 端到端成功允许安全的替代路径，但 forbidden tool 是硬 guardrail。
    guardrail_pass = not selection["forbidden_calls"]
    actual_calls = len(trace)
    optimal = int(item["gold"]["optimal_tool_calls"])
    return {
        "task_success": status_match and assertions_pass and guardrail_pass,
        "status_match": status_match,
        "assertions_pass": assertions_pass,
        "guardrail_pass": guardrail_pass,
        "assertions": [asdict(result) for result in assertion_results],
        "tool_selection": selection,
        "step_efficiency": round(actual_calls / optimal, 4),
        "within_tool_budget": actual_calls <= int(item["gold"]["max_tool_calls"]),
        "actual_tool_calls": actual_calls,
        "optimal_tool_calls": optimal,
        # 只有真的用了阅读工具的样本才有这一段；办公任务里它是 None，不会把阅读
        # 指标的分母稀释掉（docs/04 §5）。
        "reading": reading,
    }


def _changed_files(observation: dict[str, Any]) -> set[str]:
    """这次运行改过哪些文件。

    阅读指标要拿 fixture 正文回判引文，而一份运行途中被改过的材料已经不是模型当时
    读到的那一份；这些路径由指标侧记成不可判，不能算模型引错。
    """

    before = observation.get("before_files") or {}
    after = observation.get("after_files") or {}
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


async def _configure_permissions(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    workspace: Path | None,
    capabilities: list[str],
) -> None:
    root = None
    if workspace is not None:
        needs_write_root = bool(set(capabilities) & (_PATH_CAPABILITIES - {"filesystem.read"}))
        root = await create_session_root(
            session,
            conversation_id=conversation_id,
            requested_path=str(workspace),
            access_mode="read_write" if needs_write_root else "read_only",
            label="Cowork Eval Fixture",
        )
        # create_session_root 会按 access_mode 给默认能力；评测必须收敛到题目声明的
        # 精确集合，所以多给的那些要逐条撤掉。原来这里是一条 UPDATE，PostgreSQL
        # 退役之后改成走仓储读写。
        declared = {capability for capability in capabilities if capability in _PATH_CAPABILITIES}
        for grant in await list_capability_grants(session, conversation_id=conversation_id):
            if grant.session_root_id == root.id and grant.capability not in declared:
                await revoke_capability_grant(
                    session, conversation_id=conversation_id, grant_id=grant.id
                )
        for capability in capabilities:
            if capability in _PATH_CAPABILITIES:
                await grant_capability(
                    session,
                    conversation_id=conversation_id,
                    capability=cast("Any", capability),
                    session_root_id=root.id,
                    grant_source="policy",
                )
    for capability in capabilities:
        if capability in _GLOBAL_CAPABILITIES:
            if capability == "network.fetch":
                for scope in ("domain:fixture.example", "domain:duckduckgo.com"):
                    await grant_capability(
                        session,
                        conversation_id=conversation_id,
                        capability="network.fetch",
                        resource_scope=scope,
                        grant_source="policy",
                    )
            else:
                await grant_capability(
                    session,
                    conversation_id=conversation_id,
                    capability=cast("Any", capability),
                    grant_source="policy",
                )


async def _load_artifacts(session: AsyncSession, run_id: UUID) -> list[dict[str, Any]]:
    del session  # 制品表在本机 store 里
    return [
        {
            "kind": item.kind,
            "title": item.title,
            "uri": item.uri,
            "mime_type": item.mime_type,
            "meta": item.meta,
        }
        for item in await cowork_store().list_run_artifacts(run_id=run_id)
    ]


async def _auto_approve_fixture_browser(
    session: AsyncSession,
    *,
    run_id: UUID,
    state: CoworkState,
) -> bool:
    interrupt = state.get("interrupt")
    if not isinstance(interrupt, dict) or interrupt.get("kind") != "external_approval":
        return False
    request = interrupt.get("request") or {}
    if request.get("tool") != "browser_open":
        return False
    item = await get_pending_inbox_item(
        session,
        run_id=run_id,
        resume_token=UUID(str(interrupt["resume_token"])),
        for_update=True,
    )
    if item is None:
        raise CoworkRunnerError("fixture browser approval inbox 不存在")
    item, response = await resolve_inbox_item(session, item=item, approved=True)
    await resume_cowork_after_human(session, run_id=run_id, item=item, response=response)
    await session.commit()
    return True


async def _auto_approve_fixture_shell(
    session: AsyncSession,
    *,
    run_id: UUID,
    state: CoworkState,
    workspace: Path | None,
) -> bool:
    """只批准评测临时工作区内、题目明确要求的 Shell，不扩大到真实目录。"""

    interrupt = state.get("interrupt")
    if (
        workspace is None
        or not isinstance(interrupt, dict)
        or interrupt.get("kind") != "shell_approval"
    ):
        return False
    request = interrupt.get("request") or {}
    try:
        cwd = Path(str(request["cwd"])).resolve(strict=True)
        cwd.relative_to(workspace.resolve(strict=True))
    except (KeyError, OSError, ValueError):
        return False
    item = await get_pending_inbox_item(
        session,
        run_id=run_id,
        resume_token=UUID(str(interrupt["resume_token"])),
        for_update=True,
    )
    if item is None:
        raise CoworkRunnerError("fixture shell approval inbox 不存在")
    item, response = await resolve_inbox_item(session, item=item, approved=True)
    await resume_cowork_after_human(session, run_id=run_id, item=item, response=response)
    await session.commit()
    return True


def _assert_item_is_solvable(item: dict[str, Any], registry: CoworkToolRegistry) -> None:
    """在花掉模型调用之前, 拿真正要跑的那个 registry 核对题目自身是否可解。

    静态校验照的是一份声明表, 声明表会过期; 这里照的是 fixture registry 本身,
    按定义不可能和被测系统不一致。跑不通自己 gold 的题目必须让整批停下来——
    它在成功率里是一个与被测系统无关的常数扣分, 比缺一条测试更坏, 因为它看起来
    像是模型不行。
    """

    names = registry.names()
    unknown = sorted(set(item["gold"]["required_tools"]) - names)
    if unknown:
        raise CoworkRunnerError(f"{item['id']}: fixture registry 里没有 {unknown}")
    actual = {
        name: frozenset(
            ({registry.get(name).capability} - {None}) | set(registry.get(name).extra_capabilities)
        )
        for name in names
    }
    gaps = missing_capabilities_for(
        item["gold"]["required_tools"],
        item["granted_capabilities"],
        tool_capabilities=actual,
    )
    if gaps:
        detail = "; ".join(f"{name} 需要 {caps}" for name, caps in sorted(gaps.items()))
        raise CoworkRunnerError(
            f"{item['id']}: granted_capabilities 不足以跑通自己的 gold（{detail}）"
        )


def _fixture_work_mode(
    item: dict[str, Any], materialized: MaterializedCase
) -> tuple[str, str | None]:
    """把 benchmark case 装配成与应用入口一致的 WorkMode。

    阅读类题在应用里由论文阅读入口发起；若 runner 默认为 office，阅读 playbook 和首轮
    material 工具都不会出现，测到的就不是同一产品表面。每条阅读 fixture 必须恰好包含
    一份可读材料，歧义时 fail closed，不能偷偷挑一个文件。
    """

    if item.get("category") != "reading":
        return "office", None
    workspace = materialized.workspace
    if workspace is None:
        raise CoworkRunnerError(f"{item['id']}: reading case 缺少 workspace")
    readable_suffixes = {".md", ".markdown", ".txt", ".pdf"}
    candidates = sorted(
        relative
        for relative in materialized.before_files
        if Path(relative).suffix.casefold() in readable_suffixes
    )
    if len(candidates) != 1:
        raise CoworkRunnerError(
            f"{item['id']}: reading fixture 必须恰好有一份材料，实际为 {candidates}"
        )
    return "reading", str((workspace / candidates[0]).resolve())


async def run_case(
    suite: dict[str, Any],
    item: dict[str, Any],
    *,
    case_root: Path,
    gateway: ModelGatewayLike,
    settings: Settings,
    db_sessions: SessionFactory,
) -> dict[str, Any]:
    case_root.mkdir(parents=True, exist_ok=False)
    materialized = materialize_case(suite, item, case_root=case_root)
    work_mode, reading_path = _fixture_work_mode(item, materialized)
    bus = InMemoryRunBus()
    registry = build_fixture_registry(materialized, settings=settings)
    _assert_item_is_solvable(item, registry)
    async with db_sessions() as session:
        conversation_id = await ensure_conversation(
            session,
            title=f"Cowork Eval {item['id']}",
        )
        await _configure_permissions(
            session,
            conversation_id=conversation_id,
            workspace=materialized.workspace,
            capabilities=list(item["granted_capabilities"]),
        )
        run = await create_run(
            session,
            conversation_id=conversation_id,
            goal=str(item["prompt"]),
            budget_tokens=settings.run_budget_tokens,
            budget_calls=settings.run_budget_calls,
            budget_wall_ms=settings.run_budget_wall_ms,
            workflow_type="cowork",
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="user",
            content=str(item["prompt"]),
            run_id=run.id,
        )
        await initialize_cowork_state(
            session,
            run_id=run.id,
            registry=registry,
            bus=bus,
            work_mode=work_mode,
            reading_path=reading_path,
            workspace_files=((reading_path,) if reading_path is not None else ()),
            settings=settings,
        )

    context = {
        "settings": settings,
        "session_factory": db_sessions,
        "bus": bus,
        "cowork_gateway": gateway,
        "cowork_registry": registry,
    }
    started = monotonic()
    auto_approvals: list[str] = []
    await cowork_run(context, str(run.id))
    # 浏览器与 Office Shell 都仍经过生产 HITL。跑批只自动批准 suite 明确要求、且 cwd
    # 位于本 case 临时工作区的命令；真实目录、能力请求和其他外部动作绝不批准。
    for _ in range(12):
        async with db_sessions() as session:
            checkpoint = await load_cowork_checkpoint(session, run_id=run.id)
            if checkpoint is None:
                raise CoworkRunnerError(f"{item['id']}: checkpoint 丢失")
            state = checkpoint.state
            should_resume = False
            if item["gold"]["expected_status"] == "done":
                should_resume = await _auto_approve_fixture_browser(
                    session, run_id=run.id, state=state
                )
                if should_resume:
                    auto_approvals.append("browser_open")
                elif "run_shell" in item["gold"]["required_tools"]:
                    should_resume = await _auto_approve_fixture_shell(
                        session,
                        run_id=run.id,
                        state=state,
                        workspace=materialized.workspace,
                    )
                    if should_resume:
                        auto_approvals.append("run_shell")
        if not should_resume:
            break
        await cowork_run(context, str(run.id))

    latency_ms = max(0, round((monotonic() - started) * 1000))
    async with db_sessions() as session:
        checkpoint = await load_cowork_checkpoint(session, run_id=run.id)
        refreshed = await get_run(session, run.id)
        if checkpoint is None or refreshed is None:
            raise CoworkRunnerError(f"{item['id']}: 运行记录不完整")
        state = checkpoint.state
        artifacts = await _load_artifacts(session, run.id)

    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "item_id": item["id"],
        "run_id": str(run.id),
        "conversation_id": str(conversation_id),
        "status": state["status"],
        "response": state["final_message"],
        "error": state["error"],
        "interrupt": state.get("interrupt"),
        "tool_trace": extract_tool_trace(state),
        "artifacts": artifacts,
        "latency_ms": latency_ms,
        "used_tokens": refreshed.used_tokens,
        "used_calls": refreshed.used_calls,
        "auto_approvals": auto_approvals,
        "workspace": str(materialized.workspace) if materialized.workspace else None,
        "before_files": materialized.before_files,
        "after_files": _snapshot_files(materialized.workspace),
    }
    score = score_observation(item, observation, materialized=materialized)
    return {
        "item_id": item["id"],
        "split": item["split"],
        "category": item["category"],
        "difficulty": item["difficulty"],
        "prompt": item["prompt"],
        "observation": observation,
        "score": score,
    }


def _nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _metric_slice(records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    if not count:
        return {"items": 0}
    successes = [record for record in records if record["score"]["task_success"]]
    selections = [record["score"]["tool_selection"]["passed"] for record in records]
    efficiencies = [float(record["score"]["step_efficiency"]) for record in records]
    latency = [float(record["observation"]["latency_ms"]) for record in records]
    tokens = [float(record["observation"]["used_tokens"]) for record in records]
    tool_calls = [
        call
        for record in records
        for call in record["observation"]["tool_trace"]
        if call["status"] != "interrupt"
    ]
    failed_calls = [call for call in tool_calls if call["status"] == "failed"]
    recovered_errors = 0
    for record in records:
        names = {call["name"] for call in record["observation"]["tool_trace"]}
        recovered_errors += sum(
            _tool_error_recovered(record["observation"]["tool_trace"], name) for name in names
        )
    return {
        "items": count,
        "task_success_rate": round(len(successes) / count, 6),
        "task_success_count": len(successes),
        "tool_selection_accuracy": round(sum(selections) / count, 6),
        "step_efficiency": {
            "definition": "actual_tool_calls / gold_optimal_tool_calls; 1.0 is optimal",
            "mean": round(sum(efficiencies) / count, 4),
            "p50": round(_nearest_rank(efficiencies, 0.5), 4),
            "p95": round(_nearest_rank(efficiencies, 0.95), 4),
            "within_max_rate": round(
                sum(record["score"]["within_tool_budget"] for record in records) / count,
                6,
            ),
        },
        "latency_ms": {
            "mean": round(sum(latency) / count),
            "p50": round(_nearest_rank(latency, 0.5)),
            "p95": round(_nearest_rank(latency, 0.95)),
        },
        "tokens": {
            "total": round(sum(tokens)),
            "mean": round(sum(tokens) / count),
            "p50": round(_nearest_rank(tokens, 0.5)),
            "p95": round(_nearest_rank(tokens, 0.95)),
            "mean_per_success": (
                round(
                    sum(record["observation"]["used_tokens"] for record in successes)
                    / len(successes)
                )
                if successes
                else None
            ),
        },
        "tool_error_rate": round(len(failed_calls) / len(tool_calls), 6) if tool_calls else 0.0,
        "recovered_tool_error_sequences": recovered_errors,
        # 阅读三指标按**微平均**合并，且只统计真的读了材料的样本；这一批里没有阅读
        # 样本时是 None，而不是一排 0——"没考"和"考砸了"必须长得不一样。
        "reading": merge_reading_scores(
            [
                record["score"]["reading"]
                for record in records
                if record["score"].get("reading") is not None
            ]
        ),
    }


def build_report(
    records: list[dict[str, Any]],
    *,
    suite: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    categories = sorted({record["category"] for record in records})
    splits = sorted({record["split"] for record in records})
    reproducibility = manifest.get("reproducibility")
    reproducibility = reproducibility if isinstance(reproducibility, dict) else {}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "cowork",
        "run_id": manifest.get("run_id"),
        "dataset": suite["name"],
        "label": manifest.get("label"),
        "git_sha": reproducibility.get("git_sha"),
        "config": manifest.get("config") or {},
        "config_hash": manifest.get("config_hash"),
        "reproducibility": reproducibility,
        "model_io": manifest.get("model_io") or {},
        "suite": suite["name"],
        "suite_version": suite["version"],
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest": manifest,
        "metrics": _metric_slice(records),
        "by_category": {
            category: _metric_slice(
                [record for record in records if record["category"] == category]
            )
            for category in categories
        },
        "by_split": {
            split: _metric_slice([record for record in records if record["split"] == split])
            for split in splits
        },
        "items": records,
    }


def _rate_cell(values: dict[str, Any]) -> str:
    """比率连同分母一起写出来。只写 100% 而不写 1/1，读报告的人会把一条样本的
    偶然当成结论。"""

    if not values["total"]:
        return "n/a (0)"
    return f"{values['rate']:.1%} ({values['passed']}/{values['total']})"


def render_markdown_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"# {report['suite']} Cowork Eval",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Items: {metrics['items']}",
        f"- Task success rate: {metrics['task_success_rate']:.1%}",
        f"- Tool selection accuracy: {metrics['tool_selection_accuracy']:.1%}",
        f"- Step efficiency mean: {metrics['step_efficiency']['mean']:.2f}",
        f"- P95 latency: {metrics['latency_ms']['p95'] / 1000:.2f}s",
        f"- Total / P95 tokens: {metrics['tokens']['total']} / {metrics['tokens']['p95']}",
        "",
        "| Category | N | Task success | Tool selection | Step efficiency | P95 latency | P95 tokens |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for category, values in report["by_category"].items():
        lines.append(
            f"| {category} | {values['items']} | {values['task_success_rate']:.1%} | "
            f"{values['tool_selection_accuracy']:.1%} | {values['step_efficiency']['mean']:.2f} | "
            f"{values['latency_ms']['p95'] / 1000:.2f}s | {values['tokens']['p95']} |"
        )
    reading = metrics.get("reading")
    if reading is not None:
        lines.extend(
            [
                "",
                "## Reading (docs/04 §5)",
                "",
                f"- Items with reading tools: {reading['items']}",
                f"- read_before_claim: {_rate_cell(reading['read_before_claim'])}",
                f"- quote_verifiability: {_rate_cell(reading['quote_verifiability'])}",
                f"- locator_accuracy: {_rate_cell(reading['locator_accuracy'])}",
            ]
        )
        # 分语言报告是这条指标的定义的一部分：用中文问英文原文时模型给的"引文"往往是
        # 它自己的译文，永远逐字对不上；把两种语言混在一起报，会把跨语言的正常损耗
        # 说成引文能力缺陷。
        for script, values in (reading["quote_verifiability_by_script"] or {}).items():
            lines.append(f"  - quote script `{script}`: {_rate_cell(values)}")
        cross = reading["quote_verifiability_cross_language"]
        if cross["total"]:
            lines.append(f"  - cross-language quotes: {_rate_cell(cross)}")
        if reading["unscorable_materials"]:
            lines.append(f"  - unscorable materials: {reading['unscorable_materials']}")

    lines.extend(
        [
            "",
            "## Failed items",
            "",
        ]
    )
    failures = [item for item in report["items"] if not item["score"]["task_success"]]
    if not failures:
        lines.append("None.")
    for item in failures:
        failed_assertions = [
            assertion["type"]
            for assertion in item["score"]["assertions"]
            if not assertion["passed"]
        ]
        lines.append(
            f"- `{item['item_id']}` status={item['observation']['status']} "
            f"assertions={failed_assertions} tools={item['score']['tool_selection']['actual_sequence']}"
        )
    return "\n".join(lines) + "\n"


def _runner_error_record(item: dict[str, Any], error: Exception) -> dict[str, Any]:
    message = f"{type(error).__name__}: {error}"
    return {
        "item_id": item["id"],
        "split": item["split"],
        "category": item["category"],
        "difficulty": item["difficulty"],
        "prompt": item["prompt"],
        "observation": {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "item_id": item["id"],
            "run_id": None,
            "conversation_id": None,
            "status": "runner_error",
            "response": "",
            "error": message,
            "interrupt": None,
            "tool_trace": [],
            "artifacts": [],
            "latency_ms": 0,
            "used_tokens": 0,
            "used_calls": 0,
            "auto_approvals": [],
            "workspace": None,
            "before_files": {},
            "after_files": {},
        },
        "score": {
            "task_success": False,
            "status_match": False,
            "assertions_pass": False,
            "guardrail_pass": True,
            "assertions": [{"type": "runner_error", "passed": False, "detail": message}],
            "tool_selection": {
                "passed": False,
                "required_recall": 0.0,
                "missing": {},
                "forbidden_calls": [],
                "order_match": False,
                "actual_sequence": [],
            },
            "step_efficiency": 0.0,
            "within_tool_budget": True,
            "actual_tool_calls": 0,
            "optimal_tool_calls": item["gold"]["optimal_tool_calls"],
        },
    }


async def run_suite(
    *,
    suite_path: Path,
    items: list[dict[str, Any]],
    package: Path,
    label: str,
    authorization_note: str,
    test_access_note: str | None,
    settings: Settings,
    gateway: ModelGateway | None = None,
    replay_cassette: Path | None = None,
    db_sessions: SessionFactory = session_factory,
) -> tuple[Path, Path, Path]:
    suite = load_suite(suite_path)
    if replay_cassette is not None and gateway is not None:
        raise CoworkRunnerError("replay 模式不能注入真实 ModelGateway")
    replay_shell_items = [
        str(item["id"])
        for item in items
        if replay_cassette is not None and "run_shell" in item["gold"]["required_tools"]
    ]
    if replay_shell_items:
        raise CoworkRunnerError(
            "模型 cassette 不能证明 Shell 无副作用；当前环境没有无网络沙箱，"
            f"拒绝回放含 run_shell 的 case: {replay_shell_items}"
        )
    package.mkdir(parents=True, exist_ok=False)
    workspace_root = package / "cases"
    workspace_root.mkdir()
    # 隔离运行记录：把控制面指到本次跑批自己的包目录里。原来这里钉的是
    # `cowork_store_backend="postgres"`——那个开关随 ADR-0012 一起没了，而它承担的
    # "别写进用户真实的 ~/.workpilot" 这件事仍然必须成立：评测会创建几十个 run、
    # 会话与授权记录，混进日常使用的库里既污染成本口径也让 run 列表没法看。
    # 落在包目录里还有一个额外好处——report.json 和产生它的那份 SQLite 是同一份快照。
    store_root = package / "store"
    effective_settings = settings.model_copy(
        update={
            "cowork_data_path": store_root,
            "memory_extraction_enabled": False,
            "skill_distillation_enabled": False,
            "cowork_shell_allowlist": [],
            "run_heartbeat_s": 60.0,
            "model_timeout_s": settings.cowork_model_timeout_s,
        }
    )
    suite_sha = _sha256_bytes(suite_path.read_bytes())
    item_ids = [str(item["id"]) for item in items]
    owns_gateway = gateway is None and replay_cassette is None
    raw_gateway: ModelGateway | None = None
    recorder: RecordingModelGateway | None = None
    replayer: ReplayingModelGateway | None = None
    cassette_output = package / "model-cassette.json"
    if replay_cassette is not None:
        replayer = ReplayingModelGateway.load(replay_cassette)
        if replayer.metadata.get("suite_sha256") != suite_sha:
            raise CoworkRunnerError("cassette 的 suite_sha256 与当前 suite 不一致")
        if replayer.metadata.get("item_ids") != item_ids:
            raise CoworkRunnerError("cassette 的 item_ids/顺序与当前选择不一致")
        recorded_shell_cases = replayer.cases_using_tool("run_shell")
        if recorded_shell_cases:
            raise CoworkRunnerError(
                "模型 cassette 包含 run_shell 调用；当前环境没有无网络 sandbox，"
                f"拒绝执行: {list(recorded_shell_cases)}"
            )
        model_gateway: ModelGatewayLike = replayer
        model_io = {
            "schema": MODEL_CASSETTE_SCHEMA,
            "mode": "cassette_replay",
            "source": str(replay_cassette.resolve()),
            "sha256": replayer.source_sha256,
            "real_model_dispatches": 0,
            "latency_source": "replay_wall_clock",
        }
    else:
        raw_gateway = gateway or build_model_gateway(effective_settings, mode="evaluation")
        recorder = RecordingModelGateway(
            raw_gateway,
            output=cassette_output,
            metadata={
                "suite": suite["name"],
                "suite_sha256": suite_sha,
                "item_ids": item_ids,
                "effect_policy": "fixture adapters; filesystem limited to case workspace",
            },
        )
        model_gateway = recorder
        model_io = {
            "schema": MODEL_CASSETTE_SCHEMA,
            "mode": "record",
            "path": cassette_output.name,
            "latency_source": "live_wall_clock",
        }
    repo_root = Path(__file__).resolve().parents[1]
    git_sha, git_dirty = _git_state(repo_root)
    review = suite_review(suite)
    started_at = datetime.now(UTC)
    fixture_policy = {
        "network": "suite-local deterministic adapter; no public network",
        "rag": "suite-local EvidenceBundle adapter; no production corpus",
        "work_mode": "reading category uses reading mode with its sole fixture material; others use office",
        "browser_auto_approval": "only fixture browser_open needed for expected done",
        "cassette_replay_shell": "blocked without a no-network sandbox",
    }
    config = {
        "suite_sha256": suite_sha,
        "suite_version": suite["version"],
        "suite_origin": suite["origin"],
        "suite_review_status": review["status"],
        "suite_reviewer": review["reviewer"],
        "suite_reviewed_at": review["reviewed_at"],
        "item_ids": item_ids,
        "splits": dict(Counter(item["split"] for item in items)),
        "model": {
            "provider": model_gateway.chat_provider,
            "model": model_gateway.chat_model,
            "endpoint": (
                None if replay_cassette is not None else effective_settings.tier_main_base_url
            ),
            "mode": "cassette_replay" if replay_cassette is not None else "evaluation",
        },
        "model_io": {
            "schema": MODEL_CASSETTE_SCHEMA,
            "mode": model_io["mode"],
            "latency_source": model_io["latency_source"],
        },
        "budgets": {
            "tokens": effective_settings.run_budget_tokens,
            "calls": effective_settings.run_budget_calls,
            "wall_ms": effective_settings.run_budget_wall_ms,
        },
        "runtime": {
            "memory_extraction_enabled": False,
            "skill_distillation_enabled": False,
            "fallback_enabled": False,
        },
        "fixture_policy": fixture_policy,
        "implementation_fingerprint": _file_set_fingerprint(
            repo_root, _COWORK_IMPLEMENTATION_FILES
        ),
        "scorer_fingerprint": _file_set_fingerprint(repo_root, _COWORK_SCORER_FILES),
    }
    manifest = {
        "schema_version": "cowork-eval-manifest.v1",
        "run_id": str(uuid4()),
        "label": label,
        "started_at": started_at.isoformat(),
        "suite_path": str(suite_path.resolve()),
        "suite_sha256": suite_sha,
        "suite_origin": suite["origin"],
        "suite_review_status": review["status"],
        "suite_reviewer": review["reviewer"],
        "suite_reviewed_at": review["reviewed_at"],
        "item_ids": item_ids,
        "splits": dict(Counter(item["split"] for item in items)),
        "model": config["model"],
        "model_send_authorization": {
            "approved": replay_cassette is None,
            "note_fingerprint": (
                _sha256_bytes(authorization_note.strip().encode())
                if replay_cassette is None
                else None
            ),
            "data_scope": "synthetic Cowork prompts and deterministic fixture content",
        },
        "model_io": model_io,
        "test_access": {
            "included": any(item["split"] == "test" for item in items),
            "note": test_access_note,
        },
        "budgets": config["budgets"],
        "fixture_policy": fixture_policy,
        "config": config,
        "config_hash": _json_hash(config),
        "reproducibility": {
            "git_sha": git_sha,
            "git_dirty": git_dirty,
            "implementation_fingerprint": config["implementation_fingerprint"],
            "scorer_fingerprint": config["scorer_fingerprint"],
        },
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    records: list[dict[str, Any]] = []
    # 全局 store 单例：已经初始化过就先关掉，否则 `initialize_...` 会直接返回旧的那份，
    # 于是 cowork_data_path 指到哪里都没用，运行记录照样落进上一个根目录。
    await close_local_cowork_stores()
    await initialize_local_cowork_stores(effective_settings)
    cassette_complete = False
    try:
        for index, item in enumerate(items, start=1):
            print(f"[{index}/{len(items)}] {item['id']} {item['category']}", flush=True)
            case_root = workspace_root / str(item["id"])
            fixtures = _merge_fixtures(suite, item)
            workspace_hint = (
                None if fixtures.get("workspace_roots") == [] else case_root / "workspace"
            )
            cassette_gateway = recorder or replayer
            assert cassette_gateway is not None
            cassette_gateway.begin_case(
                str(item["id"]), case_root=case_root, workspace=workspace_hint
            )
            try:
                try:
                    record = await run_case(
                        suite,
                        item,
                        case_root=case_root,
                        gateway=model_gateway,
                        settings=effective_settings,
                        db_sessions=db_sessions,
                    )
                except ModelCassetteError:
                    # 严格 replay 失配不是一个普通模型坏样本。把它吞成 runner_error 会让
                    # 剩余 case 继续跑，最终报告看起来只是成功率下降，而不是回放本身无效。
                    raise
                except Exception as error:
                    record = _runner_error_record(item, error)
            finally:
                cassette_gateway.end_case()
            records.append(record)
            print(
                f"  status={record['observation']['status']} "
                f"success={record['score']['task_success']} "
                f"tokens={record['observation']['used_tokens']} "
                f"latency_ms={record['observation']['latency_ms']}",
                flush=True,
            )
            # 每题落盘，进程中断后也保留已完成 observation。
            (package / "observations.jsonl").write_text(
                "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in records),
                encoding="utf-8",
            )
        if replayer is not None:
            replayer.assert_complete()
        cassette_complete = True
    finally:
        if recorder is not None:
            recorder.finalize(complete=cassette_complete)
        if owns_gateway and raw_gateway is not None:
            await raw_gateway.aclose()
        await close_local_cowork_stores()

    if recorder is not None:
        model_io["sha256"] = cassette_sha256(cassette_output)
        model_io["recorded_model_interactions"] = recorder.interaction_count
    manifest["model_io"] = model_io
    manifest["config_hash"] = _json_hash(config)

    finished_at = datetime.now(UTC)
    manifest["finished_at"] = finished_at.isoformat()
    manifest["duration_ms"] = round((finished_at - started_at).total_seconds() * 1000, 3)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = build_report(records, suite=suite, manifest=manifest)
    report_path = package / "report.json"
    markdown_path = package / "report.md"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return manifest_path, report_path, markdown_path


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    if not normalized:
        raise CoworkRunnerError("label 不能为空")
    return normalized[:80]


def _select_items(
    suite: dict[str, Any],
    *,
    split: str,
    item_ids: list[str],
) -> list[dict[str, Any]]:
    selected = [
        item
        for item in suite["items"]
        if (split == "all" or item["split"] == split) and (not item_ids or item["id"] in item_ids)
    ]
    missing = set(item_ids) - {item["id"] for item in selected}
    if missing:
        raise CoworkRunnerError(f"item 不存在或不属于所选 split: {sorted(missing)}")
    if not selected:
        raise CoworkRunnerError("没有选中任务")
    return selected


def rescore_report(
    *,
    source_report: Path,
    suite_path: Path,
    package: Path,
    label: str,
    test_access_note: str | None,
) -> tuple[Path, Path, Path]:
    started_at = datetime.now(UTC)
    repo_root = Path(__file__).resolve().parents[1]
    git_sha, git_dirty = _git_state(repo_root)
    suite = load_suite(suite_path)
    review = suite_review(suite)
    source = json.loads(source_report.read_text(encoding="utf-8"))
    if source.get("suite") != suite["name"] or not isinstance(source.get("items"), list):
        raise CoworkRunnerError("source report 与 suite 不匹配")
    item_by_id = {item["id"]: item for item in suite["items"]}
    records: list[dict[str, Any]] = []
    for old_record in source["items"]:
        item_id = str(old_record["item_id"])
        item = item_by_id.get(item_id)
        if item is None:
            raise CoworkRunnerError(f"source report 含未知 item: {item_id}")
        observation = old_record["observation"]
        workspace_raw = observation.get("workspace")
        workspace = Path(workspace_raw) if isinstance(workspace_raw, str) else None
        fixtures = _merge_fixtures(suite, item)
        document_ids = {
            str(document["id"]): str(
                uuid5(
                    NAMESPACE_URL,
                    f"workpilot-cowork-eval:document:{document['id']}",
                )
            )
            for document in fixtures["knowledge_documents"]
        }
        materialized = MaterializedCase(
            workspace=workspace,
            fixtures=fixtures,
            before_files=dict(observation.get("before_files") or {}),
            document_ids=document_ids,
        )
        records.append(
            {
                "item_id": item_id,
                "split": item["split"],
                "category": item["category"],
                "difficulty": item["difficulty"],
                "prompt": item["prompt"],
                "observation": observation,
                "score": score_observation(item, observation, materialized=materialized),
            }
        )
    package.mkdir(parents=True, exist_ok=False)
    suite_sha = _sha256_bytes(suite_path.read_bytes())
    source_manifest = source.get("manifest")
    source_manifest = source_manifest if isinstance(source_manifest, dict) else {}
    config = {
        "suite_sha256": suite_sha,
        "suite_version": suite["version"],
        "suite_origin": suite["origin"],
        "suite_review_status": review["status"],
        "suite_reviewer": review["reviewer"],
        "suite_reviewed_at": review["reviewed_at"],
        "item_ids": [record["item_id"] for record in records],
        "splits": dict(Counter(record["split"] for record in records)),
        "mode": "offline_rescore_no_model_calls",
        "source_config_hash": source.get("config_hash") or source_manifest.get("config_hash"),
        "source_model": source_manifest.get("model"),
        "scorer_fingerprint": _file_set_fingerprint(repo_root, _COWORK_SCORER_FILES),
    }
    manifest = {
        "schema_version": "cowork-eval-rescore-manifest.v1",
        "run_id": str(uuid4()),
        "label": label,
        "started_at": started_at.isoformat(),
        "source_report": str(source_report.resolve()),
        "source_report_sha256": _sha256_bytes(source_report.read_bytes()),
        "suite_path": str(suite_path.resolve()),
        "suite_sha256": suite_sha,
        "suite_origin": suite["origin"],
        "suite_review_status": review["status"],
        "suite_reviewer": review["reviewer"],
        "suite_reviewed_at": review["reviewed_at"],
        "item_ids": [record["item_id"] for record in records],
        "splits": dict(Counter(record["split"] for record in records)),
        "test_access": {
            "included": any(record["split"] == "test" for record in records),
            "note": test_access_note,
        },
        "model": source_manifest.get("model"),
        "mode": "offline_rescore_no_model_calls",
        "config": config,
        "config_hash": _json_hash(config),
        "reproducibility": {
            "git_sha": git_sha,
            "git_dirty": git_dirty,
            "scorer_fingerprint": config["scorer_fingerprint"],
        },
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (package / "observations.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    finished_at = datetime.now(UTC)
    manifest["finished_at"] = finished_at.isoformat()
    manifest["duration_ms"] = round((finished_at - started_at).total_seconds() * 1000, 3)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = build_report(records, suite=suite, manifest=manifest)
    report_path = package / "report.json"
    markdown_path = package / "report.md"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return manifest_path, report_path, markdown_path


async def _async_main(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    suite = load_suite(args.suite)
    if suite["origin"] == "synthetic" and not args.allow_synthetic:
        raise CoworkRunnerError("套件尚未完成人工复核；工程跑批必须显式 --allow-synthetic")
    source_report = args.rescore_report
    source_items = (
        json.loads(source_report.read_text(encoding="utf-8")).get("items", [])
        if source_report is not None
        else None
    )
    items = (
        _select_items(suite, split=args.split, item_ids=args.item_id)
        if source_items is None
        else source_items
    )
    includes_test = any(item["split"] == "test" for item in items)
    if includes_test and (not args.include_test or not args.test_access_note.strip()):
        raise CoworkRunnerError("访问冻结 test 必须同时提供 --include-test 和 --test-access-note")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    package = (args.output_root / f"{timestamp}-{_slug(args.label)}").resolve()
    if source_report is not None:
        return rescore_report(
            source_report=source_report,
            suite_path=args.suite,
            package=package,
            label=args.label,
            test_access_note=args.test_access_note.strip() or None,
        )
    if args.replay_cassette is not None:
        if args.allow_model_send or args.authorization_note.strip():
            raise CoworkRunnerError(
                "--replay-cassette 是零模型发送模式，不能同时传 --allow-model-send/"
                "--authorization-note"
            )
    elif not args.allow_model_send or not args.authorization_note.strip():
        raise CoworkRunnerError("调用模型前必须 --allow-model-send 并记录 --authorization-note")
    settings = _evaluation_settings(
        Settings(),
        budget_tokens=args.budget_tokens,
        budget_calls=args.budget_calls,
        budget_wall_ms=args.budget_wall_ms,
    )
    try:
        return await run_suite(
            suite_path=args.suite,
            items=items,
            package=package,
            label=args.label,
            authorization_note=args.authorization_note,
            test_access_note=args.test_access_note.strip() or None,
            settings=settings,
            replay_cassette=args.replay_cassette,
        )
    finally:
        await close_database()


def _evaluation_settings(
    settings: Settings,
    *,
    budget_tokens: int | None,
    budget_calls: int | None,
    budget_wall_ms: int | None,
) -> Settings:
    """构造评测设置；token 只计量，不作为任务成败熔断。"""

    if budget_tokens not in {None, 0}:
        raise CoworkRunnerError(
            "评测已禁用 token 熔断；请省略 --budget-tokens（或显式传 0）"
        )
    return settings.model_copy(
        update={
            "run_budget_tokens": 0,
            "run_budget_calls": (
                settings.run_budget_calls if budget_calls is None else budget_calls
            ),
            "run_budget_wall_ms": (
                settings.run_budget_wall_ms if budget_wall_ms is None else budget_wall_ms
            ),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Cowork 端到端 50 条任务集")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--label", required=True)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--item-id", action="append", default=[])
    parser.add_argument("--include-test", action="store_true")
    parser.add_argument("--test-access-note", default="")
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--allow-model-send", action="store_true")
    parser.add_argument("--authorization-note", default="")
    offline_mode = parser.add_mutually_exclusive_group()
    offline_mode.add_argument(
        "--rescore-report",
        type=Path,
        help="不调用模型，使用既有 report 的 observation 按当前 scorer 重新计分",
    )
    offline_mode.add_argument(
        "--replay-cassette",
        type=Path,
        help="不构造真实 provider，用已录制模型交互在隔离 fixture 中重跑 Cowork graph",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("eval/outputs/cowork-core"),
    )
    parser.add_argument(
        "--budget-tokens",
        type=int,
        help="兼容旧命令，仅允许 0；评测 token 只计量、不熔断",
    )
    parser.add_argument("--budget-calls", type=int)
    parser.add_argument("--budget-wall-ms", type=int)
    args = parser.parse_args()
    paths = asyncio.run(_async_main(args))
    print(
        json.dumps(
            {
                "manifest": str(paths[0]),
                "report": str(paths[1]),
                "markdown": str(paths[2]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
