"""JKG-INT-02 回归套件：只读服务层（节点状态过滤 + 有界多跳遍历）。

直接经 ``JkgCompositionAdapter(manager).query_service()`` 读取，数据经
``write_service()`` 真实写入。表结构只来自 ``_fixtures.jkg_schema_factory``。

覆盖：节点列表（类型 / 状态 / 分页 / 空图 / 边界）、遍历（跳数、方向、关系过滤、
环、硬上限截断、扫描预算、无效节点与边、读时遗忘、多来源）、治理脱敏、序列化、
只读性（不写库）、禁用态 / 缺表态。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.errors import NotFoundError, ValidationError
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg import query as jkg_query
from jarvismind.jkg.adapter import JkgDisabledError, JkgSchemaNotReadyError
from jarvismind.jkg.governance import sanitize_edge_payload, sanitize_node_payload
from jarvismind.jkg.schema import JKG_ENABLED_KEY

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)
_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, 5.0, 0.9, :status, :now, :now"
    ")"
)
_SET_MEMORY_STATUS = text("UPDATE memory_records SET status = :status WHERE id = :id")
_SET_NODE_STATUS = text("UPDATE jkg_nodes SET status = :status WHERE node_id = :id")

_CREDENTIAL_SHAPED = "api" + "_key: " + "sk-" + "live-" + "ABCDEF0123456789"
_CONTENT_MARKER = "MARKER" + "-JKG-INT-02-DO-NOT-LEAK"
_REDACTED = "[REDACTED]"


# ---------------------------------------------------------------- helpers
def _set_enabled(manager, value: str = "1") -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _insert_memory(manager, content: str, *, status: str = "ACTIVE") -> str:
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {"id": memory_id, "content": content, "status": status, "now": utc_now_iso()},
        )
    return memory_id


def _set_memory_status(manager, memory_id: str, status: str) -> None:
    with manager.session() as session:
        session.execute(_SET_MEMORY_STATUS, {"id": memory_id, "status": status})


def _set_node_status(manager, node_id: str, status: str) -> None:
    with manager.session() as session:
        session.execute(_SET_NODE_STATUS, {"id": node_id, "status": status})


def _ready(manager):
    """装表 + 启用，返回 (只读服务, 写入服务)。"""
    create_jkg_schema(manager)
    _set_enabled(manager, "1")
    adapter = JkgCompositionAdapter(manager)
    return adapter.query_service(), adapter.write_service()


def _node(writer, memory_id: str, entity_type: str, name: str) -> str:
    return writer.upsert_node(
        entity_type=entity_type, name=name, source_memory_id=memory_id
    )["node_id"]


def _edge(writer, memory_ids, subject: str, predicate: str, obj: str) -> str:
    return writer.create_edge(
        subject_id=subject,
        predicate=predicate,
        object_id=obj,
        source_memory_ids=list(memory_ids),
    )["edge_id"]


def _names(result) -> dict:
    return {entry["node"]["name"]: entry["hop"] for entry in result["nodes"]}


def _counts(manager) -> tuple:
    with manager.session() as session:
        return tuple(
            session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            for table in ("jkg_nodes", "jkg_edges", "jkg_edge_sources", "memory_records")
        )


def _chain(writer, memory_id: str, names):
    ids = [_node(writer, memory_id, "PERSON", name) for name in names]
    for left, right in zip(ids, ids[1:]):
        _edge(writer, [memory_id], left, "KNOWS", right)
    return ids


def _hub(writer, memory_id: str, leaves: int):
    hub = _node(writer, memory_id, "PERSON", "Hub")
    leaf_ids = []
    for index in range(leaves):
        leaf = _node(writer, memory_id, "PERSON", f"Leaf {index}")
        _edge(writer, [memory_id], hub, "KNOWS", leaf)
        leaf_ids.append(leaf)
    return hub, leaf_ids


# ================================================================ 节点列表
def test_get_node_reports_source_validity_labels(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "node label source")
    node_id = _node(writer, memory, "PERSON", "Alice")

    payload = reader.get_node(node_id)
    assert payload["display_name"] == "Alice"
    assert payload["source_memory_status"] == "ACTIVE"
    assert payload["source_memory_valid"] is True


def test_get_missing_node_raises_not_found(manager):
    reader, _ = _ready(manager)
    with pytest.raises(NotFoundError):
        reader.get_node("no-such-node")


def test_list_nodes_empty_graph_is_an_empty_page(manager):
    reader, _ = _ready(manager)
    result = reader.list_nodes()
    assert result["items"] == []
    assert result["total"] == 0


def test_list_nodes_filters_by_entity_type(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "type filter")
    _node(writer, memory, "PERSON", "Alice")
    _node(writer, memory, "PROJECT", "JarvisMind")

    persons = reader.list_nodes(entity_type="PERSON")
    assert persons["total"] == 1
    assert {item["entity_type"] for item in persons["items"]} == {"PERSON"}


def test_list_nodes_status_filter_matches_exactly(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "status filter")
    active = _node(writer, memory, "PERSON", "Alice")
    invalid = _node(writer, memory, "PERSON", "Bob")
    _set_node_status(manager, invalid, "INVALID")

    only_active = reader.list_nodes(status="ACTIVE")
    assert [item["node_id"] for item in only_active["items"]] == [active]
    assert only_active["total"] == 1

    only_invalid = reader.list_nodes(status="INVALID")
    assert [item["node_id"] for item in only_invalid["items"]] == [invalid]
    assert only_invalid["items"][0]["status"] == "INVALID"


def test_list_nodes_status_combines_with_type_filter(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "status and type")
    _node(writer, memory, "PERSON", "Alice")
    project = _node(writer, memory, "PROJECT", "JarvisMind")
    _set_node_status(manager, project, "INVALID")

    result = reader.list_nodes(entity_type="PROJECT", status="INVALID")
    assert result["total"] == 1
    assert result["items"][0]["node_id"] == project
    assert reader.list_nodes(entity_type="PERSON", status="INVALID")["total"] == 0


def test_list_nodes_rejects_unknown_status(manager):
    reader, _ = _ready(manager)
    with pytest.raises(ValidationError):
        reader.list_nodes(status="DELETED")


def test_list_nodes_status_none_keeps_previous_behaviour(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "status none")
    _node(writer, memory, "PERSON", "Alice")
    _node(writer, memory, "PERSON", "Bob")
    assert reader.list_nodes()["total"] == 2
    assert reader.list_nodes(status=None)["total"] == 2


def test_list_nodes_pagination_total_is_exact_and_pages_disjoint(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "paging")
    ids = {_node(writer, memory, "PERSON", name) for name in ("Ann", "Ben", "Cal")}

    first = reader.list_nodes(limit=2, offset=0)
    second = reader.list_nodes(limit=2, offset=2)
    assert first["total"] == 3 and second["total"] == 3
    got = [item["node_id"] for item in first["items"] + second["items"]]
    assert len(got) == len(set(got)) == 3
    assert set(got) == ids


def test_list_nodes_page_beyond_end_is_empty_with_real_total(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "beyond end")
    _node(writer, memory, "PERSON", "Ann")
    result = reader.list_nodes(limit=5, offset=50)
    assert result["items"] == []
    assert result["total"] == 1


def test_list_nodes_rejects_out_of_range_limit(manager):
    reader, _ = _ready(manager)
    with pytest.raises(ValidationError):
        reader.list_nodes(limit=0)
    with pytest.raises(ValidationError):
        reader.list_nodes(limit=jkg_query.MAX_PAGE_SIZE + 1)


def test_list_nodes_reports_forgotten_source_without_hiding(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "forgotten listing")
    _node(writer, memory, "PERSON", "Ghost")
    _set_memory_status(manager, memory, "DELETED")

    item = reader.list_nodes()["items"][0]
    assert item["name"] == "Ghost"
    assert item["source_memory_status"] == "DELETED"
    assert item["source_memory_valid"] is False


# ============================================================ 遍历：基本
def test_traverse_isolated_root_returns_only_itself(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "isolated")
    root = _node(writer, memory, "PERSON", "Solo")

    result = reader.traverse_nodes(root)
    assert [entry["hop"] for entry in result["nodes"]] == [0]
    assert result["edges"] == []
    assert result["truncated"] is False
    assert result["visited_nodes"] == 1
    assert result["visited_edges"] == 0


def test_traverse_one_hop_returns_direct_neighbours_with_hop_one(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "one hop")
    a, b, c = _chain(writer, memory, ["Aa", "Bb", "Cc"])

    result = reader.traverse_nodes(b, hops=1)
    assert _names(result) == {"Bb": 0, "Aa": 1, "Cc": 1}
    assert result["visited_edges"] == 2
    assert result["truncated"] is False


def test_traverse_expands_layer_by_layer(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "layers")
    _chain(writer, memory, ["Pa", "Pb", "Pc", "Pd"])
    root = reader.list_nodes(entity_type="PERSON")["items"]
    root_id = next(item["node_id"] for item in root if item["name"] == "Pa")

    two = reader.traverse_nodes(root_id, hops=2)
    assert _names(two) == {"Pa": 0, "Pb": 1, "Pc": 2}
    assert two["visited_edges"] == 2

    three = reader.traverse_nodes(root_id, hops=3)
    assert _names(three) == {"Pa": 0, "Pb": 1, "Pc": 2, "Pd": 3}


def test_traverse_respects_hops_and_reports_no_truncation_at_full_depth(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "full depth")
    a, *_ = _chain(writer, memory, ["Qa", "Qb", "Qc"])
    result = reader.traverse_nodes(a, hops=3)
    assert _names(result) == {"Qa": 0, "Qb": 1, "Qc": 2}
    assert result["truncated"] is False


@pytest.mark.parametrize("hops", [0, 4, -1, True])
def test_traverse_hops_outside_range_is_rejected(manager, hops):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "hops bounds")
    root = _node(writer, memory, "PERSON", "Hops")
    with pytest.raises(ValidationError):
        reader.traverse_nodes(root, hops=hops)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_nodes": 0},
        {"max_nodes": jkg_query.MAX_TRAVERSAL_NODES + 1},
        {"max_edges": 0},
        {"max_edges": jkg_query.MAX_TRAVERSAL_EDGES + 1},
        {"direction": "sideways"},
    ],
)
def test_traverse_bounds_and_direction_are_validated(manager, kwargs):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "bounds")
    root = _node(writer, memory, "PERSON", "Bounds")
    with pytest.raises(ValidationError):
        reader.traverse_nodes(root, **kwargs)


def test_traverse_hard_caps_are_the_documented_values():
    assert jkg_query.MAX_TRAVERSAL_HOPS == 3
    assert jkg_query.MAX_TRAVERSAL_NODES == 100
    assert jkg_query.MAX_TRAVERSAL_EDGES == 1000
    assert jkg_query.DEFAULT_TRAVERSAL_HOPS <= jkg_query.MAX_TRAVERSAL_HOPS
    assert jkg_query.DEFAULT_TRAVERSAL_NODES <= jkg_query.MAX_TRAVERSAL_NODES
    assert jkg_query.DEFAULT_TRAVERSAL_EDGES <= jkg_query.MAX_TRAVERSAL_EDGES


def test_traverse_missing_root_raises_not_found(manager):
    reader, _ = _ready(manager)
    with pytest.raises(NotFoundError):
        reader.traverse_nodes("no-such-root")


@pytest.mark.parametrize("node_id", ["", "   ", None])
def test_traverse_blank_root_id_is_rejected(manager, node_id):
    reader, _ = _ready(manager)
    with pytest.raises(ValidationError):
        reader.traverse_nodes(node_id)


def test_traverse_direction_outgoing_follows_only_outgoing_edges(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "direction")
    a = _node(writer, memory, "PERSON", "Dira")
    b = _node(writer, memory, "PERSON", "Dirb")
    c = _node(writer, memory, "PERSON", "Dirc")
    _edge(writer, [memory], a, "KNOWS", b)  # A -> B
    _edge(writer, [memory], c, "KNOWS", a)  # C -> A

    outgoing = reader.traverse_nodes(a, direction="outgoing")
    assert _names(outgoing) == {"Dira": 0, "Dirb": 1}
    incoming = reader.traverse_nodes(a, direction="incoming")
    assert _names(incoming) == {"Dira": 0, "Dirc": 1}
    both = reader.traverse_nodes(a, direction="both")
    assert _names(both) == {"Dira": 0, "Dirb": 1, "Dirc": 1}


def test_traverse_predicate_filter_keeps_only_that_relation(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "predicate")
    person = _node(writer, memory, "PERSON", "Preda")
    friend = _node(writer, memory, "PERSON", "Predb")
    project = _node(writer, memory, "PROJECT", "Predproj")
    _edge(writer, [memory], person, "KNOWS", friend)
    _edge(writer, [memory], person, "WORKS_ON", project)

    works = reader.traverse_nodes(person, predicate="WORKS_ON")
    assert _names(works) == {"Preda": 0, "Predproj": 1}
    assert {edge["predicate"] for edge in works["edges"]} == {"WORKS_ON"}


def test_traverse_unknown_predicate_is_rejected(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "bad predicate")
    root = _node(writer, memory, "PERSON", "Badp")
    with pytest.raises(ValidationError):
        reader.traverse_nodes(root, predicate="LIKES")


# ============================================================ 环与去重
def test_traverse_cycle_terminates_and_visits_each_node_once(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "cycle")
    a, b, c = _chain(writer, memory, ["Cya", "Cyb", "Cyc"])
    _edge(writer, [memory], c, "KNOWS", a)  # closes the ring A->B->C->A

    result = reader.traverse_nodes(a, hops=3, max_nodes=100, max_edges=100)
    # C reaches A directly through C->A, so it sits at its minimum hop (1).
    assert _names(result) == {"Cya": 0, "Cyb": 1, "Cyc": 1}
    assert result["visited_nodes"] == 3
    assert result["visited_edges"] == 3
    assert len({edge["edge_id"] for edge in result["edges"]}) == 3
    assert result["truncated"] is False


def test_traverse_two_node_back_edge_is_not_duplicated(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "two-way")
    a = _node(writer, memory, "PERSON", "Twoa")
    b = _node(writer, memory, "PERSON", "Twob")
    _edge(writer, [memory], a, "KNOWS", b)
    _edge(writer, [memory], b, "KNOWS", a)

    result = reader.traverse_nodes(a, hops=3)
    assert _names(result) == {"Twoa": 0, "Twob": 1}
    assert result["visited_edges"] == 2


# ============================================================ 硬上限
def test_traverse_max_nodes_truncates_and_never_exceeds_the_cap(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "max nodes")
    hub, _ = _hub(writer, memory, leaves=5)

    result = reader.traverse_nodes(hub, hops=1, max_nodes=3)
    assert result["visited_nodes"] == 3
    assert len(result["nodes"]) == 3
    assert result["truncated"] is True
    assert result["limits"]["max_nodes"] == 3


def test_traverse_max_nodes_exact_fit_is_not_truncated(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "exact fit")
    hub, _ = _hub(writer, memory, leaves=2)
    result = reader.traverse_nodes(hub, hops=1, max_nodes=3)
    assert result["visited_nodes"] == 3
    assert result["truncated"] is False


def test_traverse_max_edges_truncates_and_never_exceeds_the_cap(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "max edges")
    hub, _ = _hub(writer, memory, leaves=5)

    result = reader.traverse_nodes(hub, hops=1, max_edges=2, max_nodes=100)
    assert result["visited_edges"] == 2
    assert len(result["edges"]) == 2
    assert result["truncated"] is True


def test_traverse_scan_budget_exhaustion_is_reported_as_truncated(
    manager, monkeypatch
):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "scan budget")
    hub, _ = _hub(writer, memory, leaves=5)

    monkeypatch.setattr(jkg_query, "MAX_TRAVERSAL_SCAN", 2)
    result = reader.traverse_nodes(hub, hops=1, max_nodes=100, max_edges=100)
    assert result["visited_edges"] == 2
    assert result["truncated"] is True


def test_traverse_default_caps_are_applied_when_not_given(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "defaults")
    root = _node(writer, memory, "PERSON", "Defaults")
    result = reader.traverse_nodes(root)
    assert result["limits"] == {
        "hops": jkg_query.DEFAULT_TRAVERSAL_HOPS,
        "max_nodes": jkg_query.DEFAULT_TRAVERSAL_NODES,
        "max_edges": jkg_query.DEFAULT_TRAVERSAL_EDGES,
    }


# ============================================================ 有效性与遗忘
def test_traverse_hides_edge_without_live_source_by_default(manager):
    reader, writer = _ready(manager)
    live = _insert_memory(manager, "live")
    dead = _insert_memory(manager, "dead")
    a = _node(writer, live, "PERSON", "Vala")
    b = _node(writer, live, "PERSON", "Valb")
    _edge(writer, [live], a, "KNOWS", b)
    c = _node(writer, live, "PERSON", "Valc")
    gone = _edge(writer, [dead], a, "KNOWS", c)
    _set_memory_status(manager, dead, "DELETED")
    writer.sync_memory(dead)

    default = reader.traverse_nodes(a, hops=2)
    assert "Valc" not in _names(default)
    assert gone not in {edge["edge_id"] for edge in default["edges"]}


def test_traverse_valid_only_false_returns_invalid_edge_marked(manager):
    reader, writer = _ready(manager)
    live = _insert_memory(manager, "live false")
    dead = _insert_memory(manager, "dead false")
    a = _node(writer, live, "PERSON", "Vfa")
    c = _node(writer, live, "PERSON", "Vfc")
    gone = _edge(writer, [dead], a, "KNOWS", c)
    _set_memory_status(manager, dead, "DELETED")
    writer.sync_memory(dead)

    result = reader.traverse_nodes(a, valid_only=False)
    edges = {edge["edge_id"]: edge for edge in result["edges"]}
    assert gone in edges
    assert edges[gone]["is_valid"] is False
    assert edges[gone]["valid_source_count"] == 0
    assert _names(result)["Vfc"] == 1


def test_traverse_read_time_forgetting_hides_edge_before_any_sync(manager):
    """读时遗忘：只改权威表状态，不调用生命周期同步，边也应立即被隐藏。"""
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "read time")
    a = _node(writer, memory, "PERSON", "Rta")
    b = _node(writer, memory, "PERSON", "Rtb")
    edge = _edge(writer, [memory], a, "KNOWS", b)
    assert edge in {e["edge_id"] for e in reader.traverse_nodes(a)["edges"]}

    _set_memory_status(manager, memory, "DELETED")
    result = reader.traverse_nodes(a)
    assert edge not in {e["edge_id"] for e in result["edges"]}
    assert _names(result) == {"Rta": 0}


def test_traverse_node_with_forgotten_source_is_labelled_not_hidden(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "forgotten node")
    root = _node(writer, memory, "PERSON", "Fnroot")
    _set_memory_status(manager, memory, "DELETED")

    result = reader.traverse_nodes(root)
    entry = result["nodes"][0]
    assert entry["node"]["node_id"] == root
    assert entry["node"]["source_memory_status"] == "DELETED"
    assert entry["node"]["source_memory_valid"] is False


def test_traverse_multi_source_edge_survives_one_forgotten_source(manager):
    reader, writer = _ready(manager)
    memory_a = _insert_memory(manager, "multi a")
    memory_b = _insert_memory(manager, "multi b")
    left = _node(writer, memory_a, "PERSON", "Mleft")
    right = _node(writer, memory_b, "PROJECT", "Mright")
    edge = _edge(writer, [memory_a, memory_b], left, "WORKS_ON", right)
    _set_memory_status(manager, memory_a, "DELETED")

    result = reader.traverse_nodes(left)
    item = next(e for e in result["edges"] if e["edge_id"] == edge)
    assert item["is_valid"] is True
    assert item["source_count"] == 2
    assert item["valid_source_count"] == 1
    assert _names(result) == {"Mleft": 0, "Mright": 1}


# ============================================================ 序列化
def test_traverse_payloads_carry_the_documented_fields(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "serialise")
    a = _node(writer, memory, "PERSON", "Sera")
    b = _node(writer, memory, "PERSON", "Serb")
    _edge(writer, [memory], a, "KNOWS", b)

    result = reader.traverse_nodes(a)
    node = result["nodes"][0]["node"]
    for key in ("node_id", "entity_type", "name", "display_name", "source_memory_id",
                "status", "source_memory_status", "source_memory_valid"):
        assert key in node
    edge = result["edges"][0]
    for key in ("edge_id", "predicate", "relation_type", "subject_id", "object_id",
                "source_count", "valid_source_count", "is_valid"):
        assert key in edge
    assert edge["relation_type"] == edge["predicate"] == "KNOWS"
    assert "sources" not in edge


def test_traverse_output_is_deterministic(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "determinism")
    hub, _ = _hub(writer, memory, leaves=4)
    first = reader.traverse_nodes(hub, hops=2)
    second = reader.traverse_nodes(hub, hops=2)
    assert first == second


# ============================================================ 只读与治理
def test_traverse_and_list_never_write_to_the_database(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "read only")
    hub, leaves = _hub(writer, memory, leaves=3)
    before = _counts(manager)

    reader.traverse_nodes(hub, hops=3, valid_only=False)
    reader.traverse_nodes(leaves[0], direction="incoming")
    reader.list_nodes(status="ACTIVE")
    reader.list_edges(node_id=hub, valid_only=False)

    assert _counts(manager) == before


def test_traverse_output_never_contains_memory_content(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "prefix " + _CONTENT_MARKER + " suffix")
    hub, _ = _hub(writer, memory, leaves=2)
    result = reader.traverse_nodes(hub, hops=2)
    assert _CONTENT_MARKER not in repr(result)


def test_sanitizer_withholds_credential_shaped_labels_on_traversal_output(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "credential traversal")
    root = writer.upsert_node(
        entity_type="PERSON",
        name=_CREDENTIAL_SHAPED,
        source_memory_id=memory,
        metadata={"note": _CREDENTIAL_SHAPED},
    )["node_id"]
    friend = _node(writer, memory, "PERSON", "Credfriend")
    _edge(writer, [memory], root, "KNOWS", friend)

    result = reader.traverse_nodes(root)
    sanitized_nodes = [sanitize_node_payload(entry["node"]) for entry in result["nodes"]]
    sanitized_edges = [sanitize_edge_payload(edge) for edge in result["edges"]]
    root_clean = sanitized_nodes[0]
    assert root_clean["name"] == _REDACTED
    assert root_clean["metadata"] is None
    assert root_clean["sensitivity"] == "CREDENTIAL"
    assert "ABCDEF0123456789" not in repr(sanitized_nodes)
    assert "ABCDEF0123456789" not in repr(sanitized_edges)


def test_sanitizer_keeps_ordinary_traversal_labels_public(manager):
    reader, writer = _ready(manager)
    memory = _insert_memory(manager, "public traversal")
    a, b = _chain(writer, memory, ["Pubone", "Pubtwo"])
    result = reader.traverse_nodes(a)
    names = [sanitize_node_payload(entry["node"])["name"] for entry in result["nodes"]]
    assert names == ["Pubone", "Pubtwo"]
    assert all(
        sanitize_edge_payload(edge)["sensitivity"] == "PUBLIC"
        for edge in result["edges"]
    )


# ============================================================ 禁用 / 缺表
def test_disabled_switch_blocks_traversal_before_any_read(manager):
    create_jkg_schema(manager)  # 表存在但开关关闭
    with pytest.raises(JkgDisabledError):
        JkgCompositionAdapter(manager).query_service().traverse_nodes("anything")


def test_missing_schema_blocks_traversal_and_creates_nothing(manager):
    _set_enabled(manager, "1")
    with pytest.raises(JkgSchemaNotReadyError):
        JkgCompositionAdapter(manager).query_service().traverse_nodes("anything")
    with manager.session() as session:
        from jarvismind.jkg.schema import present_jkg_tables

        assert present_jkg_tables(session) == []
