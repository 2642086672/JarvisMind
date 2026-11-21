"""Consistent SQLite backup, verification, and restore.

The backup uses SQLite's online backup API rather than copying the main file,
so a WAL that is being written to does not produce a torn copy.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REQUIRED_TABLES: tuple = (
    "schema_migrations",
    "system_settings",
    "memory_records",
    "memory_sources",
    "memory_audit_logs",
)


class BackupError(Exception):
    """Raised when a backup is unreadable, incomplete, or unsafe to restore."""


def _table_names(connection: sqlite3.Connection) -> set:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def _check_integrity(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "unknown"


def verify_backup(path: Path) -> Dict[str, Any]:
    """Validate a backup file and return a summary. Raises BackupError."""
    if not path.exists():
        raise BackupError(f"Backup file not found: {path}")
    connection = sqlite3.connect(str(path))
    try:
        integrity = _check_integrity(connection)
        if integrity != "ok":
            raise BackupError(f"Backup failed integrity_check: {integrity}")
        tables = _table_names(connection)
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            raise BackupError(f"Backup is missing required tables: {missing}")
        counts = {
            name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in REQUIRED_TABLES
        }
        version_row = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = int(version_row[0]) if version_row and version_row[0] is not None else 0
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Backup is not a readable SQLite database: {exc}") from exc
    finally:
        connection.close()
    return {
        "path": str(path),
        "integrity": integrity,
        "schema_version": version,
        "tables": sorted(tables),
        "counts": counts,
    }


def create_backup(db_path: Path, dest_path: Path) -> Dict[str, Any]:
    """Write a consistent backup of ``db_path`` to ``dest_path`` and verify it."""
    if not db_path.exists():
        raise BackupError(f"Database file not found: {db_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(dest_path))
    try:
        source.backup(target)
        integrity = _check_integrity(target)
        if integrity != "ok":
            raise BackupError(f"Newly written backup failed integrity_check: {integrity}")
        tables = _table_names(target)
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            raise BackupError(f"Backup is missing required tables: {missing}")
        counts = {
            name: int(target.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in REQUIRED_TABLES
        }
        version_row = target.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = int(version_row[0]) if version_row and version_row[0] is not None else 0
        target.commit()
    finally:
        target.close()
        source.close()
    return {
        "path": str(dest_path),
        "integrity": integrity,
        "schema_version": version,
        "counts": counts,
    }


def _remove_stale_wal(db_path: Path) -> List[str]:
    """Delete WAL/SHM side files so a restored database cannot replay stale pages.

    The caller must have already made a safety copy of the live database; a
    side file that another process still holds is left in place and reported.
    """
    removed = []
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if not side.exists():
            continue
        try:
            side.unlink()
            removed.append(str(side))
        except OSError:
            continue
    return removed


def restore_backup(
    backup_path: Path,
    db_path: Path,
    *,
    force: bool = False,
    safety_dir: Path | None = None,
) -> Dict[str, Any]:
    """Restore ``backup_path`` over ``db_path`` after verifying it.

    If the target already exists, ``force`` must be true; the existing database
    is then written to a safety backup first so the restore is reversible.

    Restore is an offline operation: the caller must close every SQLite /
    SQLAlchemy connection to ``db_path`` first. On POSIX, unlinking a live
    ``-wal`` / ``-shm`` file succeeds and then corrupts the open connection,
    which surfaces as ``sqlite3.OperationalError: disk I/O error``.
    """
    info = verify_backup(backup_path)

    safety_backup = None
    if db_path.exists():
        if not force:
            raise BackupError(
                f"Target database already exists: {db_path}. "
                "Pass force=True to replace it (a safety copy is made first)."
            )
        if safety_dir is None:
            safety_dir = db_path.parent / "backups"
        safety_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_path = safety_dir / f"{db_path.name}.pre-restore-{stamp}"
        safety_backup = create_backup(db_path, safety_path)
        info["safety_backup"] = safety_backup

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Side files must go before the target is opened, otherwise SQLite would
    # try to replay pages that no longer belong to this database.
    removed = _remove_stale_wal(db_path)

    source = sqlite3.connect(str(backup_path))
    target = sqlite3.connect(str(db_path))
    try:
        source.backup(target)
        integrity = _check_integrity(target)
        target.commit()
    finally:
        target.close()
        source.close()

    info["restored_to"] = str(db_path)
    info["integrity_after_restore"] = integrity
    info["removed_side_files"] = removed
    return info
