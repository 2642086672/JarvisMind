"""JKG-INT-05 / A2b: the initializer's finalize hands over to the lifecycle.

A full backfill scans memories up to a snapshot. Anything written after the snapshot
(creations, edits, forgets) is caught by replaying the audit window from the
snapshot, once the scan is exhausted. These tests write those events *during* the
scan, through a hook that runs between two scan batches, so the replay is what has
to find them.

Every database is the shared ``manager`` fixture's temporary file.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.container import Container
from jarvismind.core.errors import ValidationError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg.config import JkgConfig
from jarvismind.jkg.ingestor import IngestOutcome, JkgIngestor
from jarvismind.jkg.initializer import JkgInitializer
from jarvismind.jkg.lifecycle import CURSOR_KEY, JkgLifecycleSync
from jarvismind.jkg.schema import JKG_ENABLED_KEY, present_jkg_tables

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, summary, importance, confidence, status, "
    "created_at, updated_at, expires_at, metadata_json"
    ") VALUES (:id, 'SEMANTIC', :content, '', 5.0, 0.9, :status, :created, :created, NULL, NULL)"
)
_SET_STATUS = text(
    "UPDATE memory_records SET status = :status, updated_at = :now WHERE id = :id"
)
_SET_CONTENT = text(
    "UPDATE memory_records SET content = :content, updated_at = :now WHERE id = :id"
)
_INSERT_AUDIT = text(
    "INSERT INTO memory_audit_logs(id, memory_id, action, actor, detail_json, created_at) "
    "VALUES (:id, :memory_id, :action, 'system', NULL, :created_at)"
)
_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)
_READ_SETTING = text("SELECT value FROM system_settings WHERE key = :key")
_BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)
_ENABLED = JkgConfig.from_values(setting_value="1", env_value="")
_FINALIZE_CODE = "jkg_init_finalize_failed"


class _ScanHook:
    """Stands in for the scan's ingestor and runs ``on_scan`` before each memory."""

    def __init__(self, inner, *, on_scan=None):
        self._inner = inner
        self._on_scan = on_scan
        self.calls = 0

    def ingest_memory(self, memory_id, *, init_task_id=None):
        self.calls += 1
        if self._on_scan is not None:
            self._on_scan(self.calls, memory_id)
        return self._inner.ingest_memory(memory_id, init_task_id=init_task_id)


@pytest.fixture()
def graph(manager):
    install_p1_schema(manager)
    create_jkg_schema(manager)
    _set_enabled(manager, "1")
    return manager


@pytest.fixture()
def add_memories(graph):
    def make(contents):
        ids = []
        for index, content in enumerate(contents):
            created = (_BASE + timedelta(seconds=index)).isoformat()
            ids.append(_insert_memory(graph, content, created=created))
        return ids

    return make


def _initializer(manager, *, on_scan=None, lifecycle_ingest=None, finalizer=None):
    adapter = JkgCompositionAdapter(manager, config=_ENABLED)
    real = JkgIngestor(manager, adapter=adapter)
    lifecycle = JkgLifecycleSync(
        manager, adapter=adapter, ingest=lifecycle_ingest or real.ingest_memory
    )
    init = JkgInitializer(
        manager,
        adapter=adapter,
        ingestor=_ScanHook(real, on_scan=on_scan),
        lifecycle=lifecycle,
        finalizer=finalizer,
    )
    return init, lifecycle


def _run_to_end(init, task_id, *, max_batches=20, limit=500):
    for _ in range(limit):
        payload = init.run_task(task_id, max_batches=max_batches)
        if payload["status"] != "RUNNING":
            return payload
    raise AssertionError("initialization did not finish")


def _set_enabled(manager, value: str) -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _set_cursor(manager, *, created_at: str, entry_id: str) -> None:
    value = json.dumps({"version": 1, "created_at": created_at, "id": entry_id})
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": CURSOR_KEY, "value": value, "now": utc_now_iso()},
        )


def _get_setting(manager, key: str):
    with manager.session() as session:
        return session.execute(_READ_SETTING, {"key": key}).scalar()


def _insert_memory(manager, content: str, *, created: str = "", status: str = "ACTIVE") -> str:
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "content": content,
                "status": status,
                "created": created or utc_now_iso(),
            },
        )
    return memory_id


def _update_content(manager, memory_id: str, content: str) -> None:
    with manager.session() as session:
        session.execute(
            _SET_CONTENT, {"id": memory_id, "content": content, "now": utc_now_iso()}
        )


def _update_status(manager, memory_id: str, status: str) -> None:
    with manager.session() as session:
        session.execute(
            _SET_STATUS, {"id": memory_id, "status": status, "now": utc_now_iso()}
        )


