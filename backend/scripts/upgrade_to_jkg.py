#!/usr/bin/env python3
"""JKG-INT-01 · explicit, fail-closed installer for the proposed JKG Schema.

Scope: installs the three ``jkg_*`` tables, their nine explicit indexes (plus the
SQLite-generated primary-key autoindexes) and records the JKG Schema version in
``system_settings['jkg.schema_version']``. Nothing else.

* Nothing imports this module. Startup, requests, Container and ``main.py`` never
  run it (guarded by a test). A human must pass ``--mode install``.
* The DDL comes only from ``app.jkg.schema.JKG_SCHEMA_STATEMENTS`` and must match
  a pinned SHA-256 digest of its normalised text. Any change to that DDL needs
  re-approval and re-pinning.
* Target paths: UNC, extended (``\\\\?\\``) and device paths are refused. Any
  path that resolves into the repository ``data`` directory (by string, by
  ancestor identity, or by hard link to ``jarvismind.db``) is refused unless
  ``--allow-real-database`` and a non-empty ``--approval-ref`` are given.
* Preconditions: file exists, SQLite header, ``integrity_check = ok``, JME tables
  and columns present, ``schema_migrations`` exactly 1..6.
* State classification: ABSENT (install), INSTALLED (no-op), anything else is
  refused. Partial or conflicting structures are never repaired.
* One ``BEGIN IMMEDIATE`` transaction: state is re-classified under the write
  lock, DDL runs, JME content and schema fingerprints and the JKG structure are
  verified, then ``COMMIT``. Any failure rolls back.
* After commit, a fresh read-only connection re-verifies the committed state.
* Optional ``--backup-dir``: a consistent copy with a SHA-256 manifest, verified
  by fingerprint before installing.

Exit codes: 0 ok (inspected, installed, or already installed); 2 refused
(nothing written); 3 rolled back; 4 unknown or post-commit state (restore from
backup and report); 5 unexpected internal error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_JME_ROOT = Path(__file__).resolve().parents[1]
if str(_JME_ROOT) not in sys.path:
    sys.path.insert(0, str(_JME_ROOT))

from jarvismind.jkg import schema as jkg_schema  # noqa: E402

INSTALLER_VERSION = 2
PRODUCTION_DATA_DIR = _JME_ROOT / "data"
PRODUCTION_DB_PATH = PRODUCTION_DATA_DIR / "jarvismind.db"
LOCK_TIMEOUT_SECONDS = 10

# SHA-256 over the normalised canonical DDL statements joined by LF. A change to
# app/jkg/schema.py changes this digest; the change must be re-approved and re-pinned.
EXPECTED_PLAN_SHA256 = "c3aa48ddf539c29e6692230c09e820963c1bff7844906a34cfc5e790fc456458"

JARVISMIND_TARGET_VERSION = 6
JARVISMIND_REQUIRED_VERSIONS: Tuple[int, ...] = tuple(range(1, JARVISMIND_TARGET_VERSION + 1))
JARVISMIND_REQUIRED_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "memory_records": ("id", "content"),
    "memory_candidates": ("id", "content"),
    "system_settings": ("key", "value", "updated_at"),
    "schema_migrations": ("version", "name", "applied_at"),
}
VERSION_KEY = jkg_schema.JKG_SCHEMA_VERSION_KEY
VERSION_VALUE = str(jkg_schema.JKG_SCHEMA_VERSION)
JKG_TABLES: Tuple[str, ...] = tuple(jkg_schema.JKG_TABLES)
_JKG_NAME_PREFIXES = ("jkg_", "idx_jkg_", "uq_jkg_", "sqlite_autoindex_jkg_")
_SQLITE_HEADER = b"SQLite format 3\x00"

_CREATE_RE = re.compile(
    r"^CREATE (?P<kind>TABLE|UNIQUE INDEX|INDEX) IF NOT EXISTS "
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?: ON (?P<on>[A-Za-z_][A-Za-z0-9_]*)\()?",
)
_FORBIDDEN_RE = re.compile(
    r"\b(DROP|ALTER|DELETE|UPDATE|INSERT|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|"
    r"TRIGGER|VIEW|REINDEX|SAVEPOINT|COMMIT|ROLLBACK|BEGIN|SELECT)\b",
    re.IGNORECASE,
)

E_TARGET_PRODUCTION = "JKG-INST-001"
E_TARGET_MISSING = "JKG-INST-002"
E_NOT_SQLITE = "JKG-INST-003"
E_INTEGRITY = "JKG-INST-004"
E_JME_PRECONDITION = "JKG-INST-005"
E_JKG_PARTIAL = "JKG-INST-006"
E_JKG_VERSION = "JKG-INST-007"
E_JKG_CONFLICT = "JKG-INST-008"
E_JKG_UNEXPECTED = "JKG-INST-009"
E_PLAN_REJECTED = "JKG-INST-010"
E_APPROVAL_MISSING = "JKG-INST-011"
E_BACKUP_FAILED = "JKG-INST-012"
E_LOCKED = "JKG-INST-013"
E_TARGET_UNSUPPORTED = "JKG-INST-014"
E_DDL_FAILED = "JKG-INST-020"
E_VERIFY_FAILED = "JKG-INST-021"
E_ROLLBACK_FAILED = "JKG-INST-022"
E_POST_COMMIT = "JKG-INST-030"
E_UNEXPECTED = "JKG-INST-090"

STATE_ABSENT = "ABSENT"
STATE_INSTALLED = "INSTALLED"
_STATUS_BY_EXIT = {2: "REFUSED", 3: "ROLLED_BACK", 4: "UNKNOWN_STATE"}


class InstallerError(RuntimeError):
    exit_code = 2

    def __init__(self, code: str, message: str, details: Optional[List[str]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = list(details or [])


class Refused(InstallerError):
    exit_code = 2


class RolledBack(InstallerError):
    exit_code = 3


class UnknownState(InstallerError):
    exit_code = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(sql: str) -> str:
    return " ".join(str(sql).split())


def _plan_digest(plan: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(plan).encode("utf-8")).hexdigest()


def _ro_uri(path: Path) -> str:
    posix = Path(path).resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file:" + urllib.parse.quote(posix, safe="/:") + "?mode=ro"


def _quote_table(name: str) -> str:
    if name not in JKG_TABLES:
        raise Refused(E_PLAN_REJECTED, f"table outside JKG allowlist: {name}")
    return '"' + name + '"'


def _quote_index(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise Refused(E_PLAN_REJECTED, f"unexpected index name: {name}")
    return '"' + name + '"'


def _is_jkg_object(name: str, tbl_name: str) -> bool:
    return name in JKG_TABLES or tbl_name in JKG_TABLES or name.startswith(_JKG_NAME_PREFIXES)


# ---------------------------------------------------------------------------
# Target validation (no SQLite I/O)
# ---------------------------------------------------------------------------
def _unsupported_form(raw: str) -> bool:
    return raw.startswith(("\\\\", "//"))


def _is_under_data_dir(target: Path) -> bool:
    """Fail-closed identity check: string, ancestor identity, and hard link."""
    try:
        target_norm = os.path.normcase(os.path.abspath(str(target)))
        data_norm = os.path.normcase(os.path.abspath(str(PRODUCTION_DATA_DIR)))
        if target_norm == data_norm or target_norm.startswith(data_norm.rstrip(os.sep) + os.sep):
            return True
        if PRODUCTION_DATA_DIR.exists():
            for ancestor in (target, *target.parents):
                if ancestor.exists() and os.path.samefile(ancestor, PRODUCTION_DATA_DIR):
                    return True
        if target.exists() and PRODUCTION_DB_PATH.exists():
            if os.path.samefile(target, PRODUCTION_DB_PATH):
                return True
    except (OSError, ValueError):
        return True
    return False


def check_target(
    db_path: Any,
    *,
    allow_real_database: bool = False,
    approval_ref: str = "",
) -> Path:
    raw = str(db_path)
    if _unsupported_form(raw):
        raise Refused(
            E_TARGET_UNSUPPORTED,
            "UNC, extended (\\\\?\\) and device paths are not supported; use a plain local path",
        )
    target = Path(raw).expanduser().resolve()
    if _is_under_data_dir(target):
        if not allow_real_database:
            raise Refused(
                E_TARGET_PRODUCTION,
                "target resolves into the repository data directory; refused by default",
            )
        if not str(approval_ref or "").strip():
            raise Refused(
                E_APPROVAL_MISSING,
                "--allow-real-database requires a non-empty --approval-ref",
            )
    return target


def _check_backup_dir(backup_dir: Any) -> Path:
    raw = str(backup_dir)
    if _unsupported_form(raw):
        raise Refused(E_TARGET_UNSUPPORTED, "backup directory must be a plain local path")
    resolved = Path(raw).expanduser().resolve()
    if _is_under_data_dir(resolved):
        raise Refused(
            E_TARGET_PRODUCTION,
            "backup directory resolves into the repository data directory; refused",
        )
    return resolved


def build_plan(statements: Sequence[str] = jkg_schema.JKG_SCHEMA_STATEMENTS) -> List[str]:
    """Return the normalised DDL plan or refuse. Pure; no database access."""
    names_from_source = [
        m.group("name")
        for m in (_CREATE_RE.match(_norm(s)) for s in jkg_schema.JKG_SCHEMA_STATEMENTS)
        if m and m.group("kind") in ("INDEX", "UNIQUE INDEX")
    ]
    allowed_objects = set(JKG_TABLES) | set(names_from_source)
    plan: List[str] = []
    for raw in statements:
        stmt = _norm(raw)
        if ";" in stmt or "--" in stmt or "/*" in stmt:
            raise Refused(E_PLAN_REJECTED, "stacked statement or comment marker refused")
        match = _CREATE_RE.match(stmt)
        if not match:
            raise Refused(E_PLAN_REJECTED, "only CREATE ... IF NOT EXISTS is allowed")
        if _FORBIDDEN_RE.search(stmt.replace("ON DELETE CASCADE", "")):
            raise Refused(E_PLAN_REJECTED, "forbidden keyword in DDL plan")
        name = match.group("name")
        if name not in allowed_objects:
            raise Refused(E_PLAN_REJECTED, f"object outside allowlist: {name}")
        if match.group("kind") == "TABLE":
            if name not in JKG_TABLES:
                raise Refused(E_PLAN_REJECTED, f"table outside allowlist: {name}")
        elif match.group("on") not in JKG_TABLES:
            raise Refused(E_PLAN_REJECTED, f"index on non-JKG table: {name}")
        plan.append(stmt)
    if len(plan) != len(JKG_TABLES) + len(set(names_from_source)):
        raise Refused(E_PLAN_REJECTED, f"plan has {len(plan)} statements, expected 12")
    if _plan_digest(plan) != EXPECTED_PLAN_SHA256:
        raise Refused(
            E_PLAN_REJECTED,
            "DDL plan does not match the pinned canonical digest; re-approval and re-pinning required",
        )
    return plan


# ---------------------------------------------------------------------------
# JKG structure snapshot and comparison
# ---------------------------------------------------------------------------
def _describe_table(conn: sqlite3.Connection, table: str) -> Dict[str, Any]:
    quoted = _quote_table(table)
    columns = [
        (str(r[1]), str(r[2] or "").upper(), int(r[3]), str(r[4]), int(r[5]))
        for r in conn.execute(f"PRAGMA table_info({quoted})")
    ]
    fks = sorted(
        (str(r[2]), str(r[3]), str(r[4]), str(r[5]), str(r[6]))
        for r in conn.execute(f"PRAGMA foreign_key_list({quoted})")
    )
    indexes: Dict[str, Any] = {}
    for row in conn.execute(f"PRAGMA index_list({quoted})"):
        index_name = str(row[1])
        cols = tuple(
            str(info[2])
            for info in conn.execute(f"PRAGMA index_info({_quote_index(index_name)})")
        )
        indexes[index_name] = {"unique": int(row[2]), "origin": str(row[3]), "columns": cols}
    return {"columns": columns, "foreign_keys": fks, "indexes": indexes}


def _snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    objects: Dict[str, Dict[str, Any]] = {}
    for obj_type, name, tbl_name, sql in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ).fetchall():
        if _is_jkg_object(str(name), str(tbl_name)):
            objects[str(name)] = {
                "type": str(obj_type),
                "tbl": str(tbl_name),
                "sql": _norm(sql) if sql else None,
            }
    present = [t for t in JKG_TABLES if t in objects and objects[t]["type"] == "table"]
    return {"objects": objects, "tables": {t: _describe_table(conn, t) for t in present}}


_EXPECTED_CACHE: Optional[Dict[str, Any]] = None


def expected_snapshot() -> Dict[str, Any]:
    """Structure produced by the pinned DDL on a throwaway in-memory database."""
    global _EXPECTED_CACHE
    if _EXPECTED_CACHE is None:
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            for stmt in build_plan():
                mem.execute(stmt)
            _EXPECTED_CACHE = _snapshot(mem)
        finally:
            mem.close()
    return _EXPECTED_CACHE


def _diff(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[Tuple[str, str]]:
    problems: List[Tuple[str, str]] = []
    exp_objects, act_objects = expected["objects"], actual["objects"]
    for name in sorted(exp_objects):
        if name not in act_objects:
            problems.append(("missing", name))
        elif exp_objects[name] != act_objects[name]:
            problems.append(("conflict", name))
    for name in sorted(set(act_objects) - set(exp_objects)):
        problems.append(("unexpected", name))
    for table, exp_desc in expected["tables"].items():
        act_desc = actual["tables"].get(table)
        if act_desc is None:
            continue
        if act_desc["columns"] != exp_desc["columns"] or act_desc["foreign_keys"] != exp_desc["foreign_keys"]:
            problems.append(("conflict", table))
            continue
        for index_name, exp_index in exp_desc["indexes"].items():
            act_index = act_desc["indexes"].get(index_name)
            if act_index is not None and act_index != exp_index:
                problems.append(("conflict", index_name))
    return problems


def _classify_problems(problems: List[Tuple[str, str]]) -> Tuple[str, str]:
    kinds = {kind for kind, _ in problems}
    if "unexpected" in kinds:
        return E_JKG_UNEXPECTED, "unexpected jkg_* object present"
    if "conflict" in kinds:
        return E_JKG_CONFLICT, "existing object conflicts with the pinned structure"
    return E_JKG_PARTIAL, "partial JKG structure"


# ---------------------------------------------------------------------------
# JME fingerprint (all JME tables, all rows, all schema objects) and preconditions
# ---------------------------------------------------------------------------
def _table_digest(conn: sqlite3.Connection, table: str) -> Tuple[int, str]:
    quoted = '"' + table.replace('"', '""') + '"'
    cursor = conn.execute(f"SELECT * FROM {quoted}")
    names = [d[0] for d in cursor.description]
    key_index = names.index("key") if table == "system_settings" and "key" in names else None
    hashes: List[str] = []
    for row in cursor:
        if key_index is not None and row[key_index] == VERSION_KEY:
            continue
        hashes.append(hashlib.sha256(repr(tuple(row)).encode("utf-8")).hexdigest())
    hashes.sort()
    body = "|".join(names) + "\n" + "\n".join(hashes)
    return len(hashes), hashlib.sha256(body.encode("utf-8")).hexdigest()


def _jme_schema_digest(conn: sqlite3.Connection) -> Tuple[int, str]:
    rows = sorted(
        "|".join((str(t), str(n), str(tbl), _norm(sql) if sql else ""))
        for t, n, tbl, sql in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        if not _is_jkg_object(str(n), str(tbl))
    )
    return len(rows), hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    tables: Dict[str, Dict[str, Any]] = {}
    names = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite%' ORDER BY name"
        ).fetchall()
    ]
    for name in names:
        if _is_jkg_object(name, name):
            continue
        count, digest = _table_digest(conn, name)
        tables[name] = {"rows": count, "sha256": digest}
    schema_count, schema_digest = _jme_schema_digest(conn)
    return {
        "jme_tables": tables,
        "jme_schema_objects": {"count": schema_count, "sha256": schema_digest},
        "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
    }


def _check_jme(conn: sqlite3.Connection) -> None:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise Refused(E_INTEGRITY, f"PRAGMA integrity_check = {integrity}")
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in JARVISMIND_REQUIRED_COLUMNS if t not in tables]
    if missing:
        raise Refused(E_JME_PRECONDITION, "JME tables missing", missing)
    for table, required in JARVISMIND_REQUIRED_COLUMNS.items():
        cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
        absent = [c for c in required if c not in cols]
        if absent:
            raise Refused(E_JME_PRECONDITION, f"JME columns missing in {table}", absent)
    versions = tuple(
        int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    )
    if versions != JARVISMIND_REQUIRED_VERSIONS:
        raise Refused(
            E_JME_PRECONDITION,
            f"schema_migrations must be exactly {list(JARVISMIND_REQUIRED_VERSIONS)}",
            [str(list(versions))],
        )


def classify(conn: sqlite3.Connection) -> Tuple[str, Dict[str, Any], List[Tuple[str, str]]]:
    """Return (state, actual_snapshot, problems) or raise Refused. Read-only."""
    actual = _snapshot(conn)
    problems = _diff(expected_snapshot(), actual)
    version_rows = conn.execute(
        "SELECT value FROM system_settings WHERE key = ?", (VERSION_KEY,)
    ).fetchall()
    version = str(version_rows[0][0]) if version_rows else None
    has_objects = bool(actual["objects"])

    if not has_objects and version is None:
        return STATE_ABSENT, actual, []
    if not has_objects:
        raise Refused(E_JKG_VERSION, "version key present but no JKG objects exist")
    if problems:
        code, message = _classify_problems(problems)
        raise Refused(code, message, [f"{kind}:{name}" for kind, name in problems])
    if version is None:
        raise Refused(E_JKG_PARTIAL, "complete JKG objects exist without a version key")
    if version != VERSION_VALUE:
        raise Refused(E_JKG_VERSION, f"JKG version {version!r} != {VERSION_VALUE!r}")
    return STATE_INSTALLED, actual, []


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(path), uri=True)


def _check_header(path: Path) -> None:
    if not path.is_file():
        raise Refused(E_TARGET_MISSING, f"database file does not exist: {path}")
    with path.open("rb") as handle:
        header = handle.read(16)
    if header != _SQLITE_HEADER:
        raise Refused(E_NOT_SQLITE, "target is not a SQLite database file")


def _readonly_report(path: Path) -> Tuple[str, Dict[str, Any]]:
    _check_header(path)
    try:
        conn = _open_ro(path)
        try:
            _check_jme(conn)
            state, _, _ = classify(conn)
            fp = fingerprint(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise Refused(E_INTEGRITY, f"database cannot be read: {type(exc).__name__}") from exc
    return state, fp


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_backup(path: Path, backup_dir: Path, source_fp: Dict[str, Any]) -> Dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backup_dir / f"jme-pre-jkg-{stamp}.db"
    manifest_path = dest.with_suffix(".manifest.json")
    if dest.exists() or manifest_path.exists():
        raise Refused(E_BACKUP_FAILED, "backup destination already exists")
    try:
        src = _open_ro(path)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        check = _open_ro(dest)
        try:
            backup_fp = fingerprint(check)
        finally:
            check.close()
        if backup_fp != source_fp:
            raise Refused(E_BACKUP_FAILED, "backup fingerprint does not match source")
        manifest = {
            "backup_file": dest.name,
            "sha256": digest,
            "source_db": path.name,
            "created_at": _now(),
            "fingerprint": backup_fp,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:
        _remove_quietly(dest)
        _remove_quietly(manifest_path)
        raise
    return {"backup_file": str(dest), "manifest_file": str(manifest_path), "sha256": digest}


def _rollback_or_unknown(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise UnknownState(
            E_ROLLBACK_FAILED,
            f"ROLLBACK failed ({type(exc).__name__}); state not verified; restore from backup",
        ) from exc


def _assert_in_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise UnknownState(
            E_POST_COMMIT,
            "transaction ended inside the installer; it may have been committed or rolled back; "
            "state unknown; run inspect and restore from backup if needed",
        )


def _transaction_install(
    path: Path,
    plan: List[str],
    fp_before: Dict[str, Any],
    *,
    execute_hook: Optional[Callable[[str], None]],
    verify_hook: Optional[Callable[[sqlite3.Connection], None]],
) -> Tuple[int, bool]:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=LOCK_TIMEOUT_SECONDS)
    executed = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise Refused(E_LOCKED, "database is locked; stop JME writers and retry") from exc
        try:
            _check_jme(conn)
            state, _, _ = classify(conn)
            if state != STATE_ABSENT:
                _rollback_or_unknown(conn)
                return 0, False
            for stmt in plan:
                try:
                    if execute_hook is not None:
                        execute_hook(stmt)
                    conn.execute(stmt)
                except sqlite3.Error as exc:
                    raise RolledBack(E_DDL_FAILED, f"DDL failed: {type(exc).__name__}: {exc}") from exc
                executed += 1
                _assert_in_transaction(conn)
            conn.execute(
                "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                (VERSION_KEY, VERSION_VALUE, _now()),
            )
            if verify_hook is not None:
                verify_hook(conn)
            _assert_in_transaction(conn)
            try:
                post_state, _, _ = classify(conn)
            except Refused as exc:
                raise RolledBack(E_VERIFY_FAILED, exc.message, exc.details) from exc
            if post_state != STATE_INSTALLED:
                raise RolledBack(E_VERIFY_FAILED, "structure not INSTALLED before commit")
            fp_after = fingerprint(conn)
            if fp_after != fp_before:
                raise RolledBack(E_VERIFY_FAILED, "JME fingerprint changed inside the transaction")
            _assert_in_transaction(conn)
            conn.execute("COMMIT")
        except InstallerError:
            _rollback_or_unknown(conn)
            raise
        except Exception as exc:
            _rollback_or_unknown(conn)
            raise RolledBack(E_VERIFY_FAILED, f"unexpected error, rolled back: {type(exc).__name__}") from exc
        return executed, True
    finally:
        conn.close()


def _postcommit_verify(path: Path, before_fp: Dict[str, Any]) -> None:
    conn = _open_ro(path)
    try:
        try:
            state, _, _ = classify(conn)
        except Refused as exc:
            raise UnknownState(E_POST_COMMIT, exc.message, exc.details) from exc
        after_fp = fingerprint(conn)
    finally:
        conn.close()
    if state != STATE_INSTALLED:
        raise UnknownState(E_POST_COMMIT, f"post-commit state is {state}; restore from backup")
    if after_fp != before_fp:
        raise UnknownState(E_POST_COMMIT, "post-commit JME fingerprint changed; restore from backup")


def install(
    db_path: Any,
    *,
    mode: str = "inspect",
    backup_dir: Optional[Any] = None,
    allow_real_database: bool = False,
    approval_ref: str = "",
    _before_lock_hook: Optional[Callable[[Path], None]] = None,
    _execute_hook: Optional[Callable[[str], None]] = None,
    _verify_hook: Optional[Callable[[sqlite3.Connection], None]] = None,
) -> Dict[str, Any]:
    """Run the installer. The ``_*_hook`` parameters are for tests only."""
    report: Dict[str, Any] = {
        "installer": "upgrade_to_jkg",
        "installer_version": INSTALLER_VERSION,
        "jkg_schema_version": jkg_schema.JKG_SCHEMA_VERSION,
        "mode": mode,
        "db": str(db_path),
        "statements_planned": 0,
        "statements_executed": 0,
        "backup": None,
        "approval_ref": str(approval_ref or "").strip() or None,
        "error_code": None,
        "message": None,
        "details": [],
    }
    try:
        if mode not in ("inspect", "install"):
            raise Refused(E_PLAN_REJECTED, f"unknown mode: {mode}")
        target = check_target(db_path, allow_real_database=allow_real_database, approval_ref=approval_ref)
        report["db"] = str(target)
        if backup_dir is not None:
            _check_backup_dir(backup_dir)
        plan = build_plan()
        report["statements_planned"] = len(plan)
        state_before, fp_before = _readonly_report(target)
        report["state_before"] = state_before
        report["jme_before"] = fp_before
        if mode == "inspect":
            report.update(status="INSPECTED", state_after=state_before, jme_after=fp_before)
            return report
        if state_before == STATE_INSTALLED:
            report.update(status="ALREADY_INSTALLED", state_after=STATE_INSTALLED, jme_after=fp_before)
            return report
        if backup_dir is not None:
            try:
                report["backup"] = _write_backup(target, Path(backup_dir), fp_before)
            except (OSError, sqlite3.Error) as exc:
                raise Refused(E_BACKUP_FAILED, f"backup failed: {type(exc).__name__}") from exc
        if _before_lock_hook is not None:
            _before_lock_hook(target)
        check_target(target, allow_real_database=allow_real_database, approval_ref=approval_ref)
        executed, committed = _transaction_install(
            target, plan, fp_before, execute_hook=_execute_hook, verify_hook=_verify_hook
        )
        report["statements_executed"] = executed
        if committed:
            try:
                _postcommit_verify(target, fp_before)
            except InstallerError:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to the unknown-state exit code
                raise UnknownState(
                    E_POST_COMMIT, f"post-commit verification error: {type(exc).__name__}"
                ) from exc
            report.update(status="INSTALLED", state_after=STATE_INSTALLED, jme_after=fp_before)
        else:
            state_now, fp_now = _readonly_report(target)
            report.update(status="ALREADY_INSTALLED", state_after=state_now, jme_after=fp_now)
        return report
    except InstallerError as exc:
        report.update(
            status=_STATUS_BY_EXIT[exc.exit_code],
            error_code=exc.code,
            message=exc.message,
            details=exc.details,
            exit_code=exc.exit_code,
        )
        return report
    except Exception as exc:  # noqa: BLE001 - reported, never raised to the caller
        report.update(
            status="ERROR",
            error_code=E_UNEXPECTED,
            message=f"unexpected internal error: {type(exc).__name__}",
            exit_code=5,
        )
        return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit, fail-closed installer for the JKG Schema (JKG-INT-01)."
    )
    parser.add_argument("--db", required=True, help="explicit path to the SQLite database")
    parser.add_argument(
        "--mode", choices=("inspect", "install"), default="inspect",
        help="inspect is read-only and the default; install writes DDL in one transaction",
    )
    parser.add_argument("--backup-dir", default=None, help="write a verified pre-install backup here")
    parser.add_argument(
        "--allow-real-database", action="store_true",
        help="permit a path inside the repository data directory (also needs --approval-ref)",
    )
    parser.add_argument("--approval-ref", default="", help="owner approval reference for real-database use")
    args = parser.parse_args(argv)
    report = install(
        args.db,
        mode=args.mode,
        backup_dir=args.backup_dir,
        allow_real_database=args.allow_real_database,
        approval_ref=args.approval_ref,
    )
    exit_code = int(report.pop("exit_code", 0))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
