"""JW-01 persistence for memory forget requests.

Concurrency design (this is where most of the safety lives)
-----------------------------------------------------------
Every state transition is a CONDITIONAL ``UPDATE ... WHERE status = <expected>``
and the caller inspects ``rowcount``. That single mechanism gives all of the
following without any in-process lock (which would not survive multi-worker or
multi-process deployment):

* approving twice          -> second UPDATE matches 0 rows -> ConflictError
* rejecting after approve  -> 0 rows -> ConflictError
* executing twice          -> 0 rows -> ConflictError
* approve/reject races     -> exactly one wins, the other sees 0 rows
* re-execution after crash -> status is EXECUTED, so 0 rows

The unique partial index (see ``app.database.forget_schema``) additionally makes
"two PENDING requests for one memory" impossible at the storage layer.

Tokens are never stored in plaintext: only a SHA-256 digest plus an expiry is
persisted, and the digest is cleared the moment it is consumed (one-time use).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso
from ..database.forget_schema import FORGET_TABLE, ensure_forget_schema

COLUMNS = (
    "id, memory_id, status, request_source, requester_label, reason, "
    "authentication_level, authorization_verification_result, "
    "confirmation_expires_at, confirmation_consumed_at, requested_at, "
    "decided_at, decided_by, decision_source, executed_at, execution_result, "
    "failure_reason, metadata_json, updated_at"
)


def new_request_id() -> str:
    return uuid.uuid4().hex


def hash_token(token: str) -> str:
    """Digest a one-time confirmation token. Plaintext is never persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_dict(row) -> Dict[str, Any]:
    mapping = row._mapping
    return {key: mapping[key] for key in mapping.keys()}


