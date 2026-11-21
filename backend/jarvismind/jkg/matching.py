"""Conservative, deterministic entity match keys for JKG (JKG-INT-05 / A1).

What this is
------------
A *secondary* key used only to find an existing node that the exact dedupe key
``normalized_key`` would miss, e.g. ``JarvisMind`` and ``JarvisMind 项目`` naming
the same project. It is never stored in place of ``normalized_key`` and it never
changes the uniqueness rule of ``jkg_nodes``. It lives in node metadata under
``{"jkg": {"match_key": ...}}``.

Rules are per entity type, and the guiding principle is **不确定不合并**
(when unsure, do not merge). A wrong merge puts two people or two events on one
node, which is worse than a missed merge:

* PERSON: NFKC + casefold + whitespace collapse only. Internal spaces, hyphens
  and underscores are kept, so ``Alice Smith`` and ``Alice-Smith`` stay apart.
  No suffix is stripped.
* PROJECT: additionally removes every whitespace, ``-``, ``_`` and ``·`` and a
  trailing ``项目``, so ``JarvisMind`` / ``Jarvis Mind`` / ``JarvisMind 项目`` agree.
  Project names are the place where separator and suffix variants are common
  and rarely carry a different meaning.
* EVENT: NFKC + casefold + whitespace collapse only, like PERSON. Event titles
  differ in word boundaries in ways that can change meaning, so no separator is
  removed.

No fuzzy matching and no edit distance: two strings either produce the same key
under these rules or they do not.
"""
from __future__ import annotations

import re
import unicodedata

from ..core.errors import ValidationError
from .normalization import (
    MAX_NORMALIZED_KEY_LENGTH,
    canonicalize_entity_name,
    normalize_entity_name,
    validate_entity_type,
)

_PROJECT_SUFFIX = "项目"
_PROJECT_SEPARATORS = re.compile(r"[\s\-_·]+")


def _fold(value: str) -> str:
    """NFKC, casefold, collapse whitespace. Keeps every visible character."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(folded.split())


def entity_match_key(entity_type: str, canonical_name: str) -> str:
    """Return the secondary match key for one entity label.

    Raises ``ValidationError`` for an unsupported type, a non-string or empty
    name, a name longer than the normalization bound, or a key that exceeds the
    normalized-key bound.
    """
    entity = validate_entity_type(entity_type)
    canonical = canonicalize_entity_name(canonical_name)
    key = _fold(canonical)
    if entity == "PROJECT":
        key = _PROJECT_SEPARATORS.sub("", key)
        if key.endswith(_PROJECT_SUFFIX):
            key = key[: -len(_PROJECT_SUFFIX)]
    if not key:
        key = normalize_entity_name(canonical)
    if len(key) > MAX_NORMALIZED_KEY_LENGTH:
        raise ValidationError(
            f"Match key exceeds {MAX_NORMALIZED_KEY_LENGTH} characters"
        )
    return key


__all__ = ["entity_match_key"]
