"""JkgInitializer: full historical backfill with resume, pause, isolation (JKG-INT-05 / A2a).

Every database is the shared ``manager`` fixture's temporary file. The P1 task
tables and the proposed JKG tables are installed by the real installers. No
network, no model, no production database.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.errors import ValidationError
from jarvismind.governance.projection import ReadOnlyProjection
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jkg.adapter import JkgCompositionAdapter, JkgDisabledError
from jarvismind.jkg.config import JkgConfig
from jarvismind.jkg.errors import (
    JkgInitConflictError,
    JkgInitP1MissingError,
    JkgInitStateError,
    JkgInitTaskNotFoundError,
)
from jarvismind.jkg.ingestor import IngestOutcome, JkgIngestor
from jarvismind.jkg.initializer import JkgInitializer

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, summary, importance, confidence, status, "
    "created_at, updated_at, expires_at, metadata_json"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, '', 5.0, 0.9, 'ACTIVE', "
    ":created, :created, NULL, NULL"
    ")"
)
_BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)
_ENABLED = JkgConfig.from_values(setting_value="1", env_value="")
_DISABLED = JkgConfig.from_values(setting_value="0", env_value="")


class FakeIngestor:
    """Wrap the real ingestor and inject outcomes or exceptions for chosen ids."""

    def __init__(self, inner, *, fail_ids=(), raise_ids=(), exc_text=""):
        self._inner = inner
        self._fail = set(fail_ids)
        self._raise = set(raise_ids)
        self._exc_text = exc_text

    def ingest_memory(self, memory_id, *, init_task_id=None):
        if memory_id in self._raise:
            raise RuntimeError(self._exc_text)
        if memory_id in self._fail:
            return IngestOutcome("failed", "FAKE_FAIL", 0, 0, 0, 0, 0)
        return self._inner.ingest_memory(memory_id, init_task_id=init_task_id)


@pytest.fixture()
def graph(manager):
    install_p1_schema(manager)
    create_jkg_schema(manager)
    return manager


@pytest.fixture()
def add_memories(graph):
    def make(contents):
        ids = []
        with graph.session() as session:
            for index, content in enumerate(contents):
                memory_id = uuid.uuid4().hex
                created = (_BASE + timedelta(seconds=index)).isoformat()
                session.execute(_INSERT, {"id": memory_id, "content": content, "created": created})
                ids.append(memory_id)
        return ids

    return make


def _initializer(manager, *, config=_ENABLED, wrap=None, finalizer=None):
    adapter = JkgCompositionAdapter(manager, config=config)
    ingestor = JkgIngestor(manager, adapter=adapter)
    if wrap is not None:
        ingestor = wrap(ingestor)
    return JkgInitializer(manager, adapter=adapter, ingestor=ingestor, finalizer=finalizer)


def _run_to_end(init, task_id, *, max_batches=20, limit=500):
    for _ in range(limit):
        payload = init.run_task(task_id, max_batches=max_batches)
        if payload["status"] != "RUNNING":
            return payload
    raise AssertionError("initialization did not finish")


def _counts(manager) -> dict:
    with manager.session() as session:
        def one(sql: str) -> int:
            return int(session.execute(text(sql)).scalar() or 0)

        return {
            "nodes": one("SELECT COUNT(*) FROM jkg_nodes"),
            "edges": one("SELECT COUNT(*) FROM jkg_edges"),
            "sources": one("SELECT COUNT(*) FROM jkg_edge_sources"),
        }


def _authority(manager) -> tuple:
    with manager.session() as session:
        def one(sql: str) -> int:
            return int(session.execute(text(sql)).scalar() or 0)

        return (
            ReadOnlyProjection(manager).content_digest(),
            one("SELECT COUNT(*) FROM memory_candidates"),
            one("SELECT COUNT(*) FROM memory_audit_logs"),
        )


def test_zero_memories_completes_and_calls_finalizer_once(graph):
    calls = []
    init = _initializer(graph, finalizer=calls.append)
    task = init.create_task(batch_size=10)
    assert task["status"] == "PENDING"

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["total"] == 0
    assert payload["progress"]["percent"] == 100
    assert calls == [task["task_id"]]


def test_single_extractable_memory_writes_graph(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。"])
    init = _initializer(graph)
    task = init.create_task(batch_size=10)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["processed"] == 1
    assert _counts(graph) == {"nodes": 2, "edges": 1, "sources": 1}


def test_hundred_memories_are_batched_without_duplicates(graph, add_memories):
    add_memories([f"张{i:03d} 负责 项目{i:03d}。" for i in range(100)])
    init = _initializer(graph)
    task = init.create_task(batch_size=10)

    runs = 0
    payload = None
    for _ in range(200):
        payload = init.run_task(task["task_id"], max_batches=1)
        runs += 1
        if payload["status"] != "RUNNING":
            break

    assert payload["status"] == "COMPLETED"
    assert runs > 1
    assert payload["progress"]["scanned"] == 100
    assert _counts(graph) == {"nodes": 200, "edges": 100, "sources": 100}


def test_thousand_candidate_free_memories_show_scan_progress(graph, add_memories):
    add_memories(["今天天气不错。"] * 1000)
    init = _initializer(graph)
    task = init.create_task(batch_size=100)

    payload = init.run_task(task["task_id"], max_batches=20)

    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["scanned"] == 1000
    assert payload["progress"]["skipped"] == 1000
    assert payload["failed_total"] == 0
    assert _counts(graph) == {"nodes": 0, "edges": 0, "sources": 0}


def test_breakpoint_resumes_from_cursor_without_duplicates(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(30)])
    init = _initializer(graph)
    task = init.create_task(batch_size=10)

    payload = init.run_task(task["task_id"], max_batches=1)
    assert payload["status"] == "RUNNING"
    assert payload["progress"]["scanned"] == 10
    assert payload["progress"]["percent"] == 33

    payload = _run_to_end(init, task["task_id"])
    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["scanned"] == 30
    assert _counts(graph) == {"nodes": 60, "edges": 30, "sources": 30}


def test_pause_then_resume_then_complete(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(30)])
    init = _initializer(graph)
    task = init.create_task(batch_size=10)
    init.run_task(task["task_id"], max_batches=1)

    assert init.pause_task(task["task_id"])["status"] == "PAUSED"
    assert init.pause_task(task["task_id"])["status"] == "PAUSED"
    with pytest.raises(JkgInitStateError):
        init.run_task(task["task_id"])

    assert init.resume_task(task["task_id"])["status"] == "RUNNING"
    payload = _run_to_end(init, task["task_id"])
    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["scanned"] == 30


def test_pause_takes_effect_at_the_next_memory(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(10)])
    seen = {"n": 0}
    holder = {}

    class PausingIngestor:
        def __init__(self, inner):
            self._inner = inner

        def ingest_memory(self, memory_id, *, init_task_id=None):
            seen["n"] += 1
            if seen["n"] == 3:
                holder["init"].pause_task(init_task_id)
            return self._inner.ingest_memory(memory_id, init_task_id=init_task_id)

    init = _initializer(graph, wrap=PausingIngestor)
    holder["init"] = init
    task = init.create_task(batch_size=10)

    payload = init.run_task(task["task_id"], max_batches=5)

    assert payload["status"] == "PAUSED"
    assert payload["progress"]["scanned"] == 3


def test_failed_memory_is_isolated_and_recorded_without_body(graph, add_memories):
    ids = add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(5)])
    init = _initializer(graph, wrap=lambda inner: FakeIngestor(inner, fail_ids=[ids[2]]))
    task = init.create_task(batch_size=2)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert payload["failed_total"] == 1
    assert _counts(graph)["sources"] == 4
    failures = init.failures(task["task_id"])
    assert failures["failures"] == [
        {"memory_id": ids[2], "error_type": "ingest_failed", "error_code": "FAKE_FAIL"}
    ]


def test_unexpected_exception_fails_task_with_redacted_payload(graph, add_memories):
    ids = add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(3)])
    secret = "SECRET-BODY-XYZ-123"
    init = _initializer(
        graph, wrap=lambda inner: FakeIngestor(inner, raise_ids=[ids[1]], exc_text=secret)
    )
    task = init.create_task(batch_size=10)

    payload = init.run_task(task["task_id"], max_batches=5)

    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "jkg_init_failed"
    assert secret not in json.dumps(payload, ensure_ascii=False)


def test_failed_task_retry_then_complete(graph, add_memories):
    ids = add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(4)])
    init = _initializer(
        graph, wrap=lambda inner: FakeIngestor(inner, raise_ids=[ids[1]], exc_text="x")
    )
    task = init.create_task(batch_size=10)
    assert init.run_task(task["task_id"], max_batches=5)["status"] == "FAILED"

    healthy = _initializer(graph)
    retried = healthy.retry_task(task["task_id"])
    assert retried["status"] == "PENDING"
    assert retried["retry_count"] == 1

    payload = _run_to_end(healthy, task["task_id"])
    assert payload["status"] == "COMPLETED"
    assert _counts(graph)["sources"] == 4


def test_retry_failed_items_creates_retry_task(graph, add_memories):
    ids = add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(3)])
    init = _initializer(graph, wrap=lambda inner: FakeIngestor(inner, fail_ids=[ids[0]]))
    source = init.create_task(batch_size=10)
    assert _run_to_end(init, source["task_id"])["failed_total"] == 1

    healthy = _initializer(graph)
    child = healthy.retry_failed_task(source["task_id"])
    assert child["mode"] == "retry"
    assert child["source_task_id"] == source["task_id"]
    assert child["progress"]["total"] == 1

    assert _run_to_end(healthy, child["task_id"])["status"] == "COMPLETED"
    with pytest.raises(JkgInitStateError):
        healthy.retry_failed_task(child["task_id"])


def test_two_full_runs_over_same_memories_are_idempotent(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(8)])
    init = _initializer(graph)
    _run_to_end(init, init.create_task(batch_size=3)["task_id"])
    first = _counts(graph)

    second = _run_to_end(init, init.create_task(batch_size=5)["task_id"])

    assert second["status"] == "COMPLETED"
    assert _counts(graph) == first
    assert second["progress"]["nodes_added"] == 0
    assert second["progress"]["edges_added"] == 0


def test_second_task_is_blocked_while_first_is_running(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(30)])
    init = _initializer(graph)
    first = init.create_task(batch_size=10)
    second = init.create_task(batch_size=10)
    init.run_task(first["task_id"], max_batches=1)

    with pytest.raises(JkgInitConflictError) as info:
        init.run_task(second["task_id"])
    assert info.value.code == "jkg_init_conflict"

    _run_to_end(init, first["task_id"])
    assert _run_to_end(init, second["task_id"])["status"] == "COMPLETED"


def test_create_without_p1_tables_is_rejected(manager):
    create_jkg_schema(manager)
    init = _initializer(manager)
    with pytest.raises(JkgInitP1MissingError) as info:
        init.create_task(batch_size=10)
    assert info.value.code == "p1_schema_missing"
    assert "p1_tasks" in info.value.detail["missing"]


def test_authority_tables_are_untouched(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。", "李四 属于 JarvisMind团队。"])
    before = _authority(graph)
    init = _initializer(graph)
    _run_to_end(init, init.create_task(batch_size=10)["task_id"])

    assert _authority(graph) == before


def test_cancel_is_terminal_and_idempotent(graph, add_memories):
    add_memories([f"张{i:02d} 负责 项目{i:02d}。" for i in range(10)])
    init = _initializer(graph)
    task = init.create_task(batch_size=5)

    cancelled = init.cancel_task(task["task_id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["finished_at"]
    assert init.cancel_task(task["task_id"])["status"] == "CANCELLED"
    with pytest.raises(JkgInitStateError):
        init.run_task(task["task_id"])
    with pytest.raises(JkgInitStateError):
        init.resume_task(task["task_id"])


def test_list_get_and_validation(graph):
    init = _initializer(graph)
    init.create_task(batch_size=10)
    init.create_task(batch_size=20)

    listed = init.list_tasks(limit=10)
    assert listed["total"] == 2
    assert len(listed["data"]) == 2
    assert init.list_tasks(status="PENDING")["total"] == 2
    assert init.list_tasks(status="COMPLETED")["total"] == 0

    with pytest.raises(JkgInitTaskNotFoundError):
        init.get_task("jkg-init-missing")
    for bad in (0, 101, True):
        with pytest.raises(ValidationError):
            init.create_task(batch_size=bad)
    with pytest.raises(ValidationError):
        init.create_task(mode="retry", retry_memory_ids=["x"])


def test_preview_rejects_disabled_graph(graph):
    init = _initializer(graph, config=_DISABLED)
    with pytest.raises(JkgDisabledError):
        init.preview()


def test_preview_reports_eligible_count_and_empty_graph(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。", "今天天气不错。"])
    preview = _initializer(graph).preview()

    assert preview["eligible_memories"] == 2
    assert preview["attributes_supported"] is False
    assert preview["p1_available"] is True
    assert preview["graph"] == {"nodes": 0, "edges": 0, "sources": 0}
