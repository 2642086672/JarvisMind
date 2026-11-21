"""JKG-INT-04 / A2b: lifecycle extraction through the injected ingestor.

The lifecycle decides which memories need an extraction and when (after the
stale pass). The ingestor decides what an extraction yields. These tests run the
two together over the real write path, so every graph row here was written by
``JkgIngestor.ingest_memory``; none is hand-built.

Every database is the shared ``manager`` fixture's temporary file.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.container import Container
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg.ingestor import IngestOutcome, JkgIngestor
from jarvismind.jkg.lifecycle import CURSOR_KEY, STALE_REASON, JkgLifecycleSync
from jarvismind.jkg.schema import JKG_ENABLED_KEY

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, summary, importance, confidence, status, "
    "created_at, updated_at, expires_at, metadata_json"
    ") VALUES (:id, 'SEMANTIC', :content, '', 5.0, 0.9, :status, :now, :now, NULL, NULL)"
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
_GRAPH_TABLES = ("jkg_nodes", "jkg_edges", "jkg_edge_sources")
_TICK_TOTALS = (
    "ingested_processed",
    "ingested_skipped",
    "ingested_failed",
    "governance_rejected",
)


class _Spy:
    """Wrap an ingest callable and remember every memory it was asked to extract."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list = []

    def __call__(self, memory_id):
        self.calls.append(memory_id)
        return self._inner(memory_id)


@pytest.fixture()
def graph(manager):
    create_jkg_schema(manager)
    _set_enabled(manager, "1")
    return manager


@pytest.fixture()
def ingestor(graph):
    return JkgIngestor(graph, adapter=JkgCompositionAdapter(graph))


@pytest.fixture()
def spy(ingestor):
    return _Spy(ingestor.ingest_memory)


@pytest.fixture()
def sync(graph, spy):
    return JkgLifecycleSync(graph, ingest=spy)


def _set_enabled(manager, value: str) -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _get_setting(manager, key: str):
    with manager.session() as session:
        return session.execute(_READ_SETTING, {"key": key}).scalar()


def _insert_memory(manager, content: str, *, status: str = "ACTIVE") -> str:
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {"id": memory_id, "content": content, "status": status, "now": utc_now_iso()},
        )
    return memory_id


def _update_memory(manager, memory_id: str, *, content=None, status=None) -> None:
    with manager.session() as session:
        if content is not None:
            session.execute(
                _SET_CONTENT,
                {"id": memory_id, "content": content, "now": utc_now_iso()},
            )
        if status is not None:
            session.execute(
                _SET_STATUS,
                {"id": memory_id, "status": status, "now": utc_now_iso()},
            )


def _audit(manager, memory_id: str, action: str, *, created_at: str = "") -> str:
    audit_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_AUDIT,
            {
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "created_at": created_at or utc_now_iso(),
            },
        )
    return audit_id


def _counts(manager) -> dict:
    with manager.session() as session:
        return {
            table: int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
            for table in _GRAPH_TABLES
        }


_ZERO_COUNTS = {"jkg_nodes": 0, "jkg_edges": 0, "jkg_edge_sources": 0}


def _edges_of(manager, memory_id: str) -> dict:
    """``{edge_id: status}`` for every edge holding a source row for the memory."""
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


def _sources_of(manager, memory_id: str) -> dict:
    """``{edge_id: (invalidated_at, invalidation_reason)}`` for the memory's sources."""
    with manager.session() as session:
        rows = session.execute(
            text(
                "SELECT edge_id, invalidated_at, invalidation_reason "
                "FROM jkg_edge_sources WHERE memory_id = :memory_id"
            ),
            {"memory_id": memory_id},
        ).all()
    return {str(row[0]): (row[1], row[2]) for row in rows}


