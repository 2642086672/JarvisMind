"""Exit-side privacy governance for MCE-01.

The authority for credential/PII detection is the existing JMR stack:
``app.jmr.sensitive.evaluate`` and ``app.core.personal_sensitivity``. MCE does
not invent a second detector and does not lower an existing decision.

For this surface the policy is stricter than JMR's general proposal policy:
credentials (DENY), regex PII (HOLD) **and** personally-sensitive categories
are all withheld. A memory that merely *discusses* a sensitive topic may be
useful to a person, but injecting it into a third-party model without an
explicit consent step is not acceptable for cross-session reconstruction.

Logs carry a count, a stable reason code and a memory id. They never carry the
memory text, a matched value, a category name or a regex hit.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

from ..core.personal_sensitivity import categories as personal_categories
from ..jmr.sensitive import (
    DECISION_ALLOW,
    REASON_DETECTION_FAILED as JMR_DETECTION_FAILED,
    evaluate,
)

logger = logging.getLogger("jme.mce.governance")

MAX_METADATA_DEPTH = 6
MAX_METADATA_STRINGS = 100
MAX_METADATA_CHARS = 20_000

REASON_OK = "ok"
REASON_CREDENTIAL = "credential"
REASON_PII = "pii"
REASON_PERSONAL = "personal_sensitive"
REASON_INJECTION = "prompt_injection"
REASON_LIVE_SECRET = "live_secret"
REASON_DETECTION_FAILED = "detection_failed"
REASON_METADATA_INVALID = "metadata_invalid"
REASON_METADATA_SCAN_LIMIT = "metadata_scan_limit"


@dataclass(frozen=True)
class GovernanceDecision:
    allowed: bool
    reason: str = REASON_OK
    credential_hits: int = 0
    pii_hits: int = 0
    personal_hits: int = 0
    injection_hits: int = 0

    def as_counts(self) -> dict:
        """Value-free counters suitable for a response/log."""
        return {
            "credential": self.credential_hits,
            "pii": self.pii_hits,
            "personal": self.personal_hits,
            "injection": self.injection_hits,
        }


def _metadata_strings(value: Any) -> Tuple[Tuple[str, ...], bool]:
    """Walk a parsed metadata object and return bounded string atoms.

    Returns ``(strings, complete)``. ``complete=False`` means the object was
    deeper or larger than the scan budget, so the caller must fail closed.
    """
    out: list[str] = []
    total = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_METADATA_DEPTH:
            return tuple(out), False
        if isinstance(current, Mapping):
            for key, child in current.items():
                out.append(str(key))
                if len(out) > MAX_METADATA_STRINGS:
                    return tuple(out), False
                stack.append((child, depth + 1))
        elif isinstance(current, (list, tuple, set, frozenset)):
            for child in current:
                stack.append((child, depth + 1))
        elif isinstance(current, str):
            out.append(current)
            total += len(current)
            if total > MAX_METADATA_CHARS:
                return tuple(out), False
            if len(out) > MAX_METADATA_STRINGS:
                return tuple(out), False
        elif current is not None:
            out.append(str(current))
            if len(out) > MAX_METADATA_STRINGS:
                return tuple(out), False
    return tuple(out), True


class MceGovernance:
    """Fail-closed text/metadata gate. One instance is stateless and reusable."""

    def __init__(self, *, exact_tokens: Iterable[str] = ()) -> None:
        self._exact_tokens = tuple(
            token for token in exact_tokens if isinstance(token, str) and token
        )

    def inspect_text(self, text: Optional[str]) -> GovernanceDecision:
        if text is None or not str(text).strip():
            return GovernanceDecision(True)
        value = str(text)
        if any(token in value for token in self._exact_tokens):
            return GovernanceDecision(False, REASON_LIVE_SECRET)
        try:
            decision = evaluate(value, exact_tokens=self._exact_tokens)
        except Exception:  # noqa: BLE001 - fail closed, never leak on error
            return GovernanceDecision(False, REASON_DETECTION_FAILED)
        credential_hits = len(decision.credential_categories)
        pii_hits = len(decision.pii_categories)
        injection_hits = len(decision.injection_signals)
        if decision.decision != DECISION_ALLOW:
            if credential_hits:
                return GovernanceDecision(
                    False,
                    REASON_CREDENTIAL,
                    credential_hits=credential_hits,
                    pii_hits=pii_hits,
                    injection_hits=injection_hits,
                )
            if injection_hits:
                return GovernanceDecision(
                    False,
                    REASON_INJECTION,
                    credential_hits=credential_hits,
                    pii_hits=pii_hits,
                    injection_hits=injection_hits,
                )
            if decision.reason_code == JMR_DETECTION_FAILED:
                return GovernanceDecision(False, REASON_DETECTION_FAILED)
            return GovernanceDecision(
                False,
                REASON_PII,
                credential_hits=credential_hits,
                pii_hits=pii_hits,
                injection_hits=injection_hits,
            )
        try:
            personal_hits = len(personal_categories(value))
        except Exception:  # noqa: BLE001
            return GovernanceDecision(False, REASON_DETECTION_FAILED)
        if personal_hits:
            return GovernanceDecision(
                False,
                REASON_PERSONAL,
                credential_hits=credential_hits,
                pii_hits=pii_hits,
                personal_hits=personal_hits,
                injection_hits=injection_hits,
            )
        return GovernanceDecision(
            True,
            credential_hits=credential_hits,
            pii_hits=pii_hits,
            personal_hits=personal_hits,
            injection_hits=injection_hits,
        )

    def inspect_metadata(self, metadata: Any) -> GovernanceDecision:
        if metadata is None:
            return GovernanceDecision(True)
        if not isinstance(metadata, Mapping):
            return GovernanceDecision(False, REASON_METADATA_INVALID)
        strings, complete = _metadata_strings(metadata)
        if not complete:
            return GovernanceDecision(False, REASON_METADATA_SCAN_LIMIT)
        for atom in strings:
            decision = self.inspect_text(atom)
            if not decision.allowed:
                return decision
        return GovernanceDecision(True)

    def inspect_memory(
        self,
        *,
        content: Optional[str],
        summary: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> GovernanceDecision:
        """Inspect every exit-visible part of one memory, fail closed."""
        for text in (content, summary):
            decision = self.inspect_text(text)
            if not decision.allowed:
                return decision
        return self.inspect_metadata(metadata)


def metadata_digest(metadata: Optional[Mapping[str, Any]]) -> str:
    """Small non-content fingerprint for logs/tests (never the raw metadata)."""
    if not metadata:
        return "-"
    try:
        raw = json.dumps(metadata, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "invalid"
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def log_rejection(memory_id: str, decision: GovernanceDecision) -> None:
    """Log a rejection without content, categories or matched values."""
    logger.info(
        "mce memory withheld: reason=%s memory=%s",
        decision.reason,
        (memory_id or "")[:12],
    )


__all__ = [
    "GovernanceDecision",
    "MceGovernance",
    "REASON_CREDENTIAL",
    "REASON_DETECTION_FAILED",
    "REASON_INJECTION",
    "REASON_LIVE_SECRET",
    "REASON_METADATA_INVALID",
    "REASON_METADATA_SCAN_LIMIT",
    "REASON_OK",
    "REASON_PERSONAL",
    "REASON_PII",
    "log_rejection",
    "metadata_digest",
]
