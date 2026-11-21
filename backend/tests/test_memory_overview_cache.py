"""Cache behaviour of the memory overview (JME-MEMORY-OVERVIEW-01).

The cache exists so re-opening the page does not spend money. These tests pin
the three states the API exposes: ``none`` / ``current`` / ``stale``.
"""
from __future__ import annotations

import uuid

import pytest

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.models import MemoryRecord
from jarvismind.services.memory_overview import MemoryOverviewService
from jarvismind.services.memory_overview_cache import CachedOverview, MemoryOverviewCache


def _insert(
    manager,
    *,
    content: str,
    memory_type: str = "SEMANTIC",
    status: str = "ACTIVE",
    importance: float = 7.0,
) -> str:
    """Seed a row directly. The overview is a read-only consumer."""
    now = utc_now_iso()
    mid = uuid.uuid4().hex
    with manager.session() as session:
        session.add(
            MemoryRecord(
                id=mid,
                memory_type=memory_type,
                content=content,
                summary=None,
                importance=importance,
                confidence=0.8,
                status=status,
                created_at=now,
                updated_at=now,
                expires_at=None,
                metadata_json=None,
            )
        )
        session.commit()
    return mid


def _set_status(manager, memory_id: str, status: str) -> None:
    with manager.session() as session:
        record = session.get(MemoryRecord, memory_id)
        assert record is not None
        record.status = status
        record.updated_at = utc_now_iso()
        session.commit()


def _category(payload, key):
    return next(item for item in payload["categories"] if item["key"] == key)


def _decorator(model: str = "fake-model"):
    """Stand-in for the model: writes ai_summary for every non-empty category."""

    def enrich(payload):
        categories = [
            {**category, "ai_summary": f"模型写的{category['title']}"}
            if category["items"]
            else category
            for category in payload["categories"]
        ]
        return {
            **payload,
            "categories": categories,
            "ai_enhanced": True,
            "ai_model": model,
        }

    return enrich


def test_no_enricher_means_deterministic_only(manager):
    _insert(manager, content="用户喜欢跑步")
    payload = MemoryOverviewService(manager).build()
    assert payload["ai_state"] == "none"
    assert payload["ai_enhanced"] is False
    assert _category(payload, "interests")["available"] is True


def test_enrich_populates_the_cache(manager):
    _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_decorator(), cache=cache)

    payload = service.build(enrich=True)
    assert payload["ai_state"] == "current"
    assert payload["ai_enhanced"] is True
    assert payload["ai_model"] == "fake-model"
    assert payload["ai_generated_at"] is not None

    entry = cache.get()
    assert entry is not None
    assert entry.source_version == payload["source_version"]
    assert "interests" in entry.summaries


def test_cached_summaries_are_reused_without_a_model_call(manager):
    _insert(manager, content="用户喜欢跑步")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        return _decorator()(payload)

    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=counting, cache=cache)

    first = service.build(enrich=True)
    second = service.build()  # plain refresh, no enrich

    assert calls["n"] == 1, "a cached overview must not call the model again"
    assert second["ai_state"] == "current"
    assert (
        _category(second, "interests")["ai_summary"]
        == _category(first, "interests")["ai_summary"]
    )


def test_new_memory_marks_the_cache_stale(manager):
    _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_decorator(), cache=cache)
    service.build(enrich=True)

    _insert(manager, content="用户喜欢爬山")

    payload = service.build()
    assert payload["ai_state"] == "stale"
    assert payload["ai_enhanced"] is False
    # R2/B-2：记忆已变（含遗忘）时旧摘要必须隐藏，不能继续展示旧事实。
    assert "ai_summary" not in _category(payload, "interests")


def test_refresh_regenerates_after_a_change(manager):
    _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_decorator(), cache=cache)
    service.build(enrich=True)

    _insert(manager, content="用户喜欢爬山")

    refreshed = service.build(enrich=True, refresh=True)
    assert refreshed["ai_state"] == "current"
    assert refreshed["ai_enhanced"] is True
    entry = cache.get()
    assert entry is not None and entry.source_version == refreshed["source_version"]


def test_refresh_forces_a_call_even_when_current(manager):
    _insert(manager, content="用户喜欢跑步")
    calls = {"n": 0}

    def counting(payload):
        calls["n"] += 1
        return _decorator()(payload)

    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=counting, cache=cache)

    service.build(enrich=True)
    service.build(enrich=True)  # current -> no call
    assert calls["n"] == 1

    service.build(enrich=True, refresh=True)
    assert calls["n"] == 2


def test_failing_enricher_does_not_poison_the_cache(manager):
    _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()

    def boom(_payload):
        raise RuntimeError("synthetic failure")

    service = MemoryOverviewService(manager, enricher=boom, cache=cache)
    payload = service.build(enrich=True)

    assert payload["ai_state"] == "none"
    assert payload["ai_enhanced"] is False
    assert cache.get() is None


def test_forgotten_memory_invalidates_the_cache(manager):
    memory_id = _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_decorator(), cache=cache)
    first = service.build(enrich=True)
    assert _category(first, "interests")["ai_summary"] is not None

    _set_status(manager, memory_id, "DELETED")

    payload = service.build()
    assert payload["ai_state"] == "stale"
    # R2/B-2：遗忘后旧摘要不得再展示。
    for category in payload["categories"]:
        assert category.get("ai_summary") is None


def test_cache_entry_is_immutable():
    entry = CachedOverview(
        source_version="v1",
        generated_at="2026-11-19T00:00:00Z",
        model="m",
        summaries={},
    )
    assert entry.is_current("v1") is True
    assert entry.is_current("v2") is False
    with pytest.raises(Exception):
        entry.source_version = "v2"  # frozen dataclass


def test_cache_clear_forgets_everything(manager):
    _insert(manager, content="用户喜欢跑步")
    cache = MemoryOverviewCache()
    service = MemoryOverviewService(manager, enricher=_decorator(), cache=cache)
    service.build(enrich=True)
    assert cache.get() is not None

    cache.clear()
    assert cache.get() is None
    assert service.build()["ai_state"] == "none"
