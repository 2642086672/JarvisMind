"""Proposed JKG schema and its approval/migration contract.

Why this is not a v7 entry in ``app.database.migrations.MIGRATIONS``
--------------------------------------------------------------------
The core migration line is frozen at v6 by the P0-03A acceptance and the
Authority Spec §11. This module therefore follows the same separate-version-line
pattern as ``app.governance.schema`` and ``app.database.forget_schema``:

* the DDL lives outside ``MIGRATIONS`` and never runs from startup;
* only append-only ``CREATE TABLE IF NOT EXISTS`` / ``CREATE UNIQUE INDEX IF
  NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` statements are allowed;
* no ALTER/DROP/DELETE/UPDATE of any existing table or row;
* no installer is exposed here. Installing these tables is a separate,
  explicitly approved database change (see the JKG-CORE-01 design document).
  Until that approval, every JKG service reports ``jkg_schema_not_ready``.

Rollback follows Authority Spec §11: logical deactivation plus restoration of
the pre-change ``jarvismind.db`` / ``jarvismind.db-wal`` / ``jarvismind.db-shm`` backup. ``DROP
TABLE`` is never the rollback mechanism.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

JKG_SCHEMA_VERSION = 1
JKG_SCHEMA_VERSION_KEY = "jkg.schema_version"
JKG_ENABLED_KEY = "jkg.enabled"
JKG_TABLES: Tuple[str, ...] = ("jkg_nodes", "jkg_edges", "jkg_edge_sources")

JKG_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS jkg_nodes (
        node_id          TEXT PRIMARY KEY,
        entity_type      TEXT NOT NULL
                         CHECK (entity_type IN ('PERSON', 'PROJECT', 'EVENT')),
        name             TEXT NOT NULL,
        canonical_name   TEXT NOT NULL,
        normalized_key   TEXT NOT NULL,
        source_memory_id TEXT NOT NULL
                         REFERENCES memory_records(id) ON DELETE CASCADE,
        status           TEXT NOT NULL DEFAULT 'ACTIVE',
        generation       INTEGER NOT NULL DEFAULT 1,
        metadata_json    TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_jkg_nodes_type_key "
    "ON jkg_nodes(entity_type, normalized_key)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_nodes_type "
    "ON jkg_nodes(entity_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_nodes_source_memory "
    "ON jkg_nodes(source_memory_id)",
    """CREATE TABLE IF NOT EXISTS jkg_edges (
        edge_id       TEXT PRIMARY KEY,
        predicate     TEXT NOT NULL
                      CHECK (predicate IN ('WORKS_ON', 'ATTENDS', 'BELONGS_TO', 'KNOWS')),
        subject_id    TEXT NOT NULL
                      REFERENCES jkg_nodes(node_id) ON DELETE CASCADE,
        object_id     TEXT NOT NULL
                      REFERENCES jkg_nodes(node_id) ON DELETE CASCADE,
        status        TEXT NOT NULL DEFAULT 'ACTIVE',
        generation    INTEGER NOT NULL DEFAULT 1,
        confidence    REAL NOT NULL DEFAULT 0.5
                      CHECK (confidence >= 0.0 AND confidence <= 1.0),
        metadata_json TEXT,
        recorded_at   TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        CHECK (subject_id <> object_id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_jkg_edges_triple "
    "ON jkg_edges(subject_id, predicate, object_id)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_edges_subject "
    "ON jkg_edges(subject_id, predicate)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_edges_object "
    "ON jkg_edges(object_id, predicate)",
    """CREATE TABLE IF NOT EXISTS jkg_edge_sources (
        source_id           TEXT PRIMARY KEY,
        edge_id             TEXT NOT NULL
                            REFERENCES jkg_edges(edge_id) ON DELETE CASCADE,
        memory_id           TEXT NOT NULL
                            REFERENCES memory_records(id) ON DELETE CASCADE,
        created_at          TEXT NOT NULL,
        invalidated_at      TEXT,
        invalidation_reason TEXT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_jkg_edge_sources_edge_memory "
    "ON jkg_edge_sources(edge_id, memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_edge_sources_memory "
    "ON jkg_edge_sources(memory_id, invalidated_at)",
    "CREATE INDEX IF NOT EXISTS idx_jkg_edge_sources_edge "
    "ON jkg_edge_sources(edge_id, invalidated_at)",
)

_ALLOWED_PREFIXES = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
    "CREATE UNIQUE INDEX IF NOT EXISTS ",
)

# Human-executed rollback. No SQL is executed by this module.
JKG_ROLLBACK_PLAN: Tuple[str, ...] = (
    "1. Stop every JKG consumer (no extraction, query, or sync process).",
    "2. Back up the current jarvismind.db / jarvismind.db-wal / jarvismind.db-shm three-file set.",
    "3. Set system_settings['jkg.enabled'] = '0' (logical deactivation; tables and rows stay).",
    "4. Verify JME API, search, chat, and JMR continue to work with JKG disabled.",
    "5. For a physical rollback, restore the pre-change three-file backup offline; never DROP TABLE.",
)


class JkgSchemaError(RuntimeError):
    """A proposed statement is not append-only."""


def validate_statements(
    statements: Iterable[str] = JKG_SCHEMA_STATEMENTS,
) -> List[str]:
    """Refuse anything that is not an append-only CREATE ... IF NOT EXISTS."""
    normalised: List[str] = []
    for raw in statements:
        statement = " ".join(str(raw).split())
        upper = statement.upper()
        if not any(upper.startswith(prefix.upper()) for prefix in _ALLOWED_PREFIXES):
            raise JkgSchemaError(
                "JKG schema plan only accepts append-only CREATE statements"
            )
        normalised.append(statement)
    return normalised


def present_jkg_tables(session: Session) -> List[str]:
    """Return the subset of JKG tables that currently exists (read-only probe)."""
    placeholders = ", ".join(f":n{i}" for i in range(len(JKG_TABLES)))
    params = {f"n{i}": name for i, name in enumerate(JKG_TABLES)}
    rows = session.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
            + placeholders
            + ")"
        ),
        params,
    ).scalars()
    present = set(rows)
    return [name for name in JKG_TABLES if name in present]


def missing_jkg_tables(session: Session) -> List[str]:
    present = set(present_jkg_tables(session))
    return [name for name in JKG_TABLES if name not in present]


def jkg_tables_present(session: Session) -> bool:
    return not missing_jkg_tables(session)


__all__ = [
    "JKG_SCHEMA_VERSION",
    "JKG_SCHEMA_VERSION_KEY",
    "JKG_ENABLED_KEY",
    "JKG_TABLES",
    "JKG_SCHEMA_STATEMENTS",
    "JKG_ROLLBACK_PLAN",
    "JkgSchemaError",
    "validate_statements",
    "present_jkg_tables",
    "missing_jkg_tables",
    "jkg_tables_present",
]
