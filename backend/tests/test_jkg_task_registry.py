"""TaskRegistry.update_metadata (JKG-INT-05 / A2a).

The method only merges metadata and optionally appends an event. It never
changes the status, so every case here also checks that state stays put.
"""
from __future__ import annotations

import pytest

from jarvismind.governance.errors import TaskStateError
from jarvismind.governance.schema import install_p1_schema
from jarvismind.governance.task_registry import CREATED, TaskRegistry


@pytest.fixture()
def registry(manager):
    install_p1_schema(manager)
    return TaskRegistry(manager)


def test_update_metadata_merges_into_existing_keys(registry):
    registry.create("t-meta-1", "JKG_INIT", metadata={"a": 1, "b": 1})

    out = registry.update_metadata("t-meta-1", {"b": 2, "c": 3})

    assert out["metadata"] == {"a": 1, "b": 2, "c": 3}
    assert out["status"] == CREATED
    assert registry.get("t-meta-1")["metadata"] == {"a": 1, "b": 2, "c": 3}


def test_second_update_keeps_keys_from_the_first(registry):
    registry.create("t-meta-2", "JKG_INIT", metadata={"seed": True})
    registry.update_metadata("t-meta-2", {"first": 1})
    registry.update_metadata("t-meta-2", {"second": 2})

    assert registry.get("t-meta-2")["metadata"] == {"seed": True, "first": 1, "second": 2}


def test_update_metadata_unknown_task_raises(registry):
    with pytest.raises(TaskStateError):
        registry.update_metadata("no-such-task", {"a": 1})


def test_update_metadata_appends_event_only_when_asked(registry):
    registry.create("t-meta-3", "JKG_INIT")

    registry.update_metadata("t-meta-3", {"x": 1})
    assert [e["event_type"] for e in registry.events("t-meta-3")] == ["CREATE"]

    registry.update_metadata("t-meta-3", {"y": 2}, event_type="NOTE", detail={"k": "v"})
    events = registry.events("t-meta-3")
    assert [e["event_type"] for e in events] == ["CREATE", "NOTE"]
    assert events[-1]["detail"] == {"k": "v"}
    assert registry.get("t-meta-3")["status"] == CREATED
