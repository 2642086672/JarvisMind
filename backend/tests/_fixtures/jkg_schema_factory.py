"""Test-only installer for the proposed JKG tables.

The production code intentionally exposes no installer: creating these tables
is a separate, explicitly approved database change. Tests still need the real
DDL to exercise the repositories, so this helper executes the exact
``JKG_SCHEMA_STATEMENTS`` list against the fixture's temporary database.

It never calls a migration entry point and never opens a database path outside
``manager.settings`` (the shared ``manager`` fixture is already sandboxed by the
P0-03A session guards).
"""
from __future__ import annotations

from sqlalchemy import text

from jarvismind.jkg.schema import validate_statements


def create_jkg_schema(manager) -> None:
    """Create the three proposed tables in the fixture database."""
    statements = validate_statements()
    with manager.session() as session:
        for statement in statements:
            session.execute(text(statement))


__all__ = ["create_jkg_schema"]
