"""JW-01 forget approval service.

The one invariant this module exists to enforce
-----------------------------------------------
**A forget REQUEST never deletes anything. Only an approved request, backed by a
server-issued one-time token, may soft-delete the memory.**

Design consequences, each enforced here rather than by convention:

1. ``create_request`` reads ``memory_records`` and writes only
   ``memory_forget_requests``. There is no code path from creation to deletion.
2. ``approve`` requires a plaintext token that only the server ever issued
   (persisted as a SHA-256 digest with an expiry, burnt on first use). A client
   cannot mint one, and the request id alone is not sufficient.
3. Approval and execution happen inside ONE database transaction. Either the
   memory becomes DELETED *and* the request becomes EXECUTED, or neither
   happens. A failure therefore cannot leave a request claiming success.
4. Every transition is a conditional UPDATE, so double approval, approval after
   rejection, and double execution all fail on ``rowcount == 0`` rather than on
   a fallible pre-check.
5. Model / background / service callers are refused at approval time. There is
   no HTTP surface a tool call could reach and no ``approved=true`` flag.
6. Identity is reported truthfully: ``authentication_level`` is always
   ``API_TOKEN_SHARED`` and ``authorization_verification_result`` is always
   ``NOT_VERIFIED``. The shared token proves possession of a secret, not who the
   human is, so JW-01 never claims verified personal identity.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from ..core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..core.approved_protection import internal_write
from ..core.timeutils import utc_now, utc_now_iso
from ..database.connection import DatabaseManager
from ..database.forget_schema import ensure_forget_schema
from ..repositories.forget_request_repository import (
    ForgetRequestRepository,
    hash_token,
    new_request_id,
)
from ..repositories.memory_repository import MemoryRepository
from ..schemas.forget import (
    NON_HUMAN_ACTOR_TOKENS,
    ForgetRequestResponse,
    ForgetRequestSource,
    ForgetRequestStatus,
)
from ..schemas.memory import MemoryStatus

logger = logging.getLogger("jme.forget")

#: Identity accounting. Fixed values - never derived from a request field.
AUTHENTICATION_LEVEL = "API_TOKEN_SHARED"
AUTH_NOT_VERIFIED = "NOT_VERIFIED"

#: Server-written actor label. Mirrors the 003-B1 convention so the audit trail
#: states, in one place, that no human identity was proven.
SERVER_ACTOR_LABEL = "shared-api-token:human-identity-not-verified"

DECISION_SOURCE_USER = "USER_EXPLICIT_CONFIRMATION"

DEFAULT_TOKEN_TTL_SECONDS = 300


#: Requester label written by the agent facade. Only the owner may decide such a
#: request, because its confirmation token is never shown to the agent.
AGENT_REQUESTER_PREFIX = "agent:"
#: Origin marker the agent facade passes to ``create_request``. Only server code can
#: pass it; the public API refuses the reserved ``agent:`` requester label.
AGENT_FACADE_ORIGIN = "agent-facade"


class ForgetRequestNotFoundError(NotFoundError):
    code = "forget_request_not_found"
    message = "Forget request not found"


class ForgetApprovalRequiredError(ForbiddenError):
    """Raised when a legacy deletion entry point is called without credentials.

    This is the error that stops ``DELETE /memories/{id}`` and
    ``POST /memories/forget?confirm=true`` from being approval bypasses.
    """

    code = "forget_approval_required"
    message = "Deleting a memory requires an approved forget request"


class ForgetTokenInvalidError(ForbiddenError):
    """Token missing, wrong, already used, expired, or issued for another request."""

    code = "forget_token_invalid"
    message = "The forget confirmation token is invalid or has expired"


class ForgetStateConflictError(ConflictError):
    """The request is not in a state that permits the requested action."""

    code = "forget_request_state_conflict"
    message = "The forget request is not in a state that allows this action"


class ModelApprovalForbiddenError(ForbiddenError):
    """A non-human caller tried to approve or reject a forget request."""

    code = "model_approval_forbidden"
    message = "Only an explicit user action may approve a forget request"


class MemoryNotForgettableError(ConflictError):
    code = "memory_not_forgettable"
    message = "The target memory cannot be forgotten"


class _PreconditionFailed(Exception):
    """Internal: a precondition failed inside the transaction, with a reason.

    Carried out of the transaction so the FAILED marker is written AFTER the
    rollback restore, not inside the transaction that is about to be discarded.
    """

    def __init__(self, reason: str, error: Exception) -> None:
        self.reason = reason
        self.error = error
        super().__init__(reason)


def _looks_non_human(label: Optional[str]) -> bool:
    """Conservative check on a CLIENT-SUPPLIED actor label.

    The label is only a claim, so this is not an identity check; it exists so an
    obvious ``actor_label="model"`` cannot be laundered into an approval. Real
    identity enforcement is ``NOT_VERIFIED`` and stated as such everywhere.
    """
    if not label:
        return False
    normalized = str(label).strip().lower().replace("_", "-")
    if normalized in NON_HUMAN_ACTOR_TOKENS:
        return True
    tokens = {
        token
        for token in normalized.replace(":", "-").replace("/", "-").split("-")
        if token
    }
    return bool(tokens & NON_HUMAN_ACTOR_TOKENS)


def _row_to_response(row: Dict[str, Any]) -> ForgetRequestResponse:
    status = ForgetRequestStatus(str(row["status"]))
    return ForgetRequestResponse(
        id=str(row["id"]),
        memory_id=str(row["memory_id"]),
        status=status,
        request_source=str(row["request_source"]),
        requester_label=str(row.get("requester_label") or ""),
        reason=row.get("reason"),
        authentication_level=str(
            row.get("authentication_level") or AUTHENTICATION_LEVEL
        ),
        authorization_verification_result=str(
            row.get("authorization_verification_result") or AUTH_NOT_VERIFIED
        ),
        requested_at=str(row["requested_at"]),
        confirmation_expires_at=row.get("confirmation_expires_at"),
        decided_at=row.get("decided_at"),
        decided_by=row.get("decided_by"),
        decision_source=row.get("decision_source"),
        executed_at=row.get("executed_at"),
        execution_result=row.get("execution_result"),
        failure_reason=row.get("failure_reason"),
        updated_at=str(row["updated_at"]),
        awaiting_user_decision=status is ForgetRequestStatus.PENDING,
        # Only a completed execution may ever claim the memory is gone.
        memory_forgotten=(
            status is ForgetRequestStatus.EXECUTED
            and str(row.get("execution_result") or "") == "DELETED"
        ),
    )


class ForgetApprovalService:
    """Owns the forget-request state machine and its audit trail."""

    def __init__(
        self,
        manager: DatabaseManager,
        *,
        vector_store: Any = None,
        token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> None:
        self.manager = manager
        self.vector_store = vector_store
        self.token_ttl = max(30, int(token_ttl_seconds))
        self._schema_ready = False

    # ------------------------------------------------------------------ utils
    def _ensure_schema(self) -> None:
        """Install the forget-request table in its OWN committed transaction.

        Kept separate so that a later rollback of a business transaction can
        never discard the DDL and leave the following call without a table.
        """
        if self._schema_ready:
            return
        with self.manager.session() as session:
            ensure_forget_schema(session)
        self._schema_ready = True

    def _new_token(self) -> str:
        # 32 bytes of entropy: not guessable, and never derived from the id.
        return secrets.token_urlsafe(32)

    def _expiry(self) -> str:
        return (utc_now() + timedelta(seconds=self.token_ttl)).isoformat()

    def _audit(
        self,
        repo: MemoryRepository,
        *,
        memory_id: str,
        action: str,
        actor: Optional[str],
        detail: Dict[str, Any],
    ) -> None:
        import uuid

        repo.add_audit(
            audit_id=uuid.uuid4().hex,
            memory_id=memory_id,
            action=action,
            actor=actor,
            detail=detail,
        )

    def _remove_vector(self, memory_id: str) -> bool:
        """Best-effort Qdrant removal AFTER the SQLite transaction committed.

        SQLite is authoritative; the vector index is rebuildable. A failure here
        must not turn a committed deletion into a rolled-back one, and must not
        leave ``vector_status`` claiming INDEXED (JME-5 issue A).
        """
        store = self.vector_store
        removed = False
        if store is not None:
            try:
                if getattr(store, "is_ready", False):
                    store.delete_memory(memory_id)
                    removed = True
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Vector delete failed for %s: %s", memory_id, exc)
        try:
            with self.manager.session() as session:
                MemoryRepository(session).update_vector_status(
                    memory_id, "REMOVED", utc_now_iso()
                )
        except Exception:  # pragma: no cover - defensive
            pass
        return removed

    # --------------------------------------------------------- create request
    def create_request(
        self,
        memory_id: str,
        *,
        reason: Optional[str] = None,
        requester_label: Optional[str] = None,
        request_source: str = ForgetRequestSource.USER_INTERFACE.value,
        server_origin: Optional[str] = None,
    ) -> Tuple[ForgetRequestResponse, str, bool]:
        """Create (or reuse) a PENDING forget request. Never mutates the memory.

        Returns ``(response, plaintext_token, reused_existing)``. The plaintext
        token is returned exactly once, here - it is not readable later.
        """
        label = requester_label or ""
        if label.lower().startswith(AGENT_REQUESTER_PREFIX) and server_origin != AGENT_FACADE_ORIGIN:
            raise ValidationError(
                "the 'agent:' requester label is reserved for the agent facade",
                detail={"code": "reserved_requester_label"},
            )
        if request_source not in {item.value for item in ForgetRequestSource}:
            raise ValidationError(
                "Unsupported request source",
                detail={"code": "invalid_request_source"},
            )
        self._ensure_schema()

        token = self._new_token()
        expires_at = self._expiry()

        try:
            return self._create_or_reuse(
                memory_id,
                reason=reason,
                requester_label=requester_label,
                request_source=request_source,
                token=token,
                expires_at=expires_at,
                server_origin=server_origin,
            )
        except IntegrityError:
            # Lost the race against a concurrent create: the unique partial index
            # rejected the second PENDING row. Fall back to the reuse path.
            logger.info("Concurrent forget request insert for %s; reusing", memory_id)
            return self._create_or_reuse(
                memory_id,
                reason=reason,
                requester_label=requester_label,
                request_source=request_source,
                token=token,
                expires_at=expires_at,
                server_origin=server_origin,
            )

    def _create_or_reuse(
        self,
        memory_id: str,
        *,
        reason: Optional[str],
        requester_label: Optional[str],
        request_source: str,
        token: str,
        expires_at: str,
        server_origin: Optional[str],
    ) -> Tuple[ForgetRequestResponse, str, bool]:
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == MemoryStatus.DELETED.value:
                # A deleted memory has nothing to forget. Refuse instead of
                # silently creating a request that can never execute.
                raise MemoryNotForgettableError(
                    "Memory is already forgotten",
                    detail={"code": "memory_already_deleted", "status": record.status},
                )
            if record.status == MemoryStatus.MERGED.value:
                raise MemoryNotForgettableError(
                    "Merged memories are retained as provenance and cannot be forgotten",
                    detail={"code": "memory_merged", "status": record.status},
                )

            repo = ForgetRequestRepository(session)
            existing = repo.find_pending(memory_id)

            if existing is not None:
                existing_id = str(existing["id"])
                existing_label = str(existing.get("requester_label") or "")
                if server_origin == AGENT_FACADE_ORIGIN:
                    # The agent facade never rotates a token. A repeat from the same
                    # agent returns the pending request, and no token reaches the agent.
                    if existing_label != (requester_label or ""):
                        raise ForgetStateConflictError(
                            "A forget request for this memory is already waiting for owner review",
                            detail={"code": "forget_already_pending", "forget_request_id": existing_id},
                        )
                    unchanged = repo.get(existing_id)
                    assert unchanged is not None
                    return _row_to_response(unchanged), "", True
                if existing_label.lower().startswith(AGENT_REQUESTER_PREFIX):
                    # An agent's request has no token the owner can use, so the owner
                    # decides it directly instead of re-issuing a token for it.
                    raise ForgetStateConflictError(
                        "An agent forget request for this memory is pending; decide it directly",
                        detail={"code": "agent_request_pending", "forget_request_id": existing_id},
                    )
                # Duplicate owner submission: keep the single PENDING row and rotate
                # the token so the previous one stops working.
                rotated = repo.rotate_token(
                    str(existing["id"]),
                    token_hash=hash_token(token),
                    expires_at=expires_at,
                )
                if rotated != 1:
                    # Lost a race with a concurrent approve/reject.
                    raise ForgetStateConflictError(
                        "The existing forget request changed state; retry",
                        detail={"code": "forget_request_race"},
                    )
                self._audit(
                    memory_repo,
                    memory_id=memory_id,
                    action="FORGET_REQUEST_REISSUED",
                    actor=SERVER_ACTOR_LABEL,
                    detail={
                        "forget_request_id": str(existing["id"]),
                        "request_source": request_source,
                        "client_requester_claim": requester_label or "",
                        "authentication_level": AUTHENTICATION_LEVEL,
                        "authorization_verification_result": AUTH_NOT_VERIFIED,
                        "note": "duplicate submission reused the pending request",
                    },
                )
                refreshed = repo.get(str(existing["id"]))
                assert refreshed is not None
                return _row_to_response(refreshed), token, True

            request_id = new_request_id()
            repo.insert(
                request_id=request_id,
                memory_id=memory_id,
                request_source=request_source,
                requester_label=requester_label or "",
                reason=reason,
                authentication_level=AUTHENTICATION_LEVEL,
                authorization_verification_result=AUTH_NOT_VERIFIED,
                token_hash=hash_token(token),
                expires_at=expires_at,
                metadata={"client_requester_claim": requester_label or ""},
            )
            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="FORGET_REQUESTED",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "forget_request_id": request_id,
                    "request_source": request_source,
                    "client_requester_claim": requester_label or "",
                    "reason": reason,
                    "memory_status_at_request": record.status,
                    "authentication_level": AUTHENTICATION_LEVEL,
                    "authorization_verification_result": AUTH_NOT_VERIFIED,
                    "note": "memory NOT modified by request creation",
                },
            )
            created = repo.get(request_id)
            assert created is not None
            return _row_to_response(created), token, False

    # ------------------------------------------------------------------- read
    def get_request(self, request_id: str) -> ForgetRequestResponse:
        self._ensure_schema()
        with self.manager.session() as session:
            row = ForgetRequestRepository(session).get(request_id)
            if row is None:
                raise ForgetRequestNotFoundError(
                    f"Forget request {request_id} not found"
                )
            return _row_to_response(row)

    def list_requests(
        self,
        *,
        status: Optional[str] = None,
        memory_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[ForgetRequestResponse], int]:
        if status is not None and status not in {
            item.value for item in ForgetRequestStatus
        }:
            raise ValidationError(
                "Unknown forget request status",
                detail={
                    "code": "invalid_status",
                    "allowed": [item.value for item in ForgetRequestStatus],
                },
            )
        self._ensure_schema()
        with self.manager.session() as session:
            rows, total = ForgetRequestRepository(session).list(
                status=status, memory_id=memory_id, page=page, page_size=page_size
            )
            return [_row_to_response(row) for row in rows], total

    # ---------------------------------------------------------------- approve
    def require_agent_origin(self, request_id: str) -> None:
        """Refuse a token-less owner decision unless the request came from the agent facade.

        The token of an agent-originated request is withheld from the agent at
        creation, so only the owner can decide it without presenting one. Every
        other request must present its token; omitting it is a validation error,
        and the request state is left untouched.
        """
        self._ensure_schema()
        with self.manager.session() as session:
            row = ForgetRequestRepository(session).get(request_id)
        if row is None:
            raise ForgetRequestNotFoundError(f"Forget request {request_id} not found")
        label = str(row.get("requester_label") or "")
        if not label.startswith(AGENT_REQUESTER_PREFIX):
            raise ValidationError(
                "confirmation_token is required for this request",
                detail={"code": "confirmation_token_required"},
            )

    def approve(
        self,
        request_id: str,
        *,
        confirmation_token: Optional[str] = None,
        actor_label: Optional[str] = None,
        owner_authority: bool = False,
    ) -> ForgetRequestResponse:
        """Approve AND execute in one transaction.

        Order of checks (each cheap and each fail-closed):

        1. the request exists;
        2. it is PENDING (so APPROVED/EXECUTED/REJECTED/FAILED cannot re-run);
        3. the caller is not obviously a model/daemon;
        4. the one-time token is valid, unexpired and unused (burned atomically);
        5. the target memory still exists and is still forgettable;
        6. PENDING -> APPROVED, soft delete, APPROVED -> EXECUTED.

        Steps 5-6 share one transaction, so a failure anywhere leaves the memory
        ACTIVE and the request terminal as FAILED: no partial success is
        observable, and no request can sit in APPROVED claiming success.
        """
        if _looks_non_human(actor_label):
            raise ModelApprovalForbiddenError(
                "Model, service and background actors cannot approve a forget request",
                detail={"code": "non_human_actor", "actor_label": actor_label},
            )

        self._ensure_schema()
        digest = hash_token(confirmation_token or "")

        try:
            with self.manager.session() as session:
                memory_id = self._approve_and_execute(
                    session,
                    request_id=request_id,
                    digest=digest,
                    actor_label=actor_label,
                    owner_authority=owner_authority,
                )
        except _PreconditionFailed as failure:
            # The transaction rolled back (memory untouched, token burn undone).
            # Record the terminal FAILED state in a fresh transaction.
            self._mark_failed(request_id, failure.reason)
            raise failure.error
        except Exception as exc:
            # Unexpected failure mid-execution: roll back, then record FAILED
            # only if nothing was committed.
            if not isinstance(
                exc,
                (
                    ForgetTokenInvalidError,
                    ForgetStateConflictError,
                    ModelApprovalForbiddenError,
                    ForgetRequestNotFoundError,
                ),
            ):
                self._mark_failed(request_id, f"{type(exc).__name__}")
            raise

        # Outside the transaction: SQLite already committed the deletion.
        removed = self._remove_vector(memory_id)
        logger.info(
            "Forget request %s executed for memory %s (vector_removed=%s)",
            request_id,
            memory_id,
            removed,
        )
        return self.get_request(request_id)

    def _approve_and_execute(
        self,
        session,
        *,
        request_id: str,
        digest: str,
        actor_label: Optional[str],
        owner_authority: bool = False,
    ) -> str:
        """Run the whole approve+execute flow; return the memory_id.

        Raises ``_PreconditionFailed`` for truthfully-recordable refusals so the
        caller can persist FAILED after the rollback.
        """
        repo = ForgetRequestRepository(session)
        row = repo.get(request_id)
        if row is None:
            raise ForgetRequestNotFoundError(f"Forget request {request_id} not found")

        status = str(row["status"])
        if status != ForgetRequestStatus.PENDING.value:
            raise ForgetStateConflictError(
                f"Forget request is {status}; only PENDING can be approved",
                detail={"code": "forget_request_not_pending", "status": status},
            )

        memory_id = str(row["memory_id"])

        # Atomic verify+burn. A replay or a wrong token matches 0 rows.
        if owner_authority:
            # Owner decision on an agent-originated request: the token was withheld
            # from the requester, so it is burned without being presented.
            label = str(row.get("requester_label") or "")
            if not label.startswith(AGENT_REQUESTER_PREFIX):
                raise ForgetTokenInvalidError(
                    "A confirmation token is required for this request",
                    detail={"code": "forget_token_required"},
                )
            burned = repo.burn_agent_token(request_id) == 1
        else:
            burned = repo.consume_token(request_id, token_hash=digest) == 1
        if not burned:
            raise ForgetTokenInvalidError(
                "The confirmation token is wrong, expired, or already used",
                detail={"code": "forget_token_invalid"},
            )

        memory_repo = MemoryRepository(session)
        record = memory_repo.get_by_id(memory_id)
        if record is None:
            raise _PreconditionFailed(
                "memory_missing",
                MemoryNotForgettableError(
                    "The target memory no longer exists",
                    detail={"code": "memory_missing"},
                ),
            )
        if record.status == MemoryStatus.DELETED.value:
            raise _PreconditionFailed(
                "memory_already_deleted",
                MemoryNotForgettableError(
                    "The target memory is already forgotten",
                    detail={"code": "memory_already_deleted"},
                ),
            )
        if record.status == MemoryStatus.MERGED.value:
            raise _PreconditionFailed(
                "memory_merged",
                MemoryNotForgettableError(
                    "Merged memories are retained as provenance",
                    detail={"code": "memory_merged"},
                ),
            )
        if repo.has_executed(memory_id):
            raise _PreconditionFailed(
                "memory_already_has_executed_request",
                ForgetStateConflictError(
                    "This memory already has an executed forget request",
                    detail={"code": "forget_already_executed"},
                ),
            )

        decided_at = utc_now_iso()
        if (
            repo.transition(
                request_id,
                expected_status=ForgetRequestStatus.PENDING.value,
                new_status=ForgetRequestStatus.APPROVED.value,
                values={
                    "decided_at": decided_at,
                    "decided_by": SERVER_ACTOR_LABEL,
                    "decision_source": DECISION_SOURCE_USER,
                },
            )
            != 1
        ):
            raise ForgetStateConflictError(
                "The forget request changed state during approval",
                detail={"code": "forget_request_not_pending", "status": status},
            )

        self._audit(
            memory_repo,
            memory_id=memory_id,
            action="FORGET_APPROVED",
            actor=SERVER_ACTOR_LABEL,
            detail={
                "forget_request_id": request_id,
                "decision_source": DECISION_SOURCE_USER,
                "client_actor_claim": actor_label or "",
                "authentication_level": AUTHENTICATION_LEVEL,
                "authorization_verification_result": AUTH_NOT_VERIFIED,
            },
        )

        # ----- execution (same transaction as the approval above) ------------
        # JW-03: this is the ONE legitimate path that may set status=DELETED on
        # an approved memory. It routes through MemoryRepository.update (the
        # single authoritative write choke point) inside an explicit
        # forget_approve scope. The scope is correct here rather than a
        # modification request: an approved TWO-STEP forget already IS the
        # approval - demanding a third grant would duplicate JW-01's own
        # server-issued one-time token.
        previous_status = record.status
        with internal_write("forget_approve", operation="forget approve"):
            memory_repo.update(
                record,
                {
                    "status": MemoryStatus.DELETED.value,
                    "forgotten_at": utc_now_iso(),
                    "forget_reason": row.get("reason"),
                },
            )
            # JMR-08 audit P1-8: void derived JMR copies that reference this
            # memory in the same approval step, so a forgotten source cannot
            # leave a reviewable/usable derived copy behind. Logical VOID only.
            from ..jmr.store import DerivedStore

            DerivedStore(self.manager).void_by_source_memory(
                memory_id, session=session
            )

        self._audit(
            memory_repo,
            memory_id=memory_id,
            action="FORGET_EXECUTED",
            actor=SERVER_ACTOR_LABEL,
            detail={
                "forget_request_id": request_id,
                "previous_status": previous_status,
                "reason": row.get("reason"),
                "execution_result": "DELETED",
                "note": "triggered only by an explicit user confirmation",
            },
        )

        if (
            repo.transition(
                request_id,
                expected_status=ForgetRequestStatus.APPROVED.value,
                new_status=ForgetRequestStatus.EXECUTED.value,
                values={
                    "executed_at": utc_now_iso(),
                    "execution_result": "DELETED",
                    "failure_reason": None,
                },
            )
            != 1
        ):
            # Only reachable if something outside this session mutated the row.
            raise ForgetStateConflictError(
                "Forget execution could not be recorded",
                detail={"code": "forget_state_write_failed"},
            )

        return memory_id

    def _mark_failed(self, request_id: str, reason: str) -> None:
        """Persist a terminal FAILED marker in its OWN transaction.

        Called only after the business transaction rolled back, so the memory is
        already restored. It never claims a deletion happened.
        """
        try:
            with self.manager.session() as session:
                repo = ForgetRequestRepository(session)
                row = repo.get(request_id)
                if row is None:
                    return
                if (
                    repo.transition(
                        request_id,
                        expected_status=ForgetRequestStatus.PENDING.value,
                        new_status=ForgetRequestStatus.FAILED.value,
                        values={
                            "executed_at": None,
                            "execution_result": "NOT_EXECUTED",
                            "failure_reason": reason,
                            "decided_at": utc_now_iso(),
                            "decided_by": SERVER_ACTOR_LABEL,
                            "decision_source": "SYSTEM_EXECUTION_FAILED",
                        },
                    )
                    == 1
                ):
                    self._audit(
                        MemoryRepository(session),
                        memory_id=str(row["memory_id"]),
                        action="FORGET_FAILED",
                        actor=SERVER_ACTOR_LABEL,
                        detail={
                            "forget_request_id": request_id,
                            "failure_reason": reason,
                            "execution_result": "NOT_EXECUTED",
                            "note": "transaction rolled back; memory unchanged",
                        },
                    )
        except Exception:  # pragma: no cover - never mask the original error
            logger.warning("Could not record FAILED for forget request %s", request_id)

    # ----------------------------------------------------------------- reject
    def reject(
        self,
        request_id: str,
        *,
        confirmation_token: Optional[str] = None,
        reason: Optional[str] = None,
        actor_label: Optional[str] = None,
        owner_authority: bool = False,
    ) -> ForgetRequestResponse:
        """Reject a PENDING request. The memory is never touched."""
        if _looks_non_human(actor_label):
            raise ModelApprovalForbiddenError(
                "Model, service and background actors cannot decide a forget request",
                detail={"code": "non_human_actor", "actor_label": actor_label},
            )

        self._ensure_schema()
        digest = hash_token(confirmation_token or "")
        with self.manager.session() as session:
            repo = ForgetRequestRepository(session)
            row = repo.get(request_id)
            if row is None:
                raise ForgetRequestNotFoundError(
                    f"Forget request {request_id} not found"
                )
            status = str(row["status"])
            if status != ForgetRequestStatus.PENDING.value:
                raise ForgetStateConflictError(
                    f"Forget request is {status}; only PENDING can be rejected",
                    detail={"code": "forget_request_not_pending", "status": status},
                )
            if owner_authority:
                label = str(row.get("requester_label") or "")
                if not label.startswith(AGENT_REQUESTER_PREFIX):
                    raise ForgetTokenInvalidError(
                        "A confirmation token is required for this request",
                        detail={"code": "forget_token_required"},
                    )
                burned = repo.burn_agent_token(request_id) == 1
            else:
                burned = repo.consume_token(request_id, token_hash=digest) == 1
            if not burned:
                raise ForgetTokenInvalidError(
                    "The confirmation token is wrong, expired, or already used",
                    detail={"code": "forget_token_invalid"},
                )
            if (
                repo.transition(
                    request_id,
                    expected_status=ForgetRequestStatus.PENDING.value,
                    new_status=ForgetRequestStatus.REJECTED.value,
                    values={
                        "decided_at": utc_now_iso(),
                        "decided_by": SERVER_ACTOR_LABEL,
                        "decision_source": DECISION_SOURCE_USER,
                        "failure_reason": reason,
                        "execution_result": "NOT_EXECUTED",
                    },
                )
                != 1
            ):
                raise ForgetStateConflictError(
                    "The forget request changed state during rejection",
                    detail={"code": "forget_request_not_pending", "status": status},
                )
            self._audit(
                MemoryRepository(session),
                memory_id=str(row["memory_id"]),
                action="FORGET_REJECTED",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "forget_request_id": request_id,
                    "reason": reason,
                    "client_actor_claim": actor_label or "",
                    "authentication_level": AUTHENTICATION_LEVEL,
                    "authorization_verification_result": AUTH_NOT_VERIFIED,
                    "note": "memory unchanged",
                },
            )
        return self.get_request(request_id)

    # ----------------------------------------------- legacy entry-point guard
    def approve_via_legacy_endpoint(
        self,
        *,
        memory_id: str,
        forget_request_id: Optional[str],
        confirmation_token: Optional[str],
        actor_label: Optional[str] = None,
    ) -> ForgetRequestResponse:
        """Serve ``DELETE /memories/{id}`` and ``POST /memories/forget``.

        Both are historical direct-delete entries. They are allowed to work ONLY
        as a transport for an already-created forget request: the caller must
        present the request id AND its one-time token, and the request must point
        at the same memory. Without that, the call is refused - which is what
        closes the bypass.
        """
        if not forget_request_id or not confirmation_token:
            raise ForgetApprovalRequiredError(
                "Deleting a memory requires an approved forget request. "
                "Create one with POST /api/v1/memories/{id}/forget-requests, "
                "then approve it with "
                "POST /api/v1/forget-requests/{request_id}/approve",
                detail={
                    "code": "forget_approval_required",
                    "hint": "legacy delete requires forget_request_id + confirmation_token",
                },
            )
        self._ensure_schema()
        with self.manager.session() as session:
            row = ForgetRequestRepository(session).get(forget_request_id)
        if row is None:
            raise ForgetRequestNotFoundError(
                f"Forget request {forget_request_id} not found"
            )
        if str(row["memory_id"]) != memory_id:
            # A token issued for memory A must never delete memory B.
            raise ForgetTokenInvalidError(
                "The forget request does not belong to this memory",
                detail={
                    "code": "forget_token_invalid",
                    "forget_request_id": forget_request_id,
                },
            )
        return self.approve(
            forget_request_id,
            confirmation_token=confirmation_token,
            actor_label=actor_label,
        )


__all__ = [
    "AUTHENTICATION_LEVEL",
    "AUTH_NOT_VERIFIED",
    "DECISION_SOURCE_USER",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "ForgetApprovalRequiredError",
    "ForgetApprovalService",
    "ForgetRequestNotFoundError",
    "ForgetStateConflictError",
    "ForgetTokenInvalidError",
    "MemoryNotForgettableError",
    "ModelApprovalForbiddenError",
    "SERVER_ACTOR_LABEL",
]
