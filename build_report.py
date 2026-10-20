"""Safe build-log redaction helpers for user-facing diagnostics."""
from __future__ import annotations

import re


def _redact_url_query(match: re.Match[str]) -> str:
    """Keep URL host/path while removing query values that may contain credentials."""
    raw = match.group(1)
    head, query = raw.split("?", 1)
    keys = []
    for part in query.split("&"):
        key = part.split("=", 1)[0]
        if key:
            keys.append(f"{key}=[REDACTED]")
    return head + ("?" + "&".join(keys) if keys else "")


_PATTERNS = [
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://[^\s?#]+\?[^\s#]*)"), _redact_url_query),
]


def redact_sensitive_text(text: str, max_chars: int | None = None) -> str:
    """Redact common credentials before showing logs to Telegram users."""
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    if max_chars is not None and len(result) > max_chars:
        result = result[-max_chars:]
    return result
