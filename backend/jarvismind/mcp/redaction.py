"""Credential redaction and defensive field stripping.

Every outbound JSON-RPC payload and every stderr line passes through these
helpers.  The patterns are intentionally broad enough to catch the two token
shapes issued by JarvisMind while staying dependency-free.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED_TOKEN]"

_TOKEN_PATTERNS = (
    re.compile(r"jmt_[0-9a-f]{8}_[A-Za-z0-9_-]{8,}"),
    re.compile(r"jmt_[A-Za-z0-9_-]{20,}"),
)

#: Fields that must never reach an MCP client, even if an upstream response
#: grows them again in a later version.
SECRET_FIELDS = frozenset({"confirmation_token", "plaintext_token"})


def redact(text: str) -> str:
    """Replace any agent-token-shaped substring with ``[REDACTED_TOKEN]``."""
    if not isinstance(text, str):
        text = str(text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts, lists and tuples."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            (redact(key) if isinstance(key, str) else key): redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def strip_secret_fields(value: Any) -> Any:
    """Recursively drop credential fields from a JSON-like structure."""
    if isinstance(value, dict):
        return {
            key: strip_secret_fields(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.lower() in SECRET_FIELDS)
        }
    if isinstance(value, list):
        return [strip_secret_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_secret_fields(item) for item in value)
    return value