"""Configuration for the native stdio MCP server.

Everything here is read from explicit values or the process environment.  The
module deliberately imports no backend internals: the MCP server is a thin HTTP
bridge, not part of the service process.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
DEFAULT_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_TIMEOUT = 5.0

ENV_URL = "JARVISMIND_URL"
ENV_TOKEN = "JARVISMIND_AGENT_TOKEN"
ENV_TOKEN_FILE = "JARVISMIND_AGENT_TOKEN_FILE"
ENV_SPACE = "JARVISMIND_SPACE"
ENV_TIMEOUT = "JARVISMIND_MCP_TIMEOUT"

_MISSING_TOKEN_MESSAGE = (
    "No agent token configured. Set --token, "
    f"{ENV_TOKEN}, --token-file or {ENV_TOKEN_FILE}."
)


class MCPConfigurationError(ValueError):
    """Raised when a configuration value cannot be used.

    Messages never contain token material: only variable names and file paths.
    """


def _env_value(environ: Mapping[str, str], name: str) -> Optional[str]:
    value = environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _expand_user_path(value: str) -> Path:
    """Expand ``~`` / ``~/`` / ``~\\`` portably.

    ``os.path.expanduser`` handles both separators on Windows, but the manual
    fallback keeps behaviour stable when only ``HOME`` or ``USERPROFILE`` is set
    (tests and CI containers differ here).
    """
    raw = value.strip()
    if os.sep == "\\" and "/" in raw:
        # Normalise forward slashes so np.expanduser sees a native path.
        raw = raw.replace("/", "\\")
    expanded = os.path.expanduser(raw)
    if expanded.startswith("~"):
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        if home:
            expanded = os.path.join(home, expanded[1:].lstrip("\\/"))
    return Path(expanded)


def _validate_base_url(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        candidate = DEFAULT_BASE_URL
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise MCPConfigurationError(
            "base URL must be an absolute http:// or https:// URL"
        )
    return candidate.rstrip("/")


def _coerce_timeout(value) -> float:
    if value is None:
        return DEFAULT_TIMEOUT
    if isinstance(value, bool):
        raise MCPConfigurationError("timeout must be a positive number of seconds")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MCPConfigurationError(
            "timeout must be a positive number of seconds"
        ) from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or parsed <= 0:
        raise MCPConfigurationError("timeout must be a positive, finite number of seconds")
    return parsed


@dataclass(frozen=True)
class MCPConfig:
    """Resolved MCP bridge configuration.

    ``token`` and ``token_error`` are excluded from ``repr`` so a dataclass dump
    can never leak the credential.
    """

    base_url: str = DEFAULT_BASE_URL
    token: Optional[str] = field(default=None, repr=False)
    token_error: Optional[str] = field(default=None, repr=False)
    space: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        # Frozen dataclass: normalise through object.__setattr__ so a config
        # built directly with a trailing slash behaves like from_values().
        object.__setattr__(self, "base_url", _validate_base_url(str(self.base_url)))
        object.__setattr__(self, "timeout", _coerce_timeout(self.timeout))
        token = self.token.strip() if isinstance(self.token, str) else self.token
        object.__setattr__(self, "token", token or None)
        if isinstance(self.space, str):
            space = self.space.strip()
            object.__setattr__(self, "space", space or None)

    @classmethod
    def from_values(
        cls,
        *,
        url: Optional[str] = None,
        token: Optional[str] = None,
        token_file: Optional[str] = None,
        space: Optional[str] = None,
        timeout=None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "MCPConfig":
        """Resolve a config with the documented precedence.

        ``url``/``timeout``/``space``: explicit value > environment > default.
        Token: ``token`` > ``JARVISMIND_AGENT_TOKEN`` > ``token_file`` >
        ``JARVISMIND_AGENT_TOKEN_FILE``.  An environment token therefore beats an
        explicitly named token file, as documented.
        """
        env = os.environ if environ is None else environ

        resolved_url = url if url is not None and str(url).strip() else _env_value(env, ENV_URL)
        resolved_timeout = timeout if timeout is not None else _env_value(env, ENV_TIMEOUT)
        resolved_space = space if space is not None and str(space).strip() else _env_value(
            env, ENV_SPACE
        )

        resolved_token: Optional[str] = None
        token_error: Optional[str] = None

        explicit_token = token.strip() if isinstance(token, str) else None
        env_token = _env_value(env, ENV_TOKEN)
        if explicit_token:
            resolved_token = explicit_token
        elif env_token:
            resolved_token = env_token
        else:
            explicit_file = token_file.strip() if isinstance(token_file, str) else None
            file_value = explicit_file or _env_value(env, ENV_TOKEN_FILE)
            if file_value:
                path = _expand_user_path(file_value)
                try:
                    content = path.read_text(encoding="utf-8-sig").strip()
                except OSError as exc:
                    token_error = (
                        f"Cannot read the agent token file at {path}: "
                        f"{exc.strerror or exc.__class__.__name__}"
                    )
                else:
                    if content:
                        resolved_token = content
                    else:
                        token_error = f"The agent token file at {path} is empty."
            else:
                token_error = _MISSING_TOKEN_MESSAGE

        if resolved_token is None and token_error is None:
            token_error = _MISSING_TOKEN_MESSAGE

        return cls(
            base_url=resolved_url or DEFAULT_BASE_URL,
            token=resolved_token,
            token_error=token_error,
            space=resolved_space,
            timeout=_coerce_timeout(resolved_timeout),
        )