# ===========================================================================
# A · extraction on creation
# ===========================================================================
def test_create_is_extracted_into_the_graph(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")

    result = sync.tick()

    assert result["ok"] is True
    assert result["ingested_processed"] == 1
    assert _counts(graph) == {"jkg_nodes": 2, "jkg_edges": 1, "jkg_edge_sources": 1}
    assert list(_edges_of(graph, memory_id).values()) == ["ACTIVE"]


def test_create_with_no_relation_is_counted_as_skipped_not_governance(graph, sync):
    memory_id = _insert_memory(graph, "今天天气不错。")
    _audit(graph, memory_id, "CREATE")

    result = sync.tick()

    assert result["ok"] is True
    assert result["ingested_skipped"] == 1
    assert result["ingested_processed"] == 0
    assert result["governance_rejected"] == 0
    assert _counts(graph) == _ZERO_COUNTS


def test_without_an_ingest_the_tick_writes_no_graph_rows(graph):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")

    result = JkgLifecycleSync(graph).tick()

    assert _counts(graph) == _ZERO_COUNTS
    for key in _TICK_TOTALS:
        assert result[key] == 0, key


def test_an_archive_only_memory_is_not_extracted(graph, sync, spy):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。", status="ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")

    sync.tick()

    assert spy.calls == []
    assert _counts(graph) == _ZERO_COUNTS


# ===========================================================================
# B · UPDATE: the same fact is re-confirmed, a different fact retires the old one
# ===========================================================================
def test_update_with_the_same_fact_reactivates_the_edge(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, memory_id).keys()

    _audit(graph, memory_id, "UPDATE")
    result = sync.tick()

    assert result["marked_stale"] == 1
    assert result["ingested_processed"] == 1
    assert _edges_of(graph, memory_id) == {edge_id: "ACTIVE"}
    invalidated_at, reason = _sources_of(graph, memory_id)[edge_id]
    assert invalidated_at is None
    assert reason is None


def test_update_to_a_different_fact_retires_the_old_edge_and_adds_the_new(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (old_edge,) = _edges_of(graph, memory_id).keys()

    _update_memory(graph, memory_id, content="李四 负责 JarvisMind。")
    _audit(graph, memory_id, "UPDATE")
    sync.tick()

    edges = _edges_of(graph, memory_id)
    assert edges[old_edge] == "INVALID"
    new_edges = [edge for edge in edges if edge != old_edge]
    assert len(new_edges) == 1
    assert edges[new_edges[0]] == "ACTIVE"
    sources = _sources_of(graph, memory_id)
    assert sources[old_edge][1] == STALE_REASON
    assert sources[new_edges[0]][0] is None


def test_update_that_no_longer_states_a_relation_leaves_the_old_edge_invalid(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (old_edge,) = _edges_of(graph, memory_id).keys()

    _update_memory(graph, memory_id, content="今天天气不错。")
    _audit(graph, memory_id, "UPDATE")
    result = sync.tick()

    assert result["ingested_skipped"] == 1
    assert _edges_of(graph, memory_id) == {old_edge: "INVALID"}
    assert _counts(graph)["jkg_edges"] == 1


# ===========================================================================
# C · forget, soft delete, expiry and merge: never extracted, never revived
# ===========================================================================
def test_forgotten_memory_is_not_extracted_and_its_edge_stays_invalid(graph, sync, spy):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, memory_id).keys()
    spy.calls.clear()

    _update_memory(graph, memory_id, status="DELETED")
    _audit(graph, memory_id, "FORGET_APPROVED")
    sync.tick()
    assert spy.calls == []
    assert _edges_of(graph, memory_id) == {edge_id: "INVALID"}

    _audit(graph, memory_id, "CREATE")
    sync.tick()
    assert spy.calls == []
    assert _edges_of(graph, memory_id) == {edge_id: "INVALID"}


@pytest.mark.parametrize("action", ["SOFT_DELETE", "AUTO_EXPIRE"])
def test_soft_delete_or_expiry_invalidates_and_is_not_extracted(graph, sync, spy, action):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, memory_id).keys()
    spy.calls.clear()

    _update_memory(graph, memory_id, status="DELETED")
    _audit(graph, memory_id, action)
    sync.tick()

    assert spy.calls == []
    assert _edges_of(graph, memory_id) == {edge_id: "INVALID"}


def test_merged_absorbed_memory_is_not_extracted_and_its_edge_is_invalid(graph, sync, spy):
    absorbed_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, absorbed_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, absorbed_id).keys()
    spy.calls.clear()

    _update_memory(graph, absorbed_id, status="MERGED")
    _audit(graph, absorbed_id, "MERGE_SOURCE")
    sync.tick()

    assert spy.calls == []
    assert _edges_of(graph, absorbed_id) == {edge_id: "INVALID"}


