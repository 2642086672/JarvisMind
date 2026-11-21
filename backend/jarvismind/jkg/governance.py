"""Read-time redaction for JKG payloads (JKG-INT-03).

Why this exists
---------------
The graph is *derived* data, so a sensitive label can arrive there before any
gate saw it: an extractor may put a token-shaped string into an entity name, and
``metadata`` is free-form JSON that can carry anything. The write path's
validation is not a privacy control (it checks shape, not secrecy), so the HTTP
exit re-grades every payload with the **same** detector JMR uses and withholds
what grades badly. This is defence in depth: it does not replace write-time
filtering, and it adds no new privacy rule — ``evaluate()`` is the single source
of truth.

Pure functions, no I/O
----------------------
Nothing here opens a database, a file or a socket. A payload in, a payload out,
so the router can call it on every item without a performance or failure-mode
surprise, and tests can drive it with plain dicts.

Decision → shape
----------------
``DENY`` (a credential was detected, or the detector failed closed)
    The label *is* the secret. ``name`` / ``display_name`` / ``canonical_name``
    and the derived ``normalized_key`` are replaced with a fixed placeholder and
    ``metadata`` becomes ``None``. ``name_redacted`` / ``metadata_redacted`` say
    so, and the returned dict never contains the matched text.

``HOLD`` (personal data)
    The label is kept — a person's name is the useful part of the entity — but
    ``metadata`` is dropped, because free-form JSON is the field most likely to
    smuggle a raw value. ``sensitivity`` is ``SENSITIVE`` so the UI marks it.

``ALLOW``
    Returned byte-for-byte unchanged with ``sensitivity`` ``PUBLIC``.

Why ``normalized_key`` is redacted too
--------------------------------------
It is the lower-cased, whitespace-folded form of ``name``. Redacting ``name``
while leaving ``normalized_key`` would hand the caller the same string in a
different case, which defeats the point.

Import style
------------
``..jmr.sensitive`` is a direct submodule import. It is deliberately *not* a
function-body import: the module graph has no cycle (``app.jmr.sensitive``
imports only ``app.core`` and ``app.jmr`` leaf modules, never ``app.jkg``), and
stating that here makes a future cycle a visible failure instead of a silent
one. ``import jarvismind.jkg.governance`` is exercised by the startup-safety suite.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..jmr.sensitive import (
    DECISION_ALLOW,
    DECISION_DENY,
    DECISION_HOLD,
    LEVEL_CREDENTIAL,
    LEVEL_PUBLIC,
    LEVEL_SENSITIVE,
    PrivacyDecision,
    evaluate,
)

# Fixed placeholder. Deliberately not a truncated form of the original: a prefix
# or a character count of a credential is itself a leak, and "[REDACTED]" cannot
# be mistaken for real content the way "api_…" could.
REDACTED_PLACEHOLDER = "[REDACTED]"

# Node label fields that must be replaced together. ``normalized_key`` is
# derived from ``name`` and therefore leaks it, so it is in the same group.
_NODE_LABEL_FIELDS = ("name", "display_name", "canonical_name", "normalized_key")

_SENSITIVITY_BY_LEVEL = {
    LEVEL_PUBLIC: LEVEL_PUBLIC,
    LEVEL_SENSITIVE: LEVEL_SENSITIVE,
    LEVEL_CREDENTIAL: LEVEL_CREDENTIAL,
}

# Upper bound on how much metadata text is graded. ``normalization`` already
# caps stored metadata at 16 KiB; this keeps a hand-built payload from turning
# the regex pass into a hot spot. Truncation cannot hide a credential in
# practice — patterns are short and a 16 KiB prefix covers any real value — but
# the behaviour is stated so it is not a silent surprise.
_MAX_GRADED_METADATA_CHARS = 20_000


def _metadata_text(metadata: Any) -> str:
    """Render metadata as text so ``evaluate()`` can see inside it.

    Keys are included as well as values: a shape like ``{"api_key": "..."}``
    leaks its meaning through the key even when the value is blank.
    """
    if metadata is None:
        return ""
    if isinstance(metadata, str):
        return metadata[:_MAX_GRADED_METADATA_CHARS]
    try:
        # ``default=str`` so a date or a Decimal does not raise; a serialisation
        # failure must degrade to "grade what we could", never to "skip grading".
        rendered = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(metadata)
    return rendered[:_MAX_GRADED_METADATA_CHARS]


def _classify(*parts: Optional[str]) -> PrivacyDecision:
    """Grade the concatenation of every text part that matters.

    One call over the joined text (rather than one call per field, then a
    hand-written merge) keeps the decision identical to what the detector would
    say about the payload as a whole. ``evaluate`` itself is fail-closed on
    internal error, so a malformed payload grades ``DENY`` rather than escaping.
    """
    text = "\n".join(part for part in parts if part)
    return evaluate(text)


def _node_sensitivity(decision: PrivacyDecision) -> str:
    return _SENSITIVITY_BY_LEVEL.get(decision.level, LEVEL_CREDENTIAL)


def sanitize_node_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of a node payload with sensitive fields withheld.

    The input dict is never mutated: ``node_payload()`` returns a fresh dict per
    call today, but a future cache would make in-place redaction a leak into the
    next response.
    """
    sanitized = dict(payload)
    decision = _classify(
        payload.get("name"),
        payload.get("display_name"),
        payload.get("canonical_name"),
        payload.get("entity_type"),
        _metadata_text(payload.get("metadata")),
    )

    if decision.decision == DECISION_DENY:
        # The label itself is the secret. Replace every form of it, and drop the
        # metadata wholesale rather than filtering individual keys — a filter is
        # a guess about which key carries the value.
        for field in _NODE_LABEL_FIELDS:
            if field in sanitized:
                sanitized[field] = REDACTED_PLACEHOLDER
        sanitized["metadata"] = None
        sanitized["name_redacted"] = True
        sanitized["metadata_redacted"] = True
    elif decision.decision == DECISION_HOLD:
        # Keep the label (it is the useful part of an entity), drop metadata.
        sanitized["metadata"] = None
        sanitized["name_redacted"] = False
        sanitized["metadata_redacted"] = True
    elif decision.decision == DECISION_ALLOW:
        sanitized["name_redacted"] = False
        sanitized["metadata_redacted"] = False
    else:  # pragma: no cover - evaluate() only returns the three decisions
        # Unknown decision: treat as DENY. Fail closed rather than guess.
        for field in _NODE_LABEL_FIELDS:
            if field in sanitized:
                sanitized[field] = REDACTED_PLACEHOLDER
        sanitized["metadata"] = None
        sanitized["name_redacted"] = True
        sanitized["metadata_redacted"] = True

    sanitized["sensitivity"] = _node_sensitivity(decision)
    return sanitized


