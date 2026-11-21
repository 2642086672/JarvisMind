"""P1 ORM write gate state - deliberately a standalone top-level module.

It lives OUTSIDE the ``governance`` package on purpose: ``models.py`` is
imported very early (SQLAlchemy declarative base, every repository). If models
imported ``app.governance`` at module level, the package ``__init__`` would
import ``audit_bus`` -> ``models`` and create a circular import. This module has
zero application imports, so models can depend on it safely.

Semantics match ``governance/context.py`` (which re-exports everything here):

* ``_ACTIVE`` is process-level switch set by build_governance(enforce=True).
* ``_CURRENT`` is a ContextVar carrying the decision valid for one write.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .core.errors import ForbiddenError


class UnauthorizedWriteError(ForbiddenError):
    """Raised by the ORM gate when a formal-memory write has no authorization."""

    code = "unauthorized_memory_write"
    message = "Original memory write is not authorised by the governance layer"


@dataclass(frozen=True)
class WriteContext:
    """The decision that authorised one concrete write."""

    principal_id: str
    operation: str
    object_ref: str
    trigger: str
    task_id: str = ""
    run_id: str = ""
    grant_id: str = ""


_CURRENT: ContextVar[Optional[WriteContext]] = ContextVar(
    "p1_write_context", default=None
)
_MAINTENANCE: ContextVar[bool] = ContextVar("p1_maintenance_write", default=False)
_ACTIVE: Optional[Any] = None
_DENIAL_HOOK: Optional[Any] = None


def set_denial_hook(callback: Optional[Any]) -> None:
    """Register an audit callback for ORM-gate denials (no module coupling)."""
    global _DENIAL_HOOK
    _DENIAL_HOOK = callback


def get_denial_hook() -> Optional[Any]:
    return _DENIAL_HOOK


def current_write_context() -> Optional[WriteContext]:
    return _CURRENT.get()


@contextmanager
def authorized_write(context: WriteContext) -> Iterator[WriteContext]:
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


@contextmanager
def maintenance_write() -> Iterator[None]:
    """Mark SERVER-INTERNAL bookkeeping writes (accessed_at / vector_status /
    FTS search_text) so the gates allow them without a per-request grant.

    This is deliberately NOT usable by HTTP request bodies: a client cannot
    obtain this context, and it permits no content/status changes by design at
    the call sites that use it. Review P1 finding: enabling governance used to
    turn ordinary GET / index maintenance into 500s.
    """
    token = _MAINTENANCE.set(True)
    try:
        yield
    finally:
        _MAINTENANCE.reset(token)


def maintenance_active() -> bool:
    return bool(_MAINTENANCE.get())


def activate(governor: Any) -> Any:
    """Enable the gate; return the previous value for exact restoration."""
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = governor
    return previous


def deactivate(previous: Any = None) -> None:
    global _ACTIVE
    _ACTIVE = previous


def active_governor() -> Any:
    return _ACTIVE


def is_active() -> bool:
    governor = _ACTIVE
    return governor is not None and bool(getattr(governor, "enforce", False))


# ---------------------------------------------------------------------------
# P0-2 fix: connection-level write gate.
#
# Independent review P0-2 proved that mapper-level events (before_insert /
# before_update) do NOT cover session.execute(text(...)), Core update(),
# Query.update(), or bulk_* - all of those reach the DBAPI cursor directly.
# This guard is installed on the Engine's "before_cursor_execute" event, which
# every one of those paths must pass through, and is therefore the last common
# choke point before SQLite.
#
# Detection is deliberately FAIL-CLOSED: any statement that mentions a
# protected table and is not an obvious read is treated as a write. A CTE such
# as "WITH ... UPDATE memory_records ..." is not mistaken for a SELECT simply
# because it starts with WITH.
# ---------------------------------------------------------------------------
PROTECTED_TABLES: tuple = ("memory_records", "memory_candidates")
# Server-internal bookkeeping columns that maintenance_write() may change.
# Everything else (content, summary, status, metadata_json, ...) is denied even
# inside a maintenance window.
MAINTENANCE_COLUMNS = frozenset(
    {"accessed_at", "vector_status", "vector_updated_at", "search_text"}
)
_PROTECTED_RE = None
_READ_ONLY_RE = None


def _compile_patterns():
    global _PROTECTED_RE, _READ_ONLY_RE
    if _PROTECTED_RE is None:
        import re

        _PROTECTED_RE = re.compile(
            r"\b(" + "|".join(PROTECTED_TABLES) + r")\b", re.IGNORECASE
        )
        _READ_ONLY_RE = re.compile(
            r"^\s*(SELECT|PRAGMA|EXPLAIN|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
            re.IGNORECASE,
        )


def _strip_leading_comments(statement: str) -> str:
    """Remove -- line comments and /* block */ comments before classification."""
    import re

    text = re.sub(r"/\*.*?\*/", " ", statement, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return text.strip()


def statement_needs_authorization(statement: str) -> bool:
    """True when a statement touches a protected table as a write.

    Pure SELECT/PRAGMA/explain/transaction-control statements are allowed.
    Everything else that mentions a protected table requires an active
    ``authorized_write`` context - this is what closes raw SQL, SQLAlchemy Core,
    ORM bulk operations and CTE-wrapped writes.
    """
    import re

    _compile_patterns()
    cleaned = _strip_leading_comments(statement or "")
    if not cleaned or not _PROTECTED_RE.search(cleaned):
        return False
    upper = cleaned.upper()
    has_write_verb = bool(
        re.search(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\b", upper)
    )
    # A SELECT/PRAGMA/explain is read-only ONLY when no write verb appears
    # later (e.g. "SELECT 1; UPDATE memory_records ..." must not slip through).
    if _READ_ONLY_RE.match(cleaned) and not has_write_verb:
        return False
    # Pure read CTE: "WITH c AS (SELECT ...) SELECT ..." is not a write.
    if re.match(r"^\s*WITH\b", upper) and not has_write_verb:
        return False
    return True


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    # No governance activated -> the legacy JME behaviour is untouched.
    if not is_active():
        return
    # A decision produced by WriteGovernor is valid for this whole statement.
    if current_write_context() is not None:
        return
    # Server-internal bookkeeping (accessed_at / vector_status / search_text).
    if maintenance_active():
        return
    if not statement_needs_authorization(statement):
        return
    hook = get_denial_hook()
    if hook is not None:
        try:
            hook(table="connection_gate", operation="RAW_WRITE")
        except Exception:
            # An audit failure must not turn a refused write into an allowed one.
            pass
    raise UnauthorizedWriteError(
        "Original memory write has no P1 authorization context (connection gate)",
        detail={
            "code": "unauthorized_memory_write",
            "layer": "before_cursor_execute",
            "statement_prefix": _strip_leading_comments(statement)[:80],
        },
    )


def _sqlite_authorizer(action, arg1, arg2, dbname, source):
    """DBAPI-level authorizer: the ONLY layer that also sees TRIGGER-internal
    writes and statements issued through raw_connection() / a raw sqlite3
    cursor. SQLAlchemy Engine events cannot see those.

    Returns SQLITE_DENY (SQLite raises ``not authorized``) for INSERT/UPDATE/
    DELETE on the two authoritative memory tables when governance is active and
    no authorised (or maintenance) context exists.
    """
    import sqlite3

    if not is_active():
        return sqlite3.SQLITE_OK
    # A full signed authorisation covers the whole statement batch.
    if current_write_context() is not None:
        return sqlite3.SQLITE_OK

    table = str(arg1)
    protected = table in PROTECTED_TABLES
    dml = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)

    # P2-1 fix: maintenance is NOT a blanket bypass any more. SQLite passes the
    # changed column as arg2 for SQLITE_UPDATE, so allow only bookkeeping
    # columns; a "SET content=..." inside a maintenance window is now denied.
    if maintenance_active():
        if action == sqlite3.SQLITE_UPDATE and protected:
            return (
                sqlite3.SQLITE_OK
                if str(arg2) in MAINTENANCE_COLUMNS
                else sqlite3.SQLITE_DENY
            )
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE) and protected:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if action in dml and protected:
        return sqlite3.SQLITE_DENY

    # P2-2 fix: raw DBAPI connections bypass before_cursor_execute entirely,
    # so DDL must be denied here too (DROP/ALTER/CREATE on protected tables).
    ddl = tuple(
        getattr(sqlite3, name, default)
        for name, default in (
            ("SQLITE_CREATE_INDEX", 1),
            ("SQLITE_CREATE_TABLE", 2),
            ("SQLITE_CREATE_TRIGGER", 7),
            ("SQLITE_DROP_INDEX", 10),
            ("SQLITE_DROP_TABLE", 11),
            ("SQLITE_DROP_TRIGGER", 13),
            ("SQLITE_ALTER_TABLE", 26),
            ("SQLITE_REINDEX", 27),
        )
    )
    if action in ddl and (protected or str(arg2) in PROTECTED_TABLES):
        return sqlite3.SQLITE_DENY
    # P2-3: the old CREATE_TRIGGER branch checked arg1 (the TRIGGER NAME),
    # which never equals a table name. It was dead code; trigger-body DML is
    # already caught by the DML branch above, so no replacement is required.
    return sqlite3.SQLITE_OK


def install_sqlite_authorizer(dbapi_connection) -> None:
    """Called from the Engine 'connect' event for EVERY DBAPI connection."""
    dbapi_connection.set_authorizer(_sqlite_authorizer)


def install_connection_gate(engine) -> bool:
    """Install the guard once per Engine. Safe to call repeatedly.

    SQLAlchemy ``Engine`` has no ``.info`` dict (only ``Connection`` does), so
    use ``event.contains`` as the idempotency check instead of tagging the
    engine object.
    """
    from sqlalchemy import event

    if event.contains(engine, "before_cursor_execute", _before_cursor_execute):
        return False
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    return True


def connection_gate_installed(engine) -> bool:
    from sqlalchemy import event

    return event.contains(engine, "before_cursor_execute", _before_cursor_execute)


__all__ = [
    "UnauthorizedWriteError",
    "WriteContext",
    "set_denial_hook",
    "get_denial_hook",
    "current_write_context",
    "authorized_write",
    "activate",
    "deactivate",
    "active_governor",
    "is_active",
    "PROTECTED_TABLES",
    "statement_needs_authorization",
    "install_connection_gate",
    "connection_gate_installed",
    "install_sqlite_authorizer",
    "maintenance_write",
    "maintenance_active",
]
