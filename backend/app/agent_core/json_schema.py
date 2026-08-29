"""Bounded JSON Schema compilation for untrusted external tool contracts.

MCP servers control ``inputSchema``.  A catalog hash makes schema drift visible, but it does not
make an arbitrarily deep schema, remote ``$ref``, or catastrophic regular expression safe to
evaluate.  This module accepts a deliberately small Draft 2020-12 subset and bounds both schema
compilation and instance validation before ``jsonschema`` sees either value.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

SCHEMA_MAX_BYTES = 64 * 1024
SCHEMA_MAX_DEPTH = 20
SCHEMA_MAX_NODES = 2_048
SCHEMA_MAX_RAW_DEPTH = 64
SCHEMA_MAX_RAW_NODES = 8_192
SCHEMA_MAX_PROPERTIES = 256
SCHEMA_MAX_BRANCHES = 32
INSTANCE_MAX_DEPTH = 20
INSTANCE_MAX_NODES = 4_096
INSTANCE_MAX_CONTAINER_ITEMS = 1_024
INSTANCE_MAX_STRING_CHARS = 256 * 1024
INSTANCE_MAX_TOTAL_STRING_CHARS = 512 * 1024
INSTANCE_MAX_KEY_CHARS = 256

_ROOT_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
_LOCAL_REF = re.compile(r"^#/(?:\$defs|definitions)/([A-Za-z0-9_.-]{1,128})$")
_JSON_TYPES = frozenset({"null", "boolean", "object", "array", "number", "integer", "string"})
_FORMATS = frozenset(
    {
        "date",
        "date-time",
        "duration",
        "email",
        "hostname",
        "idn-email",
        "idn-hostname",
        "ipv4",
        "ipv6",
        "iri",
        "iri-reference",
        "json-pointer",
        "relative-json-pointer",
        "time",
        "uri",
        "uri-reference",
        "uri-template",
        "uuid",
    }
)
_ALLOWED_KEYWORDS = frozenset(
    {
        "$schema",
        "$ref",
        "$defs",
        "definitions",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "title",
        "description",
        "$comment",
        "default",
        "examples",
        "enum",
        "const",
        "deprecated",
        "readOnly",
        "writeOnly",
        "nullable",
        "minProperties",
        "maxProperties",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
        "oneOf",
        "allOf",
        "format",
    }
)


class BoundedJsonSchemaError(ValueError):
    """The external schema is invalid, too large, or outside the supported safe subset."""


class BoundedJsonValueError(ValueError):
    """The instance is too large or does not satisfy the compiled schema."""


@dataclass(frozen=True)
class BoundedJsonSchemaValidator:
    _validator: Draft202012Validator

    def validate(self, value: dict[str, Any]) -> None:
        _validate_instance_bounds(value)
        try:
            self._validator.validate(value)
        except JsonSchemaValidationError:
            # ValidationError.message/path/instance can contain the complete secret-bearing tool
            # arguments.  The caller gets only a stable classification.
            raise BoundedJsonValueError("JSON 参数不符合已固定 schema") from None


def compile_bounded_json_schema(schema: dict[str, Any]) -> BoundedJsonSchemaValidator:
    """Compile one external input schema after static safety and complexity checks."""

    _validate_raw_schema_json(schema)
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise BoundedJsonSchemaError("JSON schema 无效") from None
    if len(encoded) > SCHEMA_MAX_BYTES:
        raise BoundedJsonSchemaError("JSON schema 超出大小限制")

    root_type = schema.get("type")
    if root_type is not None and not _type_includes_object(root_type):
        raise BoundedJsonSchemaError("工具参数 schema 顶层必须接受 object")

    definitions = _root_definitions(schema)
    edges: dict[str, set[str]] = {"#": set()}
    for ref in definitions:
        edges[ref] = set()
    counter = [0]
    _validate_schema_node(
        schema,
        depth=0,
        counter=counter,
        definitions=definitions,
        edges=edges,
        owner_ref="#",
    )
    _reject_reference_cycles(edges)
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
    except (SchemaError, TypeError, ValueError):
        raise BoundedJsonSchemaError("JSON schema 无效") from None
    return BoundedJsonSchemaValidator(validator)


def _validate_raw_schema_json(value: object) -> None:
    """Bound annotations/const/default too, not only positions interpreted as schemas."""

    nodes = 0
    seen_containers: set[int] = set()
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > SCHEMA_MAX_RAW_DEPTH:
            raise BoundedJsonSchemaError("JSON schema 嵌套过深")
        nodes += 1
        if nodes > SCHEMA_MAX_RAW_NODES:
            raise BoundedJsonSchemaError("JSON schema 节点过多")
        if current is None or isinstance(current, (str, bool)):
            continue
        if isinstance(current, int):
            if current.bit_length() > 4_096:
                raise BoundedJsonSchemaError("JSON schema 数值超出边界")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise BoundedJsonSchemaError("JSON schema 数值超出边界")
            continue
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers:
                raise BoundedJsonSchemaError("JSON schema 包含循环结构")
            seen_containers.add(identity)
            for key, item in current.items():
                if not isinstance(key, str):
                    raise BoundedJsonSchemaError("JSON schema key 无效")
                stack.append((item, depth + 1))
            continue
        if isinstance(current, list):
            identity = id(current)
            if identity in seen_containers:
                raise BoundedJsonSchemaError("JSON schema 包含循环结构")
            seen_containers.add(identity)
            stack.extend((item, depth + 1) for item in current)
            continue
        raise BoundedJsonSchemaError("JSON schema 包含非 JSON 值")


def _type_includes_object(value: object) -> bool:
    if value == "object":
        return True
    return isinstance(value, list) and any(item == "object" for item in value)


def _root_definitions(schema: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for keyword in ("$defs", "definitions"):
        raw = schema.get(keyword, {})
        if raw is None:
            continue
        if not isinstance(raw, dict) or len(raw) > SCHEMA_MAX_PROPERTIES:
            raise BoundedJsonSchemaError("JSON schema definitions 无效")
        for name, definition in raw.items():
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is None:
                raise BoundedJsonSchemaError("JSON schema definition 名称无效")
            ref = f"#/{keyword}/{name}"
            result[ref] = definition
    return result


def _validate_schema_node(
    node: object,
    *,
    depth: int,
    counter: list[int],
    definitions: dict[str, object],
    edges: dict[str, set[str]],
    owner_ref: str,
) -> None:
    if depth > SCHEMA_MAX_DEPTH:
        raise BoundedJsonSchemaError("JSON schema 嵌套过深")
    counter[0] += 1
    if counter[0] > SCHEMA_MAX_NODES:
        raise BoundedJsonSchemaError("JSON schema 节点过多")
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise BoundedJsonSchemaError("JSON schema 节点必须是 object 或 boolean")
    unsupported = set(node) - _ALLOWED_KEYWORDS
    if unsupported:
        # Do not echo attacker-controlled keyword names.
        raise BoundedJsonSchemaError("JSON schema 包含不支持的关键字")
    if depth > 0 and ("$defs" in node or "definitions" in node):
        raise BoundedJsonSchemaError("JSON schema 仅允许顶层 definitions")

    schema_uri = node.get("$schema")
    if schema_uri is not None and schema_uri != _ROOT_SCHEMA_URI:
        raise BoundedJsonSchemaError("JSON schema draft 不受支持")
    ref_value = node.get("$ref")
    if ref_value is not None:
        if not isinstance(ref_value, str) or _LOCAL_REF.fullmatch(ref_value) is None:
            raise BoundedJsonSchemaError("JSON schema 仅允许本地 definition 引用")
        if ref_value not in definitions:
            raise BoundedJsonSchemaError("JSON schema 本地引用不存在")
        edges.setdefault(owner_ref, set()).add(ref_value)

    raw_type = node.get("type")
    if raw_type is not None:
        if isinstance(raw_type, str):
            valid_type = raw_type in _JSON_TYPES
        else:
            valid_type = (
                isinstance(raw_type, list)
                and 1 <= len(raw_type) <= len(_JSON_TYPES)
                and all(isinstance(item, str) and item in _JSON_TYPES for item in raw_type)
                and len(set(raw_type)) == len(raw_type)
            )
        if not valid_type:
            raise BoundedJsonSchemaError("JSON schema type 无效")

    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or len(properties) > SCHEMA_MAX_PROPERTIES:
            raise BoundedJsonSchemaError("JSON schema properties 过多或无效")
        for name, child in properties.items():
            if not isinstance(name, str) or len(name) > INSTANCE_MAX_KEY_CHARS:
                raise BoundedJsonSchemaError("JSON schema property 名称无效")
            _validate_schema_node(
                child,
                depth=depth + 1,
                counter=counter,
                definitions=definitions,
                edges=edges,
                owner_ref=owner_ref,
            )

    for keyword in ("$defs", "definitions"):
        raw_definitions = node.get(keyword)
        if raw_definitions is None:
            continue
        assert isinstance(raw_definitions, dict)  # checked by _root_definitions
        for name, child in raw_definitions.items():
            ref = f"#/{keyword}/{name}"
            _validate_schema_node(
                child,
                depth=depth + 1,
                counter=counter,
                definitions=definitions,
                edges=edges,
                owner_ref=ref,
            )

    for keyword in ("items", "additionalProperties"):
        child = node.get(keyword)
        if child is not None:
            _validate_schema_node(
                child,
                depth=depth + 1,
                counter=counter,
                definitions=definitions,
                edges=edges,
                owner_ref=owner_ref,
            )
    for keyword in ("prefixItems", "anyOf", "oneOf", "allOf"):
        branches = node.get(keyword)
        if branches is None:
            continue
        limit = SCHEMA_MAX_PROPERTIES if keyword == "prefixItems" else SCHEMA_MAX_BRANCHES
        if not isinstance(branches, list) or not 1 <= len(branches) <= limit:
            raise BoundedJsonSchemaError("JSON schema 分支数量无效")
        for child in branches:
            _validate_schema_node(
                child,
                depth=depth + 1,
                counter=counter,
                definitions=definitions,
                edges=edges,
                owner_ref=owner_ref,
            )

    required = node.get("required")
    if required is not None and (
        not isinstance(required, list)
        or len(required) > SCHEMA_MAX_PROPERTIES
        or any(not isinstance(item, str) or len(item) > INSTANCE_MAX_KEY_CHARS for item in required)
        or len(set(required)) != len(required)
    ):
        raise BoundedJsonSchemaError("JSON schema required 无效")
    enum = node.get("enum")
    if enum is not None and (not isinstance(enum, list) or not 1 <= len(enum) <= 256):
        raise BoundedJsonSchemaError("JSON schema enum 无效")
    examples = node.get("examples")
    if examples is not None and (not isinstance(examples, list) or len(examples) > 64):
        raise BoundedJsonSchemaError("JSON schema examples 无效")
    raw_format = node.get("format")
    if raw_format is not None and (not isinstance(raw_format, str) or raw_format not in _FORMATS):
        raise BoundedJsonSchemaError("JSON schema format 不受支持")
    for keyword in ("title", "description", "$comment"):
        value = node.get(keyword)
        if value is not None and (not isinstance(value, str) or len(value) > 8_192):
            raise BoundedJsonSchemaError("JSON schema annotation 无效")
    for keyword, maximum in (
        ("maxProperties", INSTANCE_MAX_CONTAINER_ITEMS),
        ("maxItems", INSTANCE_MAX_CONTAINER_ITEMS),
        ("maxLength", INSTANCE_MAX_STRING_CHARS),
    ):
        value = node.get(keyword)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value > maximum
        ):
            raise BoundedJsonSchemaError("JSON schema 声明的上限超出运行时边界")
    for keyword in (
        "minProperties",
        "minItems",
        "minLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ):
        value = node.get(keyword)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BoundedJsonSchemaError("JSON schema 数值约束无效")
        if isinstance(value, int) and value.bit_length() > 256:
            raise BoundedJsonSchemaError("JSON schema 数值约束超出边界")
        if isinstance(value, float) and not math.isfinite(value):
            raise BoundedJsonSchemaError("JSON schema 数值约束超出边界")


def _reject_reference_cycles(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise BoundedJsonSchemaError("JSON schema 不允许递归引用")
        if node in visited:
            return
        visiting.add(node)
        for child in edges.get(node, set()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def _validate_instance_bounds(value: object) -> None:
    nodes = 0
    total_string_chars = 0
    seen_containers: set[int] = set()
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > INSTANCE_MAX_DEPTH:
            raise BoundedJsonValueError("JSON 参数超出复杂度限制")
        nodes += 1
        if nodes > INSTANCE_MAX_NODES:
            raise BoundedJsonValueError("JSON 参数超出复杂度限制")
        if isinstance(current, str):
            if len(current) > INSTANCE_MAX_STRING_CHARS:
                raise BoundedJsonValueError("JSON 参数超出复杂度限制")
            total_string_chars += len(current)
        elif current is None or isinstance(current, bool):
            continue
        elif isinstance(current, int):
            if current.bit_length() > 1_024:
                raise BoundedJsonValueError("JSON 参数超出复杂度限制")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise BoundedJsonValueError("JSON 参数超出复杂度限制")
        elif isinstance(current, dict):
            identity = id(current)
            if identity in seen_containers or len(current) > INSTANCE_MAX_CONTAINER_ITEMS:
                raise BoundedJsonValueError("JSON 参数超出复杂度限制")
            seen_containers.add(identity)
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > INSTANCE_MAX_KEY_CHARS:
                    raise BoundedJsonValueError("JSON 参数超出复杂度限制")
                total_string_chars += len(key)
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            identity = id(current)
            if identity in seen_containers or len(current) > INSTANCE_MAX_CONTAINER_ITEMS:
                raise BoundedJsonValueError("JSON 参数超出复杂度限制")
            seen_containers.add(identity)
            stack.extend((item, depth + 1) for item in current)
        else:
            raise BoundedJsonValueError("JSON 参数不是受支持的 JSON 值")
        if total_string_chars > INSTANCE_MAX_TOTAL_STRING_CHARS:
            raise BoundedJsonValueError("JSON 参数超出复杂度限制")
