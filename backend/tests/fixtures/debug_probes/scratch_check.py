"""Temporary SQLite inspection probe (P0-03A-R4 hardened).

Originally this module connected to the production database at import time
with a hardcoded absolute path.  It is now an explicit CLI tool:

* no module-level database / file / network side effects;
* no hardcoded production path -- the caller must pass ``--db``;
* every path is validated by ``app.core.production_guard`` and must live
  inside an explicitly declared temporary sandbox (``--sandbox`` or the
  JARVISMIND_DEBUG_TMP_ROOT environment variable);
* the connection is opened ``mode=ro``, so no DDL/DML and no new file.

Running it with no arguments fails closed with exit code 2.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvismind.core.production_guard import (  # noqa: E402
    ProductionAccessBlocked,
    open_debug_db_readonly,
)


def inspect(conn: sqlite3.Connection) -> None:
    """Read-only report on a sandbox database. No writes, no DDL."""
    cur = conn.cursor()
    tables = {
        row[0]
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    print("== tables ==", sorted(tables))
    for table in ("memory_records", "memory_sources", "vector_index_meta"):
        if table not in tables:
            continue
        print("== count", table, "==", cur.execute(
            "SELECT COUNT(*) FROM " + table
        ).fetchone()[0])
    if "memory_records" in tables:
        print("== by status ==")
        for row in cur.execute(
            "SELECT status, COUNT(*) c FROM memory_records GROUP BY status"
        ):
            print(" ", row[0], row[1])
        print("== cols ==")
        print([d[1] for d in cur.execute("PRAGMA table_info(memory_records)")])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inspection of a TEMPORARY SQLite database"
    )
    parser.add_argument(
        "--db", required=True,
        help="临时库路径（不允许指向生产数据目录）",
    )
    parser.add_argument(
        "--sandbox", default=None,
        help="临时沙箱根目录；未传时读调试沙箱环境变量",
    )
    args = parser.parse_args(argv)
    try:
        conn = open_debug_db_readonly(args.db, explicit_root=args.sandbox)
    except ProductionAccessBlocked as exc:
        print("[P0-03A-R4 REFUSED]", exc)
        return 2
    try:
        inspect(conn)
    except sqlite3.DatabaseError as exc:
        print("[P0-03A-R4] 只读检视失败:", exc)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
