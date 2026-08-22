import ast
import json
from pathlib import Path
from typing import Any, TypedDict

import pytest
from uuid6 import uuid7

from app.agent_core.compaction import (
    CompactionPrompts,
    build_outbound_messages,
    default_compaction_state,
)
from app.agent_core.loop import run_tool_loop
from app.core.config import Settings
from app.cowork.rag_tools import register_rag_tools
from app.cowork.tools import CoworkToolContext, CoworkToolRegistry
from app.knowledge_contracts import EvidenceBundle, EvidenceSegment, RagSearchRequest


class _FakeRag:
    async def search(self, gateway: object, request: RagSearchRequest) -> EvidenceBundle:
        del gateway
        return EvidenceBundle(
            evidence=(
                EvidenceSegment(
                    citation_id="S1",
                    block_id=uuid7(),
                    version_id=uuid7(),
                    document_id=uuid7(),
                    title="Architecture",
                    source_uri="notes/architecture.md",
                    quote=f"evidence for {request.query}",
                    char_start=10,
                    char_end=30,
                    heading_path=["RAG"],
                    locations=[{"page": 2}],
                ),
            ),
            retrieved_chunks=1,
            backend="fake",
        )


@pytest.mark.asyncio
async def test_search_knowledge_returns_only_evidence_bundle_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CoworkToolRegistry()
    register_rag_tools(registry, _FakeRag())
    assert registry.get("search_knowledge").capability == "knowledge.read"
    authorized: list[str] = []

    async def authorize(_session: object, **kwargs: Any) -> None:
        authorized.append(str(kwargs["capability"]))

    monkeypatch.setattr("app.cowork.tools.authorize_capability", authorize)
    run_id = uuid7()
    result = await registry.execute(
        "search_knowledge",
        {"query": "SearchPipeline", "top_k": 3},
        context=CoworkToolContext(
            session=object(),  # type: ignore[arg-type]
            gateway=object(),  # type: ignore[arg-type]
            settings=Settings(),
            conversation_id=uuid7(),
            run_id=run_id,
            worker_id="test-worker",
            plan_step_id=uuid7(),
            tool_call_id="call-1",
        ),
    )

    encoded = json.dumps(result.output)
    assert result.output["backend"] == "fake"
    assert result.output["evidence"][0]["citation_id"] == "S1"
    assert "chunk_id" not in encoded
    assert "dense_score" not in encoded
    assert "fusion_score" not in encoded
    assert "_sa_instance_state" not in encoded
    assert authorized == ["knowledge.read"]


class _LoopState(TypedDict):
    active: bool
    pending: bool
    count: int


@pytest.mark.asyncio
async def test_agent_core_loop_is_product_neutral() -> None:
    async def decide(state: _LoopState) -> _LoopState:
        if state["count"] == 2:
            return {**state, "active": False}
        return {**state, "pending": True}

    async def execute(state: _LoopState) -> _LoopState:
        return {**state, "pending": False, "count": state["count"] + 1}

    initial: _LoopState = {"active": True, "pending": False, "count": 0}
    result = await run_tool_loop(
        initial,
        state_schema=_LoopState,
        decide=decide,
        execute_tools=execute,
        is_active=lambda state: state["active"],
        has_pending_tools=lambda state: state["pending"],
        recursion_limit=10,
    )

    assert result == {"active": False, "pending": False, "count": 2}


def test_workpilot_ai_package_carries_no_application_imports() -> None:
    """最底层「只懂模型」：包里不许出现 app.* 与持久化/Web 依赖。

    这条与 pyproject 的 importlinter 契约 1/2 重复是**故意**的：import-linter 跑在
    单独的 CI 步骤上，而 pytest 是每个 PR 都会跑的那一层，两边都挡一次。
    真正的全量层次契约见 `[tool.importlinter]`，本用例只守最底层这一条红线。
    """

    package_root = Path(__file__).parents[1] / "packages" / "workpilot-ai" / "src" / "workpilot_ai"
    forbidden = ("app", "sqlalchemy", "fastapi", "pydantic_settings", "alembic")
    violations: list[tuple[str, str]] = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if module.split(".")[0] in forbidden:
                violations.append((str(path.relative_to(package_root)), module))
    assert violations == []


def test_store_adapter_does_not_import_agent_or_service_implementations() -> None:
    app_root = Path(__file__).parents[1] / "app"
    violations: list[tuple[str, str]] = []
    for path in (app_root / "cowork_store").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module.startswith(("app.rag", "app.cowork.")):
                violations.append((str(path.relative_to(app_root)), module))
    assert violations == []


def test_compaction_is_product_neutral() -> None:
    """压缩属于框架层：换一套产品措辞就能用，不含任何 Cowork 语义。

    这条是 ADR-0011 Step 2 的回归网——`app/agent_core/compaction.py` 里
    一旦重新硬编码 Cowork 的 prompt 或 task_type，这里就会挂。
    """

    prompts = CompactionPrompts(
        system_prompt="把历史压成 {\"summary\": \"...\"}",
        outbound_prefix="<digest_history>",
        outbound_suffix="</digest_history>",
        summary_task_type="digest_compaction",
        decision_task_type="digest_decision",
    )
    compaction = default_compaction_state()
    compaction["summary"] = "早先做了三件事"
    compaction["summary_upto"] = 1

    messages = build_outbound_messages(
        [
            {"role": "user", "content": "旧目标"},
            {"role": "user", "content": "当前目标"},
        ],
        compaction,
        system_prompt="你是 digest agent",
        prompts=prompts,
    )

    rendered = "\n".join(item.content for item in messages)
    assert "<digest_history>" in rendered
    assert "早先做了三件事" in rendered
    assert "当前目标" in rendered
    assert "cowork" not in rendered.lower()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有 docstring 表达式的 id，扫描时跳过——注释里指路产品层是允许的。"""

    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            marked.add(id(body[0].value))
    return marked


def test_agent_core_carries_no_product_vocabulary() -> None:
    """框架层不认识"综述"和"Cowork"：不出现产品命名的标识符或字符串字面量。

    ADR-0011 Step 2 的回归网。压缩的 prompt、综述的 card/group 都曾经住在这里，
    搬走之后要挡住它们回来。docstring 里指路产品层不算违规。

    唯一豁免 `WorkflowType`（连同它的 Literal 成员）：它是 `agent_runs.workflow_type`
    这一列的取值域，对应数据库 CHECK 约束。它现在放在框架层是因为 `runstore` 与
    `cowork_store` 都要用，而 `runstore → cowork_store` 已经存在——挪到任何一边都会成环。
    Step 3 拆产品包时再决定它下沉到哪里，或者放宽成 str。
    """

    core_root = Path(__file__).parents[1] / "app" / "agent_core"
    markers = ("cowork", "review")
    allowed = {"WorkflowType"}
    violations: list[tuple[str, str]] = []

    for path in sorted(core_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        # 豁免类型自身的 Literal 成员，否则等于只豁免了名字、没豁免取值。
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(isinstance(t, ast.Name) and t.id in allowed for t in targets):
                skip.update(
                    id(child)
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant)
                )
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                name = node.name
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in skip:
                    continue
                name = node.value
            if isinstance(node, ast.Name) and node.id in allowed:
                continue
            if name is None or name in allowed:
                continue
            lowered = name.lower()
            if any(marker in lowered for marker in markers):
                violations.append((str(path.relative_to(core_root)), name))

    assert violations == []
