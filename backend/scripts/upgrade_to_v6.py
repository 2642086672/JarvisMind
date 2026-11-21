#!/usr/bin/env python3
"""P1-01 / stage 0: append-only, fail-closed v5 -> v6 schema upgrader.

Why a separate entry point instead of re-enabling MigrationRunner.apply_pending():

* ``app/database/migrations.py`` keeps its P0-03A hard block. 27 startup-safety
  tests assert that every schema-writing entry point raises MigrationBlocked with
  ZERO database access on the rejection path. Re-enabling it would break the
  existing regression suite and invalidate the P0 safety acceptance record.
* This module is an explicit, human-invoked channel. It never patches, wraps, or
  monkey-patches MigrationRunner. It reads the v6 SQL as DATA from MIGRATIONS,
  statically proves the SQL is append-only, and applies it in ONE transaction.

Guarantees:

* No DROP TABLE / DROP COLUMN / DELETE / UPDATE of any memory_* row.
* Historical rows are never rewritten: v6 only adds nullable/defaulted columns
  and creates new tables/indexes.
* One transaction: any statement failure rolls back the whole upgrade and leaves
  no version=6 marker behind.
* Idempotent: an already-v6 database is a no-op; a re-run cannot duplicate DDL.
* Partial v6 (objects exist but version 6 is not recorded) is refused.
* The database path is explicit; there is no implicit production-path default.

Complexity: O(rows) for the two content digests, O(1) for every DDL statement.
The digest scan reads memory_records once before and once after the upgrade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

# ``scripts/`` is not a package: make the JME application root importable.
_JME_ROOT = Path(__file__).resolve().parents[1]
if str(_JME_ROOT) not in sys.path:
    sys.path.insert(0, str(_JME_ROOT))

from jarvismind.database import migrations as mig  # noqa: E402  (path bootstrap above)

TARGET_VERSION = 6
REQUIRED_VERSIONS: Tuple[int, ...] = (1, 2, 3, 4, 5)
V6_OBJECTS: Tuple[str, ...] = mig._V6_OBJECT_NAMES

# Only these statement shapes may ever be executed. ``ALTER TABLE ... RENAME``
# and ``... DROP COLUMN`` are deliberately excluded.
_ALLOWED_CREATE_PREFIXES: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS ",
    "CREATE UNIQUE INDEX IF NOT EXISTS ",
    "CREATE INDEX IF NOT EXISTS ",
)


class UpgradeRefused(RuntimeError):
    """Precondition or safety invariant failed; no write has been attempted."""


class UpgradeFailed(RuntimeError):
    """A statement failed; the transaction was rolled back."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_uri(db_path: Path) -> str:
    posix = "/" + str(db_path.resolve()).replace("\\", "/")
    posix = posix.replace("?", "%3F").replace("#", "%23")
    return f"file:{posix}?mode=ro"


def _writable_uri(db_path: Path) -> str:
    return str(db_path.resolve())


