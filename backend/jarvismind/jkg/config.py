"""JKG activation and limit configuration (JKG-INT-02).

Two independent switches decide whether the knowledge graph may be used:

* ``system_settings['jkg.enabled']`` — the operator-facing row, read lazily at
  runtime (never at startup, never during import);
* ``JARVISMIND_JKG_ENABLED`` — a process-level kill switch for an operator who needs
  the graph off *now*, without a database write.

The process switch wins when it says OFF: an explicit ``env_disabled`` is a
hard stop that a ``'1'`` row cannot override. The reverse is not true — an
environment value of "on" never turns the graph on by itself unless no row
exists (``enabled_source == "env"``), because the stored row is the reviewed
decision and the environment is only a convenience default.

Everything here is a pure function or a frozen dataclass: no database access,
no schema installation, no import-time side effects. That keeps the resolver
unit-testable and keeps ``import jarvismind.jkg.config`` free of I/O.

Why ``enabled_source`` is part of the result
-------------------------------------------
A bare boolean cannot answer "why is the graph off?" during an incident. The
source label (``default`` / ``setting`` / ``setting_invalid`` / ``env`` /
``env_disabled``) is surfaced through the composition adapter's status
payload, so an operator can tell "nobody switched it on" apart from "somebody
switched it off in the process environment".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

JKG_ENABLED_ENV = "JARVISMIND_JKG_ENABLED"
JKG_PAGE_SIZE_ENV = "JARVISMIND_JKG_PAGE_SIZE"
JKG_MAX_SCAN_ENV = "JARVISMIND_JKG_MAX_SCAN"
JKG_RECONCILE_BATCH_LIMIT_ENV = "JARVISMIND_JKG_RECONCILE_BATCH_LIMIT"

# Hard caps. They mirror ``app.jkg.query`` (MAX_PAGE_SIZE / MAX_SCAN) so a
# config value can never widen what the query service is willing to scan.
MAX_PAGE_SIZE = 100
MAX_SCAN = 1000
MAX_RECONCILE_BATCH_LIMIT = 5000

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_SCAN = 1000
DEFAULT_RECONCILE_BATCH_LIMIT = 1000

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

# Documented, closed set of provenance labels ``resolve_enabled`` can return.
ENABLED_SOURCES = frozenset(
    {"default", "setting", "setting_invalid", "env", "env_disabled"}
)


def _normalize(raw: Optional[str]) -> Optional[str]:
    """Trim + casefold a raw switch value; blank and None both become None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    value = raw.strip().casefold()
    return value or None


