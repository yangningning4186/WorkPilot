"""持久化外部结果前的通用凭证脱敏边界。"""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|secret|password|passwd|credential|cookie|set[_-]?cookie)(?:$|[_-])"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|credential|cookie)\s*([:=])\s*"
    r"(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_TOKEN = re.compile(
    r"\b(?:sk|pk|ghp|github_pat|xox[baprs]|AKIA|ASIA)[-_][A-Za-z0-9_-]{8,}\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_persisted_tool_value(value: Any) -> Any:
    """递归移除常见 header/token/key；保留 JSON 形状供安全重放。"""

    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if _SECRET_KEY.search(str(key)) else redact_persisted_tool_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_persisted_tool_value(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = _PRIVATE_KEY.sub("<redacted-private-key>", value)
    redacted = _BEARER.sub("Bearer <redacted>", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted
    )
    redacted = _JWT.sub("<redacted-jwt>", redacted)
    return _PROVIDER_TOKEN.sub("<redacted-token>", redacted)


__all__ = ["redact_persisted_tool_value"]
