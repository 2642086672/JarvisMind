"""JKG composition-root adapter (JKG-INT-02, D21 plan A).

This module is the **only** place where the derived knowledge graph is wired
into the JME composition root. ``app.container`` imports it lazily inside a
property body, so:

* importing ``app.jkg`` (or this module) opens no connection, reads no setting
  and installs nothing;
* building the adapter opens no connection either — ``__init__`` only stores
  the manager reference;
* a JME process that never touches ``container.jkg_adapter`` pays nothing.

Why an adapter instead of importing the services directly
--------------------------------------------------------
``JkgService`` / ``JkgQueryService`` answer one question each ("is the schema
installed?") but neither knows about the *activation* switch, and neither can
report its status without raising. Callers (a future HTTP layer in INT-03, a
future JKG-owned worker, an operator health probe) need a stable three-state
machine plus a status payload that is safe to call at any time. The adapter
owns exactly that and delegates everything else.

Degradation contract
--------------------
``status()``, ``health()``, ``schema_status()`` and ``state()`` **never raise**.
They are the diagnostic surface: a missing table, a closed database or a
disabled graph must be *reported*, never thrown, otherwise a health endpoint
turns a degraded graph into a down service. Only ``query_service()`` and
``write_service()`` raise (``JkgDisabledError`` / ``JkgSchemaNotReadyError``),
because those hand out objects that would fail on first use anyway.

An adapter instance never creates a table and never installs a schema. It may
*read* ``sqlite_master`` to report what is present.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from .config import JkgConfig
from .errors import JkgError, JkgSchemaNotReadyError
from .query import JkgQueryService
from .schema import (
    JKG_SCHEMA_VERSION,
    JKG_TABLES,
    missing_jkg_tables,
    present_jkg_tables,
)
from .service import JkgService


class JkgDisabledError(JkgError):
    """The graph exists in code but is switched off by configuration.

    A distinct class (rather than a bare ``JkgSchemaNotReadyError``) because
    the remedy is completely different: "the schema was never installed, ask
    for a database change" versus "an operator switched this off, flip the
    switch". Status is 503 for both — the capability is unavailable, not
    forbidden — but ``error.code`` tells them apart.
    """

    status_code = 503
    code = "jkg_disabled"
    message = "The knowledge graph is disabled"


class JkgState(str, Enum):
    """The three reachable JKG states.

    ``str`` + ``Enum`` (not ``StrEnum``) so ``str(state)`` and ``state.value``
    stay JSON-friendly on every supported Python version without relying on
    the 3.11+ base class.
    """

    DISABLED = "disabled"
    SCHEMA_NOT_READY = "schema_not_ready"
    READY = "ready"


class JkgCompositionAdapter:
    """Lazily-configured, non-throwing entry point to the derived graph."""

    def __init__(self, manager, *, config: Optional[JkgConfig] = None) -> None:
        # Deliberately no I/O: no connection, no settings read, no schema
        # probe. Everything happens on first use and is then cached.
        self.manager = manager
        self._config: Optional[JkgConfig] = config
        self._query_service: Optional[JkgQueryService] = None
        self._write_service: Optional[JkgService] = None

    # ----------------------------------------------------------------- config
    def _load_config(self) -> JkgConfig:
        """Read ``jkg.enabled`` once, then cache the resulting config.

        The read is fully defensive: a missing ``system_settings`` table, a
        locked database or a malformed row must not break the caller. Any
        failure is treated as "no row" so the resolver falls back to its
        documented default (off) instead of propagating an exception out of a
        health probe.
        """
        if self._config is not None:
            return self._config
        setting_value: Optional[str] = None
        try:
            from ..repositories.system_repository import SystemRepository
            from .schema import JKG_ENABLED_KEY

            with self.manager.session() as session:
                setting_value = SystemRepository(session).get(JKG_ENABLED_KEY)
        except Exception:  # noqa: BLE001 - degradation surface must not raise
            setting_value = None
        self._config = JkgConfig.from_values(
            setting_value=setting_value, env_value=None
        )
        return self._config

    def config(self) -> JkgConfig:
        """Return the resolved (and cached) configuration."""
        return self._load_config()

    # ----------------------------------------------------------------- schema
    def schema_status(self) -> Dict[str, Any]:
        """Report which ``jkg_*`` tables exist. Never raises.

        A database that cannot be opened at all is reported as "nothing
        present", which is the truthful answer from the caller's perspective:
        no JKG table can be read through it.
        """
        try:
            with self.manager.session() as session:
                present = present_jkg_tables(session)
                missing = missing_jkg_tables(session)
        except Exception:  # noqa: BLE001 - degradation surface must not raise
            return {
                "schema_ready": False,
                "missing_tables": list(JKG_TABLES),
                "present_tables": [],
            }
        return {
            "schema_ready": not missing,
            "missing_tables": list(missing),
            "present_tables": list(present),
        }

    def state(self) -> JkgState:
        """Fold enablement + schema presence into one state. Never raises."""
        if not self._load_config().enabled:
            return JkgState.DISABLED
        if not self.schema_status()["schema_ready"]:
            return JkgState.SCHEMA_NOT_READY
        return JkgState.READY

    def status(self) -> Dict[str, Any]:
        """Full status snapshot with a fixed key set. Never raises.

        The expected schema version comes from the constant, never from the
        ``jkg.schema_version`` row: verifying the installed version is the
        installer's job (INT-01) and INT-02 must not depend on that row
        existing.
        """
        cfg = self._load_config()
        schema = self.schema_status()
        if not cfg.enabled:
            state = JkgState.DISABLED
        elif not schema["schema_ready"]:
            state = JkgState.SCHEMA_NOT_READY
        else:
            state = JkgState.READY
        return {
            "enabled": bool(cfg.enabled),
            "enabled_source": cfg.enabled_source,
            "state": state.value,
            "schema_ready": bool(schema["schema_ready"]),
            "missing_tables": list(schema["missing_tables"]),
            "present_tables": list(schema["present_tables"]),
            "schema_version_expected": JKG_SCHEMA_VERSION,
            "page_size": cfg.page_size,
            "max_page_size": cfg.max_page_size,
            "max_scan": cfg.max_scan,
        }

    def health(self) -> Dict[str, Any]:
        """Status plus a single ``usable`` flag. Never raises."""
        payload = self.status()
        payload["usable"] = payload["state"] == JkgState.READY.value
        return payload

    # --------------------------------------------------------------- services
    def _require_ready(self) -> JkgConfig:
        """Return the config, or raise the precise reason the graph is unusable."""
        cfg = self.config()
        if not cfg.enabled:
            raise JkgDisabledError(
                detail={"state": "disabled", "enabled_source": cfg.enabled_source}
            )
        schema = self.schema_status()
        if not schema["schema_ready"]:
            raise JkgSchemaNotReadyError(
                "The JKG schema is not installed",
                detail={
                    "code": "jkg_schema_not_ready",
                    "missing": list(schema["missing_tables"]),
                },
            )
        return cfg

    def query_service(self) -> JkgQueryService:
        """Read-only facade. Raises when disabled or the schema is absent."""
        self._require_ready()
        if self._query_service is None:
            self._query_service = JkgQueryService(self.manager)
        return self._query_service

    def write_service(self) -> JkgService:
        """Write facade for JKG-owned workers.

        JME request paths must never call this: the graph is derived data, so
        only a JKG-owned consumer writes it.
        """
        self._require_ready()
        if self._write_service is None:
            self._write_service = JkgService(self.manager)
        return self._write_service


__all__ = [
    "JkgDisabledError",
    "JkgState",
    "JkgCompositionAdapter",
]
