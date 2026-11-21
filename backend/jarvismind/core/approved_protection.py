"""JW-03: protection for approved (authoritative) memories.

What "approved" means here
--------------------------
A memory row is **approved** when ``memory_records.source_candidate_id IS NOT
NULL``. That column is written exactly once, by
``approval_service.approve`` (``app/services/approval_service.py:382``), inside
the transaction that turns a candidate into a formal memory. Nothing else in the
codebase sets it: the import channel explicitly refuses to accept it from a
document (``migration_service.import_document`` builds ``MemoryRecord`` without
it), and ``PATCH`` has no field for it. So the column is a server-issued proof
that a candidate went through the approval chain.

Historical rows (imported, legacy, created by tests) keep it NULL and are NOT
protected by this module. Backfilling them was explicitly rejected by 003-B:
their authorization information was never recorded, so claiming they were
approved would fabricate provenance.

Why a separate module
---------------------
The same protection must apply at five independent write sites (PATCH, archive,
restore, expiry-review, and any future one). If each site re-implemented the
predicate, they would drift - the JW-02 review found exactly that failure mode
in its own identity policy and fixed it with ``core/lifecycle_policy.py``. This
module is the single implementation for JW-03, and a test asserts the protected
field set agrees with the columns each writer actually writes.

The three tiers
---------------
1. **Protected fields** (``content``, ``summary``, ``status``, ``expires_at``,
   ``metadata``, ``memory_type``, ``importance``, ``confidence``): changing any
   of them on an approved memory requires an approved *modification request*.
2. **Derived bookkeeping** (``search_text``, ``vector_status``,
   ``vector_updated_at``, ``accessed_at``, ``updated_at``): the server maintains
   these; they are never a user-supplied field and stay writable internally.
3. **Provenance** (``source_candidate_id``, ``created_at``): immutable on every
   row, approved or not. No API can set them.

The bypass token
----------------
Approved-but-legitimate flows (merge confirmation, correction resolve, forget
approval) must still write protected fields. They cannot mint a modification
request - that would add a second approval step to flows that already have
their own (preview->confirm, resolve, two-step forget). They instead enter an
explicit **internal authorisation scope** via :func:`internal_write`, which is a
context manager. The scope is thread/task-local, must name the reason that
permits the write, and is re-checked on exit so a leaked scope cannot outlive
the operation that opened it.

This is deliberately NOT a boolean parameter threaded through service calls: a
parameter is trivially forgeable by any caller with access to the service, while
a context manager forces the permission grant to be a code-level decision at a
reviewable call site.

Escalation rule
---------------
A protected write with neither an approved request nor an internal scope raises
:class:`ApprovedMemoryProtectedError` (HTTP 403). The message names the exact
remedy so the client is never left guessing.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterable, Iterator, Optional, Set

from ..core.errors import ForbiddenError

#: Columns whose change alters the meaning, state or provenance of a memory.
#: Derived bookkeeping (search_text / vector_status / vector_updated_at /
#: accessed_at / updated_at) is intentionally absent: the server owns it.
PROTECTED_FIELDS: frozenset = frozenset(
    {
        "content",
        "summary",
        "status",
        "expires_at",
        "metadata_json",
        "memory_type",
        "importance",
        "confidence",
        "forgotten_at",
        "forget_reason",
        "merged_into",
        "source_id",
    }
)

#: Fields no write path may ever set, approved or not, in or out of scope.
#: ``source_candidate_id`` is the approval proof itself; ``created_at`` is
#: historical fact; ``id`` is identity.
IMMUTABLE_FIELDS: frozenset = frozenset(
    {"id", "created_at", "source_candidate_id"}
)

#: Reasons an internal scope may be opened for. An unknown reason is refused so
#: a typo cannot silently grant authority; each string maps to a real flow.
INTERNAL_SCOPE_REASONS: frozenset = frozenset(
    {
        # merge confirmation marks sources MERGED and writes merged_into
        "merge_confirm",
        # correction resolve rewrites the chosen memory's content
        "correction_resolve",
        # two-step forget approval performs the only DELETE transition
        "forget_approve",
        # expiry review may extend expires_at or archive
        "expiry_review",
        # summary review archives/re-points nothing but flips review state
        "summary_review",
        # an approved JW-03 modification request being executed
        "approved_modification",
        # archive/restore executed under an approved request
        "approved_lifecycle",
        # JME automatic governance replaces an outdated fact with a new one
        "auto_governance_update",
        # JME automatic governance retires near-identical memories into one
        "auto_governance_merge",
        # JME automatic governance archives a memory whose own expiry passed
        "auto_governance_expire",
    }
)

#: Set of reason strings currently active on this call path. A set (not a bool)
#: so nested scopes cannot be popped by an unrelated exit.
_active_scopes: ContextVar[tuple] = ContextVar("jw03_internal_scopes", default=())


class ApprovedMemoryProtectedError(ForbiddenError):
    """An approved memory was about to be changed without authorisation.

    Separate error code so the client can offer "request a modification"
    instead of showing a generic permission failure.
    """

    status_code = 403
    code = "approved_memory_protected"
    message = (
        "This memory was approved through the candidate approval chain and is "
        "protected; use the approved modification flow to change it"
    )


class ImmutableFieldError(ForbiddenError):
    """A caller tried to write a field that is immutable on every memory row."""

    status_code = 403
    code = "immutable_field"
    message = "This field cannot be written through any API"


class UnknownInternalScopeError(RuntimeError):
    """An internal scope was opened with a reason that is not registered."""


def is_approved(record: Any) -> bool:
    """True when the row carries a server-issued approval proof.

    Reads the attribute defensively: the predicate is used by tests with plain
    namespaces and by code paths that may hold a detached object.
    """
    value = getattr(record, "source_candidate_id", None)
    return value is not None and str(value).strip() != ""


def protected_changes(
    values: Dict[str, Any], *, record: Optional[Any] = None
) -> Set[str]:
    """Which keys in ``values`` actually change a protected field.

    A key whose new value equals the current one is NOT a change: re-asserting
    the existing status is a harmless no-op in the pre-JW-03 update path, and
    JW-03 must not turn that into a 403.

    ``metadata_json`` is compared as text because the service serialises it
    deterministically (sorted keys), so an identical metadata object yields an
    identical string.
    """
    changed: Set[str] = set()
    for key, new_value in values.items():
        if key not in PROTECTED_FIELDS:
            continue
        if record is None:
            changed.add(key)
            continue
        current = getattr(record, key, None)
        if current != new_value:
            changed.add(key)
    return changed


def immutable_violations(values: Iterable[str]) -> Set[str]:
    """Which requested keys are immutable regardless of approval state."""
    return {key for key in values if key in IMMUTABLE_FIELDS}


def _scope_stack() -> tuple:
    return _active_scopes.get()


@contextmanager
def internal_write(reason: str, *, operation: str = "") -> Iterator[None]:
    """Authorise protected writes for the duration of the block.

    ``reason`` must be one of :data:`INTERNAL_SCOPE_REASONS`. The scope is
    recorded with its operation label and drained on exit - including on
    exception - so a failing flow cannot leave authority behind for the next
    request handled by the same context.
    """
    if reason not in INTERNAL_SCOPE_REASONS:
        raise UnknownInternalScopeError(
            f"unregistered internal write reason: {reason!r}"
        )
    token = _active_scopes.set(_scope_stack() + (reason,))
    try:
        yield
    finally:
        # Restore by token, not by slicing: restores the exact prior value even
        # if an inner scope failed to unwind in the expected order.
        _active_scopes.reset(token)


def has_internal_scope(*reasons: str) -> bool:
    """True when at least one of ``reasons`` is active in this context."""
    active = _scope_stack()
    if not active:
        return False
    if not reasons:
        return True
    return any(reason in active for reason in reasons)


def active_scopes() -> tuple:
    """Snapshot of the active reasons; used by audit records and diagnostics."""
    return _scope_stack()


def require_write_authority(
    record: Any,
    values: Dict[str, Any],
    *,
    operation: str,
    approved_request_id: Optional[str] = None,
) -> None:
    """Gate one write against an approved memory.

    Order matters:

    1. immutable fields are refused unconditionally, even inside a scope, so no
       internal flow can rewrite the approval proof;
    2. non-approved rows are unrestricted (legacy behaviour preserved);
    3. an approved row needs either a verifiable approved request or an
       explicit internal scope that names WHY the flow is allowed;
    4. everything else raises :class:`ApprovedMemoryProtectedError`.

    ``approved_request_id`` is not trusted here - the caller is responsible for
    having verified it against the request store. This function only decides
    *whether* authority was claimed; verifying it is the request service's job,
    because that is where the one-time token lives.
    """
    violations = immutable_violations(values.keys())
    if violations:
        raise ImmutableFieldError(
            "Immutable field(s) cannot be written: "
            + ", ".join(sorted(violations)),
            detail={
                "code": "immutable_field",
                "fields": sorted(violations),
                "operation": operation,
            },
        )
    if not is_approved(record):
        return
    changed = protected_changes(values, record=record)
    if not changed:
        return
    if approved_request_id:
        return
    if has_internal_scope():
        return
    raise ApprovedMemoryProtectedError(
        f"{operation} would change protected field(s) on an approved memory: "
        + ", ".join(sorted(changed)),
        detail={
            "code": "approved_memory_protected",
            "operation": operation,
            "fields": sorted(changed),
            "memory_id": getattr(record, "id", None),
            "source_candidate_id": getattr(record, "source_candidate_id", None),
            "remedy": "POST /api/v1/memories/{memory_id}/modification-requests",
        },
    )


@contextmanager
def authorized_write(
    record: Any,
    values: Dict[str, Any],
    *,
    reason: str,
    operation: str = "",
) -> Iterator[None]:
    """Entry-point gate: verify authority, then let the repository re-check.

    Used by ``MemoryService._update_impl`` (the PATCH path). It is STRICT and
    has NO credentials parameter: an approved memory's protected write from this
    function is always refused.

    JW-03-R1 removed the old ``approved_request_id`` parameter together with the
    PATCH bridge. The reason is the whole point of the rectification: a
    parameter that authorises "write these client-supplied values" makes the
    approved request and the written values two different things. Only
    ``ModificationRequestService.approve`` may write protected fields on an
    approved memory, and it does so from the frozen ``proposed_changes``.

    Internal flows with their own approval semantics (merge confirm, correction
    resolve, forget approval, lifecycle transitions) call :func:`internal_write`
    directly instead.
    """
    require_write_authority(record, values, operation=operation or reason)
    if not is_approved(record) or not protected_changes(values, record=record):
        # Nothing protected moves: no scope needed, and opening one would only
        # widen authority for collecting bookkeeping writes.
        yield
        return
    with internal_write(reason, operation=operation):
        yield


__all__ = [
    "ApprovedMemoryProtectedError",
    "IMMUTABLE_FIELDS",
    "INTERNAL_SCOPE_REASONS",
    "ImmutableFieldError",
    "PROTECTED_FIELDS",
    "UnknownInternalScopeError",
    "active_scopes",
    "authorized_write",
    "has_internal_scope",
    "immutable_violations",
    "internal_write",
    "is_approved",
    "protected_changes",
    "require_write_authority",
]
