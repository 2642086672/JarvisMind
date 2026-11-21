"""Domain exceptions shared across layers.

The API layer converts these into JSON responses. Messages are safe to show to
clients; internal details stay in the detail dict and the server log.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class JmeError(Exception):
    """Base class for every expected JME failure."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.message
        self.detail: Dict[str, Any] = detail or {}
        super().__init__(self.message)


class NotFoundError(JmeError):
    status_code = 404
    code = "not_found"
    message = "Resource not found"


class ValidationError(JmeError):
    status_code = 422
    code = "validation_error"
    message = "Invalid request"


class AuthenticationError(JmeError):
    status_code = 401
    code = "unauthorized"
    message = "Authentication required"


class ForbiddenError(JmeError):
    status_code = 403
    code = "forbidden"
    message = "Permission denied"


class DirectWriteDisabledError(ForbiddenError):
    """JW-00: a direct-write endpoint was called while its switch is off.

    Distinct from the generic ``forbidden`` so callers can tell "this endpoint
    is administratively disabled" from "you are not allowed at all".
    """

    status_code = 403
    code = "direct_write_disabled"
    message = "Direct memory writes are disabled"


class LifecycleWriteDisabledError(ForbiddenError):
    """JW-02: a memory-lifecycle write entry is administratively disabled.

    Separate from ``DirectWriteDisabledError`` because the remedy is different:
    JW-00's switch guards the raw CRUD writes, while this one guards the
    import / merge / review / resolve family.
    """

    status_code = 403
    code = "lifecycle_write_disabled"
    message = "This memory lifecycle write is disabled"


class PatchBridgeRemovedError(ForbiddenError):
    """JW-03-R1: the PATCH modification bridge no longer exists.

    Distinct from ``ApprovedMemoryProtectedError`` on purpose: this one means
    "the mechanism you are using was removed", not "you lack permission". A
    caller that still sends the legacy headers gets a migration instruction
    (create a request, then approve it) instead of a bare 403.
    """

    status_code = 403
    code = "patch_bridge_removed"
    message = "The PATCH modification bridge was removed; use a modification request"


class ConflictError(JmeError):
    status_code = 409
    code = "conflict"
    message = "Resource conflict"


class CorrectionTargetNotEditableError(ValidationError):
    """JW-02: the chosen memory is in a state that forbids a content rewrite."""

    status_code = 422
    code = "correction_target_not_editable"
    message = "The chosen memory cannot be corrected in its current state"


class MergeSourceNotMergeableError(ValidationError):
    """JW-02: a merge was requested with a source that cannot be merged.

    Covers both "already MERGED" and "DELETED". A dedicated subclass so the
    response's top-level ``error.code`` names the actual reason instead of the
    generic ``validation_error`` (``JmeError.code`` is a class attribute, so a
    detail dict cannot change it).
    """

    status_code = 422
    code = "merge_source_not_mergeable"
    message = "One or more memories cannot be merged"


class AlreadyResolvedError(ValidationError):
    """JW-02: the target object was already decided; the action cannot repeat.

    A dedicated subclass rather than ``detail={"code": ...}`` because
    ``JmeError.code`` is a CLASS attribute: a detail dict does not change the
    top-level ``error.code`` in the response (same lesson JW-00 recorded). A
    caller that only reads ``error.code`` must be able to tell "already decided"
    from "malformed request".

    Status stays 422 for compatibility with the existing clients and tests that
    expect 422 on a repeated review/resolve.
    """

    status_code = 422
    code = "already_resolved"
    message = "This object has already been decided and cannot be decided again"


class DatabaseError(JmeError):
    status_code = 503
    code = "database_error"
    message = "Database operation failed"


class SearchUnavailableError(JmeError):
    status_code = 503
    code = "search_unavailable"
    message = "Full-text search is not available"


class BudgetExceededError(JmeError):
    status_code = 503
    code = "budget_exceeded"
    message = "LLM budget exceeded"


class ExtractionError(JmeError):
    status_code = 502
    code = "extraction_failed"
    message = "Memory extraction failed"


class EmbeddingUnavailableError(JmeError):
    status_code = 503
    code = "embedding_unavailable"
    message = "Embedding model is not available"


class VectorIndexError(JmeError):
    status_code = 503
    code = "vector_index_error"
    message = "Vector index operation failed"
