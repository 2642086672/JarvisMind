"""003-B: SQL for security events and the approval ledger.

All statements are idempotent. UNIQUE participants are NOT NULL columns, so
SQLite's "NULLs are distinct" rule cannot silently defeat deduplication.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.errors import ConflictError, ValidationError
from ..core.security_gate import (
    AUTH_NOT_VERIFIED,
    AUTH_PASSED,
    assert_safe_identifier,
)
from ..core.timeutils import utc_now_iso


def _new_id() -> str:
    return uuid.uuid4().hex


# 003-B1: the shared API token authenticates a CLIENT, not a human. This label
# is written by the server itself and can never be supplied by a request.
SERVER_IDENTITY_LABEL = "shared-api-token:human-identity-not-verified"


class SecurityEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------- BLOCK events
    def record_block_event(
        self,
        *,
        conversation_id: str,
        message_id: str,
        turn_id: str,
        category: str,
        hit_length: int,
        ruleset_version: str,
    ) -> str:
        # 003-B1: these identifiers are client-controlled. Scan BEFORE the
        # INSERT so a blocked value can never be persisted in an event row.
        for label, value in (
            ("conversation_id", conversation_id),
            ("message_id", message_id),
            ("turn_id", turn_id),
        ):
            assert_safe_identifier(value, field=label)
        key = {
            "c": conversation_id or "",
            "m": message_id or "",
            "t": turn_id or "",
            "cat": category,
        }
        lookup = text(
            "SELECT block_event_id FROM block_events "
            "WHERE conversation_id=:c AND message_id=:m AND turn_id=:t AND category=:cat"
        )
        existing = self.session.execute(lookup, key).first()
        if existing is not None:
            return str(existing[0])

        event_id = _new_id()
        self.session.execute(
            text(
                "INSERT OR IGNORE INTO block_events("
                "block_event_id, conversation_id, message_id, turn_id, category, "
                "hit_length, ruleset_version, created_at"
                ") VALUES (:id, :c, :m, :t, :cat, :len, :ruleset, :now)"
            ),
            {
                **key,
                "id": event_id,
                "len": int(hit_length),
                "ruleset": ruleset_version,
                "now": utc_now_iso(),
            },
        )
        again = self.session.execute(lookup, key).first()
        return str(again[0]) if again is not None else event_id

    # --------------------------------------------------------- extraction events
    def record_extraction_event(
        self,
        *,
        conversation_id: str,
        message_id: str,
        turn_id: str,
        event_type: str,
        ignore_reason_enum: str = "",
        app_decision_reported: Optional[str] = None,
        server_security_result: Optional[str] = None,
        final_result: Optional[str] = None,
        ruleset_version: str,
    ) -> str:
        for label, value in (
            ("conversation_id", conversation_id),
            ("message_id", message_id),
            ("turn_id", turn_id),
            ("app_decision_reported", app_decision_reported),
        ):
            assert_safe_identifier(value, field=label)
        key = {
            "c": conversation_id or "",
            "m": message_id or "",
            "t": turn_id or "",
            "e": event_type,
            "r": ignore_reason_enum or "",
        }
        lookup = text(
            "SELECT event_id FROM extraction_events "
            "WHERE conversation_id=:c AND message_id=:m AND turn_id=:t "
            "AND event_type=:e AND ignore_reason_enum=:r"
        )
        existing = self.session.execute(lookup, key).first()
        if existing is not None:
            return str(existing[0])

        event_id = _new_id()
        self.session.execute(
            text(
                "INSERT OR IGNORE INTO extraction_events("
                "event_id, conversation_id, message_id, turn_id, event_type, "
                "ignore_reason_enum, app_decision_reported, server_security_result, "
                "final_result, ruleset_version, created_at"
                ") VALUES (:id, :c, :m, :t, :e, :r, :app, :server, :final, :ruleset, :now)"
            ),
            {
                **key,
                "id": event_id,
                "app": app_decision_reported,
                "server": server_security_result,
                "final": final_result,
                "ruleset": ruleset_version,
                "now": utc_now_iso(),
            },
        )
        again = self.session.execute(lookup, key).first()
        return str(again[0]) if again is not None else event_id

    # ---------------------------------------------------------- approval ledger
    def active_attempt(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        row = self.session.execute(
            text(
                "SELECT event_id, transaction_result, operation_result, memory_id "
                "FROM approval_events WHERE candidate_id=:cid "
                "AND operation_result IN ('SUCCEEDED','INDETERMINATE') "
                "AND transaction_result <> 'ROLLED_BACK' LIMIT 1"
            ),
            {"cid": candidate_id},
        ).first()
        if row is None:
            return None
        return {
            "event_id": str(row[0]),
            "transaction_result": str(row[1]),
            "operation_result": str(row[2]),
            "memory_id": row[3],
        }

    def prepare_approval_event(
        self,
        *,
        candidate_id: str,
        decision: str,
        decision_version: int,
        content_fingerprint: str,
        ruleset_version: str,
        idempotency_key: str,
        actor_label: str = "",
        authentication_level: str = "API_TOKEN_SHARED",
    ) -> str:
        """Write the recovery intent BEFORE the main transaction.

        003-B1: ``actor_label`` is only the CLIENT CLAIM; the server writes its
        own identity label into ``actor_label`` and stores the claim separately.
        ``authorization_verification_result`` starts as NOT_VERIFIED because no
        Grant verification exists yet - it must never be seeded with PASSED.
        """
        assert_safe_identifier(actor_label, field="client_actor_claim")
        assert_safe_identifier(candidate_id, field="candidate_id")
        existing = self.active_attempt(candidate_id)
        if existing is not None:
            raise ConflictError(
                "A previous approval attempt is still unresolved; recover it first",
                detail={"code": "approval_attempt_pending", "event_id": existing["event_id"]},
            )
        event_id = _new_id()
        try:
            self.session.execute(
                text(
                    "INSERT INTO approval_events("
                    "event_id, candidate_id, decision, decision_version, "
                    "content_fingerprint, ruleset_version, idempotency_key, "
                    "actor_label, client_actor_claim, authentication_level, "
                    "authorization_verification_result, "
                    "transaction_result, operation_result, created_at"
                    ") VALUES (:id,:cid,:decision,:version,:fp,:ruleset,:idem,"
                    ":server_actor,:claim,:auth_level,:avr,"
                    "'UNKNOWN','INDETERMINATE',:now)"
                ),
                {
                    "id": event_id,
                    "cid": candidate_id,
                    "decision": decision,
                    "version": int(decision_version),
                    "fp": content_fingerprint or "",
                    "ruleset": ruleset_version,
                    "idem": idempotency_key,
                    "server_actor": SERVER_IDENTITY_LABEL,
                    "claim": actor_label or "",
                    "auth_level": authentication_level,
                    "avr": AUTH_NOT_VERIFIED,
                    "now": utc_now_iso(),
                },
            )
        except IntegrityError as exc:
            raise ConflictError(
                "A previous approval attempt is still unresolved; recover it first",
                detail={"code": "approval_attempt_pending"},
            ) from exc
        return event_id

    def set_approval_result(
        self,
        event_id: str,
        *,
        transaction_result: str,
        operation_result: str,
        memory_id: Optional[str] = None,
        error_code: Optional[str] = None,
        authorization_verification_result: Optional[str] = None,
    ) -> None:
        # 003-B1: no code path in this phase performs a real grant/signature
        # verification, so PASSED is unattainable. 003-C must replace this
        # guard with the result of an actual server-side verification.
        if authorization_verification_result == AUTH_PASSED:
            raise ValidationError(
                "Server-verified authorization is not available in this phase",
                detail={"code": "authorization_verification_unavailable"},
            )
        self.session.execute(
            text(
                "UPDATE approval_events SET transaction_result=:tr, "
                "operation_result=:opr, memory_id=COALESCE(:mid, memory_id), "
                "error_code=:err, "
                "authorization_verification_result="
                "COALESCE(:avr, authorization_verification_result), "
                "confirmed_at=:now WHERE event_id=:id"
            ),
            {
                "id": event_id,
                "tr": transaction_result,
                "opr": operation_result,
                "mid": memory_id,
                "err": error_code,
                "avr": authorization_verification_result,
                "now": utc_now_iso(),
            },
        )

    def unknown_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.session.execute(
            text(
                "SELECT event_id, candidate_id, decision, memory_id FROM approval_events "
                "WHERE transaction_result='UNKNOWN' ORDER BY created_at LIMIT :lim"
            ),
            {"lim": limit},
        ).all()
        return [
            {
                "event_id": str(row[0]),
                "candidate_id": str(row[1]),
                "decision": str(row[2]),
                "memory_id": row[3],
            }
            for row in rows
        ]

    # ------------------------------------------------------- state inspection
    def memory_for_candidate(self, candidate_id: str) -> Optional[str]:
        row = self.session.execute(
            text(
                "SELECT id FROM memory_records WHERE source_candidate_id=:cid LIMIT 1"
            ),
            {"cid": candidate_id},
        ).first()
        return str(row[0]) if row is not None else None

    def candidate_status(self, candidate_id: str) -> Optional[str]:
        row = self.session.execute(
            text("SELECT status FROM memory_candidates WHERE id=:cid"),
            {"cid": candidate_id},
        ).first()
        return str(row[0]) if row is not None else None

    def count_events(self, table: str) -> int:
        # table is a module-internal constant, never user input.
        row = self.session.execute(text(f"SELECT COUNT(*) FROM {table}")).first()
        return int(row[0]) if row is not None else 0

    def outcome_totals(self) -> Dict[str, int]:
        """Cumulative counters using the SAME vocabulary as OutcomeCounts.

        003-B1: per-request counters count processing units; these lifetime
        counters sum the persisted events / candidate states. No category is
        counted twice: event rows and candidate rows are disjoint sources.
        """
        event_rows = self.session.execute(
            text("SELECT event_type, COUNT(*) FROM extraction_events GROUP BY event_type")
        ).all()
        events = {str(row[0]): int(row[1]) for row in event_rows}
        candidate_rows = self.session.execute(
            text("SELECT status, COUNT(*) FROM memory_candidates GROUP BY status")
        ).all()
        candidates = {str(row[0]): int(row[1]) for row in candidate_rows}
        approval_rows = self.session.execute(
            text(
                "SELECT decision, COUNT(*) FROM approval_events "
                "WHERE transaction_result='COMMITTED' AND operation_result='SUCCEEDED' "
                "GROUP BY decision"
            )
        ).all()
        approvals = {str(row[0]): int(row[1]) for row in approval_rows}
        # A conflict is a CANDIDATE (still approvable), not a rejected event.
        conflict_total = int(
            self.session.execute(
                text(
                    "SELECT COUNT(*) FROM memory_candidates "
                    "WHERE conflicts_with IS NOT NULL "
                    "AND status IN ('PENDING','PENDING_AUTHORIZATION')"
                )
            ).scalar()
            or 0
        )
        return {
            "duplicate_rejected_total": events.get("DUPLICATE", 0),
            "low_value_total": events.get("LOW_VALUE", 0),
            "empty_total": events.get("EMPTY", 0),
            "conflict_total": conflict_total,
            "pending_manual": candidates.get("PENDING", 0),
            "pending_pii_dual": candidates.get("PENDING_AUTHORIZATION", 0),
            "final_approved": candidates.get("APPROVED", 0),
            "final_rejected": candidates.get("REJECTED", 0),
            "manually_approved": approvals.get("approve", 0),
            "user_rejected": approvals.get("reject", 0),
            "auto_saved": 0,  # D1: AUTO_SAVE is disabled by default.
        }
