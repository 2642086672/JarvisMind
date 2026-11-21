"""Create a consistent SQLite backup of the JME database.

Usage:
    python scripts/backup_db.py --output backups/jme-<stamp>.db
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvismind.config import load_settings  # noqa: E402
from jarvismind.core.logging import configure_logging  # noqa: E402
from jarvismind.database.backup import BackupError, create_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the JME SQLite database")
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Destination file (default: backups/jme-<timestamp>.db)",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level, settings.api_token)

    destination = (
        Path(args.output)
        if args.output
        else Path("backups") / f"jme-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    )
    try:
        info = create_backup(settings.database_path, destination)
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, indent=2))
    print(
        f"OK: {info['counts']['memory_records']} memories, "
        f"{info['counts']['memory_audit_logs']} audit entries, "
        f"schema v{info['schema_version']} -> {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
