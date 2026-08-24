"""Cowork 运行级证据账本与最终引用校验。

工具输出是给模型看的临时上下文，不能充当审计账本：它会被截断、压缩，也可能在多次
检索时重复使用 ``S1``。本模块把工具产生的证据收敛成 JSON 可持久化的记录，为一次 run
分配稳定编号，并在最终正文落盘前确认每个引用都能回查到实际读过的原文。

这里没有数据库或模型依赖，便于把最关键的“什么算证据”与“什么引用可放行”写成纯函数
测试。检索 snippet 不会进入本模块；调用方只能传知识库 EvidenceSegment、read_material
实际返回的 unit，或 reader_goto 逐字校验成功的 quote。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast
from uuid import NAMESPACE_URL, uuid5

EvidenceKind = Literal["knowledge", "reading"]


class EvidenceRecord(TypedDict):
    ledger_id: str
    citation_id: str
    kind: EvidenceKind
    block_id: str
    version_id: str
    document_id: str
    title: str
    source_uri: str
    quote: str
    quote_sha256: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[dict[str, Any]]
    material_id: str | None
    locator: int | None
    verified: bool
    tool_call_id: str


@dataclass(frozen=True)
class CitationValidation:
    ok: bool
    errors: tuple[str, ...]
    citations: tuple[dict[str, Any], ...]


_KNOWLEDGE_REF = re.compile(r"\[((?:K|S)\d+)\]")
_LOCATOR_REF = re.compile(r"\[p\.\s*(\d[\d\s,\u2013\u2014-]*)\](?!\()", re.IGNORECASE)
_RANGE = re.compile(r"^\s*(\d+)\s*(?:[-\u2013\u2014]\s*(\d+))?\s*$")
_FENCED_CODE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_REFUSAL = re.compile(
    r"(?:未找到|没有找到|没有足够|缺少足够|无法(?:据此|基于[^，。]*)(?:回答|确认|判断)|"
    r"insufficient\s+evidence|no\s+(?:relevant\s+)?evidence)",
    re.IGNORECASE,
)
_SOCIAL_TURN = re.compile(
    r"\s*(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|"
    r"你好|您好|嗨|哈喽|在吗|早上好|下午好|晚上好|谢谢|多谢)\s*[!！?？,.，。]*\s*",
    re.IGNORECASE,
)


def requires_source_grounding(query: str) -> bool:
    """只有纯社交轮次可跳过资料引用；带实际问题的问候仍必须落到证据。"""

    return _SOCIAL_TURN.fullmatch(query) is None


def register_evidence(
    ledger: list[EvidenceRecord],
    candidates: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    namespace: Literal["K", "S"] | None,
    tool_call_id: str,
) -> tuple[list[EvidenceRecord], list[EvidenceRecord]]:
    """把一批候选证据登记到 run 账本，返回新账本与本批对应的规范记录。

    ``K``/``S`` 在整个 run 内单调编号；同一 namespace 内同一原文重复命中时复用原编号。
    阅读证据使用正文里的 ``p.N``，账本内部则靠内容指纹区分“整页已读”和“逐字已核验”。
    """

    output = [cast("EvidenceRecord", json.loads(json.dumps(item))) for item in ledger]
    registered: list[EvidenceRecord] = []
    next_ordinal = _next_ordinal(output, namespace) if namespace is not None else 0

    for candidate in candidates:
        normal = _normalise_candidate(candidate, tool_call_id=tool_call_id)
        if normal is None:
            continue
        fingerprint = _fingerprint(normal)
        existing = next(
            (
                item
                for item in output
                if _fingerprint(item) == fingerprint
                and (namespace is None or item["citation_id"].startswith(namespace))
            ),
            None,
        )
        if existing is not None:
            # reader_goto 的逐字核验强于此前 read_material 的整页读取；同一 quote 再次
            # 出现时只升级可信度，不制造第二条互相竞争的记录。
            if normal["verified"] and not existing["verified"]:
                existing["verified"] = True
                existing["locations"] = normal["locations"] or existing["locations"]
                existing["tool_call_id"] = tool_call_id
            registered.append(existing)
            continue

        if namespace is not None:
            normal["citation_id"] = f"{namespace}{next_ordinal}"
            next_ordinal += 1
        normal["ledger_id"] = fingerprint
        output.append(normal)
        registered.append(normal)
    return output, registered


def validate_final_citations(
    text: str,
    ledger: list[EvidenceRecord],
    *,
    require_knowledge: bool,
    require_reading: bool,
) -> CitationValidation:
    """校验最终答案中的知识编号和阅读 locator，并选出要随消息落盘的记录。"""

    visible = _without_code(text)
    knowledge_refs = list(dict.fromkeys(_KNOWLEDGE_REF.findall(visible)))
    locator_refs: list[int] = []
    for raw in _LOCATOR_REF.findall(visible):
        locator_refs.extend(_parse_locator_refs(raw))
    locator_refs = list(dict.fromkeys(locator_refs))

    by_citation = {
        item["citation_id"]: item
        for item in ledger
        if item["kind"] == "knowledge" and item["citation_id"]
    }
    reading_by_locator: dict[int, list[EvidenceRecord]] = {}
    for item in ledger:
        locator = item.get("locator")
        if item["kind"] == "reading" and isinstance(locator, int):
            reading_by_locator.setdefault(locator, []).append(item)

    errors: list[str] = []
    unknown_knowledge = [value for value in knowledge_refs if value not in by_citation]
    if unknown_knowledge:
        errors.append("未登记的知识引用：" + ", ".join(f"[{item}]" for item in unknown_knowledge))
    unknown_locators = [value for value in locator_refs if value not in reading_by_locator]
    if unknown_locators:
        errors.append(
            "未实际读取的 locator：" + ", ".join(f"[p.{item}]" for item in unknown_locators)
        )

    refusal = bool(_REFUSAL.search(visible))
    substantive = len(visible.strip()) >= 12
    if require_knowledge and substantive and not refusal and not knowledge_refs:
        errors.append("基于挂载知识库的答复至少需要一个 [K…] 或 [S…] 引用")
    if require_reading and substantive and not refusal and not locator_refs:
        errors.append("阅读模式的答复至少需要一个 [p.N] 引用")

    selected: list[dict[str, Any]] = []
    for citation_id in knowledge_refs:
        citation_record = by_citation.get(citation_id)
        if citation_record is not None:
            selected.append(citation_payload(citation_record))
    for locator in locator_refs:
        options = reading_by_locator.get(locator, [])
        if not options:
            continue
        # 精确核验过的短引文优先于整页文本；其次选最新记录，能反映最后一次实际读取。
        reading_record = sorted(
            options,
            key=lambda value: (value["verified"], len(value["quote"])),
            reverse=True,
        )[0]
        selected.append(citation_payload(reading_record))

    return CitationValidation(not errors, tuple(errors), tuple(selected))


def citation_payload(record: EvidenceRecord) -> dict[str, Any]:
    """收成前端 citation 事件和消息存储共同使用的公开形状。"""

    return {
        "citation_id": record["citation_id"],
        "block_id": record["block_id"],
        "version_id": record["version_id"],
        # 前端历史契约仍叫 doc_id；同时保留跨产品契约的 document_id，迁移期间两边都
        # 能读取。下一个协议大版本再删兼容别名。
        "doc_id": record["document_id"],
        "document_id": record["document_id"],
        "title": record["title"],
        "source_uri": record["source_uri"],
        "quote": record["quote"],
        "char_start": record["char_start"],
        "char_end": record["char_end"],
        "heading_path": record["heading_path"],
        "locations": record["locations"],
        "quote_sha256": record["quote_sha256"],
        "kind": record["kind"],
        "locator": record["locator"],
        "verified": record["verified"],
    }


def _normalise_candidate(
    candidate: Mapping[str, Any], *, tool_call_id: str
) -> EvidenceRecord | None:
    kind = candidate.get("kind")
    if kind not in {"knowledge", "reading"}:
        return None
    quote = str(candidate.get("quote") or "").strip()
    if not quote:
        return None
    material_id = str(candidate.get("material_id") or "").strip() or None
    locator_raw = candidate.get("locator")
    locator = locator_raw if isinstance(locator_raw, int) and locator_raw >= 1 else None
    source_uri = str(candidate.get("source_uri") or candidate.get("path") or "")

    if kind == "reading":
        identity = material_id or hashlib.sha256(source_uri.encode()).hexdigest()
        document_id = str(uuid5(NAMESPACE_URL, f"workpilot:material:{identity}"))
        version_id = str(uuid5(NAMESPACE_URL, f"workpilot:material-version:{identity}"))
        block_id = str(
            uuid5(
                NAMESPACE_URL,
                f"workpilot:material-block:{identity}:{locator or 0}:{_sha256(quote)}",
            )
        )
        citation_id = f"p.{locator}" if locator is not None else ""
    else:
        block_id = str(candidate.get("block_id") or "")
        version_id = str(candidate.get("version_id") or "")
        document_id = str(candidate.get("document_id") or candidate.get("doc_id") or "")
        if not block_id or not version_id or not document_id:
            return None
        citation_id = str(candidate.get("citation_id") or "")

    locations = candidate.get("locations")
    heading_path = candidate.get("heading_path")
    return EvidenceRecord(
        ledger_id="",
        citation_id=citation_id,
        kind=cast("EvidenceKind", kind),
        block_id=block_id,
        version_id=version_id,
        document_id=document_id,
        title=str(candidate.get("title") or source_uri or "未知来源"),
        source_uri=source_uri,
        quote=quote,
        quote_sha256=_sha256(quote),
        char_start=_safe_int(candidate.get("char_start")),
        char_end=_safe_int(candidate.get("char_end"), default=len(quote)),
        heading_path=[str(item) for item in heading_path] if isinstance(heading_path, list) else [],
        locations=(
            [dict(item) for item in locations if isinstance(item, Mapping)]
            if isinstance(locations, list)
            else []
        ),
        material_id=material_id,
        locator=locator,
        verified=bool(candidate.get("verified")),
        tool_call_id=tool_call_id,
    )


def _fingerprint(record: EvidenceRecord) -> str:
    identity = {
        "kind": record["kind"],
        "version_id": record["version_id"],
        "block_id": record["block_id"],
        "material_id": record["material_id"],
        "locator": record["locator"],
        "quote_sha256": record["quote_sha256"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _next_ordinal(ledger: list[EvidenceRecord], namespace: str | None) -> int:
    if namespace is None:
        return 0
    values = [
        int(item["citation_id"][1:])
        for item in ledger
        if re.fullmatch(rf"{re.escape(namespace)}\d+", item["citation_id"])
    ]
    return max(values, default=0) + 1


def _parse_locator_refs(raw: str) -> list[int]:
    output: list[int] = []
    for chunk in raw.split(","):
        match = _RANGE.match(chunk)
        if match is None:
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        low, high = sorted((start, end))
        # 与前端一致，单个引用最多展开 40 个 locator，避免恶意范围放大。
        output.extend(value for value in range(low, min(high, low + 39) + 1) if value >= 1)
    return output


def _without_code(text: str) -> str:
    return _INLINE_CODE.sub("", _FENCED_CODE.sub("", text))


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CitationValidation",
    "EvidenceKind",
    "EvidenceRecord",
    "citation_payload",
    "register_evidence",
    "requires_source_grounding",
    "validate_final_citations",
]
