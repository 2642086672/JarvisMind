"""Export JME memories (and their sources) to a JSON file.

Usage:
    python scripts/export_memories.py --output exports/memories.json
    python scripts/export_memories.py --memory-type PROJECT --status ACTIVE
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
from jarvismind.core.timeutils import utc_now_iso  # noqa: E402
from jarvismind.database.connection import DatabaseManager  # noqa: E402
from jarvismind.schemas.memory import MemoryStatus, MemoryType  # noqa: E402
from jarvismind.services.memory_service import MemoryService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export JME memories to JSON")
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Destination file (default: exports/memories-<timestamp>.json)",
    )
    parser.add_argument("--memory-type", default=None, choices=[t.value for t in MemoryType])
    parser.add_argument("--status", default="ACTIVE", choices=[s.value for s in MemoryStatus])
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.log_level, settings.api_token)

    destination = (
        Path(args.output)
        if args.output
        else Path("exports")
        / f"memories-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )

    manager = DatabaseManager(settings)
    try:
        payload = MemoryService(manager).export_memories(
            memory_type=args.memory_type,
            status=args.status,
        )
        document = {
            "format": "jme-export",
            "version": 1,
            "generated_at": utc_now_iso(),
            "filters": {"memory_type": args.memory_type, "status": args.status},
            "counts": {
                "memories": len(payload["memories"]),
                "sources": len(payload["sources"]),
            },
            "memories": payload["memories"],
            "sources": payload["sources"],
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        manager.dispose()

    print(
        f"OK: {document['counts']['memories']} memories, "
        f"{document['counts']['sources']} sources -> {destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
