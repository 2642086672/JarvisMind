"""JW-03: persistence for approved-memory modification requests.

Concurrency contract (same shape as JW-01's forget requests)
-----------------------------------------------------------
Every state transition is a conditional ``UPDATE ... WHERE status = <expected>``
and the caller inspects ``rowcount``. There is no read-then-write window:

* **create**        - a duplicate submission collides on the partial unique
                     index ``uq_modification_pending_per_memory`` instead of
                     producing a second approvable row;
* **rotate_token**  - only a PENDING request can be re-armed, so a stale token
                     stops working the moment a new one is issued;
* **consume**       - token verify-and-burn happens in ONE statement, so two
                     concurrent approvals cannot both succeed;
* **transition**    - expected-status guard makes double-approve and
                     approve-after-reject fail on ``rowcount == 0``.

Rows are never deleted: a rejected or executed request stays as audit history,
which is what lets an operator answer "who asked for this change and what
happened to it" months later.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.modification_schema import MODIFICATION_TABLE

#: Request kinds. UPDATE carries field edits; the rest are lifecycle moves that
#: touch ``status`` / ``expires_at`` only.
KIND_UPDATE = "UPDATE"
KIND_ARCHIVE = "ARCHIVE"
KIND_RESTORE = "RESTORE"
KIND_EXTEND = "EXTEND"
VALID_KINDS = frozenset({KIND_UPDATE, KIND_ARCHIVE, KIND_RESTORE, KIND_EXTEND})

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_EXECUTED = "EXECUTED"
VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_EXECUTED}
)


def hash_token(token: str) -> str:
    """SHA-256 hex digest of the plaintext confirmation token.

    Only the digest is stored, so a database read cannot yield a usable token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """32 bytes of entropy rendered as a URL-safe string (no padding)."""
    return secrets.token_urlsafe(32)


class ModificationRequestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads
    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.execute(
            text(f"SELECT * FROM {MODIFICATION_TABLE} WHERE id = :id"),
            {"id": request_id},
        ).mappings().first()
        return dict(row) if row else None

    def list(
        self,
        *,
        memory_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if memory_id:
            clauses.append("memory_id = :mid")
            params["mid"] = memory_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = int(
            self.session.execute(
                text(f"SELECT COUNT(*) FROM {MODIFICATION_TABLE}{where}"), params
            ).scalar()
            or 0
        )
        params["limit"] = page_size
        params["offset"] = (page - 1) * page_size
        rows = (
            self.session.execute(
                text(
                    f"SELECT * FROM {MODIFICATION_TABLE}{where} "
                    "ORDER BY requested_at DESC, id DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows], total

    def find_pending(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.execute(
            text(
                f"SELECT * FROM {MODIFICATION_TABLE} "
                "WHERE memory_id = :mid AND status = 'PENDING' LIMIT 1"
            ),
            {"mid": memory_id},
        ).mappings().first()
        return dict(row) if row else None

    # ----------------------------------------------------------------- writes
    def insert(
        self,
        *,
        request_id: str,
        memory_id: str,
        request_kind: str,
        proposed_changes: Dict[str, Any],
        reason: Optional[str],
        requester_label: str,
        authentication_level: str,
        authorization_verification_result: str,
        token_hash: str,
        expires_at: str,
        before_state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if request_kind not in VALID_KINDS:
            raise ValueError(f"unknown request_kind: {request_kind}")
        now = utc_now_iso()
        self.session.execute(
            text(
                f"INSERT INTO {MODIFICATION_TABLE}("
                "id, memory_id, request_kind, status, proposed_changes_json, "
                "reason, requester_label, authentication_level, "
                "authorization_verification_result, confirmation_token_hash, "
                "confirmation_expires_at, requested_at, before_state_json, "
                "metadata_json, updated_at"
                ") VALUES ("
                ":id,:mid,:kind,'PENDING',:changes,:reason,:label,:auth_level,:avr,"
                ":token_hash,:expires,:now,:before,:meta,:now)"
            ),
            {
                "id": request_id,
                "mid": memory_id,
                "kind": request_kind,
                "changes": json.dumps(proposed_changes, ensure_ascii=False, sort_keys=True),
                "reason": reason,
                "label": requester_label or "",
                "auth_level": authentication_level,
                "avr": authorization_verification_result,
                "token_hash": token_hash,
                "expires": expires_at,
                "now": now,
                "before": (
                    json.dumps(before_state, ensure_ascii=False, sort_keys=True)
                    if before_state is not None
                    else None
                ),
                "meta": json.dumps(metadata, ensure_ascii=False) if metadata else None,
            },
        )
        self.session.flush()

    def rotate_token(self, request_id: str, *, token_hash: str, expires_at: str) -> int:
        """Re-issue the one-time token for a request that is still PENDING."""
        result = self.session.execute(
            text(
                f"UPDATE {MODIFICATION_TABLE} "
                "SET confirmation_token_hash = :token_hash, "
                "confirmation_expires_at = :expires, updated_at = :now "
                "WHERE id = :id AND status = 'PENDING'"
            ),
            {
                "id": request_id,
                "token_hash": token_hash,
                "expires": expires_at,
                "now": utc_now_iso(),
            },
        )
        self.session.flush()
        return int(result.rowcount or 0)

    def consume_token(self, request_id: str, *, token_hash: str, now: str) -> int:
        """Verify AND burn the token in one statement.

        ``rowcount == 1`` means the token matched, had not expired, had not been
        used, and was consumed by THIS call. Any other outcome is indistinguishable
        to the caller on purpose: a wrong token and an already-used one must not
        produce different responses, or the endpoint becomes an oracle.
        """
        result = self.session.execute(
            text(
                f"UPDATE {MODIFICATION_TABLE} "
                "SET confirmation_consumed_at = :now, updated_at = :now "
                "WHERE id = :id AND status = 'PENDING' "
                "AND confirmation_consumed_at IS NULL "
                "AND confirmation_token_hash = :token_hash "
                "AND confirmation_expires_at IS NOT NULL "
                "AND confirmation_expires_at > :now"
            ),
            {"id": request_id, "token_hash": token_hash, "now": now},
        )
        self.session.flush()
        return int(result.rowcount or 0)

    #: Columns a state transition may ever touch. ``proposed_changes_json`` is
    #: deliberately ABSENT: once a request is created, the values a human
    #: approved must be immutable, or "approve" would be approving text that can
    #: change afterwards. Same for ``memory_id`` (retargeting) and
    #: ``request_kind`` (turning an ARCHIVE into an UPDATE).
    _MUTABLE_ON_TRANSITION = frozenset(
        {
            "decided_at",
            "decided_by",
            "executed_at",
            "applied_changes_json",
            "before_state_json",
            "failure_reason",
            "reason",
            "metadata_json",
            # System-maintained: transition() always sets it, and callers of the
            # older signature pass it explicitly. Accepting it explicitly is
            # harmless because the value is overwritten below either way.
            "updated_at",
        }
    )

    def transition(
        self,
        request_id: str,
        *,
        expected_status: str,
        new_status: str,
        values: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Conditional status change; 0 rows means someone else won the race.

        JW-03-R1: ``values`` is filtered through an explicit allowlist. Before
        this, any key was interpolated straight into the SET clause, so a caller
        passing ``proposed_changes_json`` could rewrite what the approver had
        agreed to - after the fact. Unknown keys now raise instead of being
        silently honoured, so a future bug surfaces immediately rather than
        becoming a mutation channel.
        """
        if new_status not in VALID_STATUSES:
            raise ValueError(f"unknown status: {new_status}")
        payload = dict(values or {})
        forbidden = sorted(
            key for key in payload if key not in self._MUTABLE_ON_TRANSITION
        )
        if forbidden:
            raise ValueError(
                "transition() cannot modify these columns: "
                + ", ".join(forbidden)
                + " (the approved content must stay frozen)"
            )
        assignments = ["status = :new_status", "updated_at = :updated_at"]
        params: Dict[str, Any] = {
            "id": request_id,
            "expected": expected_status,
            "new_status": new_status,
            "updated_at": utc_now_iso(),
        }
        for key, value in payload.items():
            assignments.append(f"{key} = :{key}")
            params[key] = value
        result = self.session.execute(
            text(
                f"UPDATE {MODIFICATION_TABLE} SET " + ", ".join(assignments) +
                " WHERE id = :id AND status = :expected"
            ),
            params,
        )
        self.session.flush()
        return int(result.rowcount or 0)


__all__ = [
    "KIND_ARCHIVE",
    "KIND_EXTEND",
    "KIND_RESTORE",
    "KIND_UPDATE",
    "ModificationRequestRepository",
    "STATUS_APPROVED",
    "STATUS_EXECUTED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "VALID_KINDS",
    "VALID_STATUSES",
    "hash_token",
    "new_token",
]
