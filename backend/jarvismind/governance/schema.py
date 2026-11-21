"""Append-only P1 schema installer.

P1 needs two small tables for task governance. They are deliberately kept on a
SEPARATE version line (``system_settings.p1.schema_version``) and are NOT added
to ``app.database.migrations.MIGRATIONS``: that list is frozen at v6 by the
P0-03A safety acceptance, and 27 startup-safety tests assert its behaviour.
Adding v7 there would invalidate that acceptance record.

Rules enforced here:

* Only ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``.
* No DROP, no DELETE, no UPDATE of any existing row except the two
  ``system_settings`` keys owned by P1.
* One transaction: a failure rolls back every DDL and leaves no version marker.
* Idempotent: re-running is a no-op and cannot duplicate objects.
* Pre/post structure checks, data compatibility check, failure path and a
  logical deactivation strategy (``deactivate_p1_schema``).

This installer is never called from the application startup path. It must be
invoked explicitly against a chosen database (tests use isolated tmp databases).
Complexity: O(1) DDL statements, O(1) metadata reads.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from sqlalchemy import text

from ..core.timeutils import utc_now_iso

P1_SCHEMA_VERSION = 1
P1_TABLES: Tuple[str, ...] = ("p1_tasks", "p1_task_events")
P1_OBJECTS: Tuple[str, ...] = (
    "p1_tasks",
    "p1_task_events",
    "idx_p1_tasks_status",
    "idx_p1_task_events_task",
)

P1_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS p1_tasks (
        task_id        TEXT PRIMARY KEY,
        task_type      TEXT NOT NULL,
        status         TEXT NOT NULL,
        owner          TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        pause_reason   TEXT,
        cancel_reason  TEXT,
        failure_reason TEXT,
        metadata_json  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS p1_task_events (
        event_id    TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL,
        run_id      TEXT NOT NULL DEFAULT '',
        event_type  TEXT NOT NULL,
        from_status TEXT NOT NULL DEFAULT '',
        to_status   TEXT NOT NULL DEFAULT '',
        detail_json TEXT,
        created_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_p1_tasks_status ON p1_tasks(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_p1_task_events_task ON p1_task_events(task_id, created_at)",
)

_ALLOWED_PREFIXES = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
)


class P1SchemaError(RuntimeError):
    """Installer precondition or postcondition failed."""


def validate_statements(statements=P1_SCHEMA_STATEMENTS) -> List[str]:
    normalized: List[str] = []
    for raw in statements:
        one = " ".join(raw.strip().split())
        if not one.upper().startswith(_ALLOWED_PREFIXES):
            raise P1SchemaError(f"non-append-only P1 statement refused: {one[:120]}")
        normalized.append(one)
    return normalized


def inspect(manager) -> Dict[str, Any]:
    """Read-only structural snapshot of the P1 surface."""
    with manager.session() as session:
        objects = {
            row[0]
            for row in session.execute(text("SELECT name FROM sqlite_master"))
        }
        present = [name for name in P1_OBJECTS if name in objects]
        version = session.execute(
            text("SELECT value FROM system_settings WHERE key='p1.schema_version'")
        ).scalar()
        counts = {}
        for table in P1_TABLES:
            if table in objects:
                counts[table] = int(
                    session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
                )
        memory_rows = int(
            session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar() or 0
        )
    return {
        "p1_objects_present": present,
        "p1_object_count": len(present),
        "p1_object_total": len(P1_OBJECTS),
        "p1_schema_version": version,
        "p1_row_counts": counts,
        "memory_records_rows": memory_rows,
    }


def _set_setting(session, key: str, value: str) -> None:
    updated = session.execute(
        text(
            "UPDATE system_settings SET value=:v, updated_at=:u WHERE key=:k"
        ),
        {"v": value, "u": utc_now_iso(), "k": key},
    )
    if int(updated.rowcount or 0) == 0:
        session.execute(
            text(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES (:k, :v, :u)"
            ),
            {"k": key, "v": value, "u": utc_now_iso()},
        )


def install_p1_schema(manager) -> Dict[str, Any]:
    """Install (or confirm) the P1 tables in ONE transaction."""
    before = inspect(manager)
    statements = validate_statements()
    with manager.session() as session:
        for statement in statements:
            session.execute(text(statement))
        _set_setting(session, "p1.schema_version", str(P1_SCHEMA_VERSION))
        _set_setting(session, "p1.governance_enabled", "1")
    after = inspect(manager)
    problems: List[str] = []
    if after["p1_object_count"] != after["p1_object_total"]:
        problems.append(f"objects {after['p1_object_count']}/{after['p1_object_total']}")
    if after["p1_schema_version"] != str(P1_SCHEMA_VERSION):
        problems.append(f"p1.schema_version={after['p1_schema_version']}")
    if after["memory_records_rows"] != before["memory_records_rows"]:
        problems.append("memory_records row count changed")
    for table, count in before["p1_row_counts"].items():
        if after["p1_row_counts"].get(table) != count:
            problems.append(f"existing P1 rows changed in {table}")
    return {
        "status": "INSTALLED" if not problems else "POSTCHECK_FAILED",
        "before": before,
        "after": after,
        "statement_total": len(statements),
        "data_compatible": not problems,
        "postcheck_problems": problems,
        "idempotent": before["p1_object_count"] == before["p1_object_total"],
    }


def deactivate_p1_schema(manager) -> Dict[str, Any]:
    """Logical deactivation: flip the owned setting, keep all tables and rows.

    No DROP TABLE and no data deletion - the v6 upgrader's rollback plan applies
    here too (restore the pre-install backup if a physical rollback is needed).
    """
    with manager.session() as session:
        _set_setting(session, "p1.governance_enabled", "0")
    snap = inspect(manager)
    return {
        "status": "DEACTIVATED",
        "p1_objects_retained": snap["p1_object_count"],
        "p1_rows_retained": snap["p1_row_counts"],
        "memory_records_rows": snap["memory_records_rows"],
        "rollback_plan": "restore the pre-install backup; never DROP TABLE",
    }
