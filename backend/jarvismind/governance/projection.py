"""P1-E read-only projection - P0-3 fix (structural isolation).

What independent review P0-3 proved about the previous version
-------------------------------------------------------------
``ModuleProjection`` did not hold a ``_manager`` attribute, but it held
``_projection``, and ``ReadOnlyProjection`` stored the DatabaseManager as
``self._manager``. So ``view._projection._manager.session()`` gave a derived
module a real write connection. ``assert_read_only()`` only inspected its own
attributes and therefore returned True - a self-attesting, always-true claim.
``content_digest()`` also hashed only ``content``, so changes to summary /
metadata / status or new rows went undetected.

New design (no indirect path back to a writable connection)
----------------------------------------------------------
* ``ReadOnlyProjection`` keeps ONLY a path string (``self._db_path``). It never
  stores the manager, engine, or session factory.
* All reads go through a fresh ``sqlite3`` connection opened with ``mode=ro``.
  A write on that connection raises ``attempt to write a readonly database``.
* ``ModuleProjection`` holds ONLY the module name and a private read function
  closure over that same path string - no parent object, no manager, no engine.
  ``__slots__`` blocks adding attributes dynamically.
* ``assert_read_only()`` is no longer self-attesting: it inspects the actual
  object graph for manager/session/engine references AND performs a real write
  probe on the read-only connection, requiring it to be rejected.
* ``content_digest()`` hashes EVERY column of EVERY row, plus the row count.

Complexity: O(page_size) per read, O(rows * columns) per digest.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .errors import ProjectionWriteError

ALLOWED_MODULES = ("JMR", "JKG", "JPA", "MCE")

_READ_COLUMNS = (
    "id, memory_type, content, summary, importance, confidence, status, "
    "source_id, source_candidate_id, created_at, updated_at, expires_at, "
    "forgotten_at, metadata_json, vector_status"
)

# Hard cap for one ``memory_audit_logs`` page. A consumer asks for a batch; it
# must not be able to ask for the whole table in one read.
_AUDIT_PAGE_MAX = 1000


def _readonly_uri(db_path: str) -> str:
    # SQLite URI paths always use forward slashes. A Windows drive path needs a
    # leading slash (C:/... -> /C:/...), but a POSIX path is already absolute:
    # prefixing it again (//private/...) makes SQLite read "private" as the URI
    # authority and fail with "invalid uri authority".
    posix = str(db_path).replace("\\", "/")
    if not posix.startswith("/"):
        posix = "/" + posix
    posix = posix.replace("?", "%3F").replace("#", "%23")
    return f"file:{posix}?mode=ro"


def _open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Independent review P0-C (B3): the previous ModuleProjection held module-level
# function objects. A derived module could do
#   view._read_page_fn.__globals__["sqlite3"].connect(view._db_path)
# and obtain a WRITABLE connection that bypasses every SQLAlchemy Engine gate.
# Now derived modules receive ONLY an opaque random token. No path, no callable,
# no module object is reachable from the ModuleProjection object graph.
_TOKEN_REGISTRY: Dict[str, str] = {}


def _register_path(db_path: str) -> str:
    token = secrets.token_urlsafe(32)
    _TOKEN_REGISTRY[token] = db_path
    return token


def _path_for(token: str) -> str:
    path = _TOKEN_REGISTRY.get(token)
    if path is None:
        raise ProjectionWriteError("unknown or revoked projection token")
    return path


def _query_page_by_token(token: str, **kwargs) -> ProjectionPage:
    return _read_page(_path_for(token), **kwargs)


def _query_one_by_token(token: str, memory_id: str) -> Optional[Mapping[str, Any]]:
    return _read_one(_path_for(token), memory_id)


def _query_scan_by_token(token: str, **kwargs) -> Tuple[Mapping[str, Any], ...]:
    return _scan_memories(_path_for(token), **kwargs)


def _query_count_by_token(token: str, **kwargs) -> int:
    return _count_memories(_path_for(token), **kwargs)


def _query_audit_by_token(
    token: str, *, after_created_at: str, after_id: str, limit: int
) -> Tuple[Mapping[str, Any], ...]:
    return _read_audit_entries(
        _path_for(token),
        after_created_at=after_created_at,
        after_id=after_id,
        limit=limit,
    )


def _query_audit_derivation_by_token(
    token: str, *, after_created_at: str, after_id: str, limit: int
) -> Tuple[Tuple[str, Optional[bool]], ...]:
    return _read_audit_derivation_changes(
        _path_for(token),
        after_created_at=after_created_at,
        after_id=after_id,
        limit=limit,
    )


def _probe_write_by_token(token: str) -> bool:
    """Real behavioural check: a write on the projection's connection must fail."""
    db_path = _path_for(token)
    try:
        with closing(_open_readonly(db_path)) as conn:
            conn.execute(
                "INSERT INTO memory_records(id, memory_type, content, importance, "
                "confidence, status, created_at, updated_at) "
                "VALUES ('p1-token-probe','SEMANTIC','probe',5.0,0.5,'ACTIVE',"
                "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
    except sqlite3.OperationalError:
        return True
    return False


@dataclass(frozen=True)
class ProjectionPage:
    """Immutable page. ``items`` is a tuple of immutable mappings."""

    items: Tuple[Mapping[str, Any], ...]
    total: int
    page: int
    page_size: int
    authority: str = "JME"
    source_table: str = "memory_records"


def _freeze(row: sqlite3.Row) -> Mapping[str, Any]:
    data: Dict[str, Any] = dict(row)
    data["lineage"] = MappingProxyType(
        {
            "authority": "JME",
            "table": "memory_records",
            "memory_id": data["id"],
            "source_candidate_id": data.get("source_candidate_id"),
        }
    )
    # MappingProxyType is a read-only view: data['content'] = ... raises TypeError.
    return MappingProxyType(data)


def _read_page(
    db_path: str,
    *,
    status: str,
    memory_type: Optional[str],
    page: int,
    page_size: int,
    max_page_size: int,
) -> ProjectionPage:
    size = max(1, min(int(page_size), int(max_page_size)))
    offset = (max(1, int(page)) - 1) * size
    where = ["status=:s"]
    params: Dict[str, Any] = {"s": status}
    if memory_type:
        where.append("memory_type=:t")
        params["t"] = memory_type
    clause = " AND ".join(where)
    with closing(_open_readonly(db_path)) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM memory_records WHERE {clause}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT {_READ_COLUMNS} FROM memory_records WHERE {clause} "
            "ORDER BY created_at ASC, id ASC LIMIT :n OFFSET :o",
            {**params, "n": size, "o": offset},
        ).fetchall()
    return ProjectionPage(
        items=tuple(_freeze(row) for row in rows),
        total=total,
        page=max(1, int(page)),
        page_size=size,
    )


