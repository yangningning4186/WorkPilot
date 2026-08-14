import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

from app.retrieval.citations import CITATION_RE, REFUSAL_TEXT

_CITATION_LIKE_RE = re.compile(r"\[(S[^\]]*)\]")


class CitationLike(Protocol):
    citation_id: str
    block_id: UUID
    version_id: UUID
    document_id: UUID
    quote: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class CitationSource:
    block_id: UUID
    version_id: UUID
    document_id: UUID
    block_char_start: int
    block_char_end: int
    full_text: str


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "issues": list(self.issues)}


@dataclass(frozen=True)
class CitationValidityResult:
    valid: bool
    citation_count: int
    format_valid: bool
    references_match: bool
    objects_exist: bool
    quotes_match: bool
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["issues"] = list(self.issues)
        return payload


def evaluate_constraints(
    answer: str, constraints: Mapping[str, Any] | None
) -> RuleResult:
    """检查人工题目里的字面约束。

    M0 的约束值是字符串数组，不把它解释成正则，避免特殊字符改变规则语义。
    英文大小写不应导致误判，因此统一使用 casefold。
    """

    constraints = constraints or {}
    issues: list[str] = []
    folded_answer = answer.casefold()
    for key in ("must_include", "must_not_include"):
        values = constraints.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            issues.append(f"invalid_{key}")
            continue
        for value in values:
            if not value:
                issues.append(f"empty_{key}")
                continue
            present = value.casefold() in folded_answer
            if key == "must_include" and not present:
                issues.append(f"missing:{value}")
            if key == "must_not_include" and present:
                issues.append(f"forbidden:{value}")
    return RuleResult(passed=not issues, issues=tuple(issues))


def evaluate_citation_validity(
    *,
    answer: str,
    citations: Sequence[CitationLike],
    sources: Mapping[UUID, CitationSource],
    refused: bool,
) -> CitationValidityResult:
    """确定性校验引用结构，不评价证据是否在语义上支撑答案。"""

    issues: list[str] = []
    exact_labels = CITATION_RE.findall(answer)
    citation_like_labels = _CITATION_LIKE_RE.findall(answer)
    record_labels = [citation.citation_id for citation in citations]

    if refused:
        if answer.strip() != REFUSAL_TEXT:
            issues.append("refusal_text_mismatch")
        if exact_labels or citations:
            issues.append("refusal_has_citations")
    else:
        if answer.strip() == REFUSAL_TEXT:
            issues.append("refusal_flag_mismatch")
        if not exact_labels:
            issues.append("missing_citation")

    malformed = [
        label for label in citation_like_labels if not re.fullmatch(r"S[1-9]\d*", label)
    ]
    if malformed:
        issues.extend(f"malformed_label:{label}" for label in dict.fromkeys(malformed))

    if len(record_labels) != len(set(record_labels)):
        issues.append("duplicate_citation_record")
    if set(exact_labels) != set(record_labels):
        issues.append("answer_record_label_mismatch")

    object_issues: list[str] = []
    quote_issues: list[str] = []
    for citation in citations:
        if not re.fullmatch(r"S[1-9]\d*", citation.citation_id):
            issues.append(f"invalid_record_label:{citation.citation_id}")
        source = sources.get(citation.block_id)
        if source is None:
            object_issues.append(f"block_not_found:{citation.block_id}")
            continue
        if source.version_id != citation.version_id:
            object_issues.append(f"version_mismatch:{citation.citation_id}")
        if source.document_id != citation.document_id:
            object_issues.append(f"document_mismatch:{citation.citation_id}")
        offsets_valid = (
            0 <= citation.char_start < citation.char_end <= len(source.full_text)
            and source.block_char_start <= citation.char_start
            and citation.char_end <= source.block_char_end
        )
        if not offsets_valid:
            quote_issues.append(f"invalid_quote_range:{citation.citation_id}")
            continue
        if source.full_text[citation.char_start : citation.char_end] != citation.quote:
            quote_issues.append(f"quote_mismatch:{citation.citation_id}")

    issues.extend(object_issues)
    issues.extend(quote_issues)
    format_valid = not any(
        issue.startswith(("malformed_label:", "invalid_record_label:"))
        or issue
        in {
            "missing_citation",
            "refusal_text_mismatch",
            "refusal_has_citations",
            "refusal_flag_mismatch",
        }
        for issue in issues
    )
    references_match = not any(
        issue in {"duplicate_citation_record", "answer_record_label_mismatch"}
        for issue in issues
    )
    return CitationValidityResult(
        valid=not issues,
        citation_count=len(citations),
        format_valid=format_valid,
        references_match=references_match,
        objects_exist=not object_issues,
        quotes_match=not quote_issues,
        issues=tuple(issues),
    )