class ForgetRequestRepository:
    """All SQL for ``memory_forget_requests`` lives here."""

    def __init__(self, session: Session) -> None:
        self.session = session
        ensure_forget_schema(session)

    # ------------------------------------------------------------------ reads
    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.execute(
            text(f"SELECT {COLUMNS} FROM {FORGET_TABLE} WHERE id = :id"),
            {"id": request_id},
        ).first()
        return _row_to_dict(row) if row is not None else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        memory_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where: List[str] = []
        params: Dict[str, Any] = {
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        if status is not None:
            where.append("status = :status")
            params["status"] = status
        if memory_id is not None:
            where.append("memory_id = :memory_id")
            params["memory_id"] = memory_id
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(
            self.session.execute(
                text(f"SELECT COUNT(*) FROM {FORGET_TABLE}{clause}"), params
            ).scalar()
            or 0
        )
        rows = self.session.execute(
            text(
                f"SELECT {COLUMNS} FROM {FORGET_TABLE}{clause} "
                "ORDER BY requested_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).all()
        return [_row_to_dict(row) for row in rows], total

    def find_pending(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.execute(
            text(
                f"SELECT {COLUMNS} FROM {FORGET_TABLE} "
                "WHERE memory_id = :mid AND status = 'PENDING'"
            ),
            {"mid": memory_id},
        ).first()
        return _row_to_dict(row) if row is not None else None

    def has_executed(self, memory_id: str) -> bool:
        """True when this memory already has an EXECUTED forget request."""
        row = self.session.execute(
            text(
                f"SELECT 1 FROM {FORGET_TABLE} "
                "WHERE memory_id = :mid AND status = 'EXECUTED' LIMIT 1"
            ),
            {"mid": memory_id},
        ).first()
        return row is not None

    # ----------------------------------------------------------------- writes
    def insert(
        self,
        *,
        request_id: str,
        memory_id: str,
        request_source: str,
        requester_label: str,
        reason: Optional[str],
        authentication_level: str,
        authorization_verification_result: str,
        token_hash: str,
        expires_at: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = utc_now_iso()
        self.session.execute(
            text(
                f"INSERT INTO {FORGET_TABLE}("
                "id, memory_id, status, request_source, requester_label, reason, "
                "authentication_level, authorization_verification_result, "
                "confirmation_token_hash, confirmation_expires_at, "
                "requested_at, metadata_json, updated_at"
                ") VALUES ("
                ":id,:mid,'PENDING',:src,:label,:reason,:auth_level,:avr,"
                ":token_hash,:expires,:now,:meta,:now)"
            ),
            {
                "id": request_id,
                "mid": memory_id,
                "src": request_source,
                "label": requester_label or "",
                "reason": reason,
                "auth_level": authentication_level,
                "avr": authorization_verification_result,
                "token_hash": token_hash,
                "expires": expires_at,
                "now": now,
                "meta": json.dumps(metadata, ensure_ascii=False) if metadata else None,
            },
        )
        self.session.flush()

    def rotate_token(self, request_id: str, *, token_hash: str, expires_at: str) -> int:
        """Re-issue the one-time token for a request that is still PENDING.

        The previous token stops working immediately, so a duplicate submission
        cannot leave two usable tokens in circulation.
        """
        result = self.session.execute(
            text(
                f"UPDATE {FORGET_TABLE} SET confirmation_token_hash = :token_hash, "
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

    def transition(
        self,
        request_id: str,
        *,
        expected_status: str,
        new_status: str,
        values: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Conditional status transition. Returns rowcount (0 == conflict).

        ``expected_status`` is enforced in the WHERE clause, so two concurrent
        callers cannot both succeed: SQLite serialises the writes and the loser
        matches zero rows.
        """
        assignments = ["status = :new_status", "updated_at = :now"]
        params: Dict[str, Any] = {
            "id": request_id,
            "expected": expected_status,
            "new_status": new_status,
            "now": utc_now_iso(),
        }
        for column, value in (values or {}).items():
            # Column names are code-controlled constants, never request input.
            assignments.append(f"{column} = :{column}")
            params[column] = value
        result = self.session.execute(
            text(
                f"UPDATE {FORGET_TABLE} SET {', '.join(assignments)} "
                "WHERE id = :id AND status = :expected"
            ),
            params,
        )
        self.session.flush()
        return int(result.rowcount or 0)

    def burn_agent_token(self, request_id: str) -> int:
        """Consume the one-time token of an agent-originated request without presenting it.

        The token is withheld from the requester at creation, so only the owner can
        approve or reject such a request. The UPDATE matches only a PENDING request
        whose requester label is an agent label, and only once.
        """
        result = self.session.execute(
            text(
                f"UPDATE {FORGET_TABLE} SET confirmation_consumed_at = :now, "
                "updated_at = :now "
                "WHERE id = :id AND status = 'PENDING' "
                "AND requester_label LIKE 'agent:%' "
                "AND confirmation_consumed_at IS NULL"
            ),
            {"id": request_id, "now": utc_now_iso()},
        )
        return result.rowcount

    def consume_token(self, request_id: str, *, token_hash: str) -> int:
        """Atomically verify + burn the one-time token.

        Returns 1 only when the digest matched, the request was still PENDING,
        the token had not been consumed and it had not expired - all evaluated
        inside one UPDATE so a replay cannot slip through a check/act window.
        """
        result = self.session.execute(
            text(
                f"UPDATE {FORGET_TABLE} SET confirmation_consumed_at = :now, "
                "updated_at = :now "
                "WHERE id = :id AND status = 'PENDING' "
                "AND confirmation_token_hash = :token_hash "
                "AND confirmation_consumed_at IS NULL "
                "AND confirmation_expires_at IS NOT NULL "
                "AND confirmation_expires_at > :now"
            ),
            {
                "id": request_id,
                "token_hash": token_hash,
                "now": utc_now_iso(),
            },
        )
        self.session.flush()
        return int(result.rowcount or 0)

    def token_is_live(self, request_id: str) -> bool:
        row = self.session.execute(
            text(
                f"SELECT 1 FROM {FORGET_TABLE} WHERE id = :id AND status = 'PENDING' "
                "AND confirmation_consumed_at IS NULL "
                "AND confirmation_expires_at IS NOT NULL "
                "AND confirmation_expires_at > :now"
            ),
            {"id": request_id, "now": utc_now_iso()},
        ).first()
        return row is not None


__all__ = [
    "FORGET_TABLE",
    "ForgetRequestRepository",
    "hash_token",
    "new_request_id",
]
