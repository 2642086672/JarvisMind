"""JkgIngestor: idempotent memory -> derived-graph write core (JKG-INT-05 / A1).

Every database is the shared ``manager`` fixture's temporary file with the
proposed JKG DDL installed by ``create_jkg_schema``. Memory rows are inserted
through the real manager session. No network, no model, no production database.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.projection import ReadOnlyProjection
from jarvismind.jkg import extraction
from jarvismind.jkg.adapter import JkgCompositionAdapter
from jarvismind.jkg.config import JkgConfig
from jarvismind.jkg.errors import JkgInvalidEntityError
from jarvismind.jkg.extraction import RULE_VERSION as RULES_VERSION
from jarvismind.jkg.ingestor import IngestOutcome, JkgIngestor
from jarvismind.jkg.repository import JkgRepository
from jarvismind.jkg.service import JkgService

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, summary, importance, confidence, status, "
    "created_at, updated_at, expires_at, metadata_json"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, :summary, 5.0, 0.9, :status, "
    ":created, :updated, :expires, :metadata"
    ")"
)

_ENABLED = JkgConfig.from_values(setting_value="1", env_value="")
_DISABLED = JkgConfig.from_values(setting_value="0", env_value="")


@pytest.fixture()
def graph(manager):
    create_jkg_schema(manager)
    return manager


@pytest.fixture()
def add_memory(graph):
    def make(
        content: str,
        *,
        status: str = "ACTIVE",
        summary: str = "",
        expires_at=None,
        metadata=None,
        created: str = "2026-05-01T00:00:00+00:00",
        updated: str = "2026-05-02T00:00:00+00:00",
    ) -> str:
        memory_id = uuid.uuid4().hex
        with graph.session() as session:
            session.execute(
                _INSERT,
                {
                    "id": memory_id,
                    "content": content,
                    "summary": summary,
                    "status": status,
                    "created": created,
                    "updated": updated,
                    "expires": expires_at,
                    "metadata": json.dumps(metadata) if metadata is not None else None,
                },
            )
        return memory_id

    return make


def _ingestor(manager, config=_ENABLED, tokens=None) -> JkgIngestor:
    adapter = JkgCompositionAdapter(manager, config=config)
    return JkgIngestor(manager, adapter=adapter, exact_tokens=tokens)


def _counts(manager) -> dict:
    with manager.session() as session:
        def one(sql: str) -> int:
            return int(session.execute(text(sql)).scalar() or 0)

        return {
            "nodes": one("SELECT COUNT(*) FROM jkg_nodes"),
            "edges": one("SELECT COUNT(*) FROM jkg_edges"),
            "sources": one("SELECT COUNT(*) FROM jkg_edge_sources"),
        }


def _node_names(manager, entity_type: str) -> list:
    with manager.session() as session:
        return [
            row[0]
            for row in session.execute(
                text("SELECT name FROM jkg_nodes WHERE entity_type = :t ORDER BY name"),
                {"t": entity_type},
            )
        ]


def _authority_snapshot(manager) -> tuple:
    projection = ReadOnlyProjection(manager)
    with manager.session() as session:
        def one(sql: str) -> int:
            return int(session.execute(text(sql)).scalar() or 0)

        return (
            projection.content_digest(),
            one("SELECT COUNT(*) FROM memory_candidates"),
            one("SELECT COUNT(*) FROM memory_audit_logs"),
        )


# --------------------------------------------------------------- happy path


def test_extractable_memory_writes_nodes_edge_and_source(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。")
    outcome = _ingestor(graph).ingest_memory(memory_id, init_task_id="task-1")

    assert outcome == IngestOutcome("processed", "INGESTED", 2, 1, 1, 0, 0)
    assert _counts(graph) == {"nodes": 2, "edges": 1, "sources": 1}
    assert _node_names(graph, "PERSON") == ["张三"]
    assert _node_names(graph, "PROJECT") == ["JarvisMind"]


def test_written_metadata_carries_rule_version_match_key_and_no_body(graph, add_memory):
    body = "张三 负责 JarvisMind。"
    memory_id = add_memory(body)
    _ingestor(graph).ingest_memory(memory_id, init_task_id="task-1")

    with graph.session() as session:
        node_meta = [
            json.loads(row[0])
            for row in session.execute(text("SELECT metadata_json FROM jkg_nodes"))
        ]
        edge_meta = [
            json.loads(row[0])
            for row in session.execute(text("SELECT metadata_json FROM jkg_edges"))
        ]
        raw_dump = " ".join(
            str(row[0] or "")
            for row in session.execute(
                text("SELECT metadata_json FROM jkg_nodes UNION ALL SELECT metadata_json FROM jkg_edges")
            )
        )

    for meta in node_meta:
        assert meta["jkg"]["rule_version"] == RULES_VERSION
        assert meta["jkg"]["init_task_id"] == "task-1"
        assert meta["jkg"]["match_key"]
    for meta in edge_meta:
        assert meta["jkg"]["rule_version"] == RULES_VERSION
        assert meta["jkg"]["rule"].startswith("zh.")
        assert meta["jkg"]["observed_at"] == "2026-05-02T00:00:00+00:00"
    assert "负责" not in raw_dump
    assert body not in raw_dump


def test_repeated_ingest_is_idempotent(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。李四 参加 Launch。")
    ingestor = _ingestor(graph)
    first = ingestor.ingest_memory(memory_id)
    counts = _counts(graph)
    second = ingestor.ingest_memory(memory_id)

    assert first.status == second.status == "processed"
    assert _counts(graph) == counts
    assert (second.nodes_added, second.edges_added, second.sources_added) == (0, 0, 0)
    assert second.nodes_matched == 4


def test_project_aliases_collapse_to_one_project_node(graph, add_memory):
    ingestor = _ingestor(graph)
    ingestor.ingest_memory(add_memory("张三 负责 JarvisMind。"))
    second = ingestor.ingest_memory(add_memory("李四 负责 Jarvis Mind。"))
    third = ingestor.ingest_memory(add_memory("王五 负责 JarvisMind 项目。"))

    assert _node_names(graph, "PROJECT") == ["JarvisMind"]
    assert second.nodes_added == 1 and second.nodes_matched == 1
    assert third.nodes_added == 1 and third.nodes_matched == 1
    assert _counts(graph)["edges"] == 3


def test_same_relation_from_two_memories_is_one_edge_with_two_sources(graph, add_memory):
    ingestor = _ingestor(graph)
    first_id = add_memory("张三 负责 JarvisMind。")
    second_id = add_memory("张三 负责 JarvisMind。")
    first = ingestor.ingest_memory(first_id)
    second = ingestor.ingest_memory(second_id)

    assert first.edges_added == 1 and first.sources_added == 1
    assert second.edges_added == 0 and second.sources_added == 1
    assert _counts(graph) == {"nodes": 2, "edges": 1, "sources": 2}


# ----------------------------------------------------------- skip reasons


def test_no_rule_match_is_skipped_and_graph_is_unchanged(graph, add_memory):
    before = _counts(graph)
    outcome = _ingestor(graph).ingest_memory(add_memory("今天天气很好。"))
    assert outcome.status == "skipped" and outcome.reason == "NO_CANDIDATES"
    assert _counts(graph) == before


def test_node_only_memory_writes_its_nodes_and_is_processed(graph, add_memory):
    # "张三 认识 张三" yields one PERSON node. The KNOWS edge is a self-reference
    # and is rejected, so the memory has a node and no edge.
    memory_id = add_memory("张三 认识 张三。")
    ingestor = _ingestor(graph)
    first = ingestor.ingest_memory(memory_id)
    assert first == IngestOutcome("processed", "INGESTED", 1, 0, 0, 0, 1)
    assert _counts(graph) == {"nodes": 1, "edges": 0, "sources": 0}
    assert _node_names(graph, "PERSON") == ["张三"]

    before = _counts(graph)
    second = ingestor.ingest_memory(memory_id)
    assert second.status == "processed"
    assert (second.nodes_added, second.nodes_matched, second.edges_added, second.sources_added) == (0, 1, 0, 0)
    assert _counts(graph) == before


@pytest.mark.parametrize("status", ["DELETED", "MERGED"])
def test_deleted_or_merged_memory_is_not_eligible(graph, add_memory, status):
    memory_id = add_memory("张三 负责 JarvisMind。", status=status)
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert (outcome.status, outcome.reason) == ("skipped", "SOURCE_NOT_ELIGIBLE")
    assert _counts(graph) == {"nodes": 0, "edges": 0, "sources": 0}


def test_archived_memory_is_still_ingested(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。", status="ARCHIVED")
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert outcome.status == "processed"
    assert _counts(graph)["sources"] == 1


def test_expired_memory_is_skipped(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。", expires_at="2020-01-01T00:00:00+00:00")
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert (outcome.status, outcome.reason) == ("skipped", "EXPIRED")
    assert _counts(graph)["nodes"] == 0


def test_future_expiry_is_still_ingested(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。", expires_at="2099-01-01T00:00:00+00:00")
    assert _ingestor(graph).ingest_memory(memory_id).status == "processed"


def test_unparseable_expiry_is_skipped_without_writing(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。", expires_at="not-a-date")
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert (outcome.status, outcome.reason) == ("skipped", "INVALID_EXPIRY")
    assert _counts(graph) == {"nodes": 0, "edges": 0, "sources": 0}


def test_missing_memory_is_not_found(graph):
    outcome = _ingestor(graph).ingest_memory(uuid.uuid4().hex)
    assert (outcome.status, outcome.reason) == ("skipped", "NOT_FOUND")


# ----------------------------------------------------------- governance


@pytest.mark.parametrize(
    "body",
    [
        "张三 负责 JarvisMind。password: hunter2",
        "张三 负责 JarvisMind。身份证号 110101199003071234",
    ],
)
def test_credential_or_pii_body_is_never_written(graph, add_memory, body):
    memory_id = add_memory(body)
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert outcome.status == "skipped"
    assert outcome.reason != "NONE_DETECTED"
    assert "hunter2" not in outcome.reason
    assert _counts(graph) == {"nodes": 0, "edges": 0, "sources": 0}


@pytest.mark.parametrize(
    "body",
    [
        "张三 负责 JarvisMind，他在医院治疗。",
        "张三 负责 JarvisMind，他家住在望京小区。",
        "张三 负责 JarvisMind，月薪三万。",
        "张三 负责 JarvisMind，他是同性恋。",
        "张三 负责 JarvisMind，他信教。",
        "张三 负责 JarvisMind，他是党员。",
    ],
)
def test_personal_sensitivity_category_is_never_written(graph, add_memory, body):
    memory_id = add_memory(body)
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert outcome.status == "skipped"
    assert _counts(graph) == {"nodes": 0, "edges": 0, "sources": 0}


def test_personal_category_in_summary_is_never_written(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。", summary="他在医院工作")
    outcome = _ingestor(graph).ingest_memory(memory_id)
    assert outcome.status == "skipped"
    assert _counts(graph)["nodes"] == 0


def test_exact_token_in_body_is_denied(graph, add_memory):
    token = "A1-LIVE-TOKEN-7731"
    memory_id = add_memory(f"张三 负责 JarvisMind。{token}")
    outcome = _ingestor(graph, tokens=lambda: [token]).ingest_memory(memory_id)
    assert outcome.status == "skipped"
    assert token not in outcome.reason
    assert _counts(graph)["nodes"] == 0


def test_exact_token_provider_failure_fails_closed(graph, add_memory):
    def broken():
        raise RuntimeError("secret provider exploded")

    memory_id = add_memory("张三 负责 JarvisMind。")
    outcome = _ingestor(graph, tokens=broken).ingest_memory(memory_id)
    assert (outcome.status, outcome.reason) == ("failed", "EXACT_TOKENS_UNAVAILABLE")
    assert "exploded" not in outcome.reason
    assert _counts(graph)["nodes"] == 0


def test_name_level_privacy_drops_one_candidate_and_keeps_the_rest(graph, add_memory, monkeypatch):
    # See tests/test_jkg_extraction_governance.py: the body gate covers ordinary
    # names, so the per-name drop is reached by patching the classifier.
    original = extraction._personal_categories

    def flag_bob_only(value):
        return ("health",) if value == "Bob" else original(value)

    monkeypatch.setattr(extraction, "_personal_categories", flag_bob_only)
    memory_id = add_memory("Alice 负责 JarvisMind。Bob 认识 Alice。")
    outcome = _ingestor(graph).ingest_memory(memory_id)

    assert outcome.status == "processed"
    assert _node_names(graph, "PERSON") == ["Alice"]
    assert _node_names(graph, "PROJECT") == ["JarvisMind"]
    assert _counts(graph) == {"nodes": 2, "edges": 1, "sources": 1}


# --------------------------------------------------------- failure handling


def test_disabled_graph_reports_a_stable_failed_code(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。")
    outcome = _ingestor(graph, config=_DISABLED).ingest_memory(memory_id)
    assert (outcome.status, outcome.reason) == ("failed", "jkg_disabled")
    assert _counts(graph)["nodes"] == 0


def test_expected_jkg_error_is_failed_and_isolated_from_existing_graph(graph, add_memory, monkeypatch):
    ingestor = _ingestor(graph)
    good_id = add_memory("张三 负责 JarvisMind。")
    assert ingestor.ingest_memory(good_id).status == "processed"
    before = _counts(graph)

    bad_id = add_memory("李四 负责 Launch项目。")
    original = JkgService.create_edge

    def fail_for_bad(self, **kwargs):
        if kwargs.get("source_memory_ids") == [bad_id]:
            raise JkgInvalidEntityError("boom with private detail")
        return original(self, **kwargs)

    monkeypatch.setattr(JkgService, "create_edge", fail_for_bad)
    outcome = ingestor.ingest_memory(bad_id)

    assert (outcome.status, outcome.reason) == ("failed", "jkg_invalid_entity")
    assert "private detail" not in outcome.reason
    with graph.session() as session:
        still_linked = session.execute(
            text("SELECT COUNT(*) FROM jkg_edge_sources WHERE memory_id = :m"),
            {"m": good_id},
        ).scalar()
    assert still_linked == 1
    assert _counts(graph)["edges"] >= before["edges"]


def test_keyboard_interrupt_is_not_swallowed(graph, add_memory, monkeypatch):
    memory_id = add_memory("张三 负责 JarvisMind。")

    def interrupt(self, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(JkgService, "create_edge", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _ingestor(graph).ingest_memory(memory_id)


# ----------------------------------------------------- authority / digests


def test_authority_tables_are_unchanged_by_ingestion(graph, add_memory):
    # Insert every memory first so the snapshot is taken after all test-data
    # writes and only the ingestion itself is measured.
    ids = [
        add_memory("张三 负责 JarvisMind。"),
        add_memory("李四 参加 Launch。"),
        add_memory("今天天气很好。"),
        add_memory("张三 负责 JarvisMind。", status="DELETED"),
    ]
    ingestor = _ingestor(graph)
    before = _authority_snapshot(graph)
    for memory_id in ids:
        ingestor.ingest_memory(memory_id)
    assert _authority_snapshot(graph) == before


# --------------------------------------------------------- repository / service


def test_find_node_by_match_key_returns_the_matching_node(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。")
    _ingestor(graph).ingest_memory(memory_id)
    with graph.session() as session:
        node = JkgRepository(session).find_node_by_match_key("PROJECT", "jarvismind")
        assert node is not None and node.name == "JarvisMind"
        assert JkgRepository(session).find_node_by_match_key("PERSON", "jarvismind") is None
        assert JkgRepository(session).find_node_by_match_key("PROJECT", "missing") is None


def test_find_node_by_match_key_ignores_legacy_and_malformed_metadata(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。")
    service = JkgService(graph)
    legacy = service.upsert_node(entity_type="PROJECT", name="Legacy", source_memory_id=memory_id)
    malformed = service.upsert_node(entity_type="PROJECT", name="Broken", source_memory_id=memory_id)
    with graph.session() as session:
        session.execute(
            text("UPDATE jkg_nodes SET metadata_json = '{not json' WHERE node_id = :id"),
            {"id": malformed["node_id"]},
        )
    with graph.session() as session:
        repo = JkgRepository(session)
        assert repo.find_node_by_match_key("PROJECT", "legacy") is None
        assert repo.find_node_by_match_key("PROJECT", "broken") is None
        assert repo.find_node(legacy["entity_type"], legacy["normalized_key"]) is not None


def test_upsert_node_reports_whether_it_created_the_node(graph, add_memory):
    memory_id = add_memory("张三 负责 JarvisMind。")
    service = JkgService(graph)
    first = service.upsert_node(entity_type="PERSON", name="张三", source_memory_id=memory_id)
    second = service.upsert_node(entity_type="PERSON", name="张三", source_memory_id=memory_id)
    assert first["created"] is True
    assert second["created"] is False
    assert first["node_id"] == second["node_id"]