def _read_one(db_path: str, memory_id: str) -> Optional[Mapping[str, Any]]:
    with closing(_open_readonly(db_path)) as conn:
        row = conn.execute(
            f"SELECT {_READ_COLUMNS} FROM memory_records WHERE id=:id",
            {"id": memory_id},
        ).fetchone()
    return _freeze(row) if row is not None else None


def _read_audit_entries(
    db_path: str, *, after_created_at: str, after_id: str, limit: int
) -> Tuple[Mapping[str, Any], ...]:
    """Page the JME lifecycle audit feed through a ``mode=ro`` connection.

    Four columns only: ``id`` / ``memory_id`` / ``action`` / ``created_at``.
    ``detail_json`` and ``actor`` are deliberately excluded — the first can hold
    a memory body and the second is a human label, and a consumer of *lifecycle
    events* needs neither.

    Keyset pagination on ``(created_at, id)``, not ``OFFSET``: rows are appended
    while a consumer pages, and an offset would skip whatever was inserted
    behind the cursor. The tuple ordering matches the ``WHERE`` predicate so the
    two cannot drift apart.

    A database old enough to predate ``memory_audit_logs`` (or a projection
    opened over a file that is not the JME database at all) reports an empty
    feed. Raising would turn "this database has no audit history" into a broken
    sync, which is the wrong failure for a derived module.
    """
    size = max(1, min(int(limit), _AUDIT_PAGE_MAX))
    with closing(_open_readonly(db_path)) as conn:
        try:
            rows = conn.execute(
                "SELECT id, memory_id, action, created_at FROM memory_audit_logs "
                "WHERE (created_at > :after_created_at) "
                "   OR (created_at = :after_created_at AND id > :after_id) "
                "ORDER BY created_at ASC, id ASC LIMIT :n",
                {
                    "after_created_at": after_created_at,
                    "after_id": after_id,
                    "n": size,
                },
            ).fetchall()
        except sqlite3.OperationalError:
            return ()
    return tuple(MappingProxyType(dict(row)) for row in rows)


