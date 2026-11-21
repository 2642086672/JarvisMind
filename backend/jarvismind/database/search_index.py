"""Search index maintenance for the FTS5 tables.

The segmented ``search_text`` column is computed in Python because SQLite has
no CJK tokenizer. On startup the app recomputes it for any row whose value is
missing or stale, then rebuilds the FTS5 index from the content table.
"""
from __future__ import annotations

from sqlalchemy import text

from ..core.search import segment_for_index
from .connection import DatabaseManager

ZH_TABLE = "memory_records_fts_zh"


def fts5_available(manager: DatabaseManager) -> bool:
    with manager.session() as session:
        rows = session.execute(text("PRAGMA compile_options")).scalars().all()
    return any(str(row).upper() == "ENABLE_FTS5" for row in rows)


def zh_index_available(manager: DatabaseManager) -> bool:
    with manager.session() as session:
        row = session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
            ),
            {"name": ZH_TABLE},
        ).first()
    return row is not None


def rebuild_search_index(manager: DatabaseManager) -> int:
    """Recompute ``search_text`` and rebuild the FTS5 index. Returns row count."""
    with manager.session() as session:
        rows = session.execute(
            text("SELECT id, content, summary, search_text FROM memory_records")
        ).all()
        for record_id, content, summary, existing in rows:
            computed = segment_for_index(f"{content or ''} {summary or ''}")
            if existing != computed:
                # Server-internal FTS bookkeeping: allowed without a request
                # grant while governance is active (review P1 finding E4).
                from ..p1_gate import maintenance_write

                with maintenance_write():
                    session.execute(
                        text("UPDATE memory_records SET search_text=:s WHERE id=:i"),
                        {"s": computed, "i": record_id},
                    )
        if zh_index_available(manager):
            session.execute(
                text(f"INSERT INTO {ZH_TABLE}({ZH_TABLE}) VALUES('rebuild')")
            )
    return len(rows)
