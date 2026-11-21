"""JW-03: controlled modification flow for approved memories.

The problem this solves
-----------------------
Before JW-03, an approved memory (``source_candidate_id IS NOT NULL``) could be
rewritten by a plain ``PATCH``: content, status, ``expires_at`` and - worst of
all - ``metadata_json``, which carries the approval provenance
(``{"candidate_id": ..., "extracted": true}``). A single unauthenticated call
could therefore silently break the link between a memory and the candidate that
authorised it. ``archive`` / ``restore`` / ``expiry-review`` had the same gap.

The invariant
-------------
**An approved memory's protected fields change only through a request that was
explicitly approved, with a server-issued one-time token.**

Design, mirrored from JW-01 so the two flows cannot drift
--------------------------------------------------------
1. ``create_request`` reads the memory and writes ONLY
   ``memory_modification_requests``. Proposing a change never touches
   ``memory_records``.
2. Approval requires a plaintext token only the server issued (stored as a
   SHA-256 digest with an expiry, burnt on first use). The request id alone is
   never sufficient.
3. Approval and execution happen in ONE transaction: either the memory changes
   AND the request becomes EXECUTED, or neither happens.
4. Every transition is a conditional UPDATE; double approval, approval after
   rejection and double execution all fail on ``rowcount == 0``.
5. Identity is reported truthfully: ``API_TOKEN_SHARED`` + ``NOT_VERIFIED``.
   The shared token proves possession of a secret, not who the human is.

Why a separate flow rather than reusing the forget request
----------------------------------------------------------
Forget has one effect (``status -> DELETED``) and one meaning ("stop using this
memory"). Modification carries arbitrary field edits plus three lifecycle moves,
and its approval must be tied to the exact proposed values - otherwise an
approver could be shown "fix the typo" and receive a full body rewrite (TOCTOU
via mutable request). The proposed changes are therefore frozen into
``proposed_changes_json`` at creation and re-read at execution, and the executed
diff is recorded in ``applied_changes_json``.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from ..core.approved_protection import (
    IMMUTABLE_FIELDS,
    PROTECTED_FIELDS,
    ApprovedMemoryProtectedError,
    internal_write,
    is_approved,
)
from ..core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..core.lifecycle_policy import (
    AUTHENTICATION_LEVEL,
    AUTH_NOT_VERIFIED,
    SERVER_ACTOR_LABEL,
    identity_fields,
    require_human_actor,
)
from ..core.timeutils import utc_now, utc_now_iso
from ..database.connection import DatabaseManager
from ..database.modification_schema import ensure_modification_schema
from ..database.models import MemoryRecord
from ..repositories.memory_repository import MemoryRepository
from ..repositories.modification_request_repository import (
    KIND_ARCHIVE,
    KIND_EXTEND,
    KIND_RESTORE,
    KIND_UPDATE,
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ModificationRequestRepository,
    hash_token,
    new_token,
)

logger = logging.getLogger("jme.modification_service")

#: Default lifetime of the one-time approval token, in seconds.
DEFAULT_TOKEN_TTL_SECONDS = 300
MIN_TOKEN_TTL_SECONDS = 30
MAX_TOKEN_TTL_SECONDS = 3600

#: Fields a modification request may propose. Derived bookkeeping
#: (``search_text`` / ``vector_status`` / ``accessed_at``) is maintained by the
#: server and is never proposable; declining it keeps the request honest about
#: what a human actually approved.
PROPOSABLE_FIELDS = frozenset(
    {
        "content",
        "summary",
        "importance",
        "confidence",
        "expires_at",
        "metadata",
        "status",
    }
)

#: Fields that can never be proposed, split out for a precise error message.
_FIELD_ALIASES = {"metadata": "metadata_json"}


class ModificationRequestError(RuntimeError):
    """Base for controlled-modification failures (kept distinct from JmeError)."""


class ModificationNotAllowedError(ForbiddenError):
    """A modification request is not permitted for this memory / payload."""

    status_code = 403
    code = "modification_not_allowed"
    message = "This modification is not permitted"


class ModificationStateConflictError(ConflictError):
    """The request changed state during the operation (lost a race)."""

    status_code = 409
    code = "modification_state_conflict"
    message = "The modification request changed state; retry"


class ModificationTokenError(ForbiddenError):
    """The confirmation token was wrong, expired, or already used."""

    status_code = 403
    code = "modification_token_invalid"
    message = "The confirmation token is invalid, expired, or already used"


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _memory_snapshot(record: MemoryRecord) -> Dict[str, Any]:
    """Pre-image captured at request creation, for later diffing."""
    return {
        "content": record.content,
        "summary": record.summary,
        "status": record.status,
        "importance": record.importance,
        "confidence": record.confidence,
        "memory_type": record.memory_type,
        "expires_at": record.expires_at,
        "metadata_json": record.metadata_json,
    }


def _protected_image(record: MemoryRecord) -> Dict[str, Any]:
    """Bounded pre/post image for the audit record.

    Mirrors ``memory_service._snapshot`` so an operator sees the same shape
    whichever path performed the write; kept local to avoid a service-to-service
    import cycle (memory_service imports this module at container level).
    """
    image: Dict[str, Any] = {}
    for field in (
        "content",
        "summary",
        "status",
        "importance",
        "confidence",
        "memory_type",
        "expires_at",
        "metadata_json",
    ):
        value = getattr(record, field, None)
        if field == "content" and isinstance(value, str) and len(value) > 120:
            value = value[:117] + "..."
        image[field] = value
    return image


def _row_to_response(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a request row for the API (token digest is never echoed)."""
    return {
        "id": str(row["id"]),
        "memory_id": str(row["memory_id"]),
        "request_kind": str(row["request_kind"]),
        "status": str(row["status"]),
        "proposed_changes": _loads(row.get("proposed_changes_json")) or {},
        "reason": row.get("reason"),
        "requested_at": str(row["requested_at"]),
        "decided_at": row.get("decided_at"),
        "decided_by": row.get("decided_by"),
        "executed_at": row.get("executed_at"),
        "applied_changes": _loads(row.get("applied_changes_json")) or None,
        "failure_reason": row.get("failure_reason"),
        "authentication_level": str(row.get("authentication_level") or ""),
        "authorization_verification_result": str(
            row.get("authorization_verification_result") or ""
        ),
    }


