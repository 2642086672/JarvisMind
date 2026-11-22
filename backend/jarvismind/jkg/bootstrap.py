"""Installing the knowledge graph into a brand-new database.

This module lives in the JKG package on purpose. ``database/bootstrap.py`` is
the lowest layer and must not import the graph, so the installer is passed *in*
through ``create_fresh_database(extra_installer=...)`` and runs inside that
function's ``BEGIN IMMEDIATE`` transaction.

Two invariants travel with this code:

* JKG is a **derived, read-only projection** of JME. These statements create
  empty tables; the sync worker fills them from ``memory_records``, and nothing
  in the graph writes back into JME.
* ``JARVISMIND_JKG_ENABLED=0`` outranks the stored ``jkg.enabled`` row. What is
  written here is a default, so an explicit environment kill-switch still wins.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .schema import (
    JKG_ENABLED_KEY,
    JKG_SCHEMA_VERSION,
    JKG_SCHEMA_VERSION_KEY,
    validate_statements,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlite3 import Cursor


def install_into_fresh_database(cursor: "Cursor", now: str) -> None:
    """Create the JKG tables and record the graph as enabled.

    Idempotent: every statement is ``IF NOT EXISTS`` and the settings rows use
    ``INSERT OR REPLACE``, so running twice is harmless. Called only for a
    database that was just created and is still inside its creation
    transaction.
    """
    for statement in validate_statements():
        cursor.execute(statement)
    for key, value in (
        (JKG_ENABLED_KEY, "1"),
        (JKG_SCHEMA_VERSION_KEY, str(JKG_SCHEMA_VERSION)),
    ):
        cursor.execute(
            "INSERT OR REPLACE INTO system_settings (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value, now),
        )


__all__ = ["install_into_fresh_database"]
