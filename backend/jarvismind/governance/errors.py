"""P1 governance exceptions. All derive from JmeError so the FastAPI error
handler already turns them into safe JSON responses without new wiring."""
from __future__ import annotations

from ..core.errors import BudgetExceededError, ConflictError, ForbiddenError
from ..p1_gate import UnauthorizedWriteError  # noqa: F401  (single exception class)


class UntrustedIdentityError(ForbiddenError):
    code = "untrusted_identity"
    message = "No server-verified identity was supplied"


class GrantVerificationError(ForbiddenError):
    code = "grant_verification_failed"
    message = "Signed grant verification failed"


class CostBlockedError(BudgetExceededError):
    code = "cost_governance_blocked"
    message = "Cost governance blocked this call"


class TaskStateError(ConflictError):
    code = "invalid_task_transition"
    message = "Task state transition is not allowed"


class ProjectionWriteError(ForbiddenError):
    code = "projection_is_read_only"
    message = "Derived modules may only read the JME projection"
