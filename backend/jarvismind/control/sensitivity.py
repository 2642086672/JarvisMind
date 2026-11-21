"""One sensitivity decision for every surface an agent or an export can reach.

Two independent policies are combined, and either one flags a memory:

* ``core.personal_sensitivity``: health, home address, finance, sexual orientation,
  religion and politics (keyword and shape rules);
* ``core.sensitive.contains_sensitive``: credentials and regex-shaped personal data
  such as phone numbers and identity numbers.

A flagged memory is withheld from an agent that lacks the ``sensitive`` permission,
and it is redacted in exports unless the owner explicitly asks for sensitive text.
"""
from __future__ import annotations

import json
from typing import Optional

from ..core.personal_sensitivity import is_sensitive as personal_sensitive
from ..core.sensitive import contains_sensitive


def is_protected(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(personal_sensitive(text)) or bool(contains_sensitive(text))


IDENTIFIER_KEYS = frozenset({"id", "uuid"})


def _is_identifier_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in IDENTIFIER_KEYS or lowered.endswith(("_id", "_key"))


_HEX = frozenset("0123456789abcdef")


def _is_generated_identifier(text: str) -> bool:
    """True for the shapes the system generates: 32 hex digits, a UUID, or a lowercase
    prefix, an underscore and 16 hex digits. Anything else is checked as free text."""
    if len(text) == 32 and set(text) <= _HEX:
        return True
    parts = text.split("-")
    if len(text) == 36 and [len(part) for part in parts] == [8, 4, 4, 4, 12]:
        if set("".join(parts)) <= _HEX:
            return True
    prefix, underscore, tail = text.partition("_")
    return bool(underscore) and prefix.isalpha() and prefix.islower() and len(tail) == 16 and set(tail) <= _HEX


def metadata_is_protected(metadata_json: Optional[str]) -> bool:
    """True when a stored metadata document holds sensitive text.

    Keys, string values and numbers are all checked. A value under an identifier key
    (``*_id``, ``*_key``, ``id``, ``uuid``) is exempt only when it has exactly the shape of
    an identifier the system generates; any other value under such a key is checked like
    free text, so a phone number stored as an identifier is still held. Metadata that cannot
    be parsed is treated as protected text.
    """
    if not metadata_json:
        return False
    try:
        parsed = json.loads(metadata_json)
    except (TypeError, ValueError):
        return is_protected(str(metadata_json))
    stack: list = [(None, parsed)]
    while stack:
        key, current = stack.pop()
        if key is not None and is_protected(key):
            return True
        if isinstance(current, dict):
            stack.extend((str(child), item) for child, item in current.items())
        elif isinstance(current, list):
            stack.extend((None, item) for item in current)
        elif current is None or isinstance(current, bool):
            continue
        else:
            text = str(current)
            if key is not None and _is_identifier_key(key) and _is_generated_identifier(text):
                continue
            if is_protected(text):
                return True
    return False


def record_is_protected(
    content: Optional[str], summary: Optional[str], metadata_json: Optional[str]
) -> bool:
    """One decision for a whole memory: content, summary and metadata all count."""
    return is_protected(content) or is_protected(summary) or metadata_is_protected(metadata_json)
