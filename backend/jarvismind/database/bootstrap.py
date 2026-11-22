"""Explicit creation of a NEW JarvisMind database.

Startup never reaches this module. It is called only by ``python -m jarvismind
init-db`` and by owner-initiated memory-space provisioning. It refuses any
database that already contains tables, so an existing store is never migrated,
rebuilt or altered here. The schema comes from ``MIGRATIONS``, which is pure data.

Creation runs inside one explicit ``BEGIN IMMEDIATE`` transaction on the raw
driver connection. SQLite DDL is transactional, so a failure leaves no partial
schema behind. Anything a fresh install must also contain is passed *in*
through ``extra_installer``: this is the lowest layer, so it never imports the
knowledge graph (which depends on it).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ..core.timeutils import utc_now_iso
from .connection import DatabaseManager
from .migrations import MIGRATIONS, SCHEMA_MIGRATIONS_TABLE

#: A hook run *inside* the database-creation transaction.
#:
#: The callable receives the live DBAPI cursor and the creation timestamp, and
#: must be idempotent. Running it in the same transaction is what makes "the
#: database exists" and "everything it needs exists" one atomic step. The cursor
#: is typed loosely because this module must not import a driver-specific type.
SchemaInstaller = Callable[[Any, str], None]

FTS5_SETTING_KEY = "jme.fts5_enabled"
SCHEMA_VERSION_SETTING_KEY = "jme.schema_version"

_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
)


class DatabaseAlreadyInitializedError(RuntimeError):
    """The target database already contains tables and is left untouched."""


def _user_table_count(cursor) -> int:
    row = cursor.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return int(row[0]) if row else 0


def _column_present(cursor, statement: str) -> bool:
    match = _ADD_COLUMN.match(statement)
    if match is None:
        return False
    table, column = match.group(1), match.group(2)
    rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]).lower() == column.lower() for row in rows)


def _fts5_available(cursor) -> bool:
    rows = cursor.execute("PRAGMA compile_options").fetchall()
    return any(str(row[0]).upper() == "ENABLE_FTS5" for row in rows)


def create_fresh_database(
    manager: DatabaseManager,
    *,
    extra_installer: Optional[SchemaInstaller] = None,
) -> int:
    """Create the complete schema in an empty database and return its version.

    ``extra_installer`` lets the caller add its own tables inside the same
    transaction. The composition root passes the knowledge-graph installer so a
    fresh install is complete on first launch, while this module stays free of
    any dependency on it.
    """
    code_version = MIGRATIONS[-1].version
    raw = manager.engine.raw_connection()
    try:
        driver = raw.driver_connection
        # SQLAlchemy types this as optional, but a raw connection always has a
        # DBAPI driver behind it.
        assert driver is not None
        cursor = driver.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            if _user_table_count(cursor) > 0:
                raise DatabaseAlreadyInitializedError(
                    "the database already contains tables; initialization does not modify it"
                )
            cursor.execute(SCHEMA_MIGRATIONS_TABLE)
            fts5_ok = _fts5_available(cursor)
            now = utc_now_iso()
            for migration in MIGRATIONS:
                for statement in migration.statements:
                    if _column_present(cursor, statement):
                        continue
                    cursor.execute(statement)
                if fts5_ok:
                    for statement in migration.fts5_statements:
                        cursor.execute(statement)
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, now),
                )
            cursor.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (FTS5_SETTING_KEY, "1" if fts5_ok else "0", now),
            )
            cursor.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (SCHEMA_VERSION_SETTING_KEY, str(code_version), now),
            )
            if extra_installer is not None:
                extra_installer(cursor, now)
            driver.commit()
        except Exception:
            driver.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()
    return code_version


__all__ = [
    "DatabaseAlreadyInitializedError",
    "SchemaInstaller",
    "create_fresh_database",
]