def _env_int(name: str, default: int) -> int:
    """Read an integer environment override; raise ValueError on garbage.

    An unset or blank value returns ``default``. A non-integer value raises
    instead of silently degrading a scan/page bound: a typo in an operational
    limit must be visible, not quietly replaced by a different number.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def resolve_enabled(
    *, setting_value: Optional[str], env_value: Optional[str]
) -> Tuple[bool, str]:
    """Resolve the JKG on/off decision and the reason for it.

    Precedence (highest first):

    1. ``env_value`` explicitly OFF (``"0"`` / ``"false"`` / ``"no"`` /
       ``"off"``, compared after ``strip()`` and case folding) →
       ``(False, "env_disabled")``. This is a **hard** stop: it beats the
       database row, because a process-level kill switch must not be
       overridden by stored state.
    2. ``setting_value`` present and non-blank:
       * truthy (``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``) → ``(True, "setting")``
       * falsy → ``(False, "setting")``
       * anything else → ``(False, "setting_invalid")``. Fail-closed: an
         unparseable row disables the graph instead of raising, so a bad row
         can never take the whole API down.
    3. ``env_value`` explicitly ON → ``(True, "env")``.
    4. otherwise (nothing set, or blank values) → ``(False, "default")``.

    The function never raises and never touches the database or the process
    environment; callers pass both values in explicitly.
    """
    env_normalized = _normalize(env_value)
    if env_normalized is not None and env_normalized in _FALSY:
        return False, "env_disabled"

    setting_normalized = _normalize(setting_value)
    if setting_normalized is not None:
        if setting_normalized in _TRUTHY:
            return True, "setting"
        if setting_normalized in _FALSY:
            return False, "setting"
        return False, "setting_invalid"

    if env_normalized is not None and env_normalized in _TRUTHY:
        return True, "env"

    return False, "default"


@dataclass(frozen=True)
class JkgConfig:
    """Immutable JKG activation + bound configuration.

    Construction validates the bounds so a caller cannot build a config that
    lets a query walk further than ``app.jkg.query`` is willing to go. The
    class holds policy only: creating it opens no connection and installs
    nothing.
    """

    enabled: bool = False
    enabled_source: str = "default"
    page_size: int = DEFAULT_PAGE_SIZE
    max_page_size: int = MAX_PAGE_SIZE
    max_scan: int = DEFAULT_MAX_SCAN
    reconcile_batch_limit: int = DEFAULT_RECONCILE_BATCH_LIMIT

    def __post_init__(self) -> None:
        for name in ("page_size", "max_page_size", "max_scan", "reconcile_batch_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer, got {value!r}")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        if not isinstance(self.enabled_source, str) or not self.enabled_source:
            raise ValueError("enabled_source must be a non-empty string")
        if not 1 <= self.max_page_size <= MAX_PAGE_SIZE:
            raise ValueError(
                f"max_page_size must be between 1 and {MAX_PAGE_SIZE}, "
                f"got {self.max_page_size}"
            )
        if not 1 <= self.page_size <= self.max_page_size:
            raise ValueError(
                f"page_size must be between 1 and max_page_size "
                f"({self.max_page_size}), got {self.page_size}"
            )
        if not 1 <= self.max_scan <= MAX_SCAN:
            raise ValueError(
                f"max_scan must be between 1 and {MAX_SCAN}, got {self.max_scan}"
            )
        if not 1 <= self.reconcile_batch_limit <= MAX_RECONCILE_BATCH_LIMIT:
            raise ValueError(
                "reconcile_batch_limit must be between 1 and "
                f"{MAX_RECONCILE_BATCH_LIMIT}, got {self.reconcile_batch_limit}"
            )

    @classmethod
    def from_values(
        cls,
        *,
        setting_value: Optional[str] = None,
        env_value: Optional[str] = None,
        page_size: Optional[int] = None,
        max_scan: Optional[int] = None,
        reconcile_batch_limit: Optional[int] = None,
    ) -> "JkgConfig":
        """Build a config from an already-read row plus explicit overrides.

        ``env_value is None`` means "not supplied" and falls back to
        ``os.environ.get(JARVISMIND_JKG_ENABLED)``; an explicitly empty environment
        value therefore behaves like "unset". The numeric parameters follow the
        same rule against ``JARVISMIND_JKG_PAGE_SIZE`` / ``JARVISMIND_JKG_MAX_SCAN`` /
        ``JARVISMIND_JKG_RECONCILE_BATCH_LIMIT``; a non-integer environment value
        raises ``ValueError`` rather than being ignored.
        """
        if env_value is None:
            env_value = os.environ.get(JKG_ENABLED_ENV)
        enabled, enabled_source = resolve_enabled(
            setting_value=setting_value, env_value=env_value
        )
        resolved_page_size = (
            page_size
            if page_size is not None
            else _env_int(JKG_PAGE_SIZE_ENV, DEFAULT_PAGE_SIZE)
        )
        resolved_max_scan = (
            max_scan
            if max_scan is not None
            else _env_int(JKG_MAX_SCAN_ENV, DEFAULT_MAX_SCAN)
        )
        resolved_reconcile_limit = (
            reconcile_batch_limit
            if reconcile_batch_limit is not None
            else _env_int(
                JKG_RECONCILE_BATCH_LIMIT_ENV, DEFAULT_RECONCILE_BATCH_LIMIT
            )
        )
        return cls(
            enabled=enabled,
            enabled_source=enabled_source,
            page_size=resolved_page_size,
            max_scan=resolved_max_scan,
            reconcile_batch_limit=resolved_reconcile_limit,
        )

    def as_dict(self) -> dict:
        """Plain-dict view for status payloads (no callables, no objects)."""
        payload: dict = {
            "enabled": self.enabled,
            "enabled_source": self.enabled_source,
            "page_size": self.page_size,
            "max_page_size": self.max_page_size,
            "max_scan": self.max_scan,
            "reconcile_batch_limit": self.reconcile_batch_limit,
        }
        return payload


__all__ = [
    "JKG_ENABLED_ENV",
    "JKG_PAGE_SIZE_ENV",
    "JKG_MAX_SCAN_ENV",
    "JKG_RECONCILE_BATCH_LIMIT_ENV",
    "MAX_PAGE_SIZE",
    "MAX_SCAN",
    "MAX_RECONCILE_BATCH_LIMIT",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_MAX_SCAN",
    "DEFAULT_RECONCILE_BATCH_LIMIT",
    "ENABLED_SOURCES",
    "JkgConfig",
    "resolve_enabled",
]
