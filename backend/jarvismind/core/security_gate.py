"""003-B: server-side security gate for all long-term memory writes.

This module is the *only* mandatory checkpoint for credential BLOCK (D3).
It is deliberately dependency-free and deterministic:

* credentials (API keys, tokens, passwords, OTPs, private keys, DB URIs, ...)
  are permanently blocked and never enter an approval flow;
* sensitive personal information (PII) is classified separately and can only
  proceed through the explicit dual authorization gate;
* no original matched value, digest, or reversible representation is ever
  logged, returned, or persisted.

Rule sets cannot guarantee detection of every secret (custom formats,
encoded/split values, natural-language passphrases). The gate is a defense
layer, not a completeness claim.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .errors import ValidationError
from .sensitive import (
    CREDENTIAL_CATEGORIES,
    SensitiveHit,
    credential_categories,
    pii_categories,
    scan,
)

RULESET_VERSION = "jme-secgate-1"

BLOCK_MESSAGE = "Credential content is permanently blocked from long-term memory."
PII_MESSAGE = "Sensitive personal information requires explicit dual authorization."

# 003-B1: authorization truthfulness. 003-C is the first phase that can
# produce a real, server-verified grant; until then EVERY approval must stay
# NOT_VERIFIED. No default value and no exception branch may write PASSED.
AUTH_NOT_VERIFIED = "NOT_VERIFIED"
AUTH_FAILED = "FAILED"
AUTH_PASSED = "PASSED"  # reserved for 003-C; never emitted by 003-B1 code.

# Recursive metadata inspection limits. A structure we cannot fully inspect
# is rejected (fail-closed) instead of being written unchecked.
MAX_METADATA_DEPTH = 8
MAX_METADATA_NODES = 1000
MIN_EXACT_TOKEN_LENGTH = 4


class CredentialBlockedError(ValidationError):
    """Raised when any write path attempts to persist a credential."""

    def __init__(self, metadata: dict) -> None:
        # metadata contains only categories/counts/lengths - never the value.
        super().__init__(
            BLOCK_MESSAGE,
            detail={
                "code": "credential_blocked",
                "ruleset_version": RULESET_VERSION,
                "categories": metadata.get("categories", []),
                "hit_count": metadata.get("hit_count", 0),
                **({"field": metadata["field"]} if metadata.get("field") else {}),
            },
        )


class UnsafeMetadataError(ValidationError):
    """Fail-closed: metadata contains a shape we cannot exhaustively scan."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "Metadata cannot be safely scanned; write refused",
            detail={"code": "unsafe_metadata", "reason": reason},
        )


class BlockedFieldError(ValueError):
    """Pydantic-compatible rejection; message never contains the value."""


def validate_client_value(value: Any, *, field: str) -> None:
    """Schema-stage check for client-controlled fields (003-B1 P0-2).

    Raises ValueError so pydantic turns it into a 422 BEFORE any persistence.
    Structured values are walked recursively; unscannable shapes fail closed.
    """
    if value is None:
        return
    try:
        texts = list(iter_metadata_strings(value))
    except UnsafeMetadataError as exc:
        raise BlockedFieldError(
            f"field '{field}' cannot be safely scanned; request refused"
        ) from exc
    for text in texts:
        if scan_credentials(text):
            raise BlockedFieldError(
                f"field '{field}' contains blocked credential content"
            )


class PiiAuthorizationRequiredError(ValidationError):
    """Raised when a PII candidate is approved without the required grants."""

    def __init__(self, required_grants: Sequence[str]) -> None:
        super().__init__(
            PII_MESSAGE,
            detail={
                "code": "pii_dual_authorization_required",
                "required_grants": list(required_grants),
            },
        )


def scan_credentials(
    text: Optional[str], *, exact_tokens: Iterable[str] = ()
) -> List[SensitiveHit]:
    """Return credential hits only; PII hits are intentionally excluded.

    ``exact_tokens`` covers the *live* server secrets (JME API token / LLM key):
    a token whose shape matches no regular expression is still a credential.
    ``redact()`` replacing it with ``[REDACTED]`` is NOT a reason to treat the
    unit as safe - the caller must BLOCK it before any persistence.
    """
    if not text:
        return []
    hits = [hit for hit in scan(text) if hit.category in CREDENTIAL_CATEGORIES]
    claimed = [(hit.start, hit.end) for hit in hits]
    for token in exact_tokens:
        if not token or len(token) < MIN_EXACT_TOKEN_LENGTH:
            continue
        index = text.find(token)
        while index != -1:
            span = (index, index + len(token))
            if not any(span[0] < end and start < span[1] for start, end in claimed):
                hits.append(SensitiveHit("api_token", span[0], span[1], token))
                claimed.append(span)
            index = text.find(token, index + len(token))
    hits.sort(key=lambda hit: hit.start)
    return hits


def safe_hit_metadata(hits: Iterable[SensitiveHit]) -> dict:
    """Serialize only non-sensitive metadata (no values, no digests)."""
    materialized = list(hits)
    return {
        "categories": sorted({hit.category for hit in materialized}),
        "hit_count": len(materialized),
        "max_hit_length": max((hit.end - hit.start for hit in materialized), default=0),
    }


