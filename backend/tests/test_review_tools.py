import pytest

from app.rag.review.state import ReviewCard
from app.rag.review.tools import (
    DatabaseModelReviewTools,
    ReviewToolResponseError,
    _bounded_document,
    anchor_evidence_quote,
    build_evidence_catalog,
    parse_card_payload,
    resolve_card_evidence,
    resolve_evidence_quotes,
)
from workpilot_ai.types import CompletionResult, Message, Usage


def test_card_parser_is_schema_strict() -> None:
    parsed = parse_card_payload(
        '说明 {"core_problem":"P","method_family":"memory","method":"M",'
        '"findings":["F"],"limitations":[],"evidence_quotes":["Q"]}'
    )
    assert parsed.method_family == "memory"

    with pytest.raises(ReviewToolResponseError, match="schema"):
        parse_card_payload(
            '{"core_problem":"P","method_family":"memory","method":"M",'
            '"findings":[],"limitations":[],"evidence_quotes":["Q"],"extra":1}'
        )

    parsed_ref = parse_card_payload(
        '{"core_problem":"P","method_family":"memory","method":"M",'
        '"findings":["F"],"limitations":[],"evidence_refs":["E00003"]}'
    )
    assert parsed_ref.evidence_refs == ["E00003"]


async def test_group_cards_is_deterministic() -> None:
    cards: list[ReviewCard] = [
        {
            "document_id": "b",
            "title": "B",
            "core_problem": "P",
            "method_family": "z-family",
            "method": "M",
            "findings": ["F"],
            "limitations": [],
            "evidence_quotes": ["Q"],
        },
        {
            "document_id": "a",
            "title": "A",
            "core_problem": "P",
            "method_family": "a-family",
            "method": "M",
            "findings": ["F"],
            "limitations": [],
            "evidence_quotes": ["Q"],
        },
    ]
    tools = object.__new__(DatabaseModelReviewTools)
    groups = await tools.group_cards(cards)
    assert [item["name"] for item in groups] == ["a-family", "z-family"]


def test_bounded_document_preserves_head_and_tail() -> None:
    value = "A" * 80 + "B" * 80
    bounded = _bounded_document(value, 60)
    assert bounded.startswith("A" * 40)
    assert bounded.endswith("B" * 20)
    assert "预算截断" in bounded


def test_evidence_quote_anchor_rehydrates_layout_differences() -> None:
    source = "结论：吞吐量为１２８。\n延迟低于 20 ms。"

    anchor = anchor_evidence_quote("吞吐量为128。 延迟低于20 ms。", source)

    assert anchor is not None
    assert anchor.match_kind == "layout_normalized"
    assert anchor.quote == "吞吐量为１２８。\n延迟低于 20 ms。"
    assert source[anchor.char_start : anchor.char_end] == anchor.quote


def test_evidence_quote_anchor_rejects_semantic_or_punctuation_rewrite() -> None:
    source = "该方法显著降低延迟，但没有提高吞吐量。"

    assert anchor_evidence_quote("该方法降低了延迟，但吞吐量未提高。", source) is None
    assert anchor_evidence_quote("该方法显著降低延迟；但没有提高吞吐量。", source) is None


def test_evidence_quote_anchor_rejects_ambiguous_normalized_match() -> None:
    source = "相同 片段。另一处相同　片段。"

    assert anchor_evidence_quote("相同片段", source) is None


def test_resolve_evidence_quotes_returns_only_source_slices() -> None:
    source = "原文有ＡＢＣ，也有精确证据。"

    resolved = resolve_evidence_quotes(["ABC", "精确证据"], source)

    assert resolved == ["ＡＢＣ", "精确证据"]
    assert all(quote in source for quote in resolved)


def test_evidence_catalog_uses_source_offsets_and_resolves_refs() -> None:
    source = "标题\n\n  第一条证据。\n第二条证据。\n"
    catalog = build_evidence_catalog(source)
    by_ref = {item.ref: item for item in catalog}
    payload = parse_card_payload(
        '{"core_problem":"P","method_family":"memory","method":"M",'
        '"findings":["F"],"limitations":[],"evidence_refs":["E00006"]}'
    )

    assert [(item.ref, item.quote) for item in catalog] == [
        ("E00000", "标题"),
        ("E00006", "第一条证据。"),
        ("E00013", "第二条证据。"),
    ]
    assert resolve_card_evidence(payload, source, evidence_catalog=by_ref) == [
        "第一条证据。"
    ]


def test_unknown_or_duplicate_evidence_refs_fail_closed() -> None:
    source = "唯一证据。"
    catalog = build_evidence_catalog(source)
    by_ref = {item.ref: item for item in catalog}
    payload = parse_card_payload(
        '{"core_problem":"P","method_family":"memory","method":"M",'
        '"findings":["F"],"limitations":[],"evidence_refs":["E99999"]}'
    )

    with pytest.raises(ReviewToolResponseError, match="无法解析"):
        resolve_card_evidence(payload, source, evidence_catalog=by_ref)


