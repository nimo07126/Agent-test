from __future__ import annotations

import re
from typing import Any

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"reveal\s+(all\s+)?(secrets|credentials|tokens)", re.IGNORECASE),
    re.compile(r"export\s+.*(secret|credential|token)", re.IGNORECASE),
    re.compile(r"忽略.*(之前|上面).*指令"),
    re.compile(r"泄露.*(机密|凭证|token|令牌)"),
    re.compile(r"绕过.*权限"),
]

SENSITIVE_KEYS = {
    "vendor_secret",
    "unit_cost_usd",
    "debug",
    "candidate_note",
    "secret",
    "secrets",
    "credential",
    "credentials",
    "token",
    "tokens",
}

SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"ACME-TIER-2-REBATE", re.IGNORECASE),
    re.compile(r"BETA-PRICE-FLOOR", re.IGNORECASE),
    re.compile(r"泄露全部供应商机密"),
]


def detect_prompt_injection(text: str) -> list[str]:
    """返回命中的提示词注入模式。

    TODO(candidate/P1): 将该防护接入任务创建和工具执行路径。
    """
    return [pattern.pattern for pattern in PROMPT_INJECTION_PATTERNS if pattern.search(text)]


def scrub_untrusted_text(text: str) -> str:
    """Remove instruction-like or secret-like fragments from untrusted content."""
    cleaned = text
    for pattern in PROMPT_INJECTION_PATTERNS:
        cleaned = pattern.sub("[redacted-instruction]", cleaned)
    for pattern in SENSITIVE_VALUE_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned


def redact_sensitive(value: Any) -> Any:
    """Recursively redact sensitive keys and known secret values."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in SENSITIVE_KEYS or any(token in key_lower for token in ("secret", "credential", "token")):
                continue
            redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return scrub_untrusted_text(value)
    return value
