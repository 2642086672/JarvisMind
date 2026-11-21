"""Verify and restore a JME SQLite backup.

The backup is integrity-checked and its schema is validated before anything is
written. If the live database exists, pass --force; the current database is
then written to a safety backup first.

Usage:
    python scripts/restore_db.py backups/jme-20261110-190000.db
    python scripts/restore_db.py backups/jme-20261110-190000.db --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvismind.config import load_settings  # noqa: E402
from jarvismind.core.logging import configure_logging  # noqa: E402
from jarvismind.database.backup import BackupError, restore_backup, verify_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore the JME SQLite database")
    parser.add_argument("backup", help="Backup file to restore")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing live database (a safety copy is made first)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify the backup without writing anything",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level, settings.api_token)

    backup_path = Path(args.backup)
    try:
        info = verify_backup(backup_path)
        print("BACKUP VERIFIED:")
        print(json.dumps(info, indent=2))
        if args.dry_run:
            print("Dry run: nothing was written.")
            return 0
        result = restore_backup(
            backup_path,
            settings.database_path,
            force=args.force,
            safety_dir=settings.data_dir / "backups",
        )
    except BackupError as exc:
        print(f"RESTORE FAILED: {exc}", file=sys.stderr)
        return 1

    if result.get("safety_backup"):
        print(f"Safety copy of previous database: {result['safety_backup']['path']}")
    print(
        f"OK: restored schema v{info['schema_version']} "
        f"({info['counts']['memory_records']} memories) -> {settings.database_path}"
    )
    print(f"integrity after restore: {result['integrity_after_restore']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
