"""JKG domain errors.

Every error inherits the shared ``JmeError`` hierarchy so a future HTTP layer
can translate it exactly like the rest of JME. The codes are deliberately
specific: callers must be able to tell "the graph schema has not been approved
and installed yet" apart from "this edge has no valid source".
"""
from __future__ import annotations

from ..core.errors import JmeError, ValidationError


class JkgError(JmeError):
    """Base class for expected JKG failures."""

    status_code = 500
    code = "jkg_error"
    message = "Knowledge graph operation failed"


class JkgSchemaNotReadyError(JkgError):
    """The proposed jkg_* tables are absent.

    This is the normal state until the separate schema change is approved and
    installed. It must never trigger an implicit CREATE TABLE.
    """

    status_code = 503
    code = "jkg_schema_not_ready"
    message = "The JKG schema is not installed"


class JkgSourceRequiredError(ValidationError):
    """Every edge must carry at least one JME memory source."""

    code = "jkg_source_required"
    message = "A knowledge-graph edge requires at least one source memory"


class JkgInvalidSourceError(ValidationError):
    """A supplied memory id is missing or not in a source-eligible state."""

    code = "jkg_invalid_source"
    message = "The source memory is missing or not eligible"


class JkgInvalidEntityError(ValidationError):
    """An entity id does not exist or has an unsupported type."""

    code = "jkg_invalid_entity"
    message = "The knowledge-graph entity is invalid"


class JkgEdgeTypeMismatchError(ValidationError):
    """The subject/object entity types do not match the predicate contract."""

    code = "jkg_edge_type_mismatch"
    message = "The entity types do not match this relation"


class JkgSelfEdgeError(ValidationError):
    """An edge may not point from a node to itself."""

    code = "jkg_self_edge"
    message = "A knowledge-graph edge may not be a self-edge"


class JkgInvalidRelationError(ValidationError):
    """The requested predicate is outside the frozen four-relation set."""

    code = "jkg_invalid_relation"
    message = "Unsupported knowledge-graph relation"


class JkgInvalidMetadataError(ValidationError):
    """Metadata must be a small, JSON-serialisable object."""

    code = "jkg_invalid_metadata"
    message = "Knowledge-graph metadata is invalid"


class JkgInitTaskNotFoundError(JkgError):
    """No JKG_INIT task has this id."""

    status_code = 404
    code = "jkg_init_task_not_found"
    message = "The JKG initialization task was not found"


class JkgInitConflictError(JkgError):
    """Another JKG initialization run is already active in this process."""

    status_code = 409
    code = "jkg_init_conflict"
    message = "Another JKG initialization run is in progress"


class JkgInitStateError(JkgError):
    """The requested operation is not allowed in the task's current state."""

    status_code = 409
    code = "jkg_init_invalid_state"
    message = "The JKG initialization task cannot do this in its current state"


class JkgInitP1MissingError(JkgError):
    """The P1 task tables (p1_tasks / p1_task_events) are not installed."""

    status_code = 409
    code = "p1_schema_missing"
    message = "The P1 task schema is not installed"


class JkgInitFinalizeError(JkgError):
    """The lifecycle finalizer callback failed; the task is marked FAILED."""

    status_code = 500
    code = "jkg_init_finalize_failed"
    message = "The JKG initialization finalizer failed"


__all__ = [
    "JkgError",
    "JkgSchemaNotReadyError",
    "JkgSourceRequiredError",
    "JkgInvalidSourceError",
    "JkgInvalidEntityError",
    "JkgEdgeTypeMismatchError",
    "JkgSelfEdgeError",
    "JkgInvalidRelationError",
    "JkgInvalidMetadataError",
    "JkgInitTaskNotFoundError",
    "JkgInitConflictError",
    "JkgInitStateError",
    "JkgInitP1MissingError",
    "JkgInitFinalizeError",
]
