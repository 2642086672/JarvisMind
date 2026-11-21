"""API token authentication.

Tokens are compared with hmac.compare_digest so a wrong guess cannot be timed.
The token is read from the environment at startup (or from the local token
file); it is never written to Git and never logged.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header

from .errors import AuthenticationError


def extract_token(
    x_api_token: Optional[str] = None,
    authorization: Optional[str] = None,
) -> Optional[str]:
    """Accept either the X-API-Token header or an Authorization: Bearer header."""
    if x_api_token:
        candidate = x_api_token.strip()
        if candidate:
            return candidate
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()
    return None


def token_matches(candidate: Optional[str], expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


async def require_api_token(
    settings,
    x_api_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """FastAPI dependency enforcing the shared API token."""
    candidate = extract_token(x_api_token, authorization)
    if not token_matches(candidate, settings.api_token):
        raise AuthenticationError("A valid API token is required")
    return candidate
