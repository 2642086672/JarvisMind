"""Initial long-term memory import with dedup protection.

P0-03A-R3: ``dry_run=True`` is a genuine read-only preview.  It still walks
every entry, hashes/dedups, validates and computes all statistics, but it
never creates a source row, never calls ``MemoryService.create`` and never
issues DDL/DML.  The only database statements on the dry-run path are the
SELECTs needed to build the existing-record dedup set.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List

from ..core.dedup import is_duplicate, normalize
from ..core.timeutils import utc_now_iso
from ..database.connection import DatabaseManager
from ..schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from ..schemas.source import SourceCreate
from .memory_service import MemoryService
from .source_service import SourceService


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _PreviewSource:
    """Read-only stand-in for a source that does not exist yet.

    ``dry_run`` must report which source *would* be created without creating
    it.  ``id`` is None so no caller can accidentally persist a foreign key
    pointing at a phantom row.
    """

    id: None = None
    is_preview: bool = True


def _find_source(service: SourceService, source_type: str, source_name: str):
    """SELECT-only lookup.  Never writes; safe on the dry-run path."""
    sources, _ = service.list(source_type=source_type, page=1, page_size=1000)
    for source in sources:
        if source.source_name == source_name:
            return source
    return None


def _ensure_source(
    service: SourceService,
    source_type: str,
    source_name: str,
    *,
    dry_run: bool = False,
):
    """Find an existing source, or create one **only** for a real import.

    The create branch is the single place in this module that writes
    ``memory_sources``.  ``dry_run=True`` returns ``_PreviewSource`` instead,
    so the INSERT is unreachable on the preview path.
    """
    found = _find_source(service, source_type, source_name)
    if found is not None:
        return found
    if dry_run:
        return _PreviewSource()
    return service.create(
        SourceCreate(source_type=source_type, source_name=source_name)
    )


def run_import(
    manager: DatabaseManager,
    document: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Import every entry in ``document`` once. Re-running is a no-op.

    ``dry_run`` keeps the full preview/validation/statistics loop intact and
    only suppresses the two write operations (source INSERT and memory
    INSERT).  No early return, no skipped dedup, no exception swallowing.
    """
    entries: List[Dict[str, Any]] = document["memories"]
    memory_service = MemoryService(manager)
    source_service = SourceService(manager)

    # R3: the dry-run flag is now threaded into source resolution, so the
    # unconditional INSERT that used to run before the dry_run check is gone.
    source = _ensure_source(
        source_service,
        document["source_type"],
        document["source_name"],
        dry_run=dry_run,
    )

    # SELECT-only: build the dedup set from existing rows.
    existing = memory_service.list(page=1, page_size=1_000_000)[0]
    known_hashes = {content_hash(record.content) for record in existing}
    known_contents = [record.content for record in existing]

    imported = 0
    skipped_hash = 0
    skipped_similar = 0
    by_type: Counter = Counter()

    for entry in entries:
        content = str(entry["content"]).strip()
        digest = content_hash(content)
        if digest in known_hashes:
            skipped_hash += 1
            continue
        if any(is_duplicate(content, old) for old in known_contents):
            skipped_similar += 1
            continue

        # Pure in-memory validation: preview reports bad entries instead of
        # silently accepting them.  No database access here.
        memory_type = MemoryType(entry["memory_type"])
        importance = float(entry.get("importance", 5.0))
        confidence = float(entry.get("confidence", 0.5))

        by_type[str(entry["memory_type"])] += 1
        imported += 1
        if dry_run:
            known_hashes.add(digest)
            known_contents.append(content)
            continue

        memory_service.create(
            MemoryCreate(
                memory_type=memory_type,
                content=content,
                summary=entry.get("summary"),
                importance=importance,
                confidence=confidence,
                status=MemoryStatus.ACTIVE,
                source_id=source.id,
                metadata={
                    "initial_import": True,
                    "imported_at": utc_now_iso(),
                    "content_hash": digest,
                    "pending_confirmation": bool(
                        entry.get("metadata", {}).get("pending_confirmation", False)
                    ),
                },
            ),
            actor="initial-import",
        )
        known_hashes.add(digest)
        known_contents.append(content)

    return {
        "source_id": source.id,
        "source_is_preview": bool(getattr(source, "is_preview", False)),
        "source_type": document["source_type"],
        "source_name": document["source_name"],
        "total": len(entries),
        "imported": imported,
        "skipped_identical": skipped_hash,
        "skipped_similar": skipped_similar,
        "by_type": dict(sorted(by_type.items())),
        "dry_run": dry_run,
    }


def load_document(path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
