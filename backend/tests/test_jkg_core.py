"""JKG-CORE-01 regression suite.

Every database is the shared ``manager`` fixture's isolated temporary file.
The production JKG tables deliberately do not exist yet; these tests install
the exact proposed DDL through ``tests/_fixtures/jkg_schema_factory`` and never
call a migration entry point.

Coverage maps to the task acceptance list:
entity creation, name standardisation, entity dedupe, relation creation,
relation dedupe, multi-source edges, provenance, source-less rejection,
invalid entity rejection, query correctness, lifecycle forget handling, and
the frozen v6 migration boundary.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.errors import NotFoundError, ValidationError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.migrations import MIGRATIONS
from jarvismind.jkg import (
    JKG_SCHEMA_STATEMENTS,
    JKG_SCHEMA_VERSION,
    JkgEdgeTypeMismatchError,
    JkgInvalidEntityError,
    JkgInvalidMetadataError,
    JkgInvalidRelationError,
    JkgInvalidSourceError,
    JkgQueryService,
    JkgSchemaNotReadyError,
    JkgSelfEdgeError,
    JkgService,
    JkgSourceRequiredError,
    build_jkg_services,
    canonicalize_entity_name,
    normalize_entity_name,
    present_jkg_tables,
    validate_statements,
)
from jarvismind.jkg.models import JkgEdge, JkgEdgeSource, JkgNode
from jarvismind.jkg.schema import JkgSchemaError, jkg_tables_present
from jarvismind.jkg.serialization import node_payload
from jarvismind.services.lifecycle_service import LifecycleService

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, 5.0, 0.9, 'ACTIVE', :now, :now"
    ")"
)


@pytest.fixture()
def memories(manager):
    """Create real memory_records rows in the isolated temp database."""

    def make(content: str = "memory") -> str:
        memory_id = uuid.uuid4().hex
        with manager.session() as session:
            session.execute(
                _INSERT_MEMORY,
                {
                    "id": memory_id,
                    "content": content,
                    "now": utc_now_iso(),
                },
            )
        return memory_id

    return make


@pytest.fixture()
def jkg_env(manager):
    create_jkg_schema(manager)
    return manager


@pytest.fixture()
def jkg(jkg_env):
    return JkgService(jkg_env)


@pytest.fixture()
def query(jkg_env):
    return JkgQueryService(jkg_env)


@pytest.fixture()
def lifecycle(manager):
    return LifecycleService(manager)


def _person(jkg: JkgService, memory_id: str, name: str = "Alice") -> dict:
    return jkg.upsert_node(
        entity_type="PERSON", name=name, source_memory_id=memory_id
    )


def _project(jkg: JkgService, memory_id: str, name: str = "JarvisMind") -> dict:
    return jkg.upsert_node(
        entity_type="PROJECT", name=name, source_memory_id=memory_id
    )


def _event(jkg: JkgService, memory_id: str, name: str = "Launch") -> dict:
    return jkg.upsert_node(
        entity_type="EVENT", name=name, source_memory_id=memory_id
    )


# ---------------------------------------------------------------------------
# Schema boundary: the frozen v6 line is untouched and nothing self-installs
# ---------------------------------------------------------------------------


def test_core_migrations_stay_at_v6_and_contain_no_jkg_tables():
    assert MIGRATIONS[-1].version == 6
    assert JKG_SCHEMA_VERSION == 1
    for migration in MIGRATIONS:
        for statement in migration.statements:
            assert "jkg_" not in statement.lower()


def test_jkg_schema_plan_is_append_only():
    assert validate_statements() == [
        " ".join(statement.split()) for statement in JKG_SCHEMA_STATEMENTS
    ]
    for statement in JKG_SCHEMA_STATEMENTS:
        upper = statement.upper()
        assert upper.startswith(("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ", "CREATE UNIQUE INDEX IF NOT EXISTS "))
        assert not upper.startswith(
            ("DROP ", "ALTER ", "DELETE ", "UPDATE ", "INSERT ")
        )

    with pytest.raises(JkgSchemaError):
        validate_statements(["DROP TABLE jkg_nodes"])
    with pytest.raises(JkgSchemaError):
        validate_statements(["ALTER TABLE memory_records ADD COLUMN x TEXT"])
    with pytest.raises(JkgSchemaError):
        validate_statements(["DELETE FROM memory_records"])


def test_jkg_schema_installation_does_not_change_memory_records(manager):
    def columns() -> list:
        with manager.session() as session:
            return [
                tuple(row)
                for row in session.execute(text("PRAGMA table_info(memory_records)"))
            ]

    before = columns()
    create_jkg_schema(manager)
    after = columns()
    assert before == after
    with manager.session() as session:
        assert jkg_tables_present(session) is True


def test_schema_is_absent_by_default_and_services_do_not_create_it(manager):
    with manager.session() as session:
        assert present_jkg_tables(session) == []

    with pytest.raises(JkgSchemaNotReadyError) as exc:
        JkgService(manager).upsert_node(
            entity_type="PERSON",
            name="Alice",
            source_memory_id="any-memory",
        )
    assert exc.value.code == "jkg_schema_not_ready"
    assert set(exc.value.detail["missing"]) == {
        "jkg_nodes",
        "jkg_edges",
        "jkg_edge_sources",
    }

    with manager.session() as session:
        assert present_jkg_tables(session) == []


def test_build_jkg_services_returns_write_and_read_pair(jkg_env):
    writer, reader = build_jkg_services(jkg_env)
    assert isinstance(writer, JkgService)
    assert isinstance(reader, JkgQueryService)


# ---------------------------------------------------------------------------
# Entity creation, normalisation, dedupe
# ---------------------------------------------------------------------------


def test_entity_creation_fields_and_normalisation(jkg, memories):
    memory_id = memories("Alice is the lead engineer on JarvisMind")
    node = jkg.upsert_node(
        entity_type="person",
        name="  Ａｌｉｃｅ\u200b   Smith  ",
        source_memory_id=memory_id,
        metadata={"role": "engineer"},
    )
    assert node["entity_type"] == "PERSON"
    assert node["name"] == "Alice Smith"
    assert node["display_name"] == "Alice Smith"
    assert node["canonical_name"] == "Alice Smith"
    assert node["normalized_key"] == "alice smith"
    assert node["source_memory_id"] == memory_id
    assert node["source_memory_status"] == "ACTIVE"
    assert node["source_memory_valid"] is True
    assert node["metadata"] == {"role": "engineer"}
    assert node["generation"] == 1
    assert node["status"] == "ACTIVE"


@pytest.mark.parametrize(
    ("raw", "canonical", "normalized"),
    [
        ("Alice Smith", "Alice Smith", "alice smith"),
        ("  alice   smith  ", "alice smith", "alice smith"),
        ("ALICE SMITH", "ALICE SMITH", "alice smith"),
        ("贾维斯　项目", "贾维斯 项目", "贾维斯 项目"),
    ],
)
def test_name_standardisation(raw, canonical, normalized):
    assert canonicalize_entity_name(raw) == canonical
    assert normalize_entity_name(raw) == normalized


def test_invalid_names_and_types_are_rejected(jkg, memories):
    memory_id = memories()
    with pytest.raises(ValidationError):
        jkg.upsert_node(
            entity_type="PERSON", name="   ", source_memory_id=memory_id
        )
    with pytest.raises(ValidationError):
        jkg.upsert_node(
            entity_type="PERSON",
            name="x" * 201,
            source_memory_id=memory_id,
        )
    with pytest.raises(ValidationError):
        jkg.upsert_node(
            entity_type="ORGANISATION", name="Acme", source_memory_id=memory_id
        )


def test_entity_dedupe_same_type_and_case_variants(jkg, memories):
    memory_id = memories()
    first = _person(jkg, memory_id, "Alice Smith")
    second = jkg.upsert_node(
        entity_type="PERSON",
        name="  alice   SMITH ",
        source_memory_id=memory_id,
    )
    third = _person(jkg, memory_id, "ALICE SMITH")
    assert first["node_id"] == second["node_id"] == third["node_id"]
    with jkg.manager.session() as session:
        assert (
            session.execute(select(func.count()).select_from(JkgNode)).scalar()
            == 1
        )


def test_entity_same_name_different_type_is_allowed(jkg, memories):
    memory_id = memories()
    person = _person(jkg, memory_id, "Alice")
    project = _project(jkg, memory_id, "Alice")
    assert person["node_id"] != project["node_id"]
    assert person["entity_type"] == "PERSON"
    assert project["entity_type"] == "PROJECT"


def test_entity_requires_a_valid_source_memory(jkg, memories, lifecycle):
    missing = "does-not-exist"
    with pytest.raises(JkgInvalidSourceError) as exc:
        _person(jkg, missing)
    assert missing in exc.value.detail["missing_memory_ids"]

    forgotten = memories("temporary")
    lifecycle.forget(memory_ids=[forgotten], confirm=True, reason="test")
    with pytest.raises(JkgInvalidSourceError) as exc:
        _person(jkg, forgotten)
    assert exc.value.detail["ineligible_sources"][0]["status"] == "DELETED"


def test_entity_metadata_is_validated(jkg, memories):
    memory_id = memories()
    with pytest.raises(JkgInvalidMetadataError):
        jkg.upsert_node(
            entity_type="PERSON",
            name="Alice",
            source_memory_id=memory_id,
            metadata=["not", "an", "object"],
        )
    with pytest.raises(JkgInvalidMetadataError):
        jkg.upsert_node(
            entity_type="PERSON",
            name="Bob",
            source_memory_id=memory_id,
            metadata={"blob": "x" * 20_000},
        )


# ---------------------------------------------------------------------------
# Relation creation, dedupe, multi-source, provenance
# ---------------------------------------------------------------------------


def _alice_works_on_jarvis(jkg, memories):
    memory_id = memories("Alice works on JarvisMind")
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)
    edge = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
        confidence=0.9,
        metadata={"role": "lead"},
    )
    return memory_id, alice, jarvis, edge


def test_relation_creation_and_source_traceability(jkg, memories):
    memory_id, alice, jarvis, edge = _alice_works_on_jarvis(jkg, memories)
    assert edge["created"] is True
    assert edge["predicate"] == "WORKS_ON"
    assert edge["relation_type"] == "WORKS_ON"
    assert edge["subject_id"] == alice["node_id"]
    assert edge["object_id"] == jarvis["node_id"]
    assert edge["confidence"] == 0.9
    assert edge["metadata"] == {"role": "lead"}
    assert edge["source_count"] == 1
    assert edge["valid_source_count"] == 1
    assert edge["is_valid"] is True
    assert edge["sources"][0]["memory_id"] == memory_id
    assert edge["sources"][0]["memory_status"] == "ACTIVE"
    assert edge["sources"][0]["is_valid"] is True


def test_relation_dedupe_merges_sources(jkg, memories, query):
    memory_one = memories("Alice works on JarvisMind")
    memory_two = memories("Alice is still working on JarvisMind")
    alice = _person(jkg, memory_one)
    jarvis = _project(jkg, memory_one)
    first = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_one],
    )
    second = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_one, memory_two],
    )
    assert second["created"] is False
    assert second["edge_id"] == first["edge_id"]
    assert second["source_count"] == 2
    assert set(second["sources_added"]) == {memory_two}
    with jkg.manager.session() as session:
        assert session.execute(
            select(func.count()).select_from(JkgEdge)
        ).scalar() == 1
        assert session.execute(
            select(func.count()).select_from(JkgEdgeSource)
        ).scalar() == 2

    traced = query.trace_edge(first["edge_id"])
    assert {source["memory_id"] for source in traced["sources"]} == {
        memory_one,
        memory_two,
    }
    assert traced["valid_source_count"] == 2


def test_edge_without_source_is_rejected(jkg, memories):
    memory_id = memories()
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)
    for empty in ([], (), None, "", "memory-id"):
        with pytest.raises(JkgSourceRequiredError):
            jkg.create_edge(
                subject_id=alice["node_id"],
                predicate="WORKS_ON",
                object_id=jarvis["node_id"],
                source_memory_ids=empty,
            )


def test_edge_with_missing_or_deleted_source_is_rejected(jkg, memories, lifecycle):
    memory_id = memories()
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)

    with pytest.raises(JkgInvalidSourceError) as exc:
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="WORKS_ON",
            object_id=jarvis["node_id"],
            source_memory_ids=["missing-memory"],
        )
    assert exc.value.detail["missing_memory_ids"] == ["missing-memory"]

    forgotten = memories("old fact")
    lifecycle.forget(memory_ids=[forgotten], confirm=True, reason="test")
    with pytest.raises(JkgInvalidSourceError) as exc:
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="WORKS_ON",
            object_id=jarvis["node_id"],
            source_memory_ids=[forgotten],
        )
    assert exc.value.detail["ineligible_sources"][0]["memory_id"] == forgotten


def test_edge_with_invalid_endpoint_is_rejected(jkg, memories):
    memory_id = memories()
    alice = _person(jkg, memory_id)
    with pytest.raises(JkgInvalidEntityError):
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="WORKS_ON",
            object_id="missing-node",
            source_memory_ids=[memory_id],
        )


def test_relation_direction_and_self_edge_contract(jkg, memories):
    memory_id = memories()
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)

    with pytest.raises(JkgEdgeTypeMismatchError):
        jkg.create_edge(
            subject_id=jarvis["node_id"],
            predicate="WORKS_ON",
            object_id=alice["node_id"],
            source_memory_ids=[memory_id],
        )
    with pytest.raises(JkgEdgeTypeMismatchError):
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="KNOWS",
            object_id=jarvis["node_id"],
            source_memory_ids=[memory_id],
        )
    with pytest.raises(JkgSelfEdgeError):
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="KNOWS",
            object_id=alice["node_id"],
            source_memory_ids=[memory_id],
        )
    with pytest.raises(JkgInvalidRelationError):
        jkg.create_edge(
            subject_id=alice["node_id"],
            predicate="WORKS_AT",
            object_id=jarvis["node_id"],
            source_memory_ids=[memory_id],
        )


def test_add_sources_dedupes_and_rejects_unknown_edge(jkg, memories):
    memory_one = memories()
    memory_two = memories()
    alice = _person(jkg, memory_one)
    jarvis = _project(jkg, memory_one)
    edge = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_one],
    )
    updated = jkg.add_edge_sources(
        edge_id=edge["edge_id"], memory_ids=[memory_one, memory_two]
    )
    assert updated["source_count"] == 2
    assert updated["sources_added"] == [memory_two]

    again = jkg.add_edge_sources(
        edge_id=edge["edge_id"], memory_ids=[memory_one, memory_two]
    )
    assert again["source_count"] == 2
    assert again["sources_added"] == []

    with pytest.raises(JkgInvalidEntityError):
        jkg.add_edge_sources(edge_id="missing-edge", memory_ids=[memory_one])


# ---------------------------------------------------------------------------
# Query correctness (depth 1, direction, validity)
# ---------------------------------------------------------------------------


def _graph(jkg, memories):
    memory_id = memories("Alice works on JarvisMind and knows Bob")
    alice = _person(jkg, memory_id, "Alice")
    bob = _person(jkg, memory_id, "Bob")
    jarvis = _project(jkg, memory_id, "JarvisMind")
    launch = _event(jkg, memory_id, "Launch")
    works = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
    )
    knows = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="KNOWS",
        object_id=bob["node_id"],
        source_memory_ids=[memory_id],
    )
    attends = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="ATTENDS",
        object_id=launch["node_id"],
        source_memory_ids=[memory_id],
    )
    belongs = jkg.create_edge(
        subject_id=launch["node_id"],
        predicate="BELONGS_TO",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
    )
    return {
        "memory_id": memory_id,
        "alice": alice,
        "bob": bob,
        "jarvis": jarvis,
        "launch": launch,
        "works": works,
        "knows": knows,
        "attends": attends,
        "belongs": belongs,
    }


def test_query_nodes_edges_and_neighbours_are_depth_one(jkg, query, memories):
    graph = _graph(jkg, memories)
    alice_id = graph["alice"]["node_id"]

    outgoing = query.neighbors(node_id=alice_id, direction="outgoing")
    assert outgoing["items"]
    neighbour_ids = {item["node"]["node_id"] for item in outgoing["items"]}
    assert neighbour_ids == {
        graph["jarvis"]["node_id"],
        graph["bob"]["node_id"],
        graph["launch"]["node_id"],
    }

    # Alice -> Bob (KNOWS) and Bob has no outgoing edge in this graph, so a
    # depth-1 query can never reach anything "through" Bob.
    only_knows = query.neighbors(
        node_id=alice_id, direction="outgoing", predicate="KNOWS"
    )
    assert [item["node"]["node_id"] for item in only_knows["items"]] == [
        graph["bob"]["node_id"]
    ]

    incoming = query.neighbors(
        node_id=graph["jarvis"]["node_id"],
        direction="incoming",
        predicate="WORKS_ON",
    )
    assert [item["node"]["node_id"] for item in incoming["items"]] == [alice_id]

    get_node = query.get_node(alice_id)
    assert get_node["node_id"] == alice_id
    listed = query.list_nodes(entity_type="PERSON")
    assert {item["node_id"] for item in listed["items"]} == {
        graph["alice"]["node_id"],
        graph["bob"]["node_id"],
    }


def test_query_limits_are_enforced(query):
    with pytest.raises(ValidationError):
        query.list_nodes(limit=0)
    with pytest.raises(ValidationError):
        query.list_nodes(limit=101)
    with pytest.raises(ValidationError):
        query.list_nodes(offset=-1)
    with pytest.raises(ValidationError):
        query.list_edges(limit=0)
    with pytest.raises(ValidationError):
        query.list_edges(direction="sideways")
    with pytest.raises(ValidationError):
        query.neighbors(node_id="missing", direction="sideways")


def test_query_not_found_paths(jkg, query, memories):
    with pytest.raises(NotFoundError):
        query.get_node("missing")
    with pytest.raises(NotFoundError):
        query.get_edge("missing")
    with pytest.raises(NotFoundError):
        query.get_edge("missing", valid_only=True)
    with pytest.raises(NotFoundError):
        query.neighbors(node_id="missing")


# ---------------------------------------------------------------------------
# JME lifecycle compatibility
# ---------------------------------------------------------------------------


def test_multi_source_edge_stays_valid_until_last_source_is_forgotten(
    jkg, query, memories, lifecycle
):
    memory_one = memories("Alice works on JarvisMind")
    memory_two = memories("Alice still works on JarvisMind")
    alice = _person(jkg, memory_one)
    jarvis = _project(jkg, memory_one)
    edge = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_one, memory_two],
    )

    lifecycle.forget(memory_ids=[memory_one], confirm=True, reason="first")
    # Read-time validity is already enforced before any JKG sync runs.
    page = query.list_edges(valid_only=True)
    assert [item["edge_id"] for item in page["items"]] == [edge["edge_id"]]
    assert page["items"][0]["valid_source_count"] == 1

    synced = jkg.sync_memory(memory_one)
    assert synced["sources_invalidated"] == 1
    assert synced["edges_invalidated"] == []

    lifecycle.forget(memory_ids=[memory_two], confirm=True, reason="second")
    synced = jkg.sync_memory(memory_two)
    assert synced["sources_invalidated"] == 1
    assert synced["edges_invalidated"] == [edge["edge_id"]]

    valid_page = query.list_edges(valid_only=True)
    assert valid_page["items"] == []
    invalid_page = query.list_edges(valid_only=False)
    assert [item["edge_id"] for item in invalid_page["items"]] == [
        edge["edge_id"]
    ]
    assert invalid_page["items"][0]["status"] == "INVALID"
    assert invalid_page["items"][0]["valid_source_count"] == 0
    assert invalid_page["items"][0]["is_valid"] is False

    traced = query.trace_edge(edge["edge_id"])
    assert traced["source_count"] == 2
    assert traced["valid_source_count"] == 0
    assert {source["invalidation_reason"] for source in traced["sources"]} == {
        "MEMORY_DELETED"
    }
    assert all(source["invalidated_at"] for source in traced["sources"])


def test_read_time_validity_hides_forgotten_edge_without_sync(
    jkg, query, memories, lifecycle
):
    memory_id = memories("Alice works on JarvisMind")
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)
    edge = jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
    )
    lifecycle.forget(memory_ids=[memory_id], confirm=True, reason="test")

    # No sync_memory call: the read path still refuses the edge.
    assert query.list_edges(valid_only=True)["items"] == []
    fetched = query.get_edge(edge["edge_id"])
    assert fetched["is_valid"] is False
    assert fetched["status"] == "ACTIVE"  # not materialised yet
    assert fetched["sources"][0]["memory_status"] == "DELETED"
    assert fetched["sources"][0]["invalidated_at"] is None

    report = query.integrity_report()
    assert report["counts"]["edges"] == 1
    assert [item["edge_id"] for item in report["invalid_edges"]] == [
        edge["edge_id"]
    ]
    assert report["ineligible_sources"][0]["memory_status"] == "DELETED"


# ---------------------------------------------------------------------------
# JMR-JKG-AUDIT-FIX-01 B-3: node listings label the source memory state
# ---------------------------------------------------------------------------


def test_list_nodes_labels_forgotten_source_without_hiding_it(
    jkg, query, memories, lifecycle
):
    kept_memory = memories("Alice is a teammate")
    forgotten_memory = memories("Bob is a teammate")
    kept = _person(jkg, kept_memory, "Alice")
    forgotten = _person(jkg, forgotten_memory, "Bob")
    lifecycle.forget(memory_ids=[forgotten_memory], confirm=True, reason="test")

    # No sync_memory call: the read path alone must label the node.
    page = query.list_nodes()
    by_id = {item["node_id"]: item for item in page["items"]}
    assert page["total"] == 2
    assert set(by_id) == {kept["node_id"], forgotten["node_id"]}

    assert by_id[kept["node_id"]]["source_memory_status"] == "ACTIVE"
    assert by_id[kept["node_id"]]["source_memory_valid"] is True
    assert by_id[forgotten["node_id"]]["source_memory_status"] == "DELETED"
    assert by_id[forgotten["node_id"]]["source_memory_valid"] is False

    # The listing and the single-node read must never disagree.
    for node_id, item in by_id.items():
        single = query.get_node(node_id)
        assert item["source_memory_status"] == single["source_memory_status"]
        assert item["source_memory_valid"] == single["source_memory_valid"]


def test_list_nodes_labels_survive_filters_and_paging(
    jkg, query, memories, lifecycle
):
    forgotten_memory = memories("Bob is a teammate")
    kept_memory = memories("JarvisMind is a project")
    _person(jkg, forgotten_memory, "Bob")
    _project(jkg, kept_memory, "JarvisMind")
    lifecycle.forget(memory_ids=[forgotten_memory], confirm=True, reason="test")

    people = query.list_nodes(entity_type="PERSON")
    assert people["total"] == 1
    assert people["items"][0]["source_memory_valid"] is False
    projects = query.list_nodes(entity_type="PROJECT")
    assert projects["total"] == 1
    assert projects["items"][0]["source_memory_valid"] is True

    first = query.list_nodes(limit=1, offset=0)
    second = query.list_nodes(limit=1, offset=1)
    assert first["total"] == second["total"] == 2
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["node_id"] != second["items"][0]["node_id"]
    for item in first["items"] + second["items"]:
        assert "source_memory_status" in item
        assert "source_memory_valid" in item


def test_list_nodes_labels_every_node_of_a_shared_forgotten_source(
    jkg, query, memories, lifecycle
):
    memory_id = memories("Alice works on JarvisMind")
    _person(jkg, memory_id)
    _project(jkg, memory_id)
    lifecycle.forget(memory_ids=[memory_id], confirm=True, reason="test")

    items = query.list_nodes()["items"]
    assert len(items) == 2
    assert {item["source_memory_status"] for item in items} == {"DELETED"}
    assert {item["source_memory_valid"] for item in items} == {False}


def test_list_nodes_reports_an_unknown_source_as_invalid(
    jkg, query, memories, monkeypatch
):
    _person(jkg, memories("Alice is a teammate"))
    monkeypatch.setattr(query, "_memory_status", lambda memory_id: None)

    item = query.list_nodes()["items"][0]
    assert item["source_memory_status"] is None
    assert item["source_memory_valid"] is False


def test_node_payload_omits_validity_only_when_source_was_not_looked_up(
    jkg_env, jkg, memories
):
    created = _person(jkg, memories("Alice is a teammate"))
    with jkg_env.session() as session:
        row = session.get(JkgNode, created["node_id"])
        not_looked_up = node_payload(row)
        missing = node_payload(row, source_memory_status=None)
        active = node_payload(row, source_memory_status="ACTIVE")

    assert "source_memory_status" not in not_looked_up
    assert "source_memory_valid" not in not_looked_up
    assert missing["source_memory_status"] is None
    assert missing["source_memory_valid"] is False
    assert active["source_memory_status"] == "ACTIVE"
    assert active["source_memory_valid"] is True


def test_reconcile_processes_every_source_memory(jkg, memories, lifecycle):
    memory_one = memories("first")
    memory_two = memories("second")
    alice = _person(jkg, memory_one)
    jarvis = _project(jkg, memory_one)
    jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_one, memory_two],
    )
    lifecycle.forget(memory_ids=[memory_one, memory_two], confirm=True, reason="x")
    result = jkg.reconcile()
    assert result["memories_checked"] == 2
    assert result["sources_invalidated"] == 2
    assert len(result["edges_invalidated"]) == 1


# ---------------------------------------------------------------------------
# Integrity probe
# ---------------------------------------------------------------------------


def test_integrity_report_detects_orphan_and_invalid_edges(jkg, query, memories):
    memory_id = memories()
    alice = _person(jkg, memory_id)
    jarvis = _project(jkg, memory_id)
    jkg.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
    )
    now = utc_now_iso()
    with jkg.manager.session() as session:
        session.add(
            JkgEdge(
                edge_id="orphan-edge",
                predicate="KNOWS",
                subject_id=alice["node_id"],
                object_id=jarvis["node_id"],
                status="ACTIVE",
                generation=1,
                confidence=0.5,
                metadata_json=None,
                recorded_at=now,
                updated_at=now,
            )
        )
        session.flush()

    report = query.integrity_report()
    assert report["counts"]["nodes"] == 2
    assert report["counts"]["edges"] == 2
    assert report["counts"]["sources"] == 1
    assert [item["edge_id"] for item in report["orphan_edges"]] == [
        "orphan-edge"
    ]
    assert report["truncated"] is False


def test_integrity_report_is_clean_for_a_valid_graph(jkg, query, memories):
    _graph(jkg, memories)
    report = query.integrity_report()
    assert report["orphan_edges"] == []
    assert report["invalid_edges"] == []
    assert report["missing_source_memories"] == []
    assert report["ineligible_sources"] == []
    assert report["counts"]["edges"] == 4
    assert report["counts"]["sources"] == 4