# ===========================================================================
# D · archive keeps knowledge valid; restore does not duplicate
# ===========================================================================
def test_archive_keeps_the_edge_valid(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, memory_id).keys()

    _update_memory(graph, memory_id, status="ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    sync.tick()

    assert _edges_of(graph, memory_id) == {edge_id: "ACTIVE"}
    assert _sources_of(graph, memory_id)[edge_id] == (None, None)


def test_restore_from_archive_does_not_duplicate_graph_rows(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    before = _counts(graph)

    _update_memory(graph, memory_id, status="ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    sync.tick()
    _update_memory(graph, memory_id, status="ACTIVE")
    _audit(graph, memory_id, "RESTORE")
    result = sync.tick()

    assert result["ok"] is True
    assert _counts(graph) == before
    assert list(_edges_of(graph, memory_id).values()) == ["ACTIVE"]


def test_restore_after_a_soft_delete_brings_the_edge_back(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    sync.tick()
    (edge_id,) = _edges_of(graph, memory_id).keys()

    _update_memory(graph, memory_id, status="DELETED")
    _audit(graph, memory_id, "SOFT_DELETE")
    sync.tick()
    assert _edges_of(graph, memory_id) == {edge_id: "INVALID"}

    _update_memory(graph, memory_id, status="ACTIVE")
    _audit(graph, memory_id, "RESTORE")
    sync.tick()

    assert _edges_of(graph, memory_id) == {edge_id: "ACTIVE"}
    assert _sources_of(graph, memory_id)[edge_id] == (None, None)


# ===========================================================================
# E · failure isolation and counters
# ===========================================================================
def test_a_raising_ingest_is_recorded_without_its_text_and_the_batch_goes_on(graph, ingestor):
    bad_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    good_id = _insert_memory(graph, "王五 负责 JarvisMind。")
    _audit(graph, bad_id, "CREATE", created_at="2026-01-01T00:00:00")
    good_audit = _audit(graph, good_id, "CREATE", created_at="2026-01-01T00:00:01")
    marker = "SECRET-BODY-" + "INGEST-A2B"

    class _Flaky:
        def __call__(self, memory_id):
            if memory_id == bad_id:
                raise RuntimeError("secret body " + marker)
            return ingestor.ingest_memory(memory_id)

    result = JkgLifecycleSync(graph, ingest=_Flaky()).tick()

    assert result["ok"] is False
    assert result["ingested_processed"] == 1
    assert result["ingested_failed"] == 1
    assert result["errors"] == [
        {
            "memory_id": bad_id,
            "error_type": "ingest_failed",
            "error_code": "JKG_INGEST_FAILED",
        }
    ]
    assert marker not in json.dumps(result, ensure_ascii=False, default=str)
    assert json.loads(_get_setting(graph, CURSOR_KEY))["id"] == good_audit


def test_a_failed_outcome_is_recorded_with_its_safe_reason(graph):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    failing = lambda _id: IngestOutcome("failed", "EXACT_TOKENS_UNAVAILABLE", 0, 0, 0, 0, 0)

    result = JkgLifecycleSync(graph, ingest=failing).tick()

    assert result["ok"] is False
    assert result["ingested_failed"] == 1
    assert result["errors"] == [
        {
            "memory_id": memory_id,
            "error_type": "ingest_failed",
            "error_code": "EXACT_TOKENS_UNAVAILABLE",
        }
    ]


def test_a_governance_refusal_is_counted_separately_from_a_plain_skip(graph):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "CREATE")
    refused = lambda _id: IngestOutcome("skipped", "PRIVACY_NOT_ALLOWED", 0, 0, 0, 0, 0)

    result = JkgLifecycleSync(graph, ingest=refused).tick()

    assert result["ok"] is True
    assert result["ingested_skipped"] == 1
    assert result["governance_rejected"] == 1
    assert result["errors"] == []


def test_a_memory_deleted_before_its_tick_is_not_extracted_even_with_a_create(graph, sync, spy):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。", status="DELETED")
    _audit(graph, memory_id, "CREATE")

    result = sync.tick()

    assert spy.calls == []
    assert result["ingested_processed"] == 0
    assert _counts(graph) == _ZERO_COUNTS


# ===========================================================================
# F · container wiring
# ===========================================================================
def test_container_lifecycle_extracts_through_the_container_ingestor(manager, settings):
    create_jkg_schema(manager)
    _set_enabled(manager, "1")
    container = Container(settings=settings, manager=manager, logger=None)
    memory_id = _insert_memory(manager, "张三 负责 JarvisMind。")
    _audit(manager, memory_id, "CREATE")

    result = container.jkg_lifecycle.tick()

    assert result["ingested_processed"] == 1
    assert _counts(manager)["jkg_edges"] == 1


# ===========================================================================
# G · service-level rule for reactivate_reasons
# ===========================================================================
def _mark_source(manager, edge_id: str, memory_id: str, reason: str) -> None:
    with manager.session() as session:
        session.execute(
            text(
                "UPDATE jkg_edge_sources SET invalidated_at = :now, "
                "invalidation_reason = :reason WHERE edge_id = :edge_id AND memory_id = :memory_id"
            ),
            {"now": utc_now_iso(), "reason": reason, "edge_id": edge_id, "memory_id": memory_id},
        )


def _source_rows(manager, edge_id: str) -> dict:
    with manager.session() as session:
        rows = session.execute(
            text(
                "SELECT memory_id, invalidated_at, invalidation_reason "
                "FROM jkg_edge_sources WHERE edge_id = :edge_id"
            ),
            {"edge_id": edge_id},
        ).all()
    return {str(row[0]): (row[1], row[2]) for row in rows}


def test_create_edge_reactivates_only_the_reasons_it_names(graph):
    first = _insert_memory(graph, "张三 负责 JarvisMind。")
    second = _insert_memory(graph, "张三 负责 JarvisMind。")
    writer = JkgCompositionAdapter(graph).write_service()
    left = writer.upsert_node(entity_type="PERSON", name="Alice", source_memory_id=first)
    right = writer.upsert_node(entity_type="PROJECT", name="JarvisMind", source_memory_id=first)
    edge = writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[first, second],
    )
    edge_id = edge["edge_id"]
    _mark_source(graph, edge_id, first, STALE_REASON)
    _mark_source(graph, edge_id, second, "MERGED_SOURCE")

    default = writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[first, second],
    )
    assert default["sources_reactivated"] == []
    assert _source_rows(graph, edge_id)[first][1] == STALE_REASON

    named = writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[first, second],
        reactivate_reasons=(STALE_REASON,),
    )
    assert named["sources_reactivated"] == [first]
    rows = _source_rows(graph, edge_id)
    assert rows[first] == (None, None)
    assert rows[second][1] == "MERGED_SOURCE"


# ===========================================================================
# H · imported memories
# ===========================================================================
def test_an_imported_memory_is_extracted_and_repeated_ticks_do_not_duplicate(graph, sync):
    memory_id = _insert_memory(graph, "张三 负责 JarvisMind。")
    _audit(graph, memory_id, "IMPORT")

    first = sync.tick()

    assert first["ingested_processed"] == 1
    assert _counts(graph) == {"jkg_nodes": 2, "jkg_edges": 1, "jkg_edge_sources": 1}
    assert list(_edges_of(graph, memory_id).values()) == ["ACTIVE"]

    before = _counts(graph)
    second = sync.tick()

    assert second["ok"] is True
    assert _counts(graph) == before
    assert list(_edges_of(graph, memory_id).values()) == ["ACTIVE"]