CARD_JSON = (
    '{{"core_problem":"P","method_family":"memory","method":"M",'
    '"findings":["F"],"limitations":[],"evidence_quotes":["{quote}"]}}'
)


class ScriptedGateway:
    """按脚本逐轮回放；记录每一轮实际收到的最后一条 user 消息。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts_received: list[str] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        self.prompts_received.append(messages[-1].content)
        index = min(len(self.prompts_received) - 1, len(self.responses) - 1)
        return CompletionResult(
            text=self.responses[index], model="fake", provider="fake", usage=Usage()
        )


def _tools(gateway: ScriptedGateway, style: str) -> DatabaseModelReviewTools:
    tools = object.__new__(DatabaseModelReviewTools)
    tools.gateway = gateway
    tools.card_repair_attempts = 2
    tools.card_error_style = style  # type: ignore[assignment]
    tools.card_system_prompt = "系统提示"
    tools.card_max_tokens = 2400
    tools.card_trace = []
    return tools


async def test_model_facing_repair_names_the_offending_quote() -> None:
    """面向模型的补救必须指出是哪一条不合法，并给出可执行的下一步（约束 4）。"""

    gateway = ScriptedGateway(
        [
            CARD_JSON.format(quote="这句原文里没有"),
            CARD_JSON.format(quote="真实原文"),
        ]
    )
    tools = _tools(gateway, "model_facing")
    payload = await tools._extract_card_payload(
        [Message(role="user", content="首轮")], "文档包含真实原文这几个字"
    )

    assert payload.evidence_quotes == ["真实原文"]
    repair = gateway.prompts_received[1]
    assert "这句原文里没有" in repair
    assert "逐字复制" in repair
    assert tools.card_trace == [["卡片 evidence_quotes 不是文档逐字摘录", "ok"]]


async def test_extract_payload_resolves_refs_to_legacy_review_card_quotes() -> None:
    gateway = ScriptedGateway(
        [
            '{"core_problem":"P","method_family":"memory","method":"M",'
            '"findings":["F"],"limitations":[],"evidence_refs":["E00002"]}'
        ]
    )
    tools = _tools(gateway, "none")
    source = "前缀真实证据。"
    catalog = {item.ref: item for item in build_evidence_catalog(source)}
    # 用字符偏移 2 的子 catalog 模拟生产中的离散证据项。
    anchor = anchor_evidence_quote("真实证据。", source)
    assert anchor is not None
    catalog["E00002"] = anchor

    payload = await tools._extract_card_payload(
        [Message(role="user", content="首轮")],
        source,
        evidence_catalog=catalog,
    )

    assert payload.evidence_refs == []
    assert payload.evidence_quotes == ["真实证据。"]


async def test_generic_repair_withholds_the_specifics() -> None:
    """对照组只说"错了"，不说错在哪——这是 A1 的自变量。"""

    gateway = ScriptedGateway(
        [
            CARD_JSON.format(quote="这句原文里没有"),
            CARD_JSON.format(quote="真实原文"),
        ]
    )
    tools = _tools(gateway, "generic")
    await tools._extract_card_payload(
        [Message(role="user", content="首轮")], "文档包含真实原文这几个字"
    )

    repair = gateway.prompts_received[1]
    assert "这句原文里没有" not in repair
    assert repair == "上一次输出不合法，请重新输出。"


async def test_no_repair_style_fails_on_first_invalid_response() -> None:
    gateway = ScriptedGateway([CARD_JSON.format(quote="这句原文里没有")])
    tools = _tools(gateway, "none")

    with pytest.raises(ReviewToolResponseError, match="逐字摘录"):
        await tools._extract_card_payload(
            [Message(role="user", content="首轮")], "文档正文"
        )
    # 一次都不补救, 只发一轮。
    assert len(gateway.prompts_received) == 1


async def test_schema_repair_warns_against_copying_the_template() -> None:
    """空模板被原样抄回是实测最常见的 schema 失败，补救信息必须点名这件事。"""

    gateway = ScriptedGateway(
        [
            '{"core_problem":"","method_family":"","method":"",'
            '"findings":[],"limitations":[],"evidence_quotes":[]}',
            CARD_JSON.format(quote="真实原文"),
        ]
    )
    tools = _tools(gateway, "model_facing")
    await tools._extract_card_payload(
        [Message(role="user", content="首轮")], "文档包含真实原文这几个字"
    )

    repair = gateway.prompts_received[1]
    assert "字段模板" in repair
    assert "core_problem" in repair