#: Field names in ``memory_audit_logs.detail_json["fields"]`` that **provably do
#: not** change what the graph derivation was computed from.
#:
#: The list is deliberately tiny. Anything not named here keeps the pre-P0-5
#: behaviour ("this write may have moved the derivation, mark stale"), so the fix
#: can only ever *narrow* staleness for one specific shape rather than quietly
#: loosening it for every other field:
#:
#: * ``status`` — an archive or restore rewrites no body field. Validity of the
#:   source is already decided by ``sync_memory`` from the current status, so
#:   marking the derivation stale on top of that was pure double work (P0-5).
#:
#: Everything else stays conservative on purpose:
#:
#: * ``content`` / ``summary`` / ``search_text`` — the body the rules read;
#: * ``metadata_json`` — ``JkgIngestor._governance_reason`` scans it, so a change
#:   can flip a memory from eligible to privacy-blocked;
#: * ``expires_at`` — ``JkgIngestor`` refuses an expired memory;
#: * ``importance`` / ``confidence`` / ``source_id`` — no current rule reads them,
#:   but treating them as bookkeeping would be a behaviour change nobody asked
#:   for, and being wrong here loses knowledge.
_BOOKKEEPING_FIELD_NAMES = ("status",)

_DERIVATION_CHANGE_SQL = """SELECT id, CASE
      WHEN detail_json IS NULL OR json_valid(detail_json) = 0 THEN NULL
      WHEN json_type(detail_json, '$.fields') IS NOT 'array' THEN NULL
      WHEN json_array_length(detail_json, '$.fields') = 0 THEN NULL
      WHEN EXISTS (SELECT 1 FROM json_each(detail_json, '$.fields')
                   WHERE json_each.value IS NULL
                      OR json_each.value NOT IN ({names})) THEN 1
      ELSE 0
    END AS derivation_affected
  FROM memory_audit_logs
  WHERE (created_at > :after_created_at)
     OR (created_at = :after_created_at AND id > :after_id)
  ORDER BY created_at ASC, id ASC LIMIT :n""".format(
    names=", ".join(f"'{name}'" for name in _BOOKKEEPING_FIELD_NAMES)
)


