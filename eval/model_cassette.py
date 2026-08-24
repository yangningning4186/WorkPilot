"""模型交互 cassette：录制一次真实调用，在隔离场景中零网络重放。

这层包在原始 ``ModelGateway`` 外、``BudgetedGateway`` 内。因而重放仍经过产品的
token/call 预算逻辑，但不会持有任何真实 provider。每次请求使用规范 JSON 指纹严格匹配；
未知请求、缺失记录、未消费记录、篡改和 schema 漂移全部 fail-closed。

cassette 含完整 prompt、工具 schema 与模型输出，可能包含隐私，只能作为本地运行制品。
写盘权限固定为 0600；可提交 Git 的 baseline 仍由 ``eval.regression`` 生成，那里只保留
指标和哈希。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from workpilot_ai.gateway import PromptBudget
from workpilot_ai.types import (
    CompletionChunk,
    CompletionResult,
    EmbeddingResult,
    Message,
    MessageAttachment,
    ToolCall,
    ToolDefinition,
    Usage,
)

MODEL_CASSETTE_SCHEMA = "workpilot.model-cassette"
MODEL_CASSETTE_VERSION = 1
CANONICALIZATION = "workpilot-json-sort-keys-utf8-v1"
NORMALIZER_VERSION = "workpilot-cowork-volatile-v1"
INTEGRITY_ALGORITHM = "sha256"

_ENVIRONMENT_BLOCK = re.compile(r"<environment>\n.*?\n</environment>", re.DOTALL)
_DATE_LINE = re.compile(r"(?m)^当前日期：[^\n]+$")
_TIME_LINE = re.compile(r"(?m)^当前时间：[^\n]+$")
_OPERATIONS = {
    "complete",
    "complete_with_tools",
    "stream_with_tools",
    "stream",
    "embed",
}


class ModelCassetteError(RuntimeError):
    """Cassette 不完整、不匹配或已被篡改。"""


class RecordedModelError(ModelCassetteError):
    """真实录制中该模型调用失败；重放不会改为访问网络。"""


class ModelGatewayLike(Protocol):
    chat_provider: str
    chat_model: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget: ...

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult: ...

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult: ...

    def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]: ...

    def stream(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]: ...

    async def embed(self, texts: list[str], *, task_type: str = "embedding") -> EmbeddingResult: ...


@dataclass(frozen=True)
class _CaseContext:
    case_id: str
    case_root: str
    workspace: str | None

    def normalize_text(self, value: str) -> str:
        normalized = value
        replacements = [
            (self.workspace, "${WORKSPACE}"),
            (self.case_root, "${CASE_ROOT}"),
        ]
        for source, target in sorted(
            ((source, target) for source, target in replacements if source),
            key=lambda item: len(cast("str", item[0])),
            reverse=True,
        ):
            normalized = normalized.replace(cast("str", source), target)

        # 日期和分钟是 run 起始快照，不应让同一个 synthetic scenario 次日失配；
        # 环境块的其余文字和 OS 仍参与指纹，prompt 或执行平台漂移不会被掩盖。
        def normalize_environment(match: re.Match[str]) -> str:
            block = _DATE_LINE.sub("当前日期：${RUN_DATE}", match.group(0))
            return _TIME_LINE.sub("当前时间：${RUN_TIME}", block)

        normalized = _ENVIRONMENT_BLOCK.sub(normalize_environment, normalized)
        return _normalize_known_runtime_json(normalized)

    def restore_text(self, value: str) -> str:
        restored = value.replace("${CASE_ROOT}", self.case_root)
        if self.workspace is not None:
            restored = restored.replace("${WORKSPACE}", self.workspace)
        return restored

    def normalize(self, value: object) -> object:
        return _map_strings(value, self.normalize_text)

    def restore(self, value: object) -> object:
        return _map_strings(value, self.restore_text)


def _map_strings(value: object, transform: Any) -> object:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in value]
    if isinstance(value, tuple):
        return [_map_strings(item, transform) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _map_strings(item, transform) for key, item in value.items()}
    return value


def _normalize_known_runtime_json(value: str) -> str:
    """只归一化产品明确标注为运行时身份的 JSON leaf。

    不能全局抹掉 UUID：用户提供的账号、文档或订单 ID 是语义输入。workspace root ID
    则由本地 store 每次创建，模型只把它当该次会话的句柄，按稳定列表位置绑定即可。
    """

    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    changed = False

    def visit(node: object) -> None:
        nonlocal changed
        if isinstance(node, dict):
            if isinstance(node.get("modified_at_ns"), int):
                node["modified_at_ns"] = "${FILE_MTIME_NS}"
                changed = True
            roots = node.get("roots")
            if isinstance(roots, list):
                for index, root in enumerate(roots):
                    if isinstance(root, dict) and isinstance(root.get("id"), str):
                        root["id"] = f"${{WORKSPACE_ROOT_ID_{index}}}"
                        changed = True
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    if not changed:
        return value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _first_json_difference(expected: object, actual: object, path: str = "$") -> str:
    """给严格失配一个可操作的首差异，而不是只报两个不可读的 hash。"""

    if type(expected) is not type(actual):
        return f"{path}: 类型 {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        expected_keys = set(expected)
        actual_keys = set(actual)
        if expected_keys != actual_keys:
            return (
                f"{path}: keys 仅录制={sorted(expected_keys - actual_keys)} "
                f"仅当前={sorted(actual_keys - expected_keys)}"
            )
        for key in sorted(expected_keys, key=str):
            difference = _first_json_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return ""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{path}: 长度 {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_json_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return ""
    if expected != actual:
        left = repr(expected)
        right = repr(actual)
        return f"{path}: {left[:240]} != {right[:240]}"
    return ""


def canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ModelCassetteError(f"内容不能编码为规范 JSON: {error}") from error
    return encoded.encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ModelCassetteError(f"cassette JSON 含重复键: {key}")
        value[key] = item
    return value


def content_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(payload))
    sealed.pop("integrity", None)
    sealed["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "value": content_sha256(sealed),
    }
    return sealed


def _tool_call(value: ToolCall) -> dict[str, Any]:
    return {"id": value.id, "name": value.name, "arguments": value.arguments}


def _attachment(value: MessageAttachment) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "filename": value.filename,
        "media_type": value.media_type,
        "path": value.path,
        "size_bytes": value.size_bytes,
        "sha256": value.sha256,
        "extracted_text": value.extracted_text,
    }


def _message(value: Message) -> dict[str, Any]:
    return {
        "role": value.role,
        "content": value.content,
        "tool_calls": [_tool_call(call) for call in value.tool_calls],
        "tool_call_id": value.tool_call_id,
        "attachments": [_attachment(item) for item in value.attachments],
    }


def _tool(value: ToolDefinition) -> dict[str, Any]:
    return {
        "name": value.name,
        "description": value.description,
        "parameters": value.parameters,
        "strict": value.strict,
    }


def _usage(value: Usage) -> dict[str, int]:
    return asdict(value)


def _completion(value: CompletionResult) -> dict[str, Any]:
    return {
        "text": value.text,
        "model": value.model,
        "provider": value.provider,
        "usage": _usage(value.usage),
        "tool_calls": [_tool_call(call) for call in value.tool_calls],
    }


def _chunk(value: CompletionChunk) -> dict[str, Any]:
    return {
        "text_delta": value.text_delta,
        "reasoning_delta": value.reasoning_delta,
        "result": _completion(value.result) if value.result is not None else None,
    }


def _embedding(value: EmbeddingResult) -> dict[str, Any]:
    return {
        "embeddings": value.embeddings,
        "model": value.model,
        "provider": value.provider,
        "usage": _usage(value.usage),
    }


def _request(
    operation: str,
    *,
    messages: list[Message] | None = None,
    tools: list[ToolDefinition] | None = None,
    texts: list[str] | None = None,
    task_type: str,
    max_tokens: int | None = None,
    temperature: float | None = None,
    parallel_tool_calls: bool | None = None,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "messages": None if messages is None else [_message(item) for item in messages],
        "tools": None if tools is None else [_tool(item) for item in tools],
        "texts": texts,
        "options": {
            "task_type": task_type,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "parallel_tool_calls": parallel_tool_calls,
        },
    }


def _usage_from(value: Mapping[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        prompt_cache_read_tokens=int(value.get("prompt_cache_read_tokens", 0)),
        prompt_cache_write_tokens=int(value.get("prompt_cache_write_tokens", 0)),
    )


def _tool_call_from(value: Mapping[str, Any]) -> ToolCall:
    return ToolCall(
        id=str(value.get("id") or ""),
        name=str(value.get("name") or ""),
        arguments=str(value.get("arguments") or ""),
    )


def _completion_from(value: object, context: _CaseContext) -> CompletionResult:
    restored = context.restore(value)
    if not isinstance(restored, Mapping):
        raise ModelCassetteError("completion response 必须是对象")
    usage = restored.get("usage")
    calls = restored.get("tool_calls")
    if not isinstance(usage, Mapping) or not isinstance(calls, list):
        raise ModelCassetteError("completion response 缺少 usage/tool_calls")
    return CompletionResult(
        text=str(restored.get("text") or ""),
        model=str(restored.get("model") or ""),
        provider=str(restored.get("provider") or ""),
        usage=_usage_from(usage),
        tool_calls=tuple(_tool_call_from(item) for item in calls if isinstance(item, Mapping)),
    )


def _chunk_from(value: object, context: _CaseContext) -> CompletionChunk:
    restored = context.restore(value)
    if not isinstance(restored, Mapping):
        raise ModelCassetteError("stream chunk 必须是对象")
    result = restored.get("result")
    return CompletionChunk(
        text_delta=str(restored.get("text_delta") or ""),
        reasoning_delta=str(restored.get("reasoning_delta") or ""),
        result=None if result is None else _completion_from(result, context),
    )


def _response_tool_names(value: object) -> set[str]:
    """只从模型响应的 tool_calls 字段提取实际调用名。"""

    names: set[str] = set()
    if isinstance(value, Mapping):
        calls = value.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, Mapping) and isinstance(call.get("name"), str):
                    names.add(str(call["name"]))
        for item in value.values():
            names.update(_response_tool_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_response_tool_names(item))
    return names


class _CaseBoundGateway:
    def __init__(self) -> None:
        self._case: _CaseContext | None = None

    def begin_case(self, case_id: str, *, case_root: Path, workspace: Path | None) -> None:
        if self._case is not None:
            raise ModelCassetteError(f"case {self._case.case_id} 尚未结束")
        self._case = _CaseContext(
            case_id=case_id,
            case_root=str(case_root.resolve()),
            workspace=str(workspace.resolve()) if workspace is not None else None,
        )

    def _context(self) -> _CaseContext:
        if self._case is None:
            raise ModelCassetteError("模型调用发生在 begin_case 之外")
        return self._case


class RecordingModelGateway(_CaseBoundGateway):
    """透明记录真实网关；partial JSONL 让崩溃前已完成的交互仍可审计。"""

    def __init__(
        self,
        delegate: ModelGatewayLike,
        *,
        output: Path,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._delegate = delegate
        self._output = output.resolve()
        self._partial = self._output.with_suffix(self._output.suffix + ".partial.jsonl")
        if self._output.exists() or self._partial.exists():
            raise ModelCassetteError(f"cassette 输出已存在，拒绝覆盖: {self._output}")
        self._output.parent.mkdir(parents=True, exist_ok=True)
        self._partial.touch(mode=0o600, exist_ok=False)
        self._metadata = dict(metadata or {})
        self._metadata.update(
            {
                "provider": delegate.chat_provider,
                "model": delegate.chat_model,
                "embedding_provider": delegate.embedding_provider,
                "embedding_model": delegate.embedding_model,
                "embedding_dimensions": delegate.embedding_dimensions,
            }
        )
        self._prompt_budgets: dict[str, dict[str, Any]] = {}
        self._interactions: list[dict[str, Any]] = []
        self._sequence = 0
        self.chat_provider = delegate.chat_provider
        self.chat_model = delegate.chat_model
        self.embedding_provider = delegate.embedding_provider
        self.embedding_model = delegate.embedding_model
        self.embedding_dimensions = delegate.embedding_dimensions

    @property
    def interaction_count(self) -> int:
        return len(self._interactions)

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget:
        budget = self._delegate.prompt_budget(task_type, max_tokens=max_tokens)
        key = content_sha256({"task_type": task_type, "max_tokens": max_tokens})
        self._prompt_budgets[key] = asdict(budget)
        return budget

    def _record(self, operation: str, request: dict[str, Any], response: object) -> None:
        context = self._context()
        normalized_request = context.normalize(request)
        normalized_response = context.normalize(response)
        self._sequence += 1
        interaction = {
            "sequence": self._sequence,
            "case_id": context.case_id,
            "operation": operation,
            "request_hash": content_sha256(normalized_request),
            "request": normalized_request,
            "response": normalized_response,
        }
        self._interactions.append(interaction)
        with self._partial.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(interaction, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        request = _request(
            "complete",
            messages=messages,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        try:
            result = await self._delegate.complete(
                messages,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as error:
            self._record(
                "complete",
                request,
                {"kind": "error", "type": type(error).__name__, "message": str(error)},
            )
            raise
        self._record("complete", request, {"kind": "completion", "value": _completion(result)})
        return result

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        request = _request(
            "complete_with_tools",
            messages=messages,
            tools=tools,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
            parallel_tool_calls=parallel_tool_calls,
        )
        try:
            result = await self._delegate.complete_with_tools(
                messages,
                tools=tools,
                parallel_tool_calls=parallel_tool_calls,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as error:
            self._record(
                "complete_with_tools",
                request,
                {"kind": "error", "type": type(error).__name__, "message": str(error)},
            )
            raise
        self._record(
            "complete_with_tools",
            request,
            {"kind": "completion", "value": _completion(result)},
        )
        return result

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]:
        request = _request(
            "stream_with_tools",
            messages=messages,
            tools=tools,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
            parallel_tool_calls=parallel_tool_calls,
        )
        chunks: list[dict[str, Any]] = []
        try:
            async for chunk in self._delegate.stream_with_tools(
                messages,
                tools=tools,
                parallel_tool_calls=parallel_tool_calls,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                chunks.append(_chunk(chunk))
                yield chunk
        except Exception as error:
            self._record(
                "stream_with_tools",
                request,
                {
                    "kind": "stream_error",
                    "chunks": chunks,
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            raise
        self._record("stream_with_tools", request, {"kind": "chunks", "value": chunks})

    async def stream(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        request = _request(
            "stream",
            messages=messages,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        chunks: list[str] = []
        try:
            async for chunk in self._delegate.stream(
                messages,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                chunks.append(chunk)
                yield chunk
        except Exception as error:
            self._record(
                "stream",
                request,
                {
                    "kind": "stream_error",
                    "chunks": chunks,
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            raise
        self._record("stream", request, {"kind": "text_chunks", "value": chunks})

    async def embed(self, texts: list[str], *, task_type: str = "embedding") -> EmbeddingResult:
        request = _request("embed", texts=texts, task_type=task_type)
        try:
            result = await self._delegate.embed(texts, task_type=task_type)
        except Exception as error:
            self._record(
                "embed",
                request,
                {"kind": "error", "type": type(error).__name__, "message": str(error)},
            )
            raise
        self._record("embed", request, {"kind": "embedding", "value": _embedding(result)})
        return result

    def end_case(self) -> None:
        self._context()
        self._case = None

    def finalize(self, *, complete: bool = True) -> Path:
        if self._case is not None:
            raise ModelCassetteError(f"case {self._case.case_id} 尚未结束")
        payload = _seal(
            {
                "schema": MODEL_CASSETTE_SCHEMA,
                "schema_version": MODEL_CASSETTE_VERSION,
                "canonicalization": CANONICALIZATION,
                "normalizer": NORMALIZER_VERSION,
                "metadata": {**self._metadata, "complete": complete},
                "prompt_budgets": self._prompt_budgets,
                "interactions": sorted(self._interactions, key=lambda item: int(item["sequence"])),
            }
        )
        temporary = self._output.with_suffix(self._output.suffix + ".tmp")
        if temporary.exists():
            raise ModelCassetteError(f"cassette 临时输出已存在: {temporary}")
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._output)
        self._partial.unlink(missing_ok=True)
        return self._output


class ReplayingModelGateway(_CaseBoundGateway):
    """不含真实 provider 的 fail-closed 网关。"""

    def __init__(self, payload: Mapping[str, Any], *, source: Path) -> None:
        super().__init__()
        self._source = source.resolve()
        _verify_payload(payload, source=self._source)
        metadata = payload["metadata"]
        assert isinstance(metadata, Mapping)
        if metadata.get("complete") is not True:
            raise ModelCassetteError("cassette 标记为未完成，不能执行重放")
        self._metadata = dict(metadata)
        self.chat_provider = str(metadata.get("provider") or "cassette")
        self.chat_model = str(metadata.get("model") or "cassette")
        self.embedding_provider = str(metadata.get("embedding_provider") or "cassette")
        self.embedding_model = str(metadata.get("embedding_model") or "cassette")
        self.embedding_dimensions = int(metadata.get("embedding_dimensions") or 0)
        budgets = payload.get("prompt_budgets")
        self._prompt_budgets = dict(budgets) if isinstance(budgets, Mapping) else {}
        interactions = payload.get("interactions")
        assert isinstance(interactions, list)
        self._interactions = [dict(item) for item in interactions if isinstance(item, Mapping)]
        self._consumed: set[int] = set()
        self.real_model_calls = 0

    @property
    def metadata(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._metadata))

    @property
    def source_sha256(self) -> str:
        return cassette_sha256(self._source)

    def cases_using_tool(self, tool_name: str) -> tuple[str, ...]:
        """在执行 graph 前暴露高风险工具供 runner 拒绝。"""

        return tuple(
            sorted(
                {
                    str(item["case_id"])
                    for item in self._interactions
                    if tool_name in _response_tool_names(item.get("response"))
                }
            )
        )

    @classmethod
    def load(cls, path: Path) -> ReplayingModelGateway:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (OSError, json.JSONDecodeError, ModelCassetteError) as error:
            raise ModelCassetteError(f"无法读取 cassette {path}: {error}") from error
        if not isinstance(payload, Mapping):
            raise ModelCassetteError("cassette 根节点必须是对象")
        return cls(payload, source=path)

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget:
        key = content_sha256({"task_type": task_type, "max_tokens": max_tokens})
        raw = self._prompt_budgets.get(key)
        if not isinstance(raw, Mapping):
            raise ModelCassetteError(
                f"cassette 没有 prompt_budget: task_type={task_type}, max_tokens={max_tokens}"
            )
        return PromptBudget(
            task_type=str(raw.get("task_type") or task_type),
            tier=cast("Any", raw.get("tier") or "main"),
            model=str(raw.get("model") or self.chat_model),
            context_window_tokens=int(raw.get("context_window_tokens") or 0),
            max_output_tokens=int(raw.get("max_output_tokens") or max_tokens),
            safety_tokens=int(raw.get("safety_tokens") or 0),
        )

    def _consume(self, operation: str, request: dict[str, Any]) -> Mapping[str, Any]:
        context = self._context()
        normalized = context.normalize(request)
        request_hash = content_sha256(normalized)
        remaining_items = sorted(
            (
                item
                for item in self._interactions
                if int(item.get("sequence", -1)) not in self._consumed
            ),
            key=lambda item: int(item["sequence"]),
        )
        if not remaining_items:
            raise ModelCassetteError(
                f"case {context.case_id} 出现未录制请求 "
                f"{operation}:{request_hash[:12]}；cassette 已全部消费"
            )
        selected = remaining_items[0]
        expected_case = selected.get("case_id")
        expected_operation = selected.get("operation")
        expected_hash = selected.get("request_hash")
        if (
            expected_case != context.case_id
            or expected_operation != operation
            or expected_hash != request_hash
        ):
            difference = (
                _first_json_difference(selected.get("request"), normalized)
                if expected_operation == operation
                else "下一条 interaction 的 operation 不同"
            )
            raise ModelCassetteError(
                f"case {context.case_id} 出现未录制请求或乱序请求 "
                f"{operation}:{request_hash[:12]}；期望 sequence={selected.get('sequence')} "
                f"case={expected_case} operation={expected_operation} "
                f"hash={str(expected_hash)[:12]}；首差异={difference}"
            )
        sequence = int(selected["sequence"])
        self._consumed.add(sequence)
        response = selected.get("response")
        if not isinstance(response, Mapping):  # pragma: no cover - load 阶段已验证
            raise ModelCassetteError(f"cassette interaction {sequence} response 无效")
        return response

    @staticmethod
    def _raise_recorded(response: Mapping[str, Any]) -> None:
        kind = response.get("kind")
        if kind in {"error", "stream_error"}:
            raise RecordedModelError(
                f"录制的模型错误 {response.get('type')}: {response.get('message')}"
            )

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        response = self._consume(
            "complete",
            _request(
                "complete",
                messages=messages,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        self._raise_recorded(response)
        if response.get("kind") != "completion":
            raise ModelCassetteError("complete response kind 无效")
        return _completion_from(response.get("value"), self._context())

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        response = self._consume(
            "complete_with_tools",
            _request(
                "complete_with_tools",
                messages=messages,
                tools=tools,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
                parallel_tool_calls=parallel_tool_calls,
            ),
        )
        self._raise_recorded(response)
        if response.get("kind") != "completion":
            raise ModelCassetteError("complete_with_tools response kind 无效")
        return _completion_from(response.get("value"), self._context())

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]:
        response = self._consume(
            "stream_with_tools",
            _request(
                "stream_with_tools",
                messages=messages,
                tools=tools,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
                parallel_tool_calls=parallel_tool_calls,
            ),
        )
        self._raise_recorded(response)
        raw_chunks = response.get("value")
        if response.get("kind") != "chunks" or not isinstance(raw_chunks, list):
            raise ModelCassetteError("stream_with_tools response kind 无效")
        for raw in raw_chunks:
            yield _chunk_from(raw, self._context())

    async def stream(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        response = self._consume(
            "stream",
            _request(
                "stream",
                messages=messages,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        self._raise_recorded(response)
        chunks = self._context().restore(response.get("value"))
        if response.get("kind") != "text_chunks" or not isinstance(chunks, list):
            raise ModelCassetteError("stream response kind 无效")
        for chunk in chunks:
            if not isinstance(chunk, str):
                raise ModelCassetteError("stream chunk 必须是字符串")
            yield chunk

    async def embed(self, texts: list[str], *, task_type: str = "embedding") -> EmbeddingResult:
        response = self._consume("embed", _request("embed", texts=texts, task_type=task_type))
        self._raise_recorded(response)
        restored = self._context().restore(response.get("value"))
        if response.get("kind") != "embedding" or not isinstance(restored, Mapping):
            raise ModelCassetteError("embed response kind 无效")
        vectors = restored.get("embeddings")
        usage = restored.get("usage")
        if not isinstance(vectors, list) or not isinstance(usage, Mapping):
            raise ModelCassetteError("embedding response 缺少 vectors/usage")
        return EmbeddingResult(
            embeddings=[[float(value) for value in vector] for vector in vectors],
            model=str(restored.get("model") or ""),
            provider=str(restored.get("provider") or ""),
            usage=_usage_from(usage),
        )

    def end_case(self) -> None:
        context = self._context()
        remaining = [
            int(item["sequence"])
            for item in self._interactions
            if item.get("case_id") == context.case_id
            and int(item.get("sequence", -1)) not in self._consumed
        ]
        self._case = None
        if remaining:
            raise ModelCassetteError(
                f"case {context.case_id} 结束时仍有未消费模型交互: {remaining}"
            )

    def assert_complete(self) -> None:
        if self._case is not None:
            raise ModelCassetteError(f"case {self._case.case_id} 尚未结束")
        remaining = sorted(
            int(item["sequence"])
            for item in self._interactions
            if int(item.get("sequence", -1)) not in self._consumed
        )
        if remaining:
            raise ModelCassetteError(f"cassette 仍有未消费交互: {remaining}")
        if self.real_model_calls != 0:  # pragma: no cover - 常量守卫
            raise ModelCassetteError("replay 期间发生了真实模型调用")


def _verify_payload(payload: Mapping[str, Any], *, source: Path) -> None:
    if payload.get("schema") != MODEL_CASSETTE_SCHEMA:
        raise ModelCassetteError(f"cassette schema 不受支持: {source}")
    if payload.get("schema_version") != MODEL_CASSETTE_VERSION:
        raise ModelCassetteError(f"cassette schema_version 不受支持: {source}")
    if payload.get("canonicalization") != CANONICALIZATION:
        raise ModelCassetteError(f"cassette canonicalization 不受支持: {source}")
    if payload.get("normalizer") != NORMALIZER_VERSION:
        raise ModelCassetteError(f"cassette normalizer 不受支持: {source}")
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ModelCassetteError(f"cassette 缺少完整性摘要: {source}")
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    if (
        integrity.get("algorithm") != INTEGRITY_ALGORITHM
        or integrity.get("canonicalization") != CANONICALIZATION
        or integrity.get("value") != content_sha256(unsigned)
    ):
        raise ModelCassetteError(f"cassette 完整性校验失败: {source}")
    if not isinstance(payload.get("metadata"), Mapping):
        raise ModelCassetteError("cassette metadata 必须是对象")
    interactions = payload.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        raise ModelCassetteError("cassette interactions 必须是非空数组")
    seen: set[int] = set()
    for index, item in enumerate(interactions):
        if not isinstance(item, Mapping):
            raise ModelCassetteError(f"interactions[{index}] 必须是对象")
        sequence = item.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ModelCassetteError(f"interactions[{index}].sequence 无效")
        if sequence in seen:
            raise ModelCassetteError(f"cassette sequence 重复: {sequence}")
        seen.add(sequence)
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ModelCassetteError(f"interaction {sequence} case_id 无效")
        operation = item.get("operation")
        if operation not in _OPERATIONS:
            raise ModelCassetteError(f"interaction {sequence} operation 无效: {operation}")
        request = item.get("request")
        if not isinstance(request, Mapping) or item.get("request_hash") != content_sha256(request):
            raise ModelCassetteError(f"interaction {sequence} 请求指纹不匹配")
        if request.get("operation") != operation:
            raise ModelCassetteError(f"interaction {sequence} operation/request 不一致")
        if not isinstance(item.get("response"), Mapping):
            raise ModelCassetteError(f"interaction {sequence} response 无效")
    expected_sequences = set(range(1, len(interactions) + 1))
    if seen != expected_sequences:
        raise ModelCassetteError(f"cassette sequence 必须从 1 开始且连续: {sorted(seen)}")


def cassette_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