def _audit(manager, memory_id: str, action: str) -> str:
    audit_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_AUDIT,
            {
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "created_at": utc_now_iso(),
            },
        )
    return audit_id


def _edges_of(manager, memory_id: str) -> dict:
    with manager.session() as session:
        rows = session.execute(
            text(
                "SELECT e.edge_id, e.status FROM jkg_edges e "
                "JOIN jkg_edge_sources s ON s.edge_id = e.edge_id "
                "WHERE s.memory_id = :memory_id ORDER BY e.edge_id"
            ),
            {"memory_id": memory_id},
        ).all()
    return {str(row[0]): str(row[1]) for row in rows}


def _counts(manager) -> dict:
    with manager.session() as session:
        return {
            table: int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            for table in ("jkg_nodes", "jkg_edges", "jkg_edge_sources")
        }


# ===========================================================================
# A · writes during the scan are caught up by the handover
# ===========================================================================
def test_a_memory_created_during_the_scan_is_extracted_at_completion(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。", "李四 负责 JarvisMind。"])
    late = {}

    def on_scan(call, _memory_id):
        if call == 1:
            late["id"] = _insert_memory(graph, "王五 负责 JarvisMind。")
            _audit(graph, late["id"], "CREATE")

    init, _ = _initializer(graph, on_scan=on_scan)
    task = init.create_task(batch_size=1)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert payload["progress"]["total"] == 2
    assert len(_edges_of(graph, late["id"])) == 1
    assert list(_edges_of(graph, late["id"]).values()) == ["ACTIVE"]
    assert _counts(graph)["jkg_edges"] == 3


def test_an_update_during_the_scan_retires_the_fact_the_scan_already_wrote(graph, add_memories):
    first, _second = add_memories(["张三 负责 JarvisMind。", "李四 负责 JarvisMind。"])
    seen = {}

    def on_scan(call, _memory_id):
        if call == 2:
            (seen["old_edge"],) = _edges_of(graph, first).keys()
            _update_content(graph, first, "王五 负责 JarvisMind。")
            _audit(graph, first, "UPDATE")

    init, _ = _initializer(graph, on_scan=on_scan)
    task = init.create_task(batch_size=1)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    edges = _edges_of(graph, first)
    assert edges[seen["old_edge"]] == "INVALID"
    new_edges = [edge for edge in edges if edge != seen["old_edge"]]
    assert [edges[edge] for edge in new_edges] == ["ACTIVE"]


def test_a_forget_during_the_scan_stays_invalid_and_the_cursor_reaches_the_end(graph, add_memories):
    first, _second = add_memories(["张三 负责 JarvisMind。", "李四 负责 JarvisMind。"])
    seen = {}

    def on_scan(call, _memory_id):
        if call == 2:
            _update_status(graph, first, "DELETED")
            seen["forget_audit"] = _audit(graph, first, "FORGET_APPROVED")

    init, _ = _initializer(graph, on_scan=on_scan)
    task = init.create_task(batch_size=1)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert list(_edges_of(graph, first).values()) == ["INVALID"]
    assert json.loads(_get_setting(graph, CURSOR_KEY))["id"] == seen["forget_audit"]


def test_a_cursor_ahead_of_the_start_is_rewound_and_the_window_is_replayed(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。"])
    init, _ = _initializer(graph)
    task = init.create_task(batch_size=10)
    late = _insert_memory(graph, "王五 负责 JarvisMind。")
    _audit(graph, late, "CREATE")
    _set_cursor(graph, created_at="2099-01-01T00:00:00+00:00", entry_id="ffffffff")

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert list(_edges_of(graph, late).values()) == ["ACTIVE"]
    stored = json.loads(_get_setting(graph, CURSOR_KEY))
    assert not stored["created_at"].startswith("2099")


def test_an_empty_graph_completes_with_the_handover(graph):
    init, _ = _initializer(graph)
    task = init.create_task(batch_size=10)

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert _counts(graph) == {"jkg_nodes": 0, "jkg_edges": 0, "jkg_edge_sources": 0}


# ===========================================================================
# B · failure of the handover ends the task; a retry completes it
# ===========================================================================
def test_a_failed_handover_fails_the_task_and_a_retry_completes_it(
    graph, add_memories, monkeypatch
):
    add_memories(["张三 负责 JarvisMind。"])
    init, lifecycle = _initializer(graph)
    task = init.create_task(batch_size=10)

    def boom(**_kwargs):
        raise RuntimeError("secret body SECRET-HANDOVER-XYZ")

    monkeypatch.setattr(lifecycle, "reconcile", boom)
    payload = init.run_task(task["task_id"], max_batches=5)

    assert payload["status"] == "FAILED"
    assert payload["error_code"] == _FINALIZE_CODE
    assert "SECRET-HANDOVER" not in json.dumps(payload, ensure_ascii=False)

    monkeypatch.undo()
    retried = init.retry_task(task["task_id"])
    assert retried["status"] == "PENDING"
    payload = _run_to_end(init, task["task_id"])
    assert payload["status"] == "COMPLETED"
    assert _counts(graph)["jkg_edges"] == 1


def test_a_switch_turned_off_during_the_scan_fails_the_handover(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。", "李四 负责 JarvisMind。"])

    def on_scan(call, _memory_id):
        if call == 2:
            _set_enabled(graph, "0")

    init, _ = _initializer(graph, on_scan=on_scan)
    task = init.create_task(batch_size=1)

    payload = init.run_task(task["task_id"], max_batches=20)

    assert payload["status"] == "FAILED"
    assert payload["error_code"] == _FINALIZE_CODE


def test_a_memory_that_fails_in_the_replay_is_recorded_and_can_be_retried(graph, add_memories):
    add_memories(["张三 负责 JarvisMind。"])
    late = {}
    real = JkgIngestor(graph, adapter=JkgCompositionAdapter(graph, config=_ENABLED))

    def lifecycle_ingest(memory_id):
        if memory_id == late.get("id"):
            return IngestOutcome("failed", "REPLAY_BROKEN", 0, 0, 0, 0, 0)
        return real.ingest_memory(memory_id)

    init, _ = _initializer(graph, lifecycle_ingest=lifecycle_ingest)
    task = init.create_task(batch_size=10)
    late["id"] = _insert_memory(graph, "王五 负责 JarvisMind。")
    _audit(graph, late["id"], "CREATE")

    payload = _run_to_end(init, task["task_id"])

    assert payload["status"] == "COMPLETED"
    assert payload["failed_total"] == 1
    assert init.failures(task["task_id"])["failures"] == [
        {
            "memory_id": late["id"],
            "error_type": "ingest_failed",
            "error_code": "REPLAY_BROKEN",
        }
    ]

    child = init.retry_failed_task(task["task_id"])
    assert child["mode"] == "retry"
    assert _run_to_end(init, child["task_id"])["status"] == "COMPLETED"
    assert list(_edges_of(graph, late["id"]).values()) == ["ACTIVE"]


# ===========================================================================
# C · reset_cursor_for_init on its own
# ===========================================================================
def test_reset_cursor_for_init_rewinds_even_when_the_cursor_is_ahead(graph):
    _set_cursor(graph, created_at="2099-01-01T00:00:00+00:00", entry_id="ff")
    _, lifecycle = _initializer(graph)

    result = lifecycle.reset_cursor_for_init("2026-05-01T00:00:00+00:00")

    assert result["updated"] is True
    assert result["state"] == "ready"
    assert result["cursor_before"]["created_at"].startswith("2099")
    assert result["cursor_after"] == {"created_at": "2026-05-01T00:00:00+00:00", "id": ""}
    assert json.loads(_get_setting(graph, CURSOR_KEY)) == {
        "version": 1,
        "created_at": "2026-05-01T00:00:00+00:00",
        "id": "",
    }


def test_reset_cursor_for_init_rejects_an_empty_start(graph):
    _, lifecycle = _initializer(graph)
    with pytest.raises(ValidationError):
        lifecycle.reset_cursor_for_init("   ")


def test_reset_cursor_for_init_does_nothing_when_the_graph_is_off(graph):
    _set_cursor(graph, created_at="2099-01-01T00:00:00+00:00", entry_id="ff")
    _set_enabled(graph, "0")

    result = JkgLifecycleSync(graph).reset_cursor_for_init("2026-05-01T00:00:00+00:00")

    assert result == {"updated": False, "state": "disabled"}
    assert json.loads(_get_setting(graph, CURSOR_KEY))["created_at"].startswith("2099")


# ===========================================================================
# D · container wiring (construction only: nothing is installed or started)
# ===========================================================================
def test_container_builds_the_initializer_without_installing_anything(manager, settings):
    container = Container(settings=settings, manager=manager, logger=None)

    init = container.jkg_initializer

    assert isinstance(init, JkgInitializer)
    assert init._lifecycle is container.jkg_lifecycle
    assert init._ingestor is container.jkg_ingestor
    with manager.session() as session:
        assert present_jkg_tables(session) == []
    assert _get_setting(manager, CURSOR_KEY) is None


def test_the_container_exact_tokens_come_from_the_api_token(manager, settings):
    container = Container(settings=settings, manager=manager, logger=None)
    expected = [settings.api_token] if settings.api_token else []

    assert container._jkg_exact_tokens() == expected