def _sanitize_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """Pass a source through unchanged.

    A source row holds ids, a lifecycle status and invalidation metadata — no
    memory body, no label. There is nothing to redact, and rewriting it would
    make the provenance harder to trust. Kept as an explicit function (not an
    inline no-op) so the omission reads as a decision.
    """
    return dict(source)


def sanitize_edge_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of an edge payload with sensitive fields withheld.

    The predicate is a closed enum and the endpoint fields are ids, so the only
    free-text surface is ``metadata``. ``sources`` are copied verbatim: they
    carry no memory content (see ``serialization.source_payload``).
    """
    sanitized = dict(payload)
    decision = _classify(
        payload.get("predicate"),
        payload.get("relation_type"),
        _metadata_text(payload.get("metadata")),
    )

    # There is no label to blank on an edge, so a DENY and a HOLD both resolve to
    # "drop the metadata". The site is graded separately rather than pretending a
    # relation label was redacted when none existed.
    if decision.decision in (DECISION_DENY, DECISION_HOLD):
        sanitized["metadata"] = None
        sanitized["metadata_redacted"] = True
    elif decision.decision == DECISION_ALLOW:
        sanitized["metadata_redacted"] = False
    else:  # pragma: no cover - fail closed on an unexpected decision
        sanitized["metadata"] = None
        sanitized["metadata_redacted"] = True

    sources: Optional[List[Any]] = payload.get("sources")
    if sources is not None:
        sanitized["sources"] = [
            _sanitize_source(source) if isinstance(source, dict) else source
            for source in sources
        ]

    sanitized["sensitivity"] = _node_sensitivity(decision)
    return sanitized


__all__ = [
    "REDACTED_PLACEHOLDER",
    "sanitize_node_payload",
    "sanitize_edge_payload",
]