def _read_audit_derivation_changes(
    db_path: str, *, after_created_at: str, after_id: str, limit: int
) -> Tuple[Tuple[str, Optional[bool]], ...]:
    """Per audit row, could that write have changed the derivation's inputs?

    Returns ``(audit_id, derivation_affected)`` pairs over the SAME keyset window
    as :func:`_read_audit_entries`, so the two reads can be joined by id.

    Why this exists
    ---------------
    JME writes ``action="UPDATE"`` for a bare status change (archive / restore)
    exactly as it does for a body rewrite, so the action name alone cannot tell
    the two apart — treating both as "content changed" marks a still-valid
    memory's relations stale on every archive, and ``sync_memory`` then revives
    the source, so the graph is rewritten twice to return to where it started.

    The discriminator lives in ``detail_json["fields"]``, which
    ``MemoryService._update_impl`` fills with the field names it actually wrote.
    That column can also hold the memory body (the ``before``/``after`` audit
    images), and the projection must never hand a body to a derived module, so
    the comparison happens **inside SQLite**: only field *names* from a fixed
    deny-list of proven-bookkeeping names are matched, and only a tri-state
    boolean leaves the connection. No ``detail_json`` text is ever selected or
    returned.

    Tri-state, deliberately:

    * ``True`` — the write may have moved a derivation input (the default for
      anything not proven harmless);
    * ``False`` — every written field is proven bookkeeping (``status`` alone),
      so the derivation still describes the current row;
    * ``None`` — unclassifiable: NULL / malformed JSON / no ``fields`` array
      (the correction and approved-modification paths write this shape) / an
      empty array. The caller must treat ``None`` conservatively; this function
      does not guess.

    A build without the JSON1 extension raises ``OperationalError``; every row
    then reports ``None``, which is the same conservative answer as an
    unclassifiable row. Failing open here would decide "no content change" for
    every row, which is the one answer that could leave an edited fact
    authoritative.
    """
    size = max(1, min(int(limit), _AUDIT_PAGE_MAX))
    params = {
        "after_created_at": after_created_at,
        "after_id": after_id,
        "n": size,
    }
    with closing(_open_readonly(db_path)) as conn:
        try:
            rows = conn.execute(_DERIVATION_CHANGE_SQL, params).fetchall()
        except sqlite3.OperationalError:
            try:
                ids = conn.execute(
                    "SELECT id FROM memory_audit_logs "
                    "WHERE (created_at > :after_created_at) "
                    "   OR (created_at = :after_created_at AND id > :after_id) "
                    "ORDER BY created_at ASC, id ASC LIMIT :n",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                return ()
            return tuple((str(row[0]), None) for row in ids)
    return tuple(
        (str(row[0]), None if row[1] is None else bool(row[1])) for row in rows
    )


# Scan statuses a caller may name. A whitelist, never SQL text: the status filter
# is bound as parameters after this check, so no caller can splice SQL.
_SCAN_STATUS_WHITELIST = frozenset({"ACTIVE", "ARCHIVED", "DELETED", "MERGED"})
_SCAN_MAX_STATUSES = 2
_SCAN_PAGE_MAX = 1000


def _scan_clause(
    *,
    after_created_at: str,
    after_id: str,
    created_at_lte: str,
    statuses: Tuple[str, ...],
    newest_first: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Build the parameterised WHERE clause shared by scan and count."""
    if not isinstance(statuses, (tuple, list)) or not statuses:
        raise ValueError("statuses must be a non-empty sequence")
    if len(statuses) > _SCAN_MAX_STATUSES:
        raise ValueError(f"at most {_SCAN_MAX_STATUSES} statuses may be requested")
    for status in statuses:
        if status not in _SCAN_STATUS_WHITELIST:
            raise ValueError("status is not in the scan whitelist")
    params: Dict[str, Any] = {}
    placeholders = []
    for index, status in enumerate(statuses):
        key = f"s{index}"
        params[key] = status
        placeholders.append(f":{key}")
    where = [f"status IN ({', '.join(placeholders)})"]
    if after_created_at or after_id:
        comparator = "<" if newest_first else ">"
        where.append(
            f"(created_at {comparator} :after_created_at "
            f"OR (created_at = :after_created_at AND id {comparator} :after_id))"
        )
        params["after_created_at"] = str(after_created_at)
        params["after_id"] = str(after_id)
    if created_at_lte:
        where.append("created_at <= :created_at_lte")
        params["created_at_lte"] = str(created_at_lte)
    return " AND ".join(where), params


def _scan_memories(
    db_path: str,
    *,
    after_created_at: str,
    after_id: str,
    created_at_lte: str,
    statuses: Tuple[str, ...],
    limit: int,
    newest_first: bool = False,
) -> Tuple[Mapping[str, Any], ...]:
    """Keyset-page memory rows through a ``mode=ro`` connection (snapshot-bounded).

    ``created_at_lte`` pins the upper bound of a snapshot so rows inserted while
    a consumer pages are not seen mid-run. ``limit`` is clamped to 1..1000.
    """
    size = max(1, min(int(limit), _SCAN_PAGE_MAX))
    clause, params = _scan_clause(
        after_created_at=after_created_at,
        after_id=after_id,
        created_at_lte=created_at_lte,
        statuses=tuple(statuses),
        newest_first=newest_first,
    )
    order = "DESC" if newest_first else "ASC"
    with closing(_open_readonly(db_path)) as conn:
        rows = conn.execute(
            f"SELECT {_READ_COLUMNS} FROM memory_records WHERE {clause} "
            f"ORDER BY created_at {order}, id {order} LIMIT :n",
            {**params, "n": size},
        ).fetchall()
    return tuple(_freeze(row) for row in rows)


def _count_memories(
    db_path: str,
    *,
    created_at_lte: str,
    statuses: Tuple[str, ...],
) -> int:
    clause, params = _scan_clause(
        after_created_at="",
        after_id="",
        created_at_lte=created_at_lte,
        statuses=tuple(statuses),
    )
    with closing(_open_readonly(db_path)) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM memory_records WHERE {clause}", params
            ).fetchone()[0]
        )


def _digest_all_columns(db_path: str) -> str:
    """Hash EVERY column of EVERY row (not just content)."""
    h = hashlib.sha256()
    rows = 0
    with closing(_open_readonly(db_path)) as conn:
        for row in conn.execute("SELECT * FROM memory_records ORDER BY id"):
            rows += 1
            for key in row.keys():
                value = row[key]
                h.update(str(key).encode("utf-8"))
                h.update(b"=")
                h.update(b"" if value is None else str(value).encode("utf-8"))
                h.update(b"\x1f")
            h.update(b"\x1e")
    h.update(f"rows={rows}".encode("ascii"))
    return h.hexdigest()


class ReadOnlyProjection:
    """SELECT-only view. Holds a path string, never a writable connection."""

    __slots__ = ("_db_path",)

    def __init__(self, manager) -> None:
        # Extract ONLY the path. No reference to manager / engine / session is
        # retained anywhere in this object graph.
        self._db_path = str(Path(manager.settings.database_path).resolve())

    @property
    def db_path(self) -> str:
        return self._db_path

    def get_memory(self, memory_id: str) -> Optional[Mapping[str, Any]]:
        return _read_one(self._db_path, memory_id)

    def list_memories(
        self,
        *,
        status: str = "ACTIVE",
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 200,
        max_page_size: int = 1000,
    ) -> ProjectionPage:
        return _read_page(
            self._db_path,
            status=status,
            memory_type=memory_type,
            page=page,
            page_size=page_size,
            max_page_size=max_page_size,
        )

    def source_lineage(self, source_id: str) -> Optional[Mapping[str, Any]]:
        with closing(_open_readonly(self._db_path)) as conn:
            row = conn.execute(
                "SELECT id, source_type, source_name, uri, created_at "
                "FROM memory_sources WHERE id=:sid",
                {"sid": source_id},
            ).fetchone()
        return MappingProxyType(dict(row)) if row is not None else None

    def read_audit_entries(
        self, *, after_created_at: str = "", after_id: str = "", limit: int = 200
    ) -> Tuple[Mapping[str, Any], ...]:
        """Page ``memory_audit_logs`` read-only (see ``_read_audit_entries``).

        Exposed on the projection as well as on ``ModuleProjection`` so JMR and
        JPA can reuse the same feed instead of growing their own query.
        """
        return _read_audit_entries(
            self._db_path,
            after_created_at=after_created_at,
            after_id=after_id,
            limit=limit,
        )

    def read_audit_derivation_changes(
        self, *, after_created_at: str = "", after_id: str = "", limit: int = 200
    ) -> Tuple[Tuple[str, Optional[bool]], ...]:
        """Per audit row in this window, could that write change the derivation?

        Kept alongside :meth:`read_audit_entries` because both are the same
        read-only audit feed; see :func:`_read_audit_derivation_changes` for the
        tri-state contract and why ``detail_json`` is never returned.
        """
        return _read_audit_derivation_changes(
            self._db_path,
            after_created_at=after_created_at,
            after_id=after_id,
            limit=limit,
        )

    def content_digest(self) -> str:
        """Verification helper: digest of ALL original-memory columns."""
        return _digest_all_columns(self._db_path)

    def for_module(self, module: str) -> "ModuleProjection":
        if module not in ALLOWED_MODULES:
            raise ProjectionWriteError(
                "projection is available only to derived modules",
                detail={"code": "unknown_derived_module", "module": module},
            )
        # Only an opaque token crosses the boundary - not `self`, not the path,
        # not a function object carrying a __globals__ dict.
        return ModuleProjection(module, _register_path(self._db_path))

    def assert_read_only(self) -> Dict[str, Any]:
        """Structural + behavioural evidence (not a self-attesting claim)."""
        objections = []
        for name in self.__slots__:
            value = getattr(self, name, None)
            if not isinstance(value, str):
                objections.append(f"attribute {name} is {type(value).__name__}, not a path str")
        write_probe_rejected = False
        try:
            with closing(_open_readonly(self._db_path)) as conn:
                conn.execute(
                    "INSERT INTO memory_records(id, memory_type, content, importance, "
                    "confidence, status, created_at, updated_at) "
                    "VALUES ('p1-readonly-probe','SEMANTIC','probe',5.0,0.5,"
                    "'ACTIVE','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                )
        except sqlite3.OperationalError:
            write_probe_rejected = True
        return {
            "holds_only_path_string": not objections,
            "objections": objections,
            "write_probe_rejected": write_probe_rejected,
            "read_only": not objections and write_probe_rejected,
        }


class ModuleProjection:
    """Capability-restricted view handed to JMR / JKG / JPA (P0-C fix).

    Holds ONLY the module name and an opaque random token (both plain strings).
    There is no parent object, no path, no callable, no manager, no engine, no
    session, and ``__slots__`` prevents attaching any of them. In particular a
    derived module can no longer reach ``some_function.__globals__['sqlite3']``
    or a database path from this object graph.

    Threat-model boundary (documented, not hidden): a derived module that can
    execute arbitrary Python (e.g. import JME internals, or know the DB path and
    call sqlite3.connect itself) cannot be contained inside one process. That
    requires an isolated process plus OS-level read-only permissions. The
    DBAPI authorizer installed in ``connection.py`` covers trigger-internal and
    raw_connection() writes on all JME-owned connections.
    """

    __slots__ = ("module", "_token")

    def __init__(self, module: str, token: str) -> None:
        self.module = module
        self._token = token

    def read_memories(
        self,
        *,
        status: str = "ACTIVE",
        memory_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 200,
    ) -> ProjectionPage:
        return _query_page_by_token(
            self._token,
            status=status,
            memory_type=memory_type,
            page=page,
            page_size=page_size,
            max_page_size=1000,
        )

    def read_memory(self, memory_id: str) -> Optional[Mapping[str, Any]]:
        return _query_one_by_token(self._token, memory_id)

    def read_audit_entries(
        self, *, after_created_at: str = "", after_id: str = "", limit: int = 200
    ) -> Tuple[Mapping[str, Any], ...]:
        """Page the JME lifecycle audit feed through the opaque token.

        The name does not start with write/save/create/update/delete/insert, so
        ``assert_read_only()`` keeps reporting an empty ``write_methods`` list;
        a test pins that, because a future rename here would quietly widen
        what the capability boundary claims to allow.
        """
        return _query_audit_by_token(
            self._token,
            after_created_at=after_created_at,
            after_id=after_id,
            limit=limit,
        )

    def read_audit_derivation_changes(
        self, *, after_created_at: str = "", after_id: str = "", limit: int = 200
    ) -> Tuple[Tuple[str, Optional[bool]], ...]:
        """Which audit rows in this window may have changed derivation inputs.

        Returns ``(audit_id, derivation_affected)`` pairs over the same keyset
        window as :meth:`read_audit_entries`, so a caller can join them by id.
        The flag is tri-state: ``True`` / ``False`` / ``None`` (unclassifiable).
        See :func:`_read_audit_derivation_changes` for what is and is not
        compared.

        The name starts with ``read_`` rather than a write verb, so
        ``assert_read_only()`` still reports an empty ``write_methods`` list.
        """
        return _query_audit_derivation_by_token(
            self._token,
            after_created_at=after_created_at,
            after_id=after_id,
            limit=limit,
        )

    def scan_memories(
        self,
        *,
        after_created_at: str = "",
        after_id: str = "",
        created_at_lte: str = "",
        statuses: Tuple[str, ...] = ("ACTIVE", "ARCHIVED"),
        limit: int = 200,
        newest_first: bool = False,
    ) -> Tuple[Mapping[str, Any], ...]:
        """Keyset-scan memories in ``(created_at, id)`` order, read-only.

        ``statuses`` is checked against a whitelist and bound as parameters.
        ``created_at_lte`` bounds the scan to a snapshot. ``limit`` is clamped
        to 1..1000. The name does not start with a write verb, so
        ``assert_read_only()`` keeps reporting an empty ``write_methods`` list.
        """
        return _query_scan_by_token(
            self._token,
            after_created_at=after_created_at,
            after_id=after_id,
            created_at_lte=created_at_lte,
            statuses=tuple(statuses),
            limit=limit,
            newest_first=newest_first,
        )

    def count_memories(
        self,
        *,
        created_at_lte: str = "",
        statuses: Tuple[str, ...] = ("ACTIVE", "ARCHIVED"),
    ) -> int:
        """Count memories under the same status whitelist and snapshot bound."""
        return _query_count_by_token(
            self._token,
            created_at_lte=created_at_lte,
            statuses=tuple(statuses),
        )

    def assert_read_only(self) -> Dict[str, Any]:
        """Object-graph check + a REAL write probe (not a self-attesting claim)."""
        objections = []
        for name in self.__slots__:
            value = getattr(self, name, None)
            if not isinstance(value, str):
                objections.append(f"{name} is {type(value).__name__}, not a str")
            if hasattr(value, "__globals__"):
                objections.append(f"{name} exposes __globals__")
            if hasattr(value, "session") or hasattr(value, "engine"):
                objections.append(f"{name} exposes a writable object")
        write_names = sorted(
            name
            for name in dir(self)
            if name.startswith(("write", "save", "create", "update", "delete", "insert"))
        )
        write_probe_rejected = _probe_write_by_token(self._token)
        has_parent = hasattr(self, "_projection")
        return {
            "module": self.module,
            "slots": list(self.__slots__),
            "has_parent_reference": has_parent,
            "has_path_attribute": hasattr(self, "_db_path"),
            "has_function_attribute": any(
                callable(getattr(self, n, None)) for n in self.__slots__
            ),
            "object_graph_objections": objections,
            "write_methods": write_names,
            "write_probe_rejected": write_probe_rejected,
            "read_only": (
                not objections
                and not write_names
                and not has_parent
                and write_probe_rejected
            ),
        }
