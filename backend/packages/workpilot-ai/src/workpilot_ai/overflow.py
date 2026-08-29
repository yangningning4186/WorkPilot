"""Provider-independent context-overflow classification.

OpenAI-compatible endpoints disagree on both status codes and error wording.  Keep the
experience-derived surface in one reviewed module, with explicit false-positive exclusions,
instead of letting each adapter grow a shorter private substring list.
"""

from __future__ import annotations

import re

from workpilot_ai.types import CompletionResult

_OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"context[_ -]?length[_ -]?exceeded",
        r"maximum context length",
        r"max(?:imum)? context (?:length|window)",
        r"context window(?: size)?(?: is)?(?: only)?",
        r"context size exceeded",
        r"context overflow",
        r"context limit exceeded",
        r"model context protocol.*too long",
        r"max_model_len",
        r"maximum model length",
        r"prompt is too long",
        r"input is too long",
        r"input length .{0,80} exceeds",
        r"prompt length .{0,80} exceeds",
        r"token count .{0,80} exceeds",
        r"too many input tokens",
        r"too many prompt tokens",
        r"request has too many tokens",
        r"maximum number of tokens allowed",
        r"maximum sequence length",
        r"sequence length .{0,80} exceeds",
        r"prompt tokens .{0,80} limit",
        r"reduce the length of (?:the )?(?:messages|prompt|input)",
        r"please reduce your prompt",
        r"request (?:body )?too large.{0,80}(?:token|context|prompt)",
    )
)

# These messages contain tempting words such as "too many tokens" but describe capacity,
# billing or throughput rather than the prompt context window.
_NON_OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"throttlingexception",
        r"rate.?limit",
        r"tokens per (?:minute|second|day)",
        r"requests per (?:minute|second|day)",
        r"too many concurrent",
        r"insufficient[_ -]?quota",
        r"quota (?:exhausted|exceeded)",
        r"billing",
        r"credit balance",
    )
)


def is_context_overflow_response(*, status_code: int, body: str) -> bool:
    if status_code not in {400, 413, 422}:
        return False
    if any(pattern.search(body) for pattern in _NON_OVERFLOW_PATTERNS):
        return False
    return any(pattern.search(body) for pattern in _OVERFLOW_PATTERNS)


def silent_context_overflow(
    result: CompletionResult,
    *,
    context_window_tokens: int,
) -> str | None:
    """Detect endpoints that return a nominally successful but unusable completion."""

    input_tokens = result.usage.input_tokens
    if input_tokens > context_window_tokens:
        return (
            f"provider reported input_tokens={input_tokens}, above configured "
            f"context_window_tokens={context_window_tokens}"
        )
    if (
        result.stop_reason == "length"
        and result.usage.output_tokens == 0
        and input_tokens >= context_window_tokens
    ):
        return (
            "provider filled the context window, returned stop_reason=length and produced "
            "zero output tokens"
        )
    return None


__all__ = ["is_context_overflow_response", "silent_context_overflow"]