def iter_metadata_strings(value: Any) -> Iterable[str]:
    """Walk every string in an arbitrary metadata object (003-B1 P0-1).

    Dict keys AND values are scanned. Anything we cannot exhaustively inspect -
    unknown object types, cycles, excessive depth/size, undecodable bytes -
    raises UnsafeMetadataError so the write is refused rather than partially
    checked. Complexity: O(nodes), bounded by MAX_METADATA_NODES.
    """
    nodes = 0
    # Explicit stack keeps the walker iterative (no recursion limit surprises).
    stack: List[Tuple[Any, int, Optional[set]]] = [(value, 0, None)]
    while stack:
        current, depth, ancestors = stack.pop()
        nodes += 1
        if nodes > MAX_METADATA_NODES:
            raise UnsafeMetadataError("metadata structure too large")
        if depth > MAX_METADATA_DEPTH:
            raise UnsafeMetadataError("metadata nesting too deep")
        if current is None or isinstance(current, (bool, int, float)):
            continue
        if isinstance(current, str):
            yield current
            continue
        if isinstance(current, (bytes, bytearray)):
            try:
                yield bytes(current).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UnsafeMetadataError("undecodable metadata bytes") from exc
            continue
        if isinstance(current, (dict, list, tuple, set, frozenset)):
            marker = id(current)
            lineage = set(ancestors or ())
            if marker in lineage:
                raise UnsafeMetadataError("cyclic metadata reference")
            lineage.add(marker)
            if isinstance(current, dict):
                for key, item in current.items():
                    stack.append((key, depth + 1, lineage))
                    stack.append((item, depth + 1, lineage))
            else:
                for item in current:
                    stack.append((item, depth + 1, lineage))
            continue
        raise UnsafeMetadataError(f"unsupported metadata type {type(current).__name__}")


def assert_writable(
    content: Optional[str],
    *,
    summary: Optional[str] = None,
    metadata: Any = None,
    metadata_text: Optional[str] = None,
    exact_tokens: Iterable[str] = (),
    field: Optional[str] = None,
) -> None:
    """Mandatory checkpoint before ANY formal memory write (003-B1).

    Scans content, summary, an already-serialized metadata_json string, AND
    every string inside a structured metadata object. ``exact_tokens`` adds
    live-secret matching. A hit raises CredentialBlockedError carrying only
    de-identified metadata (categories / counts / lengths) - never the value.
    """
    hits: List[SensitiveHit] = []
    for part in (content, summary, metadata_text):
        hits.extend(scan_credentials(part, exact_tokens=exact_tokens))
    if metadata is not None:
        for text in iter_metadata_strings(metadata):
            hits.extend(scan_credentials(text, exact_tokens=exact_tokens))
    if hits:
        info = safe_hit_metadata(hits)
        if field:
            info["field"] = field
        raise CredentialBlockedError(info)


# "key-name = value" rules are the ones that collide with legitimate
# identifiers (e.g. a conversation id ending in "api_key:"). Schema-level
# validation already applies the FULL rule set to raw client fields; the
# event-repository layer re-checks server-derived identifiers with the strong
# shape rules so a real secret still cannot be persisted.
IDENTIFIER_SCAN_EXCLUSIONS = frozenset({"credential_assignment", "generic_token"})


def assert_safe_text(value: Optional[str], *, field: str) -> None:
    """Scan a CLIENT-CONTROLLED value with the FULL rule set (003-B1 P0-2)."""
    hits = scan_credentials(value)
    if hits:
        info = safe_hit_metadata(hits)
        info["field"] = field
        raise CredentialBlockedError(info)


def assert_safe_identifier(
    value: Optional[str], *, field: str, strict: bool = False
) -> None:
    """Scan a (possibly server-derived) identifier with strong shape rules.

    A hit refuses the write BEFORE any INSERT, so the blocked original can
    never be stored in block_events / extraction_events / approval_events.

    ``strict=True`` also applies the ``credential_assignment`` / ``generic_token``
    rules (``key=value`` shapes such as ``api_key=abcd1234efgh5678``). Legacy
    callers keep the permissive default because some server-derived identifiers
    may legitimately contain those key names; JMR task/run/batch/dedup
    identifiers are caller-authored and are scanned strictly so a credential
    pasted into an id cannot be persisted.
    """
    hits = [
        hit
        for hit in scan_credentials(value)
        if strict or hit.category not in IDENTIFIER_SCAN_EXCLUSIONS
    ]
    if hits:
        info = safe_hit_metadata(hits)
        info["field"] = field
        raise CredentialBlockedError(info)


def classify_candidate(
    content: Optional[str],
    *,
    original_credential_flags: Sequence[str] = (),
    summary: Optional[str] = None,
) -> Tuple[bool, List[str], List[str]]:
    """Classify one extraction unit.

    Returns ``(blocked, credential_categories, pii_categories)``. The
    ``original_credential_flags`` carry hits from the *pre-redaction* source
    statement, so a credential that has already been replaced by ``[REDACTED]``
    cannot slip past the gate (003-B P0-1).
    """
    cred = set(credential_categories(content))
    cred.update(credential_categories(summary))
    cred.update(str(flag) for flag in original_credential_flags if flag)
    pii = pii_categories(content)
    return (bool(cred), sorted(cred), pii)


def required_grants_for(pii: Sequence[str], credential: Sequence[str]) -> List[str]:
    """Authorization set: credentials have none (permanent BLOCK)."""
    if credential:
        return []
    if pii:
        return ["USER_APPROVAL", "SENSITIVE_PII"]
    return ["USER_APPROVAL"]
