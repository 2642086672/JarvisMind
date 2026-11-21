"""Authentication and authorization dependencies for the v1 API.

Every decision is made on the server: the owner credential, agent tokens, the
space grant and the permission set are all checked here. The console and the
SDKs only display what the server allows; hiding a button is never a control.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, Header, Request

from ..control.errors import (
    OwnerOnlyError,
    PermissionDeniedError,
    RateLimitedError,
    SpaceAccessDeniedError,
)
from ..control.permissions import WRITE
from ..control.principal import Principal
from ..control.service import ControlService, validate_space_id
from ..core.errors import AuthenticationError
from ..core.security import extract_token, token_matches

SPACE_HEADER = "X-JarvisMind-Space"


def _control(request: Request) -> ControlService:
    return request.app.state.control


def authenticate(
    request: Request,
    x_api_token: Optional[str],
    authorization: Optional[str],
) -> Principal:
    settings = request.app.state.settings
    control = _control(request)
    limiter = request.app.state.rate_limiter
    candidate = extract_token(x_api_token, authorization)
    principal: Optional[Principal] = None
    if candidate and token_matches(candidate, settings.api_token):
        principal = control.owner_principal()
    elif candidate:
        principal = control.authenticate_token(candidate)
    if principal is None:
        client = request.client.host if request.client else ""
        digest = control.client_digest(client) or "unknown"
        allowed, retry_after = limiter.check(
            f"auth-failure:{digest}", settings.api_auth_failure_limit_per_minute
        )
        if not allowed:
            raise RateLimitedError(detail={"retry_after_seconds": retry_after})
        raise AuthenticationError("A valid API token is required")
    limit = (
        settings.api_rate_limit_owner_per_minute
        if principal.is_owner
        else settings.api_rate_limit_agent_per_minute
    )
    allowed, retry_after = limiter.check(principal.rate_key(), limit)
    if not allowed:
        raise RateLimitedError(detail={"retry_after_seconds": retry_after})
    request.state.principal = principal
    return principal


def select_space(request: Request, principal: Principal, header_value: Optional[str]) -> str:
    """Choose the memory space for this request and enforce that it is allowed.

    Owners may open any active space. Agents may open only spaces they hold a
    grant for; an unknown or ungranted space is answered the same way for
    non-owners so that space ids cannot be enumerated.
    """
    raw = (header_value or "").strip()
    space_id = validate_space_id(raw) if raw else principal.default_space_id
    if principal.is_owner:
        _control(request).require_active_space(space_id)
    elif not principal.can_read(space_id):
        raise SpaceAccessDeniedError(detail={"space_id": space_id})
    request.state.space_id = space_id
    return space_id


def owner_auth(
    request: Request,
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_jarvismind_space: Optional[str] = Header(default=None),
) -> Principal:
    """Route-level dependency for owner-only endpoints (console and operators)."""
    principal = authenticate(request, x_api_token, authorization)
    if not principal.is_owner:
        raise OwnerOnlyError()
    select_space(request, principal, x_jarvismind_space)
    return principal


def principal_auth(
    request: Request,
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_jarvismind_space: Optional[str] = Header(default=None),
) -> Principal:
    """Dependency for endpoints that owners and agents may both call."""
    principal = authenticate(request, x_api_token, authorization)
    select_space(request, principal, x_jarvismind_space)
    return principal


def require(permission: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(principal_auth)) -> Principal:
        if not principal.has(permission):
            raise PermissionDeniedError(detail={"permission": permission})
        return principal

    return dependency


def require_write_space(
    request: Request,
    principal: Principal = Depends(principal_auth),
) -> Principal:
    space_id = getattr(request.state, "space_id", None)
    if space_id is None or not principal.can_write(space_id):
        raise SpaceAccessDeniedError(detail={"access": "readwrite required"})
    return principal


__all__ = [
    "SPACE_HEADER",
    "WRITE",
    "authenticate",
    "owner_auth",
    "principal_auth",
    "require",
    "require_write_space",
    "select_space",
]
