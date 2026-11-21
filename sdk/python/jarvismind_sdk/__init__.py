"""JarvisMind Python SDK: a small, dependency-free client for the agent API."""
from .client import JarvisMindClient
from .errors import (
    AuthenticationError,
    ConflictError,
    JarvisMindError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServerError,
    ValidationFailedError,
)

__version__ = "1.0.0"

__all__ = [
    "AuthenticationError",
    "ConflictError",
    "JarvisMindClient",
    "JarvisMindError",
    "NetworkError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitedError",
    "ServerError",
    "ValidationFailedError",
    "__version__",
]
