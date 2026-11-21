"""B-2 regression: a forgotten memory must not survive through a cached summary.

Sequence under test:

1. build an AI summary that cites a memory;
2. forget that memory;
3. fetch the overview again;
4. the cached summary must not be shown, and reorganising must not cite the
   forgotten id.
"""
from __future__ import annotations

import uuid

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.models import MemoryRecord
from jarvismind.services.memory_overview import MemoryOverviewService
from jarvismind.services.memory_overview_cache import MemoryOverviewCache


def _insert(manager, *, content: str, memory_type: str = "SEMANTIC") -> str:
    now = utc_now_iso()
    mid = uuid.uuid4().hex
    with manager.session() as session:
        session.add(
            MemoryRecord(
                id=mid,
                memory_type=memory_type,
                content=content,
                summary=None,
                importance=7.0,
                confidence=0.8,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
                expires_at=None,
                metadata_json=None,
            )
        )
        session.commit()
    return mid


def _forget(manager, memory_id: str) -> None:
    with manager.session() as session:
        record = session.get(MemoryRecord, memory_id)
        assert record is not None
        record.status = "DELETED"
        record.updated_at = utc_now_iso()
        session.commit()


def _category(payload, key):
    return next(item for item in payload["categories"] if item["key"] == key)


def _enricher_citing(cited_id: str):
    """A stand-in model that writes a summary and cites the given memory."""

    def enrich(payload):
        categories = []
        for category in payload["categories"]:
            ids = [item["memory_id"] for item in category["items"]]
            if cited_id in ids:
                categories.append({**category, "ai_summary": "用户喜欢跑步"})
            else:
                categories.append(category)
        return {
            **payload,
            "categories": categories,
            "ai_enhanced": True,
            "ai_model": "fake",
        }

    return enrich


def test_forgotten_memory_hides_the_cached_summary(manager):
    memory_id = _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_enricher_citing(memory_id), cache=cache)

    first = service.build(enrich=True)
    assert _category(first, "interests").get("ai_summary") == "用户喜欢跑步"

    _forget(manager, memory_id)

    after = service.build()
    assert after["ai_state"] == "stale"
    assert after["ai_enhanced"] is False
    # The old summary must be gone, not merely flagged.
    assert "ai_summary" not in _category(after, "interests")
    # And the forgotten memory must not be listed either.
    assert _category(after, "interests")["item_count"] == 0


def test_reorganising_after_forget_cannot_cite_the_forgotten_id(manager):
    memory_id = _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    seen_ids: list[list[str]] = []

    def recording(payload):
        ids = [
            item["memory_id"]
            for category in payload["categories"]
            for item in category["items"]
        ]
        seen_ids.append(ids)
        return _enricher_citing(memory_id)(payload) if memory_id in ids else payload

    service = MemoryOverviewService(manager, enricher=recording, cache=cache)
    service.build(enrich=True)

    _forget(manager, memory_id)
    service.build(enrich=True, refresh=True)

    # The second call must not have offered the forgotten id to the model.
    assert len(seen_ids) == 2
    assert memory_id not in seen_ids[1]


def test_forget_changes_the_source_version(manager):
    memory_id = _insert(manager, content="用户喜欢跑步")
    service = MemoryOverviewService(manager)
    before = service.build()["source_version"]

    _forget(manager, memory_id)

    after = service.build()["source_version"]
    assert before != after
