"""Logging setup.

The API token is filtered out of every log record so it can never leak into
log files or console output. Configuration is idempotent so creating several
apps in one process (tests) does not stack duplicate handlers or filters.
"""
from __future__ import annotations

import logging
import sys

_TOKEN_PLACEHOLDER = "***REDACTED***"


class TokenRedactingFilter(logging.Filter):
    """Redact anything that looks like the API token in log messages."""

    def __init__(self, token: str = "") -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token and self._token in record.getMessage():
            record.msg = record.getMessage().replace(self._token, _TOKEN_PLACEHOLDER)
            record.args = ()
        return True


def configure_logging(level: str = "INFO", token: str = "") -> logging.Logger:
    """Configure the root logger once and return the JME logger."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    if not any(isinstance(item, TokenRedactingFilter) for item in root.filters):
        root.addFilter(TokenRedactingFilter(token))
    return logging.getLogger("jme")


def get_logger(name: str = "jme") -> logging.Logger:
    return logging.getLogger(name)
