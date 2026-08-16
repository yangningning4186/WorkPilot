import pytest

from app.agent.review_tools import (
    DatabaseModelReviewTools,
    ReviewToolResponseError,
    _bounded_document,
    parse_card_payload,
)
from app.agent.state import ReviewCard


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
