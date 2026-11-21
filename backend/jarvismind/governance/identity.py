"""003-C done for real: server-verified identities and cryptographically signed grants.

Current status of the pre-existing 003-C placeholder (verified against source):

* ``migrations.py:432`` defines the ``grants`` table (signature / key_version /
  nonce / scope_json / status) but NOTHING ever issued, verified, or consumed a
  row from it before P1-01.
* ``security_gate.py:40`` says ``AUTH_PASSED`` is reserved and never emitted.
* ``fingerprint.py:14`` explicitly states it is a CONTENT fingerprint, not a
  Grant signature, and that 003-C must define an independent key lifecycle.
* ``approval_service.py:48,103`` ``SERVER_ACTOR`` / ``actor_label`` / the
  ``required_grants_json`` text are labels and claims, never identities.

P1-01 supplies the missing cryptographic layer:

* A distinct domain-separated HMAC key (not the content-fingerprint key).
* ``key_version`` for rotation; a mismatch invalidates every old grant.
* ``nonce`` (unique in ``grants``) + one-shot ``CONSUMED`` status for replay
  protection.
* ``expires_at`` with a configurable TTL; expired grants are refused.
* The whole payload is covered by the signature, so changing subject,
  operation, object, grants, task_id, run_id, or grant_id invalidates it.

Failure is fail-closed: an empty server secret raises instead of silently
falling back to an unkeyed digest. Complexity: O(1) per issue/verify.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

from sqlalchemy import text

from .config import GovernanceConfig
from .errors import GrantVerificationError, UntrustedIdentityError
from .key_material import (
    GrantKeyError,
    grant_context,
    load_grant_key,
    principal_context,
    subkey_for,
)

# P0-1 fix: the signing key is server-only (key_material.load_grant_key), NOT
# derived from Settings.api_token. These labels only separate two subkeys of
# that server-only secret; they are not secrets themselves.
_PRINCIPAL_CONTEXT = principal_context()
_GRANT_CONTEXT = grant_context()
KEY_SCHEME = "p1-hmac-sha256-v2"  # v2 = server-only key; v1 grants are invalid

GRANT_ISSUED = "ISSUED"
GRANT_CONSUMED = "CONSUMED"
GRANT_REVOKED = "REVOKED"

# Capability names. A principal never receives a capability from a client
# string; the server decides the tuple at issue time.
CAP_MEMORY_READ = "MEMORY_READ"
CAP_MEMORY_WRITE = "MEMORY_WRITE"
CAP_TASK_MANAGE = "TASK_MANAGE"
CAP_SENSITIVE_PII = "SENSITIVE_PII"


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _derive_key(settings, context: bytes) -> bytes:
    """Domain-separated subkey of the SERVER-ONLY signing key.

    Independent review P0-1 proved the old implementation (api_token as HMAC
    key) let any token holder mint principals offline. load_grant_key() reads an
    independent env value or a 0600 key file the client never sees.
    """
    return subkey_for(settings, context)


def _mac(settings, context: bytes, payload: Dict[str, Any]) -> str:
    return hmac.new(
        _derive_key(settings, context), _canonical(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def server_key_fingerprint(settings) -> str:
    """Non-reversible provenance tag for audit output (never the key itself)."""
    return hmac.new(
        load_grant_key(settings), b"jme/p1-key-fingerprint/v1", hashlib.sha256
    ).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TrustedPrincipal:
    """A server-issued identity. ``mac`` makes the object unforgeable.

    Client-controlled strings (``actor``, ``actor_label``, ``SERVER_ACTOR``,
    ``required_grants_json``) can never become a TrustedPrincipal: there is no
    constructor path from a free-form label, and WriteGovernor verifies the MAC.
    """

    subject_id: str
    principal_type: str
    grants: Tuple[str, ...]
    issued_at: str
    key_version: int
    mac: str

    def payload(self) -> Dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "principal_type": self.principal_type,
            "grants": list(self.grants),
            "issued_at": self.issued_at,
            "key_version": self.key_version,
            "scheme": KEY_SCHEME,
        }

    def verify(
        self,
        settings,
        *,
        key_version: int = 1,
        max_age_seconds: int = 3600,
    ) -> bool:
        if self.key_version != key_version:
            return False
        # Reject not-yet-valid, malformed and ancient principals (review P2:
        # a correctly signed but year-old principal used to verify forever).
        try:
            issued = datetime.fromisoformat(self.issued_at)
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
        now = _now()
        if issued > now + timedelta(seconds=30):
            return False
        if now - issued > timedelta(seconds=max_age_seconds):
            return False
        expected = _mac(settings, _PRINCIPAL_CONTEXT, self.payload())
        return hmac.compare_digest(self.mac, expected)

    def has(self, capability: str) -> bool:
        return capability in self.grants

    @classmethod
    def issue(
        cls,
        settings,
        *,
        subject_id: str,
        principal_type: str,
        grants: Sequence[str] = (CAP_MEMORY_READ,),
        key_version: int = 1,
    ) -> "TrustedPrincipal":
        if not subject_id or not principal_type:
            raise UntrustedIdentityError("subject_id and principal_type are required")
        normalized = tuple(sorted(set(str(g) for g in grants)))
        issued_at = _now().isoformat()
        unsigned = cls(
            subject_id=subject_id,
            principal_type=principal_type,
            grants=normalized,
            issued_at=issued_at,
            key_version=key_version,
            mac="",
        )
        mac = _mac(settings, _PRINCIPAL_CONTEXT, unsigned.payload())
        return replace(unsigned, mac=mac)

    @classmethod
    def from_verified_api_token(
        cls,
        settings,
        verified_token: str,
        *,
        grants: Sequence[str] = (CAP_MEMORY_READ, CAP_MEMORY_WRITE),
        key_version: int = 1,
    ) -> "TrustedPrincipal":
        """Only call AFTER the shared token has been verified by TokenAuth.

        The subject id is derived from the verified token with a keyed MAC, so a
        client cannot choose its own identity by sending an ``actor`` field.
        """
        if not verified_token:
            raise UntrustedIdentityError("verified token is empty")
        digest = hmac.new(
            _derive_key(settings, _GRANT_CONTEXT),
            verified_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:16]
        return cls.issue(
            settings,
            subject_id=f"api-token:{digest}",
            principal_type="API_TOKEN",
            grants=grants,
            key_version=key_version,
        )


@dataclass(frozen=True)
class SignedGrant:
    """One-shot authorization ticket for one concrete operation."""

    grant_id: str
    subject_id: str
    operation: str
    object_ref: str
    grants: Tuple[str, ...]
    task_id: str
    run_id: str
    nonce: str
    issued_at: str
    expires_at: str
    key_version: int
    signature: str

    def payload(self) -> Dict[str, Any]:
        # Every field except ``signature`` is covered. ``scheme`` pins the
        # algorithm so a future format cannot be replayed under a new label.
        return {
            "grant_id": self.grant_id,
            "subject_id": self.subject_id,
            "operation": self.operation,
            "object_ref": self.object_ref,
            "grants": list(self.grants),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_version": self.key_version,
            "scheme": KEY_SCHEME,
        }

    def signature_is_valid(self, settings) -> bool:
        expected = _mac(settings, _GRANT_CONTEXT, self.payload())
        return hmac.compare_digest(self.signature, expected)

    def scope_json(self) -> str:
        return _canonical(
            {
                "grants": list(self.grants),
                "object_ref": self.object_ref,
                "operation": self.operation,
                "run_id": self.run_id,
                "task_id": self.task_id,
            }
        )


@dataclass(frozen=True)
class GrantVerification:
    ok: bool
    reason: str = ""


class GrantAuthority:
    """Issues, persists, verifies, and consumes SignedGrants in v6 ``grants``.

    Reusing the reserved 003-C table means P1 builds no second authorization
    ledger. The table is append-only from our side: we INSERT and UPDATE only
    the status/consumed_at of rows we created; no historical row is touched.
    """

    def __init__(self, manager, settings, config: Optional[GovernanceConfig] = None) -> None:
        self.manager = manager
        self.settings = settings
        self.config = config or GovernanceConfig()

    def issue(
        self,
        *,
        principal: TrustedPrincipal,
        operation: str,
        object_ref: str,
        grants: Sequence[str],
        task_id: str = "",
        run_id: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> SignedGrant:
        if not isinstance(principal, TrustedPrincipal) or not principal.verify(
            self.settings, key_version=self.config.grant_key_version
        ):
            raise UntrustedIdentityError("grant can only be issued to a verified principal")
        ttl = self.config.grant_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            raise GrantVerificationError("grant TTL must be positive")
        issued = _now()
        expires = issued + timedelta(seconds=ttl)
        requested = tuple(sorted(set(str(g) for g in grants)))
        missing = [g for g in requested if not principal.has(g)]
        if missing:
            raise GrantVerificationError(
                "principal lacks requested capabilities", detail={"missing": missing}
            )
        unsigned = SignedGrant(
            grant_id=uuid.uuid4().hex,
            subject_id=principal.subject_id,
            operation=str(operation),
            object_ref=str(object_ref),
            grants=requested,
            task_id=str(task_id or ""),
            run_id=str(run_id or ""),
            nonce=secrets.token_hex(16),
            issued_at=issued.isoformat(),
            expires_at=expires.isoformat(),
            key_version=self.config.grant_key_version,
            signature="",
        )
        grant = replace(
            unsigned, signature=_mac(self.settings, _GRANT_CONTEXT, unsigned.payload())
        )
        self._persist(grant)
        return grant

    def _persist(self, grant: SignedGrant) -> None:
        with self.manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO grants("
                    "grant_id, grant_type, candidate_id, decision_version, "
                    "content_fingerprint, ruleset_version, device_id, key_version, "
                    "nonce, signature, pii_categories_json, disclosure_version, "
                    "policy_id, scope_json, policy_version, issued_at, expires_at, "
                    "status, consumed_at, revoked_at, invalidated_reason, interaction_id"
                    ") VALUES ("
                    ":grant_id, :grant_type, NULL, NULL, NULL, :ruleset_version, '', "
                    ":key_version, :nonce, :signature, NULL, NULL, :policy_id, "
                    ":scope_json, :policy_version, :issued_at, :expires_at, "
                    ":status, NULL, NULL, NULL, :interaction_id)"
                ),
                {
                    "grant_id": grant.grant_id,
                    "grant_type": "P1_WRITE_GRANT",
                    "ruleset_version": "p1-governance-1",
                    "key_version": grant.key_version,
                    "nonce": grant.nonce,
                    "signature": grant.signature,
                    "policy_id": grant.operation,
                    "scope_json": grant.scope_json(),
                    "policy_version": "1",
                    "issued_at": grant.issued_at,
                    "expires_at": grant.expires_at,
                    "status": GRANT_ISSUED,
                    "interaction_id": grant.run_id or None,
                },
            )

    def verify(self, grant: SignedGrant) -> GrantVerification:
        if not isinstance(grant, SignedGrant):
            return GrantVerification(False, "not_a_signed_grant")
        if grant.key_version != self.config.grant_key_version:
            return GrantVerification(False, "key_version_mismatch")
        try:
            expires = datetime.fromisoformat(grant.expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except ValueError:
            return GrantVerification(False, "malformed_expires_at")
        if _now() >= expires:
            return GrantVerification(False, "grant_expired")
        if not grant.signature_is_valid(self.settings):
            return GrantVerification(False, "signature_mismatch")
        with self.manager.session() as session:
            row = session.execute(
                text(
                    "SELECT status, signature, nonce, scope_json, expires_at, key_version "
                    "FROM grants WHERE grant_id=:g"
                ),
                {"g": grant.grant_id},
            ).mappings().first()
        if row is None:
            return GrantVerification(False, "grant_not_registered")
        if row["status"] != GRANT_ISSUED:
            return GrantVerification(False, f"grant_status_{row['status']}")
        if not hmac.compare_digest(str(row["signature"]), grant.signature):
            return GrantVerification(False, "stored_signature_mismatch")
        if str(row["nonce"]) != grant.nonce:
            return GrantVerification(False, "nonce_mismatch")
        if str(row["scope_json"]) != grant.scope_json():
            return GrantVerification(False, "scope_mismatch")
        if str(row["expires_at"]) != grant.expires_at:
            return GrantVerification(False, "stored_expiry_mismatch")
        if int(row["key_version"]) != grant.key_version:
            return GrantVerification(False, "stored_key_version_mismatch")
        return GrantVerification(True, "verified")

    def consume(self, grant_id: str) -> bool:
        """One-shot consumption. Returns False on a replay (idempotent)."""
        with self.manager.session() as session:
            result = session.execute(
                text(
                    "UPDATE grants SET status=:consumed, consumed_at=:now "
                    "WHERE grant_id=:g AND status=:issued"
                ),
                {
                    "consumed": GRANT_CONSUMED,
                    "issued": GRANT_ISSUED,
                    "now": _now().isoformat(),
                    "g": grant_id,
                },
            )
            return int(result.rowcount or 0) == 1

    def revoke(self, grant_id: str) -> bool:
        with self.manager.session() as session:
            result = session.execute(
                text(
                    "UPDATE grants SET status=:revoked, revoked_at=:now "
                    "WHERE grant_id=:g AND status=:issued"
                ),
                {
                    "revoked": GRANT_REVOKED,
                    "issued": GRANT_ISSUED,
                    "now": _now().isoformat(),
                    "g": grant_id,
                },
            )
            return int(result.rowcount or 0) == 1

    def row_count(self) -> int:
        with self.manager.session() as session:
            return int(session.execute(text("SELECT COUNT(*) FROM grants")).scalar() or 0)
