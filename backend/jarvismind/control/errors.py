"""Errors raised by the control plane and the v1 authorization layer.

All classes derive from :class:`JmeError`, so the existing exception handler
returns the unified ``{"error": {"code", "message", "details"}}`` envelope.
"""
from __future__ import annotations

from ..core.errors import ForbiddenError, JmeError, NotFoundError


class OwnerOnlyError(ForbiddenError):
    code = "owner_only"
    message = "This endpoint requires the owner credential"


class PermissionDeniedError(ForbiddenError):
    code = "permission_denied"
    message = "The credential lacks the permission for this operation"


class SpaceAccessDeniedError(ForbiddenError):
    code = "space_access_denied"
    message = "The credential has no access to this memory space"


class SpaceNotFoundError(NotFoundError):
    code = "space_not_found"
    message = "Memory space not found"


class AgentNotFoundError(NotFoundError):
    code = "agent_not_found"
    message = "Agent not found"


class TokenNotFoundError(NotFoundError):
    code = "token_not_found"
    message = "API token not found"


class ControlConflictError(JmeError):
    status_code = 409
    code = "conflict"
    message = "The requested change conflicts with the current state"


class SpaceNotProvisionedError(JmeError):
    status_code = 503
    code = "space_not_provisioned"
    message = "The memory space has no initialized database"


class RateLimitedError(JmeError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests"
