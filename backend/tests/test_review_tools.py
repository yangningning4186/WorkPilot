import pytest

from app.agent.review_tools import (
    DatabaseModelReviewTools,
    ReviewToolResponseError,
    _bounded_document,
    parse_card_payload,
)
from app.agent.state import ReviewCard
from app.llm.types import CompletionResult, Message, Usage


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
