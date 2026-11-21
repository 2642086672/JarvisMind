"""In-process cache for the AI-generated memory overview summaries.

Why not the database: Schema v6 is frozen for this task, so the cache cannot
add a table. It also does not need durability — it is a derived, disposable
artefact that can always be regenerated from ``memory_records``.

Invalidation is content-based, not time-based: the key is the overview's
``source_version`` (a hash of every eligible memory id + updated_at). When a
memory is added, edited, merged, forgotten or expires, ``source_version``
changes and the cached summaries are reported as stale. The old text is still
returned so the user sees something immediately, but it is explicitly labelled
stale until they regenerate.

Single-user service, so one entry is enough. A lock keeps concurrent refreshes
from interleaving.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CachedOverview:
    """Summaries produced by the model for one ``source_version``."""

    source_version: str
    generated_at: str
    model: str
    #: category key -> model-written summary
    summaries: Dict[str, str]

    def is_current(self, source_version: str) -> bool:
        return self.source_version == source_version


class MemoryOverviewCache:
    """Thread-safe single-entry cache for derived summaries."""

    def __init__(self) -> None:
        self._entry: Optional[CachedOverview] = None
        self._lock = threading.Lock()

    def get(self) -> Optional[CachedOverview]:
        with self._lock:
            return self._entry

    def put(self, entry: CachedOverview) -> None:
        with self._lock:
            self._entry = entry

    def clear(self) -> None:
        with self._lock:
            self._entry = None


#: Process-wide cache. The service is single-user and the data is derived, so a
#: module-level instance is sufficient and keeps the container stateless.
overview_cache = MemoryOverviewCache()


__all__ = ["CachedOverview", "MemoryOverviewCache", "overview_cache"]
