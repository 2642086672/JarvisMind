"""JW-01: append-only schema for memory forget requests.

Why this is NOT a v7 in ``app.database.migrations.MIGRATIONS``
-------------------------------------------------------------
That list is frozen at v6 by the P0-03A safety acceptance: 27 startup-safety
cases and the gate plugin assert ``MIGRATIONS[-1].version == 6`` and
``current_version() == len(MIGRATIONS)``. Adding v7 there would invalidate that
acceptance record. This module therefore follows the same separate-version-line
pattern already used by ``app.governance.schema``:

* its own marker key in ``system_settings`` (``jme.forget_schema_version``),
* ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` only,
* no DROP, no DELETE, no UPDATE of any pre-existing row,
* no touch of ``memory_records`` or ``memory_candidates``,
* idempotent: re-running is a no-op.

It is installed lazily on first use of the forget-request API, never from the
startup path and never from the (disabled) migration runner.

Tables
------
``memory_forget_requests`` — one row per forget request. The original memory is
never modified when a request is created; only an APPROVED request mutates it.

The partial unique index ``uq_forget_pending_per_memory`` is the database-level
guarantee that a memory can have at most ONE pending request, so duplicate
submissions cannot stack up and race each other into two executions.
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.timeutils import utc_now_iso

FORGET_SCHEMA_VERSION = 1
FORGET_TABLE = "memory_forget_requests"
VERSION_KEY = "jme.forget_schema_version"

FORGET_SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS memory_forget_requests (
        id                TEXT PRIMARY KEY,
        memory_id         TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'PENDING',
        request_source    TEXT NOT NULL DEFAULT 'USER_INTERFACE',
        requester_label   TEXT NOT NULL DEFAULT '',
        reason            TEXT,
        authentication_level TEXT NOT NULL DEFAULT 'API_TOKEN_SHARED',
        authorization_verification_result TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
        confirmation_token_hash TEXT,
        confirmation_expires_at TEXT,
        confirmation_consumed_at TEXT,
        requested_at      TEXT NOT NULL,
        decided_at        TEXT,
        decided_by        TEXT,
        decision_source   TEXT,
        executed_at       TEXT,
        execution_result  TEXT,
        failure_reason    TEXT,
        metadata_json     TEXT,
        updated_at        TEXT NOT NULL
    )""",
    # At most one PENDING request per memory (duplicate submissions collide here
    # instead of creating two approvable rows).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_forget_pending_per_memory "
    "ON memory_forget_requests(memory_id) WHERE status = 'PENDING'",
    "CREATE INDEX IF NOT EXISTS idx_forget_requests_status "
    "ON memory_forget_requests(status, requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_forget_requests_memory "
    "ON memory_forget_requests(memory_id, requested_at)",
)

_ALLOWED_PREFIXES = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
    "CREATE UNIQUE INDEX IF NOT EXISTS ",
)


class ForgetSchemaError(RuntimeError):
    """Installer precondition failed (statement is not append-only)."""


def validate_statements(statements: Iterable[str] = FORGET_SCHEMA_STATEMENTS) -> List[str]:
    """Refuse any statement that is not an append-only CREATE ... IF NOT EXISTS."""
    normalised: List[str] = []
    for raw in statements:
        statement = " ".join(str(raw).split())
        upper = statement.upper()
        if not any(upper.startswith(prefix.upper()) for prefix in _ALLOWED_PREFIXES):
            raise ForgetSchemaError(
                "forget schema installer only accepts append-only CREATE statements"
            )
        normalised.append(statement)
    return normalised


def forget_table_exists(session: Session) -> bool:
    row = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": FORGET_TABLE},
    ).scalar()
    return row is not None


def ensure_forget_schema(session: Session) -> bool:
    """Create the forget-request table/indexes if absent. Returns True if created.

    Safe to call on every operation: the existence probe is one indexed
    ``sqlite_master`` lookup, so the DDL path runs at most once per database.
    """
    if forget_table_exists(session):
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
            "value": str(FORGET_SCHEMA_VERSION),
            "updated_at": utc_now_iso(),
        },
    )
    session.flush()
    return True


__all__ = [
    "FORGET_SCHEMA_VERSION",
    "FORGET_SCHEMA_STATEMENTS",
    "FORGET_TABLE",
    "VERSION_KEY",
    "ForgetSchemaError",
    "ensure_forget_schema",
    "forget_table_exists",
    "validate_statements",
]