def _content_digest(conn: sqlite3.Connection) -> str:
    """SHA-256 over (id, content) of every formal memory, ordered by id.

    Any rewrite of historical content changes this value. New nullable columns
    do not, which is exactly the compatibility property we want to verify.
    """
    h = hashlib.sha256()
    for row in conn.execute("SELECT id, content FROM memory_records ORDER BY id"):
        h.update(str(row[0]).encode("utf-8"))
        h.update(b"\x1f")
        h.update((row[1] or "").encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def _table_names(conn: sqlite3.Connection) -> List[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]


def _object_names(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def inspect(db_path: Path) -> Dict[str, Any]:
    """Read-only snapshot. Opens with mode=ro; never executes DDL/DML."""
    if not db_path.is_file():
        raise UpgradeRefused(f"database file does not exist: {db_path}")
    with closing(sqlite3.connect(_readonly_uri(db_path), uri=True)) as conn:
        tables = _table_names(conn)
        objects = set(_object_names(conn))
        has_marker = "schema_migrations" in tables
        versions = (
            [
                int(row[0])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            if has_marker
            else []
        )
        v6_present = [name for name in V6_OBJECTS if name in objects]
        counts = {
            table: _row_count(conn, table)
            for table in (
                "memory_records",
                "memory_candidates",
                "memory_audit_logs",
                "memory_sources",
                "memory_summaries",
                "memory_corrections",
                "memory_merges",
                "memory_archive_records",
                "memory_lifecycle_reviews",
            )
            if table in tables
        }
        schema_version = None
        if "system_settings" in tables:
            schema_version = conn.execute(
                "SELECT value FROM system_settings WHERE key='jme.schema_version'"
            ).fetchone()
            schema_version = schema_version[0] if schema_version else None
        return {
            "db": str(db_path),
            "size_bytes": db_path.stat().st_size,
            "table_count": len(tables),
            "tables": tables,
            "schema_migrations_exists": has_marker,
            "versions": versions,
            "v6_objects_present": v6_present,
            "v6_object_count": len(v6_present),
            "v6_object_total": len(V6_OBJECTS),
            "row_counts": counts,
            "memory_content_sha256": _content_digest(conn),
            "jme_schema_version": schema_version,
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        }


def validate_v6_sql(statements: Sequence[str]) -> List[str]:
    """Fail-closed static check: every statement must be append-only."""
    normalized: List[str] = []
    for raw in statements:
        one = " ".join(raw.strip().split())
        upper = one.upper()
        if upper.startswith("ALTER TABLE ") and " ADD COLUMN " in upper:
            normalized.append(one)
            continue
        if upper.startswith(_ALLOWED_CREATE_PREFIXES):
            normalized.append(one)
            continue
        raise UpgradeRefused(f"non-append-only v6 statement refused: {one[:120]}")
    if not normalized:
        raise UpgradeRefused("v6 migration contains no statements")
    return normalized


def v6_statements() -> List[str]:
    matches = [m for m in mig.MIGRATIONS if m.version == TARGET_VERSION]
    if len(matches) != 1:
        raise UpgradeRefused("v6 migration definition is missing or duplicated")
    return validate_v6_sql(matches[0].statements)


def precheck(before: Dict[str, Any]) -> str:
    """Return a status string; raise UpgradeRefused on any unsafe state."""
    versions = before["versions"]
    present = before["v6_object_count"]
    if not before["schema_migrations_exists"]:
        raise UpgradeRefused("schema_migrations table is missing")
    if before["integrity"] != "ok":
        raise UpgradeRefused(f"PRAGMA integrity_check = {before['integrity']}")
    if present and TARGET_VERSION not in versions:
        raise UpgradeRefused(
            "PARTIAL_V6: v6 objects exist without a version 6 marker; "
            f"present={before['v6_objects_present']}"
        )
    if TARGET_VERSION in versions and present == before["v6_object_total"]:
        return "ALREADY_V6"
    if TARGET_VERSION in versions:
        raise UpgradeRefused(
            "version 6 is recorded but v6 objects are incomplete; manual repair required"
        )
    if versions != list(REQUIRED_VERSIONS):
        raise UpgradeRefused(
            f"expected versions {list(REQUIRED_VERSIONS)}, got {versions}"
        )
    if present:
        raise UpgradeRefused(
            f"unexpected v6 objects before upgrade: {before['v6_objects_present']}"
        )
    for required in ("memory_records", "memory_candidates", "memory_audit_logs"):
        if required not in before["tables"]:
            raise UpgradeRefused(f"required base table is missing: {required}")
    return "READY"


def _idempotent_execute(conn: sqlite3.Connection, statement: str) -> bool:
    """Execute one append-only DDL; skip only genuine idempotency conflicts."""
    try:
        conn.execute(statement)
        return True
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return False
        raise


def apply(db_path: Path) -> Dict[str, Any]:
    """Apply v6 in one transaction, then verify structure and data compatibility."""
    before = inspect(db_path)
    status = precheck(before)
    statements = v6_statements()
    if status == "ALREADY_V6":
        return {
            "status": "ALREADY_V6",
            "statements_executed": 0,
            "statements_skipped_idempotent": 0,
            "statement_total": len(statements),
            "before": before,
            "after": before,
            "data_compatible": True,
            "postcheck_problems": [],
        }

    executed = 0
    skipped = 0
    conn = sqlite3.connect(_writable_uri(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            if _idempotent_execute(conn, statement):
                executed += 1
            else:
                skipped += 1
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (TARGET_VERSION, "memory_security_gate", _utc_now()),
        )
        # Only the schema-version setting changes; no memory_* row is touched.
        updated = conn.execute(
            "UPDATE system_settings SET value=?, updated_at=? "
            "WHERE key='jme.schema_version'",
            (str(TARGET_VERSION), _utc_now()),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES ('jme.schema_version', ?, ?)",
                (str(TARGET_VERSION), _utc_now()),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise UpgradeFailed(f"v6 upgrade rolled back: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()

    after = inspect(db_path)
    problems: List[str] = []
    if after["versions"] != list(REQUIRED_VERSIONS) + [TARGET_VERSION]:
        problems.append(f"versions after upgrade: {after['versions']}")
    if after["v6_object_count"] != after["v6_object_total"]:
        problems.append(
            f"v6 objects {after['v6_object_count']}/{after['v6_object_total']}"
        )
    if after["integrity"] != "ok":
        problems.append(f"integrity: {after['integrity']}")
    if after["row_counts"] != before["row_counts"]:
        problems.append(
            f"row counts changed: before={before['row_counts']} after={after['row_counts']}"
        )
    if after["memory_content_sha256"] != before["memory_content_sha256"]:
        problems.append("memory_records content digest changed")
    if after["jme_schema_version"] != str(TARGET_VERSION):
        problems.append(f"jme.schema_version={after['jme_schema_version']}")
    return {
        "status": "UPGRADED" if not problems else "POSTCHECK_FAILED",
        "statements_executed": executed,
        "statements_skipped_idempotent": skipped,
        "statement_total": len(statements),
        "before": before,
        "after": after,
        "data_compatible": not problems,
        "postcheck_problems": problems,
    }


ROLLBACK_PLAN = """Manual rollback / deactivation (never executed automatically):
  1. Stop the JME service and every process holding the database open.
  2. Restore the three pre-v6 files from the backup directory created before
     the upgrade (jarvismind.db, jarvismind.db-wal, jarvismind.db-shm) - a full file-level restore,
     not DROP TABLE and not a rebuild of any table.
  3. Verify with: python scripts/upgrade_to_v6.py --db <restored path> --verify-only
     Expected: versions [1,2,3,4,5], v6_object_count 0.
  4. Logical deactivation without restoring files: set
     system_settings key 'p1.v6_governance_enabled' = '0'. P1 governance code
     reads that key and stays disabled; v6 tables stay in place, no data is lost.
  Rollback SQL (MIGRATION_V6_ROLLBACK_SQL) is documented for human review only;
  this script never runs DROP statements.
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append-only v5 -> v6 upgrade for a JME SQLite database."
    )
    parser.add_argument("--db", required=True, help="explicit path to the SQLite database")
    parser.add_argument(
        "--apply", action="store_true", help="actually write; default is dry-run"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="print the read-only snapshot and exit"
    )
    parser.add_argument("--rollback-plan", action="store_true", help="print the manual rollback plan")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    if args.rollback_plan:
        print(ROLLBACK_PLAN)
        return 0
    db_path = Path(args.db).resolve()
    try:
        if args.verify_only:
            result: Dict[str, Any] = {"status": "INSPECTED", "snapshot": inspect(db_path)}
        else:
            snapshot = inspect(db_path)
            status = precheck(snapshot)
            if not args.apply:
                result = {
                    "status": "DRY_RUN",
                    "would_apply": status,
                    "statement_total": len(v6_statements()),
                    "snapshot": snapshot,
                }
            else:
                result = apply(db_path)
    except (UpgradeRefused, UpgradeFailed) as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:  # unexpected: never mask it as a success
        print(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = result["status"]
        if "snapshot" in result:
            snap = result["snapshot"]
        else:
            snap = result.get("after", {})
        print(f"STATUS={status}")
        print(f"DB={snap.get('db', db_path)}")
        print(f"VERSIONS_BEFORE={result.get('before', {}).get('versions', snap.get('versions'))}")
        print(f"VERSIONS_AFTER={snap.get('versions')}")
        print(f"V6_OBJECTS={snap.get('v6_object_count')}/{snap.get('v6_object_total')}")
        print(f"ROW_COUNTS={snap.get('row_counts')}")
        print(f"MEMORY_CONTENT_SHA256={snap.get('memory_content_sha256')}")
        print(f"INTEGRITY={snap.get('integrity')}")
        if "statements_executed" in result:
            print(
                f"STATEMENTS executed={result['statements_executed']} "
                f"skipped_idempotent={result['statements_skipped_idempotent']} "
                f"total={result['statement_total']}"
            )
            print(f"DATA_COMPATIBLE={result['data_compatible']}")
            if result["postcheck_problems"]:
                print("POSTCHECK_PROBLEMS=" + json.dumps(result["postcheck_problems"]))
    if status in ("POSTCHECK_FAILED", "REFUSED", "ERROR"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