class ModificationRequestService:
    """Two-step controlled modification of an approved memory."""

    def __init__(self, manager: DatabaseManager, *, token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS) -> None:
        self.manager = manager
        ttl = int(token_ttl_seconds or DEFAULT_TOKEN_TTL_SECONDS)
        self.token_ttl_seconds = max(MIN_TOKEN_TTL_SECONDS, min(MAX_TOKEN_TTL_SECONDS, ttl))

    # ------------------------------------------------------------------ schema
    def _ensure_schema(self) -> None:
        with self.manager.session() as session:
            ensure_modification_schema(session)

    def _audit(
        self,
        memory_repo: MemoryRepository,
        *,
        memory_id: str,
        action: str,
        detail: Dict[str, Any],
    ) -> None:
        import uuid

        memory_repo.add_audit(
            audit_id=uuid.uuid4().hex,
            memory_id=memory_id,
            action=action,
            actor=SERVER_ACTOR_LABEL,
            detail=detail,
        )

    # ------------------------------------------------------------------ create
    def create_request(
        self,
        memory_id: str,
        *,
        proposed_changes: Optional[Dict[str, Any]] = None,
        request_kind: str = KIND_UPDATE,
        reason: Optional[str] = None,
        requester_label: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str, bool]:
        """Register a modification request. The memory is NOT modified.

        Returns ``(request, token, reused)``. The plaintext token is returned
        exactly once - it is never persisted or logged. ``reused=True`` means a
        PENDING request already existed and its token was rotated.
        """
        self._ensure_schema()
        require_human_actor(requester_label, operation="modification request")

        changes = self._validate_changes(proposed_changes, request_kind=request_kind)
        token = new_token()
        expires_at = (utc_now() + timedelta(seconds=self.token_ttl_seconds)).isoformat()

        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")
            if record.status == "DELETED":
                raise ModificationNotAllowedError(
                    "Forgotten memories cannot be modified",
                    detail={"code": "memory_deleted", "status": record.status},
                )
            if record.status == "MERGED":
                raise ModificationNotAllowedError(
                    "Merged originals are retained as provenance and cannot be modified",
                    detail={"code": "memory_merged", "status": record.status},
                )
            # Only an APPROVED memory needs this flow. A legacy/imported row
            # (source_candidate_id IS NULL) is not protected, so asking for a
            # request would be theatre - refuse and point at the plain endpoint.
            if not is_approved(record):
                raise ModificationNotAllowedError(
                    "This memory was not approved through the candidate chain and "
                    "is not protected; use PATCH /api/v1/memories/{id}",
                    detail={
                        "code": "memory_not_approved",
                        "source_candidate_id": None,
                    },
                )

            self._assert_kind_allowed(record, request_kind=request_kind, changes=changes)

            repo = ModificationRequestRepository(session)
            existing = repo.find_pending(memory_id)
            if existing is not None:
                # JW-03-R1: a pending request already freezes one set of values.
                # Re-issuing its token while IGNORING the newly submitted
                # proposed_changes would mean the client thinks it asked for X
                # while the request still holds Y - exactly the
                # approval/content divergence this rectification removes.
                #
                # So: identical content may reuse the pending row (and rotate the
                # token so the old one stops working); DIFFERENT content is
                # refused and the caller must reject the pending request first.
                frozen = _loads(existing.get("proposed_changes_json")) or {}
                if frozen != changes:
                    raise ModificationStateConflictError(
                        "A different modification request is already pending for "
                        "this memory; reject it before creating a new one",
                        detail={
                            "code": "modification_request_conflict",
                            "pending_request_id": str(existing["id"]),
                            "pending_proposed_changes": frozen,
                            "requested_proposed_changes": changes,
                            "remedy": (
                                "POST /api/v1/modification-requests/"
                                f"{existing['id']}/reject, then create again"
                            ),
                        },
                    )
                rotated = repo.rotate_token(
                    str(existing["id"]),
                    token_hash=hash_token(token),
                    expires_at=expires_at,
                )
                if rotated != 1:
                    raise ModificationStateConflictError(
                        "The existing request changed state; retry",
                        detail={"code": "modification_request_race"},
                    )
                self._audit(
                    memory_repo,
                    memory_id=memory_id,
                    action="MODIFICATION_REQUEST_REISSUED",
                    detail={
                        "modification_request_id": str(existing["id"]),
                        "client_requester_claim": requester_label or "",
                        **identity_fields(requester_label),
                        "note": (
                            "duplicate submission with identical proposed_changes "
                            "reused the pending request; token rotated"
                        ),
                    },
                )
                refreshed = repo.get(str(existing["id"]))
                assert refreshed is not None
                return _row_to_response(refreshed), token, True

            request_id = f"mod_{new_token()[:22]}"
            try:
                repo.insert(
                    request_id=request_id,
                    memory_id=memory_id,
                    request_kind=request_kind,
                    proposed_changes=changes,
                    reason=reason,
                    requester_label=requester_label or "",
                    authentication_level=AUTHENTICATION_LEVEL,
                    authorization_verification_result=AUTH_NOT_VERIFIED,
                    token_hash=hash_token(token),
                    expires_at=expires_at,
                    before_state=_memory_snapshot(record),
                    metadata={"source_candidate_id": record.source_candidate_id},
                )
            except IntegrityError as exc:
                # Lost the race against a concurrent create: the partial unique
                # index refused a second PENDING row. Surface as a conflict, not
                # a 500, and tell the client to re-submit (which will rotate).
                raise ModificationStateConflictError(
                    "Another modification request for this memory is already pending",
                    detail={"code": "modification_request_exists"},
                ) from exc

            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="MODIFICATION_REQUEST_CREATED",
                detail={
                    "modification_request_id": request_id,
                    "request_kind": request_kind,
                    "proposed_fields": sorted(changes.keys()),
                    "client_requester_claim": requester_label or "",
                    **identity_fields(requester_label),
                },
            )
            row = repo.get(request_id)
            assert row is not None
            return _row_to_response(row), token, False

    def _validate_changes(
        self, proposed_changes: Optional[Dict[str, Any]], *, request_kind: str
    ) -> Dict[str, Any]:
        # ARCHIVE / RESTORE carry no field edits: their kind IS the instruction,
        # and the transition is applied by ``_assert_kind_allowed``. Requiring an
        # empty object would be ceremony; allowing arbitrary fields would be a
        # hole, so anything other than an empty/absent dict is refused below.
        if request_kind in {KIND_ARCHIVE, KIND_RESTORE, KIND_EXTEND}:
            changes = dict(proposed_changes or {})
            if request_kind == KIND_ARCHIVE or request_kind == KIND_RESTORE:
                if changes:
                    extra = sorted(changes)
                    raise ValidationError(
                        f"{request_kind} requests take no proposed_changes: "
                        + ", ".join(extra),
                        detail={
                            "code": "field_not_allowed_for_kind",
                            "fields": extra,
                        },
                    )
                return changes
            # EXTEND: only expires_at may be proposed.
            extra = sorted(set(changes) - {"expires_at"})
            if extra:
                raise ValidationError(
                    "EXTEND requests may only propose expires_at: "
                    + ", ".join(extra),
                    detail={"code": "field_not_allowed_for_kind", "fields": extra},
                )
            return changes

        if request_kind != KIND_UPDATE:
            raise ValidationError(
                f"Unknown request_kind: {request_kind}",
                detail={"code": "unknown_request_kind"},
            )

        if not isinstance(proposed_changes, dict) or not proposed_changes:
            raise ValidationError(
                "proposed_changes must be a non-empty object",
                detail={"code": "empty_proposed_changes"},
            )
        immutable = sorted(set(proposed_changes) & IMMUTABLE_FIELDS)
        if immutable:
            raise ModificationNotAllowedError(
                "These fields are immutable and cannot be requested: "
                + ", ".join(immutable),
                detail={"code": "immutable_field", "fields": immutable},
            )
        unknown = sorted(set(proposed_changes) - PROPOSABLE_FIELDS)
        if unknown:
            raise ValidationError(
                "These fields cannot be proposed: " + ", ".join(unknown),
                detail={"code": "field_not_proposable", "fields": unknown},
            )
        if "status" in proposed_changes:
            raise ValidationError(
                "status cannot be proposed through an UPDATE request; "
                "use an ARCHIVE / RESTORE request",
                detail={"code": "status_requires_lifecycle_kind"},
            )
        return dict(proposed_changes)

    def _assert_kind_allowed(
        self,
        record: MemoryRecord,
        *,
        request_kind: str,
        changes: Dict[str, Any],
    ) -> None:
        """Reject a request that could never be executed, at creation time."""
        if request_kind == KIND_ARCHIVE:
            if record.status == "ARCHIVED":
                raise ModificationNotAllowedError(
                    "Memory is already archived",
                    detail={"code": "already_archived"},
                )
            changes.setdefault("status", "ARCHIVED")
        elif request_kind == KIND_RESTORE:
            if record.status != "ARCHIVED":
                raise ModificationNotAllowedError(
                    "Only ARCHIVED memories can be restored",
                    detail={"code": "not_archived", "status": record.status},
                )
            changes.setdefault("status", "ACTIVE")
        elif request_kind == KIND_EXTEND:
            if not changes.get("expires_at"):
                raise ValidationError(
                    "EXTEND requests require expires_at",
                    detail={"code": "expires_at_required"},
                )
            try:
                from ..core.timeutils import parse_iso

                parse_iso(str(changes["expires_at"]))
            except ValueError as exc:
                raise ValidationError(
                    "expires_at must be ISO-8601",
                    detail={"code": "invalid_expires_at"},
                ) from exc

    def verify_for_lifecycle(
        self,
        *,
        request_id: str,
        memory_id: str,
        confirmation_token: str,
        expected_kinds: tuple,
    ) -> Optional[str]:
        """Verify a modification request for archive / restore / expiry-review.

        The request kind must be one of ``expected_kinds``, and the proposed
        values frozen at creation are what actually get applied: an ARCHIVE
        request cannot authorise a restore, and this call never accepts a
        replacement body.

        JW-03-R1 note: the sibling ``verify_for_patch`` that used to authorise a
        PATCH body was DELETED, because it let the approved request and the
        written values diverge. This lifecycle variant is retained because
        archive / restore / expiry carry no client-supplied payload at all - the
        kind fully determines the write, so there is nothing to substitute.
        """
        self._ensure_schema()
        if not confirmation_token:
            raise ModificationTokenError(
                "A confirmation token is required",
                detail={"code": "confirmation_token_required"},
            )
        now = utc_now_iso()
        token_hash = hash_token(confirmation_token)

        with self.manager.session() as session:
            repo = ModificationRequestRepository(session)
            row = repo.get(request_id)
            if row is None:
                raise NotFoundError(f"Modification request {request_id} not found")
            if str(row["memory_id"]) != str(memory_id):
                raise ModificationNotAllowedError(
                    "This modification request targets a different memory",
                    detail={
                        "code": "modification_request_memory_mismatch",
                        "request_memory_id": str(row["memory_id"]),
                    },
                )
            if row["status"] != STATUS_PENDING:
                raise ModificationStateConflictError(
                    f"Request is already {row['status']}; only a PENDING request "
                    "can authorise a lifecycle write",
                    detail={
                        "code": "modification_request_not_pending",
                        "status": row["status"],
                    },
                )
            if str(row["request_kind"]) not in expected_kinds:
                raise ModificationNotAllowedError(
                    f"Request kind {row['request_kind']} cannot authorise this "
                    f"operation (expected one of {', '.join(sorted(expected_kinds))})",
                    detail={
                        "code": "request_kind_mismatch",
                        "request_kind": str(row["request_kind"]),
                        "expected_kinds": sorted(expected_kinds),
                    },
                )
            if repo.consume_token(request_id, token_hash=token_hash, now=now) != 1:
                raise ModificationTokenError(
                    "The confirmation token is invalid, expired, or already used",
                    detail={"code": "modification_token_invalid"},
                )
            if (
                repo.transition(
                    request_id,
                    expected_status=STATUS_PENDING,
                    new_status=STATUS_EXECUTED,
                    values={
                        "decided_at": now,
                        "decided_by": SERVER_ACTOR_LABEL,
                        "executed_at": now,
                        "failure_reason": None,
                        "updated_at": now,
                    },
                )
                != 1
            ):
                raise ModificationStateConflictError(
                    "The request could not be marked executed",
                    detail={"code": "modification_state_write_failed"},
                )
            return request_id

    # -------------------------------------------------------------- read side
    def list_requests(
        self,
        *,
        memory_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        self._ensure_schema()
        with self.manager.session() as session:
            rows, total = ModificationRequestRepository(session).list(
                memory_id=memory_id, status=status, page=page, page_size=page_size
            )
        return [_row_to_response(row) for row in rows], total

    def get_request(self, request_id: str) -> Dict[str, Any]:
        self._ensure_schema()
        with self.manager.session() as session:
            row = ModificationRequestRepository(session).get(request_id)
        if row is None:
            raise NotFoundError(f"Modification request {request_id} not found")
        return _row_to_response(row)

    # ---------------------------------------------------------------- approval
    def approve(
        self,
        request_id: str,
        *,
        confirmation_token: str,
        actor_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Approve AND execute, atomically. This is the only path that writes.

        Either the memory is modified and the request becomes EXECUTED, or the
        transaction rolls back and nothing changed. A second approval finds
        ``status != PENDING`` and fails on the conditional UPDATE.
        """
        self._ensure_schema()
        require_human_actor(actor_label, operation="modification approval")
        if not confirmation_token:
            raise ModificationTokenError(
                "A confirmation token is required",
                detail={"code": "confirmation_token_required"},
            )

        token_hash = hash_token(confirmation_token)
        now = utc_now_iso()

        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            repo = ModificationRequestRepository(session)

            row = repo.get(request_id)
            if row is None:
                raise NotFoundError(f"Modification request {request_id} not found")
            if row["status"] != STATUS_PENDING:
                raise ModificationStateConflictError(
                    f"Request is already {row['status']}; only a PENDING request "
                    "can be approved",
                    detail={
                        "code": "modification_request_not_pending",
                        "status": row["status"],
                    },
                )

            # Verify-and-burn in ONE statement: no check/act window, so two
            # concurrent approvals cannot both succeed.
            if (
                repo.consume_token(request_id, token_hash=token_hash, now=now) != 1
            ):
                raise ModificationTokenError(
                    "The confirmation token is invalid, expired, or already used",
                    detail={"code": "modification_token_invalid"},
                )

            memory_id = str(row["memory_id"])
            record = memory_repo.get_by_id(memory_id)
            if record is None:
                raise NotFoundError(f"Memory {memory_id} not found")

            # Re-check the protection basis: the memory must still be the
            # approved one this request was created for. A merged/forgotten row
            # is refused rather than half-applied.
            if record.status in {"DELETED", "MERGED"}:
                repo.transition(
                    request_id,
                    expected_status=STATUS_PENDING,
                    new_status=STATUS_REJECTED,
                    values={
                        "decided_at": now,
                        "decided_by": SERVER_ACTOR_LABEL,
                        "failure_reason": f"memory became {record.status}",
                        "updated_at": now,
                    },
                )
                raise ModificationNotAllowedError(
                    f"Memory is {record.status} and can no longer be modified",
                    detail={"code": "memory_not_modifiable", "status": record.status},
                )
            if not is_approved(record):
                raise ModificationNotAllowedError(
                    "The approval basis disappeared (source_candidate_id is empty)",
                    detail={"code": "approval_basis_missing"},
                )
            if str(record.source_candidate_id) != str(
                (_loads(row.get("metadata_json")) or {}).get("source_candidate_id")
            ):
                raise ModificationNotAllowedError(
                    "The memory's approval basis changed since this request was created",
                    detail={"code": "approval_basis_changed"},
                )

            proposed = _loads(row.get("proposed_changes_json")) or {}
            applied = self._apply(
                memory_repo,
                record,
                request_kind=str(row["request_kind"]),
                proposed=proposed,
                request_id=request_id,
                actor_label=actor_label,
            )

            # Mark the request EXECUTED only after the memory write succeeded in
            # this same transaction. rowcount 0 here is unreachable in practice
            # (we hold the token), but checked so it can never be silent.
            if (
                repo.transition(
                    request_id,
                    expected_status=STATUS_PENDING,
                    new_status=STATUS_EXECUTED,
                    values={
                        "decided_at": now,
                        "decided_by": SERVER_ACTOR_LABEL,
                        "executed_at": now,
                        "applied_changes_json": _dumps(applied),
                        "updated_at": now,
                    },
                )
                != 1
            ):
                raise ModificationStateConflictError(
                    "The request could not be marked executed",
                    detail={"code": "modification_state_write_failed"},
                )

            self._audit(
                memory_repo,
                memory_id=memory_id,
                action="MODIFICATION_APPROVED",
                detail={
                    "modification_request_id": request_id,
                    "request_kind": str(row["request_kind"]),
                    "applied_fields": sorted(applied.keys()),
                    "client_actor_claim": actor_label or "",
                    **identity_fields(actor_label),
                },
            )

            refreshed = repo.get(request_id)
            assert refreshed is not None
            return _row_to_response(refreshed)

    def _apply(
        self,
        memory_repo: MemoryRepository,
        record: MemoryRecord,
        *,
        request_kind: str,
        proposed: Dict[str, Any],
        request_id: str,
        actor_label: Optional[str],
    ) -> Dict[str, Any]:
        """Translate a request into one authorised write. Returns the applied diff."""
        values: Dict[str, Any] = {}

        if request_kind == KIND_ARCHIVE:
            values["status"] = "ARCHIVED"
        elif request_kind == KIND_RESTORE:
            values["status"] = "ACTIVE"
        elif request_kind == KIND_EXTEND:
            values["expires_at"] = proposed.get("expires_at")
        elif request_kind == KIND_UPDATE:
            for field, value in proposed.items():
                if field == "metadata":
                    values["metadata_json"] = _dumps(value)
                elif field == "status":
                    # Refused at creation; belt-and-braces.
                    raise ValidationError(
                        "status cannot be applied through an UPDATE request",
                        detail={"code": "status_requires_lifecycle_kind"},
                    )
                else:
                    values[field] = value
            if "content" in values or "summary" in values:
                from ..core.search import segment_for_index

                merged = values.get("content", record.content)
                merged_summary = values.get("summary", record.summary)
                values["search_text"] = segment_for_index(
                    f"{merged} {merged_summary or ''}"
                )
        else:  # pragma: no cover - guarded at creation
            raise ValidationError(
                f"Unknown request_kind: {request_kind}",
                detail={"code": "unknown_request_kind"},
            )

        # JW-03-R1: the write applies the FROZEN ``proposed_changes`` read from
        # the request row, never a client body. It therefore enters the internal
        # scope for "an approved modification being executed" rather than using
        # a credentialed bypass - there is no client payload here that could
        # diverge from what was approved.
        with internal_write(
            "approved_modification", operation=f"approve modification {request_id}"
        ):
            before = _protected_image(record)
            memory_repo.update(record, values)
            # §3.4 修改前后的数据审计: record the pre-image, the post-image and
            # the authority that permitted the change. This is written in the
            # SAME transaction as the write, so an executed request can never
            # lack its audit trail.
            memory_repo.add_audit(
                audit_id=uuid.uuid4().hex,
                memory_id=record.id,
                action="UPDATE",
                actor=SERVER_ACTOR_LABEL,
                detail={
                    "reason": "modification_request_approved",
                    "modification_request_id": request_id,
                    "request_kind": request_kind,
                    "before": before,
                    "after": _protected_image(record),
                    "protected": {
                        "authority": "approved_modification_request",
                        "request_id": request_id,
                        "fields": sorted(
                            key for key in values if key in PROTECTED_FIELDS
                        ),
                    },
                    **identity_fields(actor_label),
                },
            )

        return {key: value for key, value in values.items() if key != "search_text"}

    # ---------------------------------------------------------------- rejection
    def reject(
        self,
        request_id: str,
        *,
        reason: Optional[str] = None,
        actor_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Refuse a pending request. The memory is untouched."""
        self._ensure_schema()
        now = utc_now_iso()
        with self.manager.session() as session:
            memory_repo = MemoryRepository(session)
            repo = ModificationRequestRepository(session)
            row = repo.get(request_id)
            if row is None:
                raise NotFoundError(f"Modification request {request_id} not found")
            changed = repo.transition(
                request_id,
                expected_status=STATUS_PENDING,
                new_status=STATUS_REJECTED,
                values={
                    "decided_at": now,
                    "decided_by": SERVER_ACTOR_LABEL,
                    "reason": reason or row.get("reason"),
                    "failure_reason": "rejected by user",
                    "updated_at": now,
                },
            )
            if changed != 1:
                raise ModificationStateConflictError(
                    f"Request is already {row['status']}",
                    detail={
                        "code": "modification_request_not_pending",
                        "status": row["status"],
                    },
                )
            self._audit(
                memory_repo,
                memory_id=str(row["memory_id"]),
                action="MODIFICATION_REJECTED",
                detail={
                    "modification_request_id": request_id,
                    "client_actor_claim": actor_label or "",
                    **identity_fields(actor_label),
                },
            )
            refreshed = repo.get(request_id)
            assert refreshed is not None
            return _row_to_response(refreshed)


__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "ModificationNotAllowedError",
    "ModificationRequestService",
    "ModificationStateConflictError",
    "ModificationTokenError",
    "PROPOSABLE_FIELDS",
]
