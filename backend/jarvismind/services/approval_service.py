"""003-B: atomic candidate approval and UNKNOWN transaction recovery.

Design (A4 §6.2):

* The recovery intent (``approval_events`` row with UNKNOWN/INDETERMINATE) is
  committed in its OWN session BEFORE the main transaction starts. A later
  crash or commit failure therefore cannot erase it.
* The main transaction contains: conditional candidate status update, formal
  memory creation, audit entry, and the approval-event memory link. All four
  share ONE session/commit. ``MemoryService.create`` is deliberately NOT used
  here because it opens a nested session (two independent commits).
* The final result is confirmed in a separate session AFTER a successful
  commit. If commit fails, is interrupted, or returns an error AFTER writing,
  the event stays UNKNOWN - we never claim ROLLED_BACK from an exception alone.
* Uniqueness does not rely on Python checks: the partial unique index on
  ``memory_records.source_candidate_id`` and ``uq_approval_success`` are the
  final protection against concurrent/replayed approvals.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..core.errors import ConflictError, DatabaseError, NotFoundError, ValidationError
from ..core.fingerprint import content_fingerprint
from ..core.security_gate import (
    AUTH_NOT_VERIFIED,
    PiiAuthorizationRequiredError,
    RULESET_VERSION,
    assert_writable,
)
from ..core.search import segment_for_index
from ..core.timeutils import utc_now_iso
from ..database.connection import DatabaseManager
from ..database.models import MemoryRecord
from ..repositories.candidate_repository import CandidateRepository
from ..repositories.memory_repository import MemoryRepository
from ..repositories.security_event_repository import SecurityEventRepository
from ..schemas.extraction import CandidateResponse, CandidateStatus

logger = logging.getLogger("jme.approval_service")

SERVER_ACTOR = "server:user-approval"
APPROVABLE_STATUSES = ("PENDING", "PENDING_AUTHORIZATION")
UNKNOWN = "UNKNOWN"
COMMITTED = "COMMITTED"
ROLLED_BACK = "ROLLED_BACK"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
INDETERMINATE = "INDETERMINATE"


def _new_id() -> str:
    return uuid.uuid4().hex


def _decode_grants(raw: Optional[str]) -> list[str]:
    if not raw:
        return ["USER_APPROVAL"]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ["USER_APPROVAL"]
    return [str(item) for item in parsed] if isinstance(parsed, list) else ["USER_APPROVAL"]


class ApprovalService:
    def __init__(self, manager: DatabaseManager) -> None:
        self.manager = manager

    def _exact_tokens(self) -> tuple[str, ...]:
        settings = self.manager.settings
        return tuple(
            token
            for token in (settings.api_token, settings.llm_api_key)
            if token
        )

    def _fingerprint(self, candidate) -> str:
        """HMAC over the CURRENT persisted fields, never a plain SHA256."""
        return content_fingerprint(
            self.manager.settings,
            content=candidate.content,
            summary=candidate.summary,
            metadata_json=candidate.metadata_json,
            decision_version=int(candidate.decision_version or 1),
        )

    # --------------------------------------------------------------- public API
    def review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: Optional[str] = None,
        actor_label: str = "",
    ) -> CandidateResponse:
        """P1: approval submission writes memory_candidates via raw SQL, so the
        whole transaction must run inside an authorised context (P0-2 fix)."""
        from ..governance.integration import (
            OP_CANDIDATE_APPROVE,
            authorized_operation,
        )

        with authorized_operation(
            manager=self.manager,
            operation=OP_CANDIDATE_APPROVE,
            object_ref=candidate_id,
            trigger="APPROVAL_SERVICE",
        ):
            return self._review_impl(
                candidate_id,
                decision=decision,
                reason=reason,
                actor_label=actor_label,
            )

    def _review_impl(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: Optional[str] = None,
        actor_label: str = "",
    ) -> CandidateResponse:
        """``actor_label`` is an UNVERIFIED client claim, never an identity.

        The server writes ``decided_by`` itself and records the claim only in
        approval_events.client_actor_claim alongside a fixed identity label.
        """
        action = decision.strip().lower()
        if action not in {"approve", "reject"}:
            raise ValidationError("decision must be 'approve' or 'reject'")

        snapshot = self._load_and_verify(candidate_id, action)
        event_id = self._prepare_intent(candidate_id, action, snapshot, actor_label)
        memory_id = self._commit_atomic(candidate_id, action, reason, snapshot, event_id)
        self._confirm(event_id, memory_id)
        return self._response(candidate_id)

    def recover_unknown(self, limit: int = 100) -> Dict[str, int]:
        """Re-read database state and converge unresolved approval attempts.

        Never creates a memory here. If the state is contradictory the event
        stays UNKNOWN, which keeps the candidate safely blocked.
        """
        with self.manager.session() as session:
            events = SecurityEventRepository(session).unknown_events(limit)
        stats = {"scanned": len(events), "committed": 0, "rolled_back": 0, "still_unknown": 0}
        for event in events:
            with self.manager.session() as session:
                repo = SecurityEventRepository(session)
                memory_id = repo.memory_for_candidate(event["candidate_id"])
                status = repo.candidate_status(event["candidate_id"])
                if memory_id is not None and status == CandidateStatus.APPROVED.value:
                    transaction_result, operation_result = COMMITTED, SUCCEEDED
                    stats["committed"] += 1
                elif (
                    memory_id is None
                    and status in APPROVABLE_STATUSES
                    and event["decision"] == "approve"
                ):
                    transaction_result, operation_result = ROLLED_BACK, FAILED
                    stats["rolled_back"] += 1
                elif memory_id is None and status == CandidateStatus.REJECTED.value:
                    transaction_result, operation_result = COMMITTED, SUCCEEDED
                    stats["committed"] += 1
                else:
                    stats["still_unknown"] += 1
                    continue
                repo.set_approval_result(
                    event["event_id"],
                    transaction_result=transaction_result,
                    operation_result=operation_result,
                    memory_id=memory_id,
                )
        return stats

    def stats(self) -> Dict[str, Any]:
        """Cumulative lifecycle statistics (separate from a single /extract)."""
        with self.manager.session() as session:
            totals = SecurityEventRepository(session).outcome_totals()
            totals["blocked_total"] = SecurityEventRepository(session).count_events("block_events")
            totals["extraction_events_total"] = SecurityEventRepository(session).count_events("extraction_events")
        return totals

    # ------------------------------------------------------------ internal steps
    def _load_and_verify(self, candidate_id: str, action: str) -> Dict[str, Any]:
        with self.manager.session() as session:
            candidate = CandidateRepository(session).get_by_id(candidate_id)
            if candidate is None:
                raise NotFoundError(f"Candidate {candidate_id} not found")
            if candidate.status not in APPROVABLE_STATUSES:
                raise ConflictError(
                    "Candidate is no longer pending",
                    detail={"code": "candidate_not_pending", "status": candidate.status},
                )
            if candidate.scan_status != "SCANNED":
                raise ValidationError(
                    "Historical candidate requires a security re-scan before approval",
                    detail={"code": "legacy_rescan_required"},
                )
            # G2: mandatory credential re-scan of the LATEST stored content,
            # including serialized metadata and exact live secrets.
            assert_writable(
                candidate.content,
                summary=candidate.summary,
                metadata_text=candidate.metadata_json,
                exact_tokens=self._exact_tokens(),
                field="memory_candidates",
            )

            required = _decode_grants(candidate.required_grants_json)
            if action == "approve" and "SENSITIVE_PII" in required:
                # 003-C will validate signed, server-issued grants. Until then
                # no server-verified grant exists, so PII stays blocked.
                raise PiiAuthorizationRequiredError(required)

            # 003-B1: a candidate whose text/metadata changed after extraction
            # has a stale approval basis. The stored HMAC must match the
            # current content or approval is refused (no silent re-approval).
            version = int(candidate.decision_version or 1)
            computed = self._fingerprint(candidate)
            if not candidate.content_fingerprint or candidate.content_fingerprint != computed:
                raise ValidationError(
                    "Candidate content changed; approval basis is stale",
                    detail={"code": "stale_content_fingerprint"},
                )

            return {
                "memory_type": candidate.memory_type,
                "content": candidate.content,
                "summary": candidate.summary,
                "importance": candidate.importance,
                "confidence": candidate.confidence,
                "source_id": candidate.source_id,
                "extraction_engine": candidate.extraction_engine,
                "decision_version": version,
                "content_fingerprint": computed,
                "metadata_json": candidate.metadata_json,
                "ruleset_version": candidate.ruleset_version or RULESET_VERSION,
            }

    def _prepare_intent(
        self,
        candidate_id: str,
        action: str,
        snapshot: Dict[str, Any],
        actor_label: str,
    ) -> str:
        # Own session => own commit. The intent survives any later failure.
        with self.manager.session() as session:
            return SecurityEventRepository(session).prepare_approval_event(
                candidate_id=candidate_id,
                decision=action,
                decision_version=snapshot["decision_version"],
                content_fingerprint=snapshot["content_fingerprint"],
                ruleset_version=snapshot["ruleset_version"],
                idempotency_key="|".join(
                    (
                        candidate_id,
                        str(snapshot["decision_version"]),
                        snapshot["content_fingerprint"],
                        snapshot["ruleset_version"],
                    )
                ),
                actor_label=actor_label,
            )

    def _commit_atomic(
        self,
        candidate_id: str,
        action: str,
        reason: Optional[str],
        snapshot: Dict[str, Any],
        event_id: str,
    ) -> Optional[str]:
        now = utc_now_iso()
        target_status = (
            CandidateStatus.APPROVED.value if action == "approve" else CandidateStatus.REJECTED.value
        )
        final_result = "MANUALLY_APPROVED" if action == "approve" else "REJECTED"
        try:
            with self.manager.session() as session:
                # 003-B1 TOCTOU: re-read the candidate INSIDE the write
                # transaction. Everything below uses this row, never the
                # earlier snapshot, so a content change between check and
                # write cannot produce a memory from stale, already-scanned
                # text.
                current = CandidateRepository(session).get_by_id(candidate_id)
                if current is None:
                    raise NotFoundError(f"Candidate {candidate_id} not found")
                current_version = int(current.decision_version or 1)
                if (
                    current.status not in APPROVABLE_STATUSES
                    or current_version != snapshot["decision_version"]
                ):
                    raise ConflictError(
                        "Candidate was already decided or its version changed",
                        detail={"code": "candidate_already_decided"},
                    )
                current_fp = self._fingerprint(current)
                if (
                    not current.content_fingerprint
                    or current_fp != snapshot["content_fingerprint"]
                ):
                    raise ValidationError(
                        "Candidate changed between verification and write",
                        detail={"code": "concurrent_content_change"},
                    )
                # G3: final credential gate on the ACTUAL row being written.
                # The ORM before_insert listener is the last backstop.
                assert_writable(
                    current.content,
                    summary=current.summary,
                    metadata_text=current.metadata_json,
                    exact_tokens=self._exact_tokens(),
                    field="memory_records",
                )

                # Conditional UPDATE binds status + version + fingerprint; a
                # concurrent writer either sees rowcount 0 or hits the unique
                # index. Neither can create a second memory.
                updated = session.execute(
                    text(
                        "UPDATE memory_candidates SET status=:status, decided_at=:now, "
                        "decided_by=:actor, reject_reason=:reason, final_result=:final "
                        "WHERE id=:cid AND status IN ('PENDING','PENDING_AUTHORIZATION') "
                        "AND decision_version=:version AND content_fingerprint=:fp"
                    ),
                    {
                        "cid": candidate_id,
                        "status": target_status,
                        "now": now,
                        "actor": SERVER_ACTOR,
                        "reason": reason if action == "reject" else None,
                        "final": final_result,
                        "version": current_version,
                        "fp": current_fp,
                    },
                )
                if updated.rowcount != 1:
                    raise ConflictError(
                        "Candidate was already decided by another request",
                        detail={"code": "candidate_already_decided"},
                    )

                memory_id: Optional[str] = None
                if action == "approve":
                    memory_id = _new_id()
                    record = MemoryRecord(
                        id=memory_id,
                        memory_type=current.memory_type,
                        content=current.content,
                        summary=current.summary,
                        importance=current.importance,
                        confidence=current.confidence,
                        status="ACTIVE",
                        source_id=current.source_id,
                        created_at=now,
                        updated_at=now,
                        accessed_at=None,
                        expires_at=None,
                        metadata_json=json.dumps(
                            {
                                "extracted": True,
                                "engine": current.extraction_engine,
                                "candidate_id": candidate_id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        search_text=segment_for_index(
                            f"{current.content} {current.summary or ''}"
                        ),
                        vector_status="PENDING",
                        source_candidate_id=candidate_id,
                    )
                    memory_repo = MemoryRepository(session)
                    memory_repo.create(record)
                    memory_repo.add_audit(
                        audit_id=_new_id(),
                        memory_id=memory_id,
                        action="CREATE",
                        actor=SERVER_ACTOR,
                        detail={"candidate_id": candidate_id, "status": "ACTIVE"},
                    )

                session.execute(
                    text(
                        "UPDATE approval_events SET memory_id=:mid WHERE event_id=:eid"
                    ),
                    {"mid": memory_id, "eid": event_id},
                )
            return memory_id
        except (ConflictError, IntegrityError) as exc:
            # A unique-index conflict is a duplicate/concurrent approval, not a
            # reason to assert "rolled back". The intent stays UNKNOWN and the
            # recovery pass will converge it from real database state.
            logger.warning("Approval conflict for candidate %s", candidate_id)
            raise DatabaseError(
                "Approval could not be completed; state is indeterminate and must be recovered",
                detail={"code": "approval_indeterminate", "event_id": event_id},
            ) from exc
        except Exception as exc:
            # Never claim ROLLED_BACK here: commit() may have raised after the
            # write reached disk. Leave UNKNOWN for recover_unknown().
            logger.warning("Approval commit result unknown for candidate %s", candidate_id)
            raise DatabaseError(
                "Approval commit result is unknown; state must be recovered",
                detail={"code": "approval_commit_unknown", "event_id": event_id},
            ) from exc

    def _confirm(self, event_id: str, memory_id: Optional[str]) -> None:
        # Separate session: only a NORMAL commit return reaches this point.
        # 003-B1: COMMITTED/SUCCEEDED describe the DATABASE TRANSACTION and the
        # BUSINESS operation only. Authorization was never cryptographically
        # verified in this phase, so it stays NOT_VERIFIED - a successful DB
        # commit must never be reported as a verified human authorization.
        with self.manager.session() as session:
            SecurityEventRepository(session).set_approval_result(
                event_id,
                transaction_result=COMMITTED,
                operation_result=SUCCEEDED,
                memory_id=memory_id,
                authorization_verification_result=AUTH_NOT_VERIFIED,
            )

    def _response(self, candidate_id: str) -> CandidateResponse:
        from ..core.sensitive import categories as sensitive_categories
        from ..schemas.memory import MemoryType

        with self.manager.session() as session:
            candidate = CandidateRepository(session).get_by_id(candidate_id)
            if candidate is None:
                raise NotFoundError(f"Candidate {candidate_id} not found")
            metadata = None
            if candidate.metadata_json:
                try:
                    metadata = json.loads(candidate.metadata_json)
                except (ValueError, TypeError):
                    metadata = None
            return CandidateResponse(
                id=candidate.id,
                conversation_id=candidate.conversation_id,
                source_id=candidate.source_id,
                memory_type=MemoryType(candidate.memory_type),
                content=candidate.content,
                summary=candidate.summary,
                importance=candidate.importance,
                confidence=candidate.confidence,
                status=CandidateStatus(candidate.status),
                duplicate_of=candidate.duplicate_of,
                conflicts_with=candidate.conflicts_with,
                extraction_engine=candidate.extraction_engine,
                extracted_at=candidate.extracted_at,
                decided_at=candidate.decided_at,
                decided_by=candidate.decided_by,
                reject_reason=candidate.reject_reason,
                sensitive_categories=sorted(set(sensitive_categories(candidate.content))),
                metadata=metadata,
                scan_status=candidate.scan_status,
                decision_version=int(candidate.decision_version or 1),
                content_fingerprint=candidate.content_fingerprint,
                ruleset_version=candidate.ruleset_version,
                required_grants=_decode_grants(candidate.required_grants_json),
                app_decision_reported=candidate.app_decision_reported,
                server_security_result=candidate.server_security_result,
                final_result=candidate.final_result,
            )
