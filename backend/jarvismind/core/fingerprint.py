"""003-B1: keyed content fingerprint for approval integrity (NOT a plain hash).

P1 security-closure fix (independent review P2 finding)
-------------------------------------------------------
The original implementation derived the HMAC key from ``Settings.api_token``.
That token is the SAME secret the client sends in ``X-API-Token``, so any token
holder could recompute ``content_fingerprint()`` for tampered candidate content
and defeat the stale-approval check. The key now comes from the SERVER-ONLY P1
key (``app.governance.key_material.load_grant_key``) with its own
domain-separation context; ``KEY_SCHEME`` is bumped to v2 so a v1 fingerprint
can never be confused with a v2 one.

Properties:

* The key never leaves process memory; it is never logged, persisted to SQLite,
  returned by an API, or hard-coded.
* A missing/short/corrupt key is FAIL-CLOSED (``GrantKeyError``).
* This is still a CONTENT fingerprint, not a SignedGrant signature; it uses a
  different domain-separation label than either P1 principal or grant MACs.
* Rotating the P1 grant key invalidates existing fingerprints (fail-closed:
  stale approvals are refused, not silently accepted). Key rotation therefore
  requires re-scanning pending candidates.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from ..config import Settings

# Domain-separation label, NOT a secret. Distinct from
# jme/p1-trusted-principal/v1 and jme/p1-signed-grant/v1.
_FINGERPRINT_CONTEXT = b"jme/content-fingerprint/v2"
KEY_SCHEME = "server-key-derived-hmac-sha256-v2"


class FingerprintKeyError(RuntimeError):
    """Legacy alias kept for import compatibility; key errors now raise
    ``GrantKeyError`` (a ForbiddenError) from key_material."""


def derive_fingerprint_key(settings: Settings) -> bytes:
    # In-function import avoids a package-level cycle: app.governance.__init__
    # imports write_governor -> schemas.memory -> core.security_gate.
    from ..governance.key_material import GrantKeyError, load_grant_key

    try:
        key = load_grant_key(settings)
    except GrantKeyError as exc:
        # Preserve the 003-B1 contract: a missing server secret must raise
        # FingerprintKeyError (fail-closed), never fall back to a plain digest.
        raise FingerprintKeyError("Server signing key is unavailable") from exc
    return hmac.new(key, _FINGERPRINT_CONTEXT, hashlib.sha256).digest()


def content_fingerprint(
    settings: Settings,
    *,
    content: Optional[str],
    summary: Optional[str] = None,
    metadata_json: Optional[str] = None,
    decision_version: int = 1,
) -> str:
    """HMAC-SHA256 over the exact fields that will be written as a memory.

    ``metadata_json`` must already be the canonical (sorted-key) serialization
    that is persisted, so a metadata change invalidates the approval basis too.
    """
    key = derive_fingerprint_key(settings)
    payload = "\x1f".join(
        (
            KEY_SCHEME,
            str(decision_version),
            content or "",
            summary or "",
            metadata_json or "",
        )
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
