"""JW-03: append-only schema for approved-memory modification requests.

Why a separate version line (again)
-----------------------------------
``app.database.migrations.MIGRATIONS`` is frozen at v6 by the P0-03A safety
acceptance (27 startup-safety cases assert ``MIGRATIONS[-1].version == 6``).
Adding v7 would invalidate that record, so this module follows the exact
pattern JW-01 established in ``database/forget_schema.py`` and
``governance/schema.py``:

* its own marker key (``jme.modification_schema_version``),
* ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` only,
* no DROP / DELETE / UPDATE of pre-existing rows,
* no touch of ``memory_records``,
* idempotent.

Installed lazily on first use of the modification-request API - never from the
startup path, never from the blocked migration runner.

One table, two request kinds
----------------------------
``memory_modification_requests`` holds both field-level edits
(``request_kind='UPDATE'``) and lifecycle transitions
(``request_kind='ARCHIVE' | 'RESTORE' | 'EXTEND'``). They share the entire state
machine, token handling and audit shape, so splitting them into separate tables
would duplicate four indexes and a concurrency contract for no benefit.

The partial unique index ``uq_modification_pending_per_memory`` guarantees at
most ONE pending request per memory, which is what stops duplicate submissions
from racing each other into two executions.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso

MODIFICATION_SCHEMA_VERSION = 1
MODIFICATION_TABLE = "memory_modification_requests"
VERSION_KEY = "jme.modification_schema_version"

MODIFICATION_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS memory_modification_requests (
        id                TEXT PRIMARY KEY,
        memory_id         TEXT NOT NULL,
        request_kind      TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'PENDING',
        proposed_changes_json TEXT NOT NULL,
        reason            TEXT,
        requester_label   TEXT NOT NULL DEFAULT '',
        authentication_level TEXT NOT NULL DEFAULT 'API_TOKEN_SHARED',
        authorization_verification_result TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
        confirmation_token_hash TEXT,
        confirmation_expires_at TEXT,
        confirmation_consumed_at TEXT,
        requested_at      TEXT NOT NULL,
        decided_at        TEXT,
        decided_by        TEXT,
        executed_at       TEXT,
        applied_changes_json TEXT,
        before_state_json TEXT,
        failure_reason    TEXT,
        metadata_json     TEXT,
        updated_at        TEXT NOT NULL
    )""",
    # At most one PENDING request per memory: duplicate submissions collide here
    # instead of producing two independently approvable rows.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_modification_pending_per_memory "
    "ON memory_modification_requests(memory_id) WHERE status = 'PENDING'",
    "CREATE INDEX IF NOT EXISTS idx_modification_requests_status "
    "ON memory_modification_requests(status, requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_modification_requests_memory "
    "ON memory_modification_requests(memory_id, requested_at)",
)

_ALLOWED_PREFIXES = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
    "CREATE UNIQUE INDEX IF NOT EXISTS ",
)


class ModificationSchemaError(RuntimeError):
    """Installer precondition failed (statement is not append-only)."""


def validate_statements(
    statements: Iterable[str] = MODIFICATION_SCHEMA_STATEMENTS,
) -> List[str]:
    """Refuse any statement that is not an append-only CREATE ... IF NOT EXISTS."""
    normalised: List[str] = []
    for raw in statements:
        statement = " ".join(str(raw).split())
        upper = statement.upper()
        if not any(upper.startswith(prefix.upper()) for prefix in _ALLOWED_PREFIXES):
            raise ModificationSchemaError(
                "modification schema installer only accepts append-only CREATE statements"
            )
        normalised.append(statement)
    return normalised


def modification_table_exists(session: Session) -> bool:
    row = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": MODIFICATION_TABLE},
    ).scalar()
    return row is not None


def ensure_modification_schema(session: Session) -> bool:
    """Create the modification-request table/indexes if absent.

    Returns True when this call created the schema. Safe to call on every
    operation: the probe is one indexed ``sqlite_master`` lookup.
    """
    if modification_table_exists(session):
        return False
    for statement in validate_statements():
        session.execute(text(statement))
    session.execute(
        text(
            "INSERT OR REPLACE INTO system_settings(key, value, updated_at) "
            "VALUES (:key, :value, :updated_at)"
        ),
        {
            "key": VERSION_KEY,
            "value": str(MODIFICATION_SCHEMA_VERSION),
            "updated_at": utc_now_iso(),
        },
    )
    session.flush()
    return True


__all__ = [
    "MODIFICATION_SCHEMA_STATEMENTS",
    "MODIFICATION_SCHEMA_VERSION",
    "MODIFICATION_TABLE",
    "VERSION_KEY",
    "ModificationSchemaError",
    "ensure_modification_schema",
    "modification_table_exists",
    "validate_statements",
]
