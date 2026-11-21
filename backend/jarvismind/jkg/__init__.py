"""Jarvis Knowledge Graph (JKG) core foundations.

JKG is a derived module: it reads JME memories through the read-only projection
and stores only entity/relation/source metadata in its own proposed tables. It
never writes ``memory_records`` or ``memory_candidates``, and no JME layer
(services / repositories / database / core) imports this package at all —
the only inbound seam is the lazy composition-root adapter described below.

Nothing here runs from application startup. The proposed jkg_* tables remain
absent until a separate database change is approved; every service reports
``jkg_schema_not_ready`` in that state.

JKG-INT-02 adds the composition-root entry point. ``JkgCompositionAdapter``
(``app.jkg.adapter``) is the single seam through which ``app.container``
reaches this package, and it is imported lazily inside a property body so that
``import jarvismind.main`` still pulls in no JKG object. The adapter:

* reads the activation switch (``system_settings['jkg.enabled']`` plus the
  ``JARVISMIND_JKG_ENABLED`` override) on first use and caches it;
* reports one of three states (``disabled`` / ``schema_not_ready`` / ``ready``);
* hands out ``JkgQueryService`` / ``JkgService`` only when the graph is usable.

It still **does not install the schema and creates no table.** These tables
stay absent until a separate database change is approved; the adapter only
*reads* ``sqlite_master`` to report what is present. No HTTP route is
registered here either (that is JKG-INT-03).
"""
from __future__ import annotations

from .adapter import JkgCompositionAdapter, JkgDisabledError, JkgState
from .config import (
    JKG_ENABLED_ENV,
    MAX_PAGE_SIZE as DEFAULT_MAX_PAGE_SIZE,
    JkgConfig,
    resolve_enabled,
)
from .errors import (
    JkgEdgeTypeMismatchError,
    JkgError,
    JkgInvalidEntityError,
    JkgInvalidMetadataError,
    JkgInvalidRelationError,
    JkgInvalidSourceError,
    JkgSchemaNotReadyError,
    JkgSelfEdgeError,
    JkgSourceRequiredError,
)
from .normalization import (
    ENTITY_TYPES,
    RELATION_ENDPOINTS,
    RELATION_TYPES,
    VALID_SOURCE_STATUSES,
    canonicalize_entity_name,
    normalize_entity_name,
)
from .query import JkgQueryService
from .schema import (
    JKG_ROLLBACK_PLAN,
    JKG_SCHEMA_STATEMENTS,
    JKG_SCHEMA_VERSION,
    JKG_TABLES,
    jkg_tables_present,
    missing_jkg_tables,
    present_jkg_tables,
    validate_statements,
)
from .service import JkgService


def build_jkg_services(manager):
    """Construct the write/read pair for a DatabaseManager.

    A convenience composition point for a future API/worker. It does not
    install the schema and does not start any background work.
    """
    return JkgService(manager), JkgQueryService(manager)


__all__ = [
    "ENTITY_TYPES",
    "RELATION_TYPES",
    "RELATION_ENDPOINTS",
    "VALID_SOURCE_STATUSES",
    "JKG_TABLES",
    "JKG_SCHEMA_VERSION",
    "JKG_SCHEMA_STATEMENTS",
    "JKG_ROLLBACK_PLAN",
    "JkgError",
    "JkgSchemaNotReadyError",
    "JkgSourceRequiredError",
    "JkgInvalidSourceError",
    "JkgInvalidEntityError",
    "JkgEdgeTypeMismatchError",
    "JkgSelfEdgeError",
    "JkgInvalidRelationError",
    "JkgInvalidMetadataError",
    "JkgService",
    "JkgQueryService",
    "build_jkg_services",
    "canonicalize_entity_name",
    "normalize_entity_name",
    "validate_statements",
    "present_jkg_tables",
    "missing_jkg_tables",
    "jkg_tables_present",
    # JKG-INT-02: composition-root adapter + activation configuration.
    # ``MAX_PAGE_SIZE`` is deliberately re-exported as ``DEFAULT_MAX_PAGE_SIZE``:
    # ``query.MAX_PAGE_SIZE`` already owns that name and the two must not
    # shadow each other in this namespace.
    "JkgConfig",
    "JKG_ENABLED_ENV",
    "DEFAULT_MAX_PAGE_SIZE",
    "resolve_enabled",
    "JkgCompositionAdapter",
    "JkgState",
    "JkgDisabledError",
]
