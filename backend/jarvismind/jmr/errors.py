"""Typed JMR errors. Nothing here swallows an unexpected exception."""
from __future__ import annotations

from typing import Any, Dict, Optional


class JMRError(RuntimeError):
    """Base class for JMR orchestration failures.

    ``status_code``/``code`` are consumed by the API exception handler. A JMR
    state conflict is a 409 by default; subclasses narrow it.
    """

    code = "jmr_error"
    status_code = 409

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class JMRTaskPaused(JMRError):
    """The owning task was paused externally between two stages."""


class JMRTaskCancelled(JMRError):
    """The owning task was cancelled externally between two stages."""


class JMRProvenanceError(JMRError):
    """A derived result references a source that is not an authorised projection row."""

    code = "jmr_provenance_invalid"


class JMRUnsafeTextError(JMRError):
    """A proposal was produced from text that never passed the content scan."""

    code = "jmr_unsafe_text"


class JMRUnavailableError(JMRError):
    """A required dependency (normally the P1 task tables) is missing."""

    code = "p1_schema_missing"


class JMRNotFoundError(JMRError):
    """Requested proposal id does not exist among JMR derived rows."""

    code = "jmr_not_found"
    status_code = 404


class JMRStateConflictError(JMRError):
    """The proposal/task is not in a state that permits the operation."""

    code = "jmr_state_conflict"


class JMRConcurrencyError(JMRError):
    """A second run of the same task was attempted while it is RUNNING."""

    code = "jmr_run_conflict"


class JMRReviewRequiredError(JMRError):
    """A JMR-derived row may only be reviewed through the JMR review bridge."""

    code = "jmr_review_required"


class JMRExpiredError(JMRError):
    """The proposal has passed its TTL and was logically invalidated."""

    code = "jmr_proposal_expired"


class JMRStaleSourceError(JMRError):
    """A source memory changed after the proposal was generated."""

    code = "jmr_source_stale"


class JMRStageTimeoutError(JMRError):
    """A JMR stage exceeded its configured execution budget."""

    code = "jmr_stage_timeout"


class JMRPrivacyBlockedError(JMRError):
    """A JMR write was refused by the privacy gate (HOLD or DENY).

    ``403`` (not ``409``): the request is well-formed but the content is not
    authorised to become memory. ``detail`` carries category names only.
    """

    code = "jmr_privacy_blocked"
    status_code = 403
