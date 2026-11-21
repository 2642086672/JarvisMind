"""Error types. Every JarvisMind error response carries ``error.code``; the SDK maps it to a class."""
from __future__ import annotations

import json
from typing import Any, Optional


class JarvisMindError(Exception):
    """Base class. ``code`` is the server's stable, machine-readable error code."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "client_error",
        details: Any = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"[{self.status or 'client'} {self.code}] {self.message}"


class NetworkError(JarvisMindError):
    """The server could not be reached."""


class AuthenticationError(JarvisMindError):
    """401: the token is missing, wrong, expired, revoked, or its agent is disabled."""


class PermissionDeniedError(JarvisMindError):
    """403: the credential lacks the permission or the space grant, or the action is owner-only."""


class NotFoundError(JarvisMindError):
    """404: the memory does not exist, was forgotten, or cannot be read through the agent facade."""


class ConflictError(JarvisMindError):
    """409: the request conflicts with the current state, for example a pending forget request."""


class ValidationFailedError(JarvisMindError):
    """422: the request body is invalid."""


class RateLimitedError(JarvisMindError):
    """429: wait ``retry_after`` seconds before retrying."""


class ServerError(JarvisMindError):
    """5xx: the server failed. Retrying with back-off is safe."""


_BY_STATUS = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationFailedError,
    429: RateLimitedError,
}


def error_for_status(status: int, raw: bytes, retry_after: Optional[str] = None) -> JarvisMindError:
    """Build the matching exception from an HTTP error response."""
    code, message, details = "http_error", f"HTTP {status}", None
    try:
        envelope = json.loads(raw.decode("utf-8") or "{}")
        error = envelope.get("error") or {}
        code = str(error.get("code") or code)
        message = str(error.get("message") or message)
        details = error.get("details")
    except (ValueError, AttributeError):
        pass
    try:
        seconds: Optional[int] = int(retry_after) if retry_after is not None else None
    except ValueError:
        seconds = None
    if status in _BY_STATUS:
        cls = _BY_STATUS[status]
    elif status >= 500:
        cls = ServerError
    else:
        cls = JarvisMindError
    return cls(message, status=status, code=code, details=details, retry_after=seconds)
