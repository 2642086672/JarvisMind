"""MCE-01A JKG enrichment tests (real graph + degradation)."""
from __future__ import annotations

from sqlalchemy import text

from jarvismind.mce.config import MceConfig
from jarvismind.mce.governance import MceGovernance
from jarvismind.mce.models import ProjectContextSnapshot
from jarvismind.mce.reconstructor import MceReconstructor
from test_mce_api import _create, _decision, _resolve, _table_counts


def _ready_graph(manager):
    from _fixtures.jkg_schema_factory import create_jkg_schema
    from jarvismind.jkg.adapter import JkgCompositionAdapter

    create_jkg_schema(manager)
    from test_mce_api import _upsert_setting

    _upsert_setting(manager, "jkg.enabled", "1")
    return JkgCompositionAdapter(manager).write_service()


def test_invalid_edge_source_is_not_injected(client, auth_headers, app, manager):
    writer = _ready_graph(manager)
    active = _create(
        app,
        "[project:jarvismind] 决策：有效来源",
        metadata=_decision("valid-source"),
    )
    deleted = _create(
        app,
        "[project:jarvismind] 决策：稍后删除的来源",
        metadata=_decision("deleted-source"),
    )
    active_node = writer.upsert_node(
        entity_type="PERSON",
        name="ActivePerson",
        source_memory_id=active.id,
        metadata={"project_id": "jarvismind"},
    )
    deleted_node = writer.upsert_node(
        entity_type="PROJECT",
        name="DeletedProject",
        source_memory_id=deleted.id,
        metadata={"project_id": "jarvismind"},
    )
    writer.create_edge(
        subject_id=active_node["node_id"],
        predicate="WORKS_ON",
        object_id=deleted_node["node_id"],
        source_memory_ids=[active.id, deleted.id],
    )
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_records SET status='DELETED' WHERE id=:id"),
            {"id": deleted.id},
        )
    writer.sync_memory(deleted.id)

    body = _resolve(client, auth_headers, project_id="jarvismind")
    assert all(
        item["neighbor_name"] != "DeletedProject"
        for item in body["project_context"]["related_knowledge"]
    )
    assert body["project_context"]["excluded"]["jkg_edges_rejected"] >= 1


def test_jkg_read_failure_degrades_without_breaking_resolve():
    class RaisingAdapter:
        def state(self):
            return type("State", (), {"value": "ready"})()

        def query_service(self):
            raise RuntimeError("graph unavailable")

    class Projection:
        def scan_memories(self, **kwargs):
            return (
                {
                    "id": "m1",
                    "memory_type": "PROJECT",
                    "content": "[project:jarvismind] 决策：仍然可读",
                    "summary": None,
                    "importance": 5.0,
                    "confidence": 0.8,
                    "status": "ACTIVE",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": None,
                    "metadata_json": {
                        "project_id": "jarvismind",
                        "context_kind": "decision",
                    },
                },
            )

    result = MceReconstructor(
        projection=Projection(),
        governance=MceGovernance(),
        config=MceConfig(),
        jkg_adapter=RaisingAdapter(),
    ).resolve(project_id="jarvismind")

    assert result.snapshot.jkg_state == "error"
    assert result.snapshot.jkg_reason.startswith("query_unavailable")
    assert result.rendered_context  # JME half still works


def test_jkg_enrichment_writes_no_graph_rows(client, auth_headers, app, manager):
    writer = _ready_graph(manager)
    memory = _create(
        app,
        "[project:jarvismind] 决策：只读图谱",
        metadata=_decision("readonly-graph"),
    )
    node = writer.upsert_node(
        entity_type="PROJECT",
        name="JarvisMind",
        source_memory_id=memory.id,
        metadata={"project_id": "jarvismind"},
    )
    other = writer.upsert_node(
        entity_type="EVENT",
        name="MCE",
        source_memory_id=memory.id,
        metadata={"project_id": "jarvismind"},
    )
    writer.create_edge(
        subject_id=other["node_id"],
        predicate="BELONGS_TO",
        object_id=node["node_id"],
        source_memory_ids=[memory.id],
    )
    before = _table_counts(manager)
    body = _resolve(client, auth_headers, project_id="jarvismind")
    after = _table_counts(manager)
    assert before == after
    assert before.get("jkg_nodes", 0) == 2
    assert before.get("jkg_edges", 0) == 1
    related = body["project_context"]["related_knowledge"]
    assert related
    assert all(item["source_memory_ids"] for item in related)
    edge_lines = [
        line
        for line in body["rendered_context"].splitlines()
        if "BELONGS_TO" in line
    ]
    assert len(edge_lines) == len(set(edge_lines)) == 1


def test_status_reports_ready_jkg_without_installing_it(client, auth_headers, manager):
    _ready_graph(manager)
    response = client.get("/api/v1/mce/status", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["jkg"]["state"] == "ready"
