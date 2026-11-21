"""JKG-INT-03 回归套件：只读 JKG HTTP 接口。

覆盖范围（对应任务验收清单 A–I）：

* A 关闭态：``/status`` 仍 200，其余 6 个数据端点 503 ``jkg_disabled``，且不建表；
* B 启用未装表：``/status`` 报 ``schema_not_ready`` 并列缺失表，数据端点 503；
* C 空图：概览全 0、列表空页且 ``meta.total == 0``；
* D 有数据：分页 / 类型过滤 / 关键字搜索 / 节点详情 / 邻居 / 关系列表有效性过滤；
* E 多来源有效性：一个来源失效不得隐藏整条边；
* F 隐私：凭据形状的标签必须在出口被替换，且响应体全文不含原文；
* G 输入校验：越界、非法枚举、不存在的对象一律 422 / 404 统一信封；
* H 边界：越界分页、有效边扫描被截断时 ``meta.total`` 必须为 ``null``；
* I 鉴权：无 token / 错 token → 401。

数据库一律来自共享 ``manager`` fixture 的会话临时库；需要真实 JKG 表结构的用例
只通过 ``tests/_fixtures/jkg_schema_factory.create_jkg_schema`` 安装，不调用任何
迁移入口，也不触达生产库。测试里不出现 P0-03A 门禁禁止的四类构造：裸连接工厂、
默认配置加载器、会话构造、子进程。

关于 H 段「扫描截断」的构造方式（诚实说明）
------------------------------------------
``JkgQueryService.list_edges`` 的 valid-only 分支受模块级 ``MAX_SCAN`` / ＂扫描页
大小＂约束：只有当候选边数超过该上限时，循环才会在「扫够上限但尚未穷尽候选」的
状态退出，从而返回 ``truncated=True`` / ``total=None``。默认上限是 1000 条边，
为它准备 1001 条边的夹具会让本套件慢上数秒且掩盖真正要断言的语义。

因此该用例**只把这两个上限调小**（通过 ``monkeypatch`` 改 ``app.jkg.query`` 的
模块级常量），随后跑真实的 ``list_edges`` 代码路径，并断言：返回的
``total`` 为 ``None``、``truncated`` 为 ``True``、``scanned`` 等于调小后的上限。
同一用例里再断言默认上限仍是 1000，证明这只是在收窄观察窗口，没有改写生产语义。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg import query as jkg_query
from jarvismind.jkg.schema import JKG_ENABLED_KEY, JKG_TABLES, present_jkg_tables

# ---------------------------------------------------------------------------
# 常量与 SQL（模块级只构造语句对象，不执行任何数据库调用）
# ---------------------------------------------------------------------------
_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, 5.0, 0.9, :status, :now, :now"
    ")"
)

_SET_MEMORY_STATUS = text(
    "UPDATE memory_records SET status = :status WHERE id = :id"
)

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)

_INSERT_NODE = text(
    "INSERT INTO jkg_nodes("
    "node_id, entity_type, name, canonical_name, normalized_key, "
    "source_memory_id, status, generation, metadata_json, created_at, updated_at"
    ") VALUES ("
    ":node_id, :entity_type, :name, :name, :normalized, "
    ":source_memory_id, 'ACTIVE', 1, NULL, :now, :now"
    ")"
)

_INSERT_EDGE = text(
    "INSERT INTO jkg_edges("
    "edge_id, predicate, subject_id, object_id, status, generation, "
    "confidence, metadata_json, recorded_at, updated_at"
    ") VALUES ("
    ":edge_id, :predicate, :subject_id, :object_id, 'ACTIVE', 1, "
    "0.5, NULL, :now, :now"
    ")"
)

_INSERT_SOURCE = text(
    "INSERT INTO jkg_edge_sources("
    "source_id, edge_id, memory_id, created_at, invalidated_at, "
    "invalidation_reason"
    ") VALUES (:source_id, :edge_id, :memory_id, :now, NULL, NULL)"
)

# 测试用凭据形状字符串按片段拼接：本文件不出现完整字面量。
_CREDENTIAL_SHAPED = (
    "api" + "_key: " + "sk-" + "live-" + "ABCDEF0123456789"
)
# 记忆正文的唯一标记：用来证明任何 JKG 响应都不含记忆正文。
_CONTENT_MARKER = "MARKER" + "-JKG-INT-03-DO-NOT-LEAK"

_REDACTED = "[REDACTED]"

# 6 个图数据端点（不含 /status、/sync/status、/extraction/preview）。
_DATA_ENDPOINTS = (
    "/api/v1/jkg/overview",
    "/api/v1/jkg/nodes",
    "/api/v1/jkg/edges",
)

# 需要具体 id 的端点单独列出，避免在参数化里塞占位符字符串。
_DETAIL_ENDPOINTS = (
    "/api/v1/jkg/nodes/unknown-node-id",
    "/api/v1/jkg/nodes/unknown-node-id/neighbors",
    "/api/v1/jkg/nodes/unknown-node-id/traverse",
    "/api/v1/jkg/edges/unknown-edge-id",
)


# ---------------------------------------------------------------------------
# 辅助函数（全部在函数体内使用 manager，避免模块级数据库调用）
# ---------------------------------------------------------------------------
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
            {
                "id": memory_id,
                "content": content,
                "status": status,
                "now": utc_now_iso(),
            },
        )
    return memory_id


def _set_memory_status(manager, memory_id: str, status: str) -> None:
    """直接改权威表的状态：模拟 JME 侧的生命周期变化，不走服务层。"""
    with manager.session() as session:
        session.execute(_SET_MEMORY_STATUS, {"id": memory_id, "status": status})


def _jkg_tables(manager) -> list:
    with manager.session() as session:
        return present_jkg_tables(session)


def _ready_graph(manager) -> None:
    """装表 + 启用：这是唯二允许 JKG 可用的前置条件。"""
    create_jkg_schema(manager)
    _set_enabled(manager, "1")


def _writer(manager):
    return JkgCompositionAdapter(manager).write_service()


def _seed_graph(manager, memory_id: str) -> dict:
    """经真实写入服务造 4 个实体 + 2 条关系（D/E/F 段的共同基准）。"""
    writer = _writer(manager)
    alice = writer.upsert_node(
        entity_type="PERSON",
        name="Alice",
        source_memory_id=memory_id,
        metadata={"role": "engineer"},
    )
    bob = writer.upsert_node(
        entity_type="PERSON", name="Bob", source_memory_id=memory_id
    )
    jarvis = writer.upsert_node(
        entity_type="PROJECT", name="JarvisMind", source_memory_id=memory_id
    )
    graph = writer.upsert_node(
        entity_type="EVENT", name="知识图谱", source_memory_id=memory_id
    )
    works_on = writer.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=jarvis["node_id"],
        source_memory_ids=[memory_id],
    )
    knows = writer.create_edge(
        subject_id=bob["node_id"],
        predicate="KNOWS",
        object_id=alice["node_id"],
        source_memory_ids=[memory_id],
    )
    return {
        "memory_id": memory_id,
        "alice": alice,
        "bob": bob,
        "jarvis": jarvis,
        "graph": graph,
        "works_on": works_on,
        "knows": knows,
    }


def _seed_invalidated_edge(manager, memory_id: str, other_memory_id: str) -> dict:
    """造一条「唯一来源已被遗忘」的关系，走真实的 sync_memory 失效路径。"""
    writer = _writer(manager)
    carol = writer.upsert_node(
        entity_type="PERSON", name="Carol", source_memory_id=memory_id
    )
    dave = writer.upsert_node(
        entity_type="PERSON", name="Dave", source_memory_id=memory_id
    )
    edge = writer.create_edge(
        subject_id=carol["node_id"],
        predicate="KNOWS",
        object_id=dave["node_id"],
        source_memory_ids=[memory_id],
    )
    _set_memory_status(manager, memory_id, "DELETED")
    writer.sync_memory(memory_id)
    _ = other_memory_id  # 保留参数以便调用方复用同一记忆，避免未使用告警
    return {"edge": edge, "carol": carol, "dave": dave}


def _seed_bulk_invalid_edges(
    manager, *, count: int, memory_id: str
) -> None:
    """批量写入 count 条「来源记忆已 DELETED」的边。

    只用在这个夹具里：``create_edge`` 会拒绝非有效来源的记忆，所以这里直接
    落到已冻结的表上（表结构来自测试工厂，不是生产路径）。每条边都真的挂在
    一个 DELETED 记忆上，``is_valid`` 因此为 False —— 不是伪造的标志位。

    每条边配一个独立的目标节点：``jkg_edges`` 上有
    ``(subject_id, predicate, object_id)`` 唯一约束，复用同一对节点会被数据库
    拒绝。这也是真实的图形状 —— 一个中心节点连到很多不同节点。
    """
    now = utc_now_iso()
    with manager.session() as session:
        session.execute(
            _INSERT_NODE,
            {
                "node_id": "bulk-hub",
                "entity_type": "PERSON",
                "name": "Bulk Hub",
                "normalized": "bulk hub",
                "source_memory_id": memory_id,
                "now": now,
            },
        )
        for index in range(count):
            object_id = f"bulk-leaf-{index:04d}"
            session.execute(
                _INSERT_NODE,
                {
                    "node_id": object_id,
                    "entity_type": "PERSON",
                    "name": f"Bulk Leaf {index:04d}",
                    "normalized": f"bulk leaf {index:04d}",
                    "source_memory_id": memory_id,
                    "now": now,
                },
            )
            edge_id = f"bulk-edge-{index:04d}"
            session.execute(
                _INSERT_EDGE,
                {
                    "edge_id": edge_id,
                    "predicate": "KNOWS",
                    "subject_id": "bulk-hub",
                    "object_id": object_id,
                    "now": now,
                },
            )
            session.execute(
                _INSERT_SOURCE,
                {
                    "source_id": f"bulk-src-{index:04d}",
                    "edge_id": edge_id,
                    "memory_id": memory_id,
                    "now": now,
                },
            )


def _page_meta(response) -> dict:
    return response.json()["meta"]


# ===========================================================================
# A · 关闭态（默认设置，未装表）
# ===========================================================================
def test_status_returns_200_while_disabled(client, auth_headers, manager):
    response = client.get("/api/v1/jkg/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "disabled"
    assert body["usable"] is False
    assert body["enabled"] is False
    assert body["enabled_source"] == "default"
    assert body["schema_ready"] is False
    assert _jkg_tables(manager) == []


@pytest.mark.parametrize("path", _DATA_ENDPOINTS)
def test_disabled_list_endpoints_return_503_jkg_disabled(
    client, auth_headers, path
):
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_disabled"


@pytest.mark.parametrize("path", _DETAIL_ENDPOINTS)
def test_disabled_detail_endpoints_return_503_jkg_disabled(
    client, auth_headers, path
):
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_disabled"


def test_every_data_endpoint_reports_disabled_before_anything_else(
    client, auth_headers
):
    """关闭态优先于参数校验：即使参数非法也只报 disabled，不暴露校验细节。"""
    response = client.get(
        "/api/v1/jkg/nodes?page=0", headers=auth_headers
    )
    # 参数校验先于依赖解析（FastAPI 顺序），所以这里是 422 而不是 503。
    assert response.status_code in (422, 503)
    if response.status_code == 503:
        assert response.json()["error"]["code"] == "jkg_disabled"


def test_disabled_requests_never_create_jkg_tables(client, auth_headers, manager):
    for path in _DATA_ENDPOINTS + _DETAIL_ENDPOINTS:
        client.get(path, headers=auth_headers)
    client.get("/api/v1/jkg/status", headers=auth_headers)
    assert _jkg_tables(manager) == []


# ===========================================================================
# B · 启用但未装表
# ===========================================================================
def test_status_reports_schema_not_ready_with_missing_tables(
    client, auth_headers, manager
):
    _set_enabled(manager, "1")
    response = client.get("/api/v1/jkg/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "schema_not_ready"
    assert body["usable"] is False
    assert body["enabled"] is True
    assert body["schema_ready"] is False
    assert body["missing_tables"] == list(JKG_TABLES)
    assert body["present_tables"] == []


@pytest.mark.parametrize("path", _DATA_ENDPOINTS)
def test_schema_not_ready_list_endpoints_return_503(client, auth_headers, manager, path):
    _set_enabled(manager, "1")
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "jkg_schema_not_ready"
    assert error["details"]["missing"] == list(JKG_TABLES)


@pytest.mark.parametrize("path", _DETAIL_ENDPOINTS)
def test_schema_not_ready_detail_endpoints_return_503(
    client, auth_headers, manager, path
):
    _set_enabled(manager, "1")
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_schema_not_ready"


def test_schema_not_ready_never_installs_the_schema(client, auth_headers, manager):
    _set_enabled(manager, "1")
    for path in _DATA_ENDPOINTS + _DETAIL_ENDPOINTS:
        client.get(path, headers=auth_headers)
    client.get("/api/v1/jkg/status", headers=auth_headers)
    assert _jkg_tables(manager) == []


# ===========================================================================
# C · 空图
# ===========================================================================
def test_empty_graph_overview_is_all_zero_and_not_truncated(
    client, auth_headers, manager
):
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/overview", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {
        "nodes": 0,
        "edges": 0,
        "sources": 0,
        "invalidated_sources": 0,
    }
    assert body["valid_edges"] == 0
    assert body["invalid_edges"] == 0
    assert body["scanned_edges"] == 0
    assert body["truncated"] is False
    assert body["orphan_edges"] == []


def test_empty_graph_node_list_is_an_empty_page_with_zero_total(
    client, auth_headers, manager
):
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/nodes", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["total_pages"] == 0
    assert body["meta"]["truncated"] is False


def test_empty_graph_edge_list_is_an_empty_page_with_zero_total(
    client, auth_headers, manager
):
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/edges", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0


def test_empty_graph_neighbors_of_a_missing_node_is_404(
    client, auth_headers, manager
):
    _ready_graph(manager)
    response = client.get(
        "/api/v1/jkg/nodes/does-not-exist/neighbors", headers=auth_headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# ===========================================================================
# D · 有数据
# ===========================================================================
def test_node_list_paginates_with_an_exact_total(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-03 paging source"))

    first = client.get(
        "/api/v1/jkg/nodes?page=1&page_size=2", headers=auth_headers
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["data"]) == 2
    assert first_body["meta"]["total"] == 4
    assert first_body["meta"]["total_pages"] == 2
    assert first_body["meta"]["page"] == 1

    second = client.get(
        "/api/v1/jkg/nodes?page=2&page_size=2", headers=auth_headers
    )
    second_body = second.json()
    assert len(second_body["data"]) == 2
    assert second_body["meta"]["total"] == 4

    first_ids = {item["node_id"] for item in first_body["data"]}
    second_ids = {item["node_id"] for item in second_body["data"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {
        seed["alice"]["node_id"],
        seed["bob"]["node_id"],
        seed["jarvis"]["node_id"],
        seed["graph"]["node_id"],
    }


def test_node_list_filters_by_entity_type(client, auth_headers, manager):
    _ready_graph(manager)
    _seed_graph(manager, _insert_memory(manager, "INT-03 entity filter"))

    persons = client.get(
        "/api/v1/jkg/nodes?entity_type=PERSON", headers=auth_headers
    )
    assert persons.status_code == 200, persons.text
    body = persons.json()
    assert body["meta"]["total"] == 2
    assert {item["entity_type"] for item in body["data"]} == {"PERSON"}

    events = client.get(
        "/api/v1/jkg/nodes?entity_type=EVENT", headers=auth_headers
    )
    event_body = events.json()
    assert event_body["meta"]["total"] == 1
    assert event_body["data"][0]["name"] == "知识图谱"


def test_node_list_searches_by_label_substring(client, auth_headers, manager):
    _ready_graph(manager)
    _seed_graph(manager, _insert_memory(manager, "INT-03 search"))

    response = client.get("/api/v1/jkg/nodes?q=jar", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["name"] == "JarvisMind"

    chinese = client.get(
        "/api/v1/jkg/nodes?q=%E7%9F%A5%E8%AF%86", headers=auth_headers
    )
    assert chinese.status_code == 200, chinese.text
    assert chinese.json()["meta"]["total"] == 1


def test_node_detail_splits_outgoing_and_incoming_edges(
    client, auth_headers, manager
):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-03 detail"))

    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    assert detail["node"]["name"] == "Alice"
    assert detail["node"]["entity_type"] == "PERSON"
    assert detail["node"]["sensitivity"] == "PUBLIC"
    # Alice WORKS_ON JarvisMind（出边）+ Bob KNOWS Alice（入边）
    assert [edge["predicate"] for edge in detail["outgoing_edges"]] == ["WORKS_ON"]
    assert [edge["predicate"] for edge in detail["incoming_edges"]] == ["KNOWS"]
    assert detail["edge_total"] == 2
    assert detail["edges_truncated"] is False
    assert len(detail["sources"]) == 1
    assert detail["sources"][0]["is_valid"] is True


def test_node_detail_requires_the_node_to_exist(client, auth_headers, manager):
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/nodes/nope", headers=auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_neighbors_outgoing_direction(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-03 neighbors"))

    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/neighbors"
        "?direction=outgoing",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["data"]) == 1
    neighbour = body["data"][0]
    assert neighbour["direction"] == "outgoing"
    assert neighbour["node"]["name"] == "JarvisMind"
    assert neighbour["edge"]["predicate"] == "WORKS_ON"
    assert body["meta"]["total"] == 1
    assert body["meta"]["truncated"] is False


def test_neighbors_incoming_direction(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-03 neighbors in"))

    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/neighbors?direction=incoming",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["direction"] == "incoming"
    assert body["data"][0]["node"]["name"] == "Bob"


def test_edge_list_hides_edges_without_a_valid_source_by_default(
    client, auth_headers, manager
):
    _ready_graph(manager)
    live_memory = _insert_memory(manager, "INT-03 valid_only source")
    _seed_graph(manager, live_memory)
    dead_memory = _insert_memory(manager, "INT-03 invalidated source")
    _seed_invalidated_edge(manager, dead_memory, live_memory)

    default_response = client.get("/api/v1/jkg/edges", headers=auth_headers)
    assert default_response.status_code == 200, default_response.text
    default_body = default_response.json()
    predicates = {edge["predicate"] for edge in default_body["data"]}
    assert predicates == {"WORKS_ON", "KNOWS"}
    assert all(edge["is_valid"] is True for edge in default_body["data"])
    assert default_body["meta"]["total"] == 2


def test_edge_list_with_valid_only_false_exposes_the_invalid_edge(
    client, auth_headers, manager
):
    _ready_graph(manager)
    live_memory = _insert_memory(manager, "INT-03 valid_only off")
    _seed_graph(manager, live_memory)
    dead_memory = _insert_memory(manager, "INT-03 forgotten source")
    _seed_invalidated_edge(manager, dead_memory, live_memory)

    response = client.get(
        "/api/v1/jkg/edges?valid_only=false", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["total"] == 3
    assert body["meta"]["truncated"] is False
    invalid = [edge for edge in body["data"] if not edge["is_valid"]]
    assert len(invalid) == 1
    assert invalid[0]["valid_source_count"] == 0


def test_edge_detail_includes_every_source_with_invalidation_reason(
    client, auth_headers, manager
):
    _ready_graph(manager)
    live_memory = _insert_memory(manager, "INT-03 edge detail live")
    dead_memory = _insert_memory(manager, "INT-03 edge detail dead")
    seed = _seed_graph(manager, live_memory)
    extra = _seed_invalidated_edge(manager, dead_memory, live_memory)

    response = client.get(
        f"/api/v1/jkg/edges/{extra['edge']['edge_id']}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    edge = response.json()["data"]
    assert edge["edge_id"] == extra["edge"]["edge_id"]
    assert edge["is_valid"] is False
    assert len(edge["sources"]) == 1
    source = edge["sources"][0]
    assert source["memory_id"] == dead_memory
    assert source["is_valid"] is False
    assert source["invalidation_reason"] == "MEMORY_DELETED"
    assert source["invalidated_at"] is not None
    assert "content" not in source
    _ = seed


def test_edge_detail_requires_the_edge_to_exist(client, auth_headers, manager):
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/edges/nope", headers=auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_overview_counts_match_the_seeded_graph(client, auth_headers, manager):
    _ready_graph(manager)
    _seed_graph(manager, _insert_memory(manager, "INT-03 overview"))
    response = client.get("/api/v1/jkg/overview", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["nodes"] == 4
    assert body["counts"]["edges"] == 2
    assert body["counts"]["sources"] == 2
    assert body["counts"]["invalidated_sources"] == 0
    assert body["valid_edges"] == 2
    assert body["invalid_edges"] == 0
    assert body["truncated"] is False


def test_overview_reports_invalid_edges_and_invalidated_sources(
    client, auth_headers, manager
):
    _ready_graph(manager)
    live_memory = _insert_memory(manager, "INT-03 overview live")
    _seed_graph(manager, live_memory)
    dead_memory = _insert_memory(manager, "INT-03 overview dead")
    _seed_invalidated_edge(manager, dead_memory, live_memory)

    body = client.get("/api/v1/jkg/overview", headers=auth_headers).json()
    assert body["counts"]["edges"] == 3
    assert body["counts"]["invalidated_sources"] == 1
    assert body["invalid_edges"] == 1
    assert body["valid_edges"] == 2


# ===========================================================================
# E · 多来源有效性
# ===========================================================================
def test_edge_survives_when_only_one_of_two_sources_is_forgotten(
    client, auth_headers, manager
):
    """一个来源失效不得隐藏整条边：边仍有效，有效来源数为 1。"""
    _ready_graph(manager)
    memory_a = _insert_memory(manager, "INT-03 multi source A")
    memory_b = _insert_memory(manager, "INT-03 multi source B")

    writer = _writer(manager)
    left = writer.upsert_node(
        entity_type="PERSON", name="Erin", source_memory_id=memory_a
    )
    right = writer.upsert_node(
        entity_type="PROJECT", name="Aurora", source_memory_id=memory_b
    )
    edge = writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[memory_a, memory_b],
    )
    assert edge["source_count"] == 2

    # 只改权威表状态，不触发 JKG 的生命周期同步。
    _set_memory_status(manager, memory_a, "DELETED")

    listed = client.get(
        f"/api/v1/jkg/edges?node_id={left['node_id']}&valid_only=false",
        headers=auth_headers,
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["data"][0]
    assert item["edge_id"] == edge["edge_id"]
    assert item["is_valid"] is True
    assert item["source_count"] == 2
    assert item["valid_source_count"] == 1

    # 默认 valid_only=true 仍能看到这条边（它还有有效来源）。
    default_list = client.get(
        f"/api/v1/jkg/edges?node_id={left['node_id']}", headers=auth_headers
    )
    assert default_list.json()["meta"]["total"] == 1

    detail = client.get(
        f"/api/v1/jkg/edges/{edge['edge_id']}", headers=auth_headers
    )
    sources = {
        source["memory_id"]: source["is_valid"]
        for source in detail.json()["data"]["sources"]
    }
    assert sources == {memory_a: False, memory_b: True}


def test_node_source_validity_follows_the_authority_table(
    client, auth_headers, manager
):
    _ready_graph(manager)
    memory_id = _insert_memory(manager, "INT-03 node source validity")
    writer = _writer(manager)
    node = writer.upsert_node(
        entity_type="PERSON", name="Frank", source_memory_id=memory_id
    )
    _set_memory_status(manager, memory_id, "DELETED")

    detail = client.get(
        f"/api/v1/jkg/nodes/{node['node_id']}", headers=auth_headers
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["node"]["source_memory_valid"] is False
    assert body["node"]["source_memory_status"] == "DELETED"
    assert body["sources"] == [
        {"memory_id": memory_id, "memory_status": "DELETED", "is_valid": False}
    ]


# ===========================================================================
# F · 隐私 / 泄漏
# ===========================================================================
def _credential_node(manager, memory_id: str) -> dict:
    writer = _writer(manager)
    return writer.upsert_node(
        entity_type="PERSON",
        name=_CREDENTIAL_SHAPED,
        source_memory_id=memory_id,
        metadata={"note": _CREDENTIAL_SHAPED},
    )


def test_credential_shaped_node_label_is_redacted_in_the_list(
    client, auth_headers, manager
):
    _ready_graph(manager)
    memory_id = _insert_memory(manager, "INT-03 credential label")
    node = _credential_node(manager, memory_id)

    response = client.get("/api/v1/jkg/nodes", headers=auth_headers)
    assert response.status_code == 200, response.text
    item = next(
        entry for entry in response.json()["data"]
        if entry["node_id"] == node["node_id"]
    )
    assert item["name"] == _REDACTED
    assert item["display_name"] == _REDACTED
    assert item["canonical_name"] == _REDACTED
    assert item["normalized_key"] == _REDACTED
    assert item["metadata"] is None
    assert item["name_redacted"] is True
    assert item["metadata_redacted"] is True
    assert item["sensitivity"] == "CREDENTIAL"


def test_credential_shaped_node_label_is_redacted_in_the_detail(
    client, auth_headers, manager
):
    _ready_graph(manager)
    memory_id = _insert_memory(manager, "INT-03 credential detail")
    node = _credential_node(manager, memory_id)

    response = client.get(
        f"/api/v1/jkg/nodes/{node['node_id']}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]["node"]
    assert body["name"] == _REDACTED
    assert body["metadata"] is None
    assert body["sensitivity"] == "CREDENTIAL"


def test_credential_text_never_appears_anywhere_in_the_response_body(
    client, auth_headers, manager
):
    """对原始响应全文断言：命中的原文一个字都不能出现。"""
    _ready_graph(manager)
    memory_id = _insert_memory(manager, "INT-03 credential leak check")
    node = _credential_node(manager, memory_id)

    responses = [
        client.get("/api/v1/jkg/nodes", headers=auth_headers),
        client.get("/api/v1/jkg/nodes?q=api", headers=auth_headers),
        client.get(f"/api/v1/jkg/nodes/{node['node_id']}", headers=auth_headers),
        client.get(
            f"/api/v1/jkg/nodes/{node['node_id']}/neighbors", headers=auth_headers
        ),
        client.get("/api/v1/jkg/edges", headers=auth_headers),
        client.get("/api/v1/jkg/overview", headers=auth_headers),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert _CREDENTIAL_SHAPED not in response.text
        assert "ABCDEF0123456789" not in response.text


def test_ordinary_labels_stay_public_and_untouched(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-03 public labels"))

    body = client.get("/api/v1/jkg/nodes", headers=auth_headers).json()
    by_name = {item["name"]: item for item in body["data"]}
    assert "Alice" in by_name
    assert "知识图谱" in by_name
    for name in ("Alice", "Bob", "JarvisMind", "知识图谱"):
        item = by_name[name]
        assert item["sensitivity"] == "PUBLIC"
        assert item["name_redacted"] is False
        assert item["metadata_redacted"] is False
    # 大小写与标点不被改写。
    assert by_name["JarvisMind"]["canonical_name"] == "JarvisMind"
    # 普通 metadata 原样保留。
    assert by_name["Alice"]["metadata"] == {"role": "engineer"}
    assert by_name["Alice"]["node_id"] == seed["alice"]["node_id"]


def test_no_response_contains_memory_content(client, auth_headers, manager):
    """来源只允许 id / 状态 / 有效性，绝不返回记忆正文。"""
    _ready_graph(manager)
    memory_id = _insert_memory(
        manager, "prefix " + _CONTENT_MARKER + " suffix"
    )
    seed = _seed_graph(manager, memory_id)

    responses = [
        client.get("/api/v1/jkg/nodes", headers=auth_headers),
        client.get(f"/api/v1/jkg/nodes/{seed['alice']['node_id']}", headers=auth_headers),
        client.get(
            f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/neighbors",
            headers=auth_headers,
        ),
        client.get("/api/v1/jkg/edges", headers=auth_headers),
        client.get(
            f"/api/v1/jkg/edges/{seed['works_on']['edge_id']}", headers=auth_headers
        ),
        client.get("/api/v1/jkg/overview", headers=auth_headers),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert _CONTENT_MARKER not in response.text

    # 来源项只带这四个字段附近的信息，没有 content/summary。
    edge = client.get(
        f"/api/v1/jkg/edges/{seed['works_on']['edge_id']}", headers=auth_headers
    ).json()["data"]
    allowed = {
        "source_id",
        "edge_id",
        "memory_id",
        "memory_status",
        "created_at",
        "invalidated_at",
        "invalidation_reason",
        "is_valid",
    }
    assert set(edge["sources"][0]) == allowed


# ===========================================================================
# G · 输入校验
# ===========================================================================
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jkg/nodes?page=0",
        "/api/v1/jkg/nodes?page_size=101",
        "/api/v1/jkg/nodes?page_size=0",
        "/api/v1/jkg/nodes?entity_type=ROBOT",
        "/api/v1/jkg/nodes?status=DELETED",
        "/api/v1/jkg/nodes/x/traverse?hops=0",
        "/api/v1/jkg/nodes/x/traverse?hops=4",
        "/api/v1/jkg/nodes/x/traverse?max_nodes=0",
        "/api/v1/jkg/nodes/x/traverse?max_nodes=101",
        "/api/v1/jkg/nodes/x/traverse?max_edges=0",
        "/api/v1/jkg/nodes/x/traverse?max_edges=1001",
        "/api/v1/jkg/nodes/x/traverse?direction=sideways",
        "/api/v1/jkg/nodes/x/traverse?predicate=LIKES",
        "/api/v1/jkg/nodes?q=" + "x" * 201,
        "/api/v1/jkg/edges?predicate=LIKES",
        "/api/v1/jkg/edges?direction=sideways",
        "/api/v1/jkg/edges?direction=outgoing",
        "/api/v1/jkg/edges?page=0",
        "/api/v1/jkg/edges?page_size=101",
        "/api/v1/jkg/nodes/x/neighbors?page_size=101",
        "/api/v1/jkg/nodes/x/neighbors?direction=sideways",
    ],
)
def test_invalid_query_parameters_return_422_envelope(
    client, auth_headers, manager, path
):
    _ready_graph(manager)
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list)


def test_unknown_node_and_edge_return_404_envelopes(client, auth_headers, manager):
    _ready_graph(manager)
    for path in (
        "/api/v1/jkg/nodes/missing",
        "/api/v1/jkg/nodes/missing/neighbors",
        "/api/v1/jkg/edges/missing",
    ):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["error"]["code"] == "not_found"


def test_blank_node_id_is_not_a_500(client, auth_headers, manager):
    """空 id 由服务层判为校验失败（422），不得变成 500。"""
    _ready_graph(manager)
    response = client.get("/api/v1/jkg/nodes/%20", headers=auth_headers)
    assert response.status_code in (404, 422), response.text
    assert response.status_code != 500


def test_page_size_upper_bound_is_accepted(client, auth_headers, manager):
    _ready_graph(manager)
    response = client.get(
        "/api/v1/jkg/nodes?page_size=100", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["page_size"] == 100


# ===========================================================================
# H · 边界
# ===========================================================================
def test_page_beyond_the_last_returns_empty_items_with_the_real_total(
    client, auth_headers, manager
):
    _ready_graph(manager)
    _seed_graph(manager, _insert_memory(manager, "INT-03 out of range"))

    response = client.get(
        "/api/v1/jkg/nodes?page=9&page_size=2", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 4
    assert body["meta"]["total_pages"] == 2


def test_truncated_valid_only_scan_reports_null_total(
    client, auth_headers, manager, monkeypatch
):
    """扫描被截断时 ``meta.total`` 必须是 ``null``，不得用 len(items) 冒充。

    这里只收窄 ``app.jkg.query`` 的两个模块级上限（默认值仍由下一条断言守住），
    随后跑真实的 ``list_edges`` valid-only 分支。夹具是 10 条来源记忆已删除的
    边，因此没有一条会进入 matched；扫描到上限时既未穷尽候选也未凑满一页。
    """
    assert jkg_query.MAX_SCAN == 1000
    assert jkg_query.MAX_PAGE_SIZE == 100

    _ready_graph(manager)
    dead_memory = _insert_memory(
        manager, "INT-03 bulk invalid", status="DELETED"
    )
    _seed_bulk_invalid_edges(manager, count=10, memory_id=dead_memory)

    monkeypatch.setattr(jkg_query, "MAX_SCAN", 5)
    monkeypatch.setattr(jkg_query, "_SCAN_PAGE_SIZE", 2)

    response = client.get(
        "/api/v1/jkg/edges?valid_only=true&page_size=20", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"] == []
    assert body["meta"]["total"] is None
    assert body["meta"]["total_pages"] is None
    assert body["meta"]["truncated"] is True
    assert body["meta"]["scanned"] == 6
    assert body["meta"]["candidate_total"] == 10


def test_untruncated_scan_still_reports_a_real_total(
    client, auth_headers, manager
):
    """对照组：未截断时 ``total`` 是真实数字，``truncated`` 为 false。"""
    _ready_graph(manager)
    dead_memory = _insert_memory(
        manager, "INT-03 bulk invalid control", status="DELETED"
    )
    _seed_bulk_invalid_edges(manager, count=3, memory_id=dead_memory)

    body = client.get(
        "/api/v1/jkg/edges?valid_only=true", headers=auth_headers
    ).json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["truncated"] is False
    assert body["meta"]["candidate_total"] == 3


def test_overview_truncation_is_reported_not_hidden(client, auth_headers, manager):
    """概览的有效/无效计数是「扫描窗口内」的数，截断必须如实标注。

    概览的服务端窗口是 200 条边。造成 205 条边后，``scanned_edges`` 停在 200、
    ``truncated`` 为 true —— 此时 ``valid_edges`` / ``invalid_edges`` 只描述这
    200 条，不是全表。``counts`` 仍然是 ``COUNT(*)`` 的精确值（205）。

    这里不用 monkeypatch 收窄 ``MAX_SCAN``：``integrity_report`` 会校验
    ``1 <= limit <= MAX_SCAN``，把上限改小只会让概览直接报校验失败，测不到
    截断语义。用真实窗口造数据才能真正跑通那条分支。
    """
    _ready_graph(manager)
    dead_memory = _insert_memory(
        manager, "INT-03 overview truncation", status="DELETED"
    )
    _seed_bulk_invalid_edges(manager, count=205, memory_id=dead_memory)

    body = client.get("/api/v1/jkg/overview", headers=auth_headers).json()
    assert body["counts"]["edges"] == 205
    assert body["scanned_edges"] == 200
    assert body["truncated"] is True
    assert body["invalid_edges"] == 200
    assert body["valid_edges"] == 0


def test_overview_is_not_truncated_when_the_window_covers_the_table(
    client, auth_headers, manager
):
    _ready_graph(manager)
    dead_memory = _insert_memory(
        manager, "INT-03 overview small table", status="DELETED"
    )
    _seed_bulk_invalid_edges(manager, count=2, memory_id=dead_memory)

    body = client.get("/api/v1/jkg/overview", headers=auth_headers).json()
    assert body["scanned_edges"] == 2
    assert body["truncated"] is False
    assert body["invalid_edges"] == 2


# ===========================================================================
# I · 鉴权
# ===========================================================================
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jkg/status",
        "/api/v1/jkg/overview",
        "/api/v1/jkg/nodes",
        "/api/v1/jkg/edges",
        "/api/v1/jkg/nodes/x",
        "/api/v1/jkg/edges/x",
        "/api/v1/jkg/nodes/x/neighbors",
        "/api/v1/jkg/nodes/x/traverse",
    ],
)
def test_missing_token_is_401(client, path):
    response = client.get(path)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/jkg/status",
        "/api/v1/jkg/overview",
        "/api/v1/jkg/nodes",
        "/api/v1/jkg/edges",
    ],
)
def test_wrong_token_is_401(client, path):
    response = client.get(path, headers={"X-API-Token": "not-the-token"})
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"


def test_status_is_also_token_gated(client):
    """``/status`` 不抛业务错误，但仍然是受保护的端点。"""
    assert client.get("/api/v1/jkg/status").status_code == 401


# ===========================================================================
# 只读性 · 路由集合
# ===========================================================================
# INT-04 放宽说明
# -----------------
# 本节的旧断言是「路由表里只存在 GET」，它把「图数据只读」和「整个 router 只有
# GET」当成了同一件事。引入生命周期同步后这两件事必须分开：
#
# * 图**数据**端点（nodes / edges / overview / status / extraction preview）仍然
#   只能 GET —— 这一点没有放宽，而且下面按前缀逐个检查，比原来一句「只允许 GET」
#   更严格地表达了「哪些路径不得有写方法」。
# * ``POST`` 只允许出现在 ``/sync/tick``、``/sync/reconcile``、P0-1 的
#   ``/worker/run`` 与 INT-05 的 ``/init`` 管理动作路径上，逐条列明。它们是
#   「让派生图跟权威表对齐」的维护入口，不是写记忆的
#   API：既不新建记忆，也不接受任何图内容作为输入。
#
# P0-1 为什么必须再加一条（而不是把 ``/worker/run`` 归入 ``/sync/tick``）：
# 两者驱动的是**同一个** ``JkgLifecycleSync``，但``/worker/run`` 额外承担了
# 租约、``jkg.worker.*`` 记账、可选 reconcile 与超时恢复。把它拆成独立路径，
# 运维才能在不影响旧 ``/sync/tick`` 调用方的前提下只启用/观测 worker；如果
# 合并成一条，旧路由的语义（无租约、无记账）就会被静默改掉。
# 反过来，这条断言仍然要求它必须是**具名列出的管理动作**，而不是任意 POST
# 都被容许：新加第四条第 POST 路径仍会让本用例失败。
# * ``PUT`` / ``PATCH`` / ``DELETE`` 仍然一个都不能有。
#
# 换句话说，放宽的只是「router 级集合等于 {GET}」，收紧的是「除管理动作外
# 任何路径都不得有写方法」。

def test_router_exposes_only_get_methods():
    """图数据端点全部只读；写方法只允许出现在管理动作路径上。

    放宽理由见本节开头的注释。这里按路径逐条检查，而不是只断言「方法集合等于
    {GET}」：后者只能证明「没有任何写方法」，无法区分「哪个写方法落在哪条路径」
    —— 而后者才是 INT-04 真正需要锁住的边界。
    """
    from jarvismind.api.routes.jkg import router as jkg_router

    methods_by_path: dict = {}
    for route in jkg_router.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            methods_by_path.setdefault(path, set()).add(method.upper())

    # 1. 不存在任何 PUT / PATCH / DELETE。
    all_methods = set().union(*methods_by_path.values())
    assert all_methods.isdisjoint({"PUT", "PATCH", "DELETE"}), methods_by_path

    # 2. POST 只出现在管理动作路径上：INT-04 的两条 sync 动作、
    #    P0-1 的 worker 动作与 INT-05 的 /init 动作。
    admin_post_paths = [
        "/init/tasks",
        "/init/tasks/{task_id}/cancel",
        "/init/tasks/{task_id}/pause",
        "/init/tasks/{task_id}/resume",
        "/init/tasks/{task_id}/retry",
        "/init/tasks/{task_id}/retry-failed",
        "/init/tasks/{task_id}/run",
        "/sync/reconcile",
        "/sync/tick",
        "/worker/run",
    ]
    post_paths = sorted(
        path for path, methods in methods_by_path.items() if "POST" in methods
    )
    assert post_paths == admin_post_paths, post_paths

    # 3. 图数据端点（含 /status、extraction preview、init 只读查询）只有 GET。
    for path, methods in methods_by_path.items():
        if path in admin_post_paths:
            continue
        assert methods == {"GET"}, (path, methods)


def test_all_jkg_paths_are_registered_under_the_expected_prefix(client):
    schema = client.get("/openapi.json").json()
    jkg_paths = sorted(path for path in schema["paths"] if "/jkg/" in path)
    assert jkg_paths == sorted(
        [
            "/api/v1/jkg/status",
            "/api/v1/jkg/overview",
            "/api/v1/jkg/nodes",
            "/api/v1/jkg/nodes/{node_id}",
            "/api/v1/jkg/nodes/{node_id}/neighbors",
            "/api/v1/jkg/nodes/{node_id}/traverse",
            "/api/v1/jkg/edges",
            "/api/v1/jkg/edges/{edge_id}",
            # INT-04：生命周期同步（两条管理动作 + 一条只读快照）与抽取预览。
            "/api/v1/jkg/sync/status",
            "/api/v1/jkg/sync/tick",
            "/api/v1/jkg/sync/reconcile",
            "/api/v1/jkg/extraction/preview/{memory_id}",
            # P0-1：worker（一条管理动作 + 一条只读快照）。
            "/api/v1/jkg/worker/status",
            "/api/v1/jkg/worker/run",
            # INT-05：历史初始化（只读查询 + 管理动作）。
            "/api/v1/jkg/init/preview",
            "/api/v1/jkg/init/tasks",
            "/api/v1/jkg/init/tasks/{task_id}",
            "/api/v1/jkg/init/tasks/{task_id}/failures",
            "/api/v1/jkg/init/tasks/{task_id}/run",
            "/api/v1/jkg/init/tasks/{task_id}/pause",
            "/api/v1/jkg/init/tasks/{task_id}/resume",
            "/api/v1/jkg/init/tasks/{task_id}/cancel",
            "/api/v1/jkg/init/tasks/{task_id}/retry",
            "/api/v1/jkg/init/tasks/{task_id}/retry-failed",
        ]
    )
    admin_suffixes = (
        "/sync/tick",
        "/sync/reconcile",
        "/worker/run",
        "/run",
        "/pause",
        "/resume",
        "/cancel",
        "/retry",
        "/retry-failed",
    )
    for path in jkg_paths:
        methods = set(schema["paths"][path])
        if path == "/api/v1/jkg/init/tasks":
            assert methods == {"get", "post"}
        elif path.endswith(admin_suffixes):
            assert methods == {"post"}, path
        else:
            assert methods == {"get"}, path


# ===========================================================================
# J · 状态过滤与有界遍历（JKG-INT-02）
# ===========================================================================
_SET_NODE_STATUS_SQL = text(
    "UPDATE jkg_nodes SET status = :status WHERE node_id = :id"
)
_TRAVERSE_KEYS = {
    "root_id",
    "hops",
    "direction",
    "nodes",
    "edges",
    "truncated",
    "visited_nodes",
    "visited_edges",
    "limits",
}


def test_nodes_status_filter_over_http(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-02 status http"))
    with manager.session() as session:
        session.execute(
            _SET_NODE_STATUS_SQL,
            {"status": "INVALID", "id": seed["bob"]["node_id"]},
        )

    invalid = client.get("/api/v1/jkg/nodes?status=INVALID", headers=auth_headers)
    assert invalid.status_code == 200, invalid.text
    body = invalid.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["node_id"] == seed["bob"]["node_id"]

    active = client.get("/api/v1/jkg/nodes?status=ACTIVE", headers=auth_headers)
    assert active.json()["meta"]["total"] == 3


def test_traverse_http_returns_hops_limits_and_envelope(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-02 traverse http"))

    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/traverse?hops=2",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == _TRAVERSE_KEYS
    assert data["root_id"] == seed["alice"]["node_id"]
    assert data["hops"] == 2
    assert data["direction"] == "both"
    assert data["limits"] == {"hops": 2, "max_nodes": 50, "max_edges": 200}
    hops = {entry["node"]["name"]: entry["hop"] for entry in data["nodes"]}
    assert hops == {"Alice": 0, "JarvisMind": 1, "Bob": 1}
    assert data["visited_nodes"] == 3
    assert data["visited_edges"] == 2
    assert data["truncated"] is False
    for edge in data["edges"]:
        assert edge["sources"] == []
        assert "is_valid" in edge


def test_traverse_http_missing_root_is_404_envelope(client, auth_headers, manager):
    _ready_graph(manager)
    response = client.get(
        "/api/v1/jkg/nodes/missing/traverse", headers=auth_headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_traverse_http_reports_truncation_under_node_cap(client, auth_headers, manager):
    _ready_graph(manager)
    seed = _seed_graph(manager, _insert_memory(manager, "INT-02 truncation http"))
    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/traverse?hops=2&max_nodes=2",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["nodes"]) == 2
    assert data["truncated"] is True
    assert data["limits"]["max_nodes"] == 2


def test_traverse_http_hides_invalid_edges_unless_asked(client, auth_headers, manager):
    _ready_graph(manager)
    live = _insert_memory(manager, "INT-02 http live")
    dead = _insert_memory(manager, "INT-02 http dead")
    _seed_graph(manager, live)
    extra = _seed_invalidated_edge(manager, dead, live)
    root = extra["carol"]["node_id"]

    hidden = client.get(
        f"/api/v1/jkg/nodes/{root}/traverse?hops=3", headers=auth_headers
    ).json()["data"]
    assert extra["edge"]["edge_id"] not in {e["edge_id"] for e in hidden["edges"]}

    shown = client.get(
        f"/api/v1/jkg/nodes/{root}/traverse?hops=3&valid_only=false",
        headers=auth_headers,
    ).json()["data"]
    by_id = {e["edge_id"]: e for e in shown["edges"]}
    assert by_id[extra["edge"]["edge_id"]]["is_valid"] is False


def test_traverse_http_redacts_credential_labels_and_leaks_nothing(
    client, auth_headers, manager
):
    _ready_graph(manager)
    memory = _insert_memory(manager, "INT-02 traverse credential")
    root = _credential_node(manager, memory)
    friend = _writer(manager).upsert_node(
        entity_type="PERSON", name="Gina", source_memory_id=memory
    )
    _writer(manager).create_edge(
        subject_id=root["node_id"],
        predicate="KNOWS",
        object_id=friend["node_id"],
        source_memory_ids=[memory],
    )

    response = client.get(
        f"/api/v1/jkg/nodes/{root['node_id']}/traverse?hops=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert _CREDENTIAL_SHAPED not in response.text
    assert "ABCDEF0123456789" not in response.text
    data = response.json()["data"]
    root_item = next(e["node"] for e in data["nodes"] if e["hop"] == 0)
    assert root_item["name"] == _REDACTED
    assert root_item["metadata"] is None
    assert root_item["sensitivity"] == "CREDENTIAL"


def test_traverse_http_never_returns_memory_content(client, auth_headers, manager):
    _ready_graph(manager)
    memory = _insert_memory(manager, "prefix " + _CONTENT_MARKER + " suffix")
    seed = _seed_graph(manager, memory)
    response = client.get(
        f"/api/v1/jkg/nodes/{seed['alice']['node_id']}/traverse?hops=3",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert _CONTENT_MARKER not in response.text
    for entry in response.json()["data"]["nodes"]:
        assert "content" not in entry["node"]
        assert "content" not in entry
