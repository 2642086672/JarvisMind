"""Import memories from a JSON file in the import format into JME.

The import is idempotent: a memory is skipped when an identical (normalised)
content hash already exists, or when a near-duplicate is found. Nothing is ever
deleted or overwritten.

Usage:
    python scripts/import_memories.py --dry-run
    python scripts/import_memories.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvismind.config import load_settings  # noqa: E402
from jarvismind.core.logging import configure_logging  # noqa: E402
from jarvismind.database.connection import DatabaseManager  # noqa: E402
from jarvismind.services.initial_import import load_document, run_import  # noqa: E402



def main() -> int:
    parser = argparse.ArgumentParser(description="Import initial user memories")
    parser.add_argument("--data", default=None, help="JSON file in the import format")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # dry-run 在解析参数后立即返回：不加载配置、不构造 DatabaseManager、
    # 不读取 args.data、不创建任何文件，保证零写入与零数据库接触。
    if args.dry_run:
        print("[JME] --dry-run: 第一阶段安全策略下不执行任何数据库操作、不读取配置、不创建任何文件。")
        return 0

    if args.data is None:
        parser.error("--data is required unless --dry-run is given")
    settings = load_settings()
    configure_logging(settings.log_level, settings.api_token)
    document = load_document(Path(args.data))

    manager = DatabaseManager(settings)
    try:
        result = run_import(manager, document, dry_run=args.dry_run)
    finally:
        manager.dispose()

    print(f"source: {result['source_type']} / {result['source_name']} ({result['source_id']})")
    print(f"total entries: {result['total']}")
    print(f"imported: {result['imported']}")
    print(f"skipped (identical): {result['skipped_identical']}")
    print(f"skipped (similar): {result['skipped_similar']}")
    print("by type:")
    for memory_type, count in result["by_type"].items():
        print(f"  {memory_type}: {count}")
    # 主流程已保证非 dry-run，此处一定发生了写入；按实际行为汇报，不得声称未写入。
    if result["dry_run"]:
        print("dry run: nothing was written")
    else:
        print("import applied: changes were written to the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
