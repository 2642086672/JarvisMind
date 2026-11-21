"""Composition-root adapter for MCE-01 (zero I/O at construction).

The adapter is the only place that wires the read-only projection, the
governance gate, the reconstructor and the optional JKG adapter together.
``__init__`` stores references only. A process that never touches
``container.mce_adapter`` pays nothing.

The public surface is intentionally small:

* ``status()`` answers "why is this usable / not usable?" and never raises;
* ``resolve()`` performs one bounded read-only reconstruction and raises only
  ``MceDisabledError`` (administratively off) or a genuine database error.

MCE is a derived module: it consumes JME authority and JKG relations, never
the reverse. No code in ``app.services``, ``app.repositories`` or
``app.models*`` imports ``app.mce``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.errors import JmeError
from ..governance.projection import ReadOnlyProjection
from .config import MceConfig
from .governance import MceGovernance
from .models import ContextResolutionResult
from .reconstructor import MceReconstructor

MCE_RESOLUTION_VERSION = "mce-v1"
MCE_PROJECTION_MODULE = "MCE"


class MceDisabledError(JmeError):
    """The MCE surface is switched off by ``JARVISMIND_MCE_ENABLED``."""

    status_code = 503
    code = "mce_disabled"
    message = "The memory context engine is disabled"


class MceAdapter:
    """Lazily configured, read-only MCE facade."""

    def __init__(
        self,
        manager,
        *,
        config: Optional[MceConfig] = None,
        jkg_adapter=None,
    ) -> None:
        # No connection, no schema probe, no settings read.
        self.manager = manager
        self._config = config
        self.jkg_adapter = jkg_adapter
        self._projection = None
        self._governance = None

    # ----------------------------------------------------------------- config
    def config(self) -> MceConfig:
        if self._config is None:
            self._config = MceConfig.from_env()
        return self._config

    def _settings(self):
        return getattr(self.manager, "settings", None)

    def governance(self) -> MceGovernance:
        if self._governance is None:
            settings = self._settings()
            exact = tuple(
                token
                for token in (
                    getattr(settings, "api_token", None),
                    getattr(settings, "llm_api_key", None),
                )
                if token
            )
            self._governance = MceGovernance(exact_tokens=exact)
        return self._governance

    def projection(self):
        """A capability-restricted ``ModuleProjection`` for MCE.

        ``ReadOnlyProjection.__init__`` only resolves the database path; it
        opens no connection. ``for_module`` registers an opaque token, still
        without opening one.
        """
        if self._projection is None:
            self._projection = ReadOnlyProjection(self.manager).for_module(
                MCE_PROJECTION_MODULE
            )
        return self._projection

    # ----------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        """Never raises. Safe for a health/diagnostic probe."""
        cfg = self.config()
        jkg: Dict[str, Any] = {
            "enabled": cfg.jkg_enabled,
            "state": "unavailable",
            "usable": False,
        }
        if self.jkg_adapter is not None:
            try:
                payload = self.jkg_adapter.status()
                jkg = {
                    "enabled": bool(payload.get("enabled", False)),
                    "state": str(payload.get("state", "unavailable")),
                    "usable": str(payload.get("state")) == "ready",
                }
            except Exception:  # noqa: BLE001 - diagnostic surface must not raise
                jkg = {
                    "enabled": cfg.jkg_enabled,
                    "state": "error",
                    "usable": False,
                }
        return {
            "enabled": cfg.enabled,
            "state": "ready" if cfg.enabled else "disabled",
            "resolution_version": MCE_RESOLUTION_VERSION,
            "read_only": True,
            "database_writes": False,
            "schema_changes": False,
            "projection_module": MCE_PROJECTION_MODULE,
            "config": cfg.as_dict(),
            "jkg": jkg,
        }

    # ---------------------------------------------------------------- resolve
    def resolve(
        self,
        *,
        project_id: Optional[str],
        query: str = "",
        max_tokens: Optional[int] = None,
        max_items: Optional[int] = None,
        include_jkg: bool = True,
        include_superseded: bool = False,
    ) -> ContextResolutionResult:
        cfg = self.config()
        if not cfg.enabled:
            raise MceDisabledError(
                detail={"code": "mce_disabled", "enabled_source": "env"}
            )
        reconstructor = MceReconstructor(
            projection=self.projection(),
            governance=self.governance(),
            config=cfg,
            jkg_adapter=self.jkg_adapter,
        )
        return reconstructor.resolve(
            project_id=project_id,
            query=query,
            max_tokens=max_tokens,
            max_items=max_items,
            include_jkg=include_jkg,
            include_superseded=include_superseded,
        )


__all__ = [
    "MCE_PROJECTION_MODULE",
    "MCE_RESOLUTION_VERSION",
    "MceAdapter",
    "MceDisabledError",
]
