"""JKG-INT-04 HTTP 回归：生命周期同步与抽取预览。

覆盖
----
* ``GET /sync/status`` 三态全部 200，且都带 ``state`` 与 ``usable``；
* ``POST /sync/tick`` 成功与 ``dry_run``（两者都带 ``dry_run`` / ``wrote``）；
* ``POST /sync/reconcile`` 分页游标；
* ``GET /extraction/preview/{id}`` 正例 / 404 / 503；
* ``limit`` 越界 422、无 token 401。

原有 ``test_jkg_api.py`` 保留不动，这里只补 INT-04 的新面。两者共用同一份夹具
约定（共享 ``manager`` 的会话临时库 + ``create_jkg_schema``），不触达生产库。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg.extraction import RULE_VERSION
from jarvismind.jkg.schema import JKG_ENABLED_KEY

_CONTENT_MARKER = "MKR" + "-INT-04-API-DO-NOT-LEAK"

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES (:id, 'SEMANTIC', :content, 5.0, 0.9, 'ACTIVE', :now, :now)"
)

_INSERT_AUDIT = text(
    "INSERT INTO memory_audit_logs(id, memory_id, action, actor, detail_json, created_at) "
    "VALUES (:id, :memory_id, :action, :actor, :detail, :created_at)"
)

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _enable(manager, value: str = "1") -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _insert_memory(manager, content: str) -> str:
    memory_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {"id": memory_id, "content": content, "now": utc_now_iso()},
        )
    return memory_id


def _audit(manager, memory_id: str, action: str) -> str:
    audit_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_AUDIT,
            {
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "actor": "system",
                "detail": None,
                "created_at": utc_now_iso(),
            },
        )
    return audit_id


def _seed_node(manager, memory_id: str, name: str = "Alice") -> dict:
    return JkgCompositionAdapter(manager).write_service().upsert_node(
        entity_type="PERSON", name=name, source_memory_id=memory_id
    )


def _seed_edge_for(manager, memory_id: str, index: int) -> dict:
    """给一个 memory 造一条边。``reconcile`` 只看 ``jkg_edge_sources``，
    而来源行只由**边**产生 —— 只造节点不够。"""
    writer = JkgCompositionAdapter(manager).write_service()
    left = writer.upsert_node(
        entity_type="PERSON", name=f"Alice{index}", source_memory_id=memory_id
    )
    right = writer.upsert_node(
        entity_type="PROJECT", name=f"JarvisMind{index}", source_memory_id=memory_id
    )
    return writer.create_edge(
        subject_id=left["node_id"],
        predicate="WORKS_ON",
        object_id=right["node_id"],
        source_memory_ids=[memory_id],
    )


@pytest.fixture()
def ready(manager):
    create_jkg_schema(manager)
    _enable(manager, "1")
    return manager


def _jkg_row_counts(manager) -> dict:
    counts = {}
    with manager.session() as session:
        for table in ("jkg_nodes", "jkg_edges", "jkg_edge_sources"):
            counts[table] = int(
                session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
            )
    return counts


# ===========================================================================
# A · GET /sync/status —— 三态都 200
# ===========================================================================
def test_sync_status_is_200_when_the_graph_is_ready(client, auth_headers, ready):
    response = client.get("/api/v1/jkg/sync/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "ready"
    assert data["usable"] is True


def test_sync_status_is_200_when_disabled(client, auth_headers, manager):
    _enable(manager, "0")
    response = client.get("/api/v1/jkg/sync/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "disabled"
    assert data["usable"] is False


def test_sync_status_is_200_when_the_schema_is_absent(client, auth_headers, manager):
    _enable(manager, "1")
    response = client.get("/api/v1/jkg/sync/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["state"] == "schema_not_ready"
    assert data["usable"] is False


def test_sync_status_reports_the_cursor_shape(client, auth_headers, ready):
    data = client.get("/api/v1/jkg/sync/status", headers=auth_headers).json()
    assert data["cursor"] == {"created_at": "", "id": ""}
    assert data["cursor_recovered"] is False
    assert data["overlap_seconds"] == 60


def test_sync_status_requires_a_token(client):
    assert client.get("/api/v1/jkg/sync/status").status_code == 401


# ===========================================================================
# B · POST /sync/tick
# ===========================================================================
def test_sync_tick_processes_a_create_entry(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind")
    _audit(ready, memory_id, "CREATE")
    response = client.post("/api/v1/jkg/sync/tick", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["skipped"] is False
    assert data["memories_processed"] == 1
    assert memory_id in data["processed_memory_ids"]
    assert data["dry_run"] is False
    assert data["wrote"] is True


def test_sync_tick_dry_run_writes_nothing(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind")
    _seed_edge_for(ready, memory_id, 0)
    _audit(ready, memory_id, "UPDATE")
    before = _jkg_row_counts(ready)

    response = client.post(
        "/api/v1/jkg/sync/tick", params={"dry_run": "true"}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dry_run"] is True
    assert data["wrote"] is False
    assert _jkg_row_counts(ready) == before

    status = client.get("/api/v1/jkg/sync/status", headers=auth_headers).json()
    assert status["cursor"]["id"] == ""


def test_sync_tick_is_skipped_when_disabled(client, auth_headers, manager):
    _enable(manager, "0")
    response = client.post("/api/v1/jkg/sync/tick", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["skipped"] is True
    assert data["state"] == "disabled"


def test_sync_tick_is_skipped_when_the_schema_is_absent(client, auth_headers, manager):
    _enable(manager, "1")
    response = client.post("/api/v1/jkg/sync/tick", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["skipped"] is True


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_sync_tick_rejects_an_out_of_range_limit(client, auth_headers, ready, limit):
    response = client.post(
        "/api/v1/jkg/sync/tick", params={"limit": limit}, headers=auth_headers
    )
    assert response.status_code == 422, response.text


def test_sync_tick_accepts_the_documented_limit_bounds(client, auth_headers, ready):
    for limit in (1, 500):
        response = client.post(
            "/api/v1/jkg/sync/tick", params={"limit": limit}, headers=auth_headers
        )
        assert response.status_code == 200, (limit, response.text)


def test_sync_tick_never_returns_a_memory_body(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind " + _CONTENT_MARKER)
    _audit(ready, memory_id, "UPDATE")
    body = client.post("/api/v1/jkg/sync/tick", headers=auth_headers).text
    assert _CONTENT_MARKER not in body


def test_sync_tick_requires_a_token(client):
    assert client.post("/api/v1/jkg/sync/tick").status_code == 401


# ===========================================================================
# C · POST /sync/reconcile
# ===========================================================================
def test_sync_reconcile_reports_the_resume_point(client, auth_headers, ready):
    memory_ids = [_insert_memory(ready, f"Alice 负责 JarvisMind {i}") for i in range(3)]
    for index, memory_id in enumerate(memory_ids):
        _seed_edge_for(ready, memory_id, index)

    response = client.post(
        "/api/v1/jkg/sync/reconcile", params={"limit": 2}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["memories_checked"] == 2
    assert data["has_more"] is True
    assert data["next_after_memory_id"] == sorted(memory_ids)[1]


def test_sync_reconcile_completes_in_two_pages(client, auth_headers, ready):
    memory_ids = [_insert_memory(ready, f"Alice 负责 JarvisMind {i}") for i in range(3)]
    for index, memory_id in enumerate(memory_ids):
        _seed_edge_for(ready, memory_id, index)

    first = client.post(
        "/api/v1/jkg/sync/reconcile", params={"limit": 2}, headers=auth_headers
    ).json()
    second = client.post(
        "/api/v1/jkg/sync/reconcile",
        params={"limit": 2, "after_memory_id": first["next_after_memory_id"]},
        headers=auth_headers,
    ).json()
    assert second["has_more"] is False
    assert second["next_after_memory_id"] == max(memory_ids)


def test_sync_reconcile_is_skipped_when_disabled(client, auth_headers, manager):
    _enable(manager, "0")
    response = client.post("/api/v1/jkg/sync/reconcile", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["skipped"] is True


@pytest.mark.parametrize("limit", [0, 501])
def test_sync_reconcile_rejects_an_out_of_range_limit(
    client, auth_headers, ready, limit
):
    response = client.post(
        "/api/v1/jkg/sync/reconcile", params={"limit": limit}, headers=auth_headers
    )
    assert response.status_code == 422, response.text


def test_sync_reconcile_requires_a_token(client):
    assert client.post("/api/v1/jkg/sync/reconcile").status_code == 401


# ===========================================================================
# D · GET /extraction/preview/{memory_id}
# ===========================================================================
def test_preview_returns_candidates_for_a_plain_memory(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind")
    response = client.get(
        f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["eligible"] is True
    assert data["rule_version"] == RULE_VERSION
    assert data["persisted"] is False
    assert data["counts"]["edges"] == 1
    assert {node["name"] for node in data["nodes"]} == {"Alice", "JarvisMind"}


def test_preview_writes_nothing(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind")
    before = _jkg_row_counts(ready)
    client.get(f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers)
    assert _jkg_row_counts(ready) == before


def test_preview_does_not_write_a_cursor(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind")
    client.get(f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers)
    status = client.get("/api/v1/jkg/sync/status", headers=auth_headers).json()
    assert status["cursor"]["id"] == ""


def test_preview_of_a_missing_memory_is_404(client, auth_headers, ready):
    response = client.get(
        "/api/v1/jkg/extraction/preview/no-such-memory", headers=auth_headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_preview_is_503_when_the_graph_is_disabled(client, auth_headers, manager):
    _enable(manager, "0")
    memory_id = _insert_memory(manager, "Alice 负责 JarvisMind")
    response = client.get(
        f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_disabled"


def test_preview_is_503_when_the_schema_is_absent(client, auth_headers, manager):
    _enable(manager, "1")
    memory_id = _insert_memory(manager, "Alice 负责 JarvisMind")
    response = client.get(
        f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_schema_not_ready"


def test_preview_never_returns_the_memory_body(client, auth_headers, ready):
    memory_id = _insert_memory(
        ready, f"笔记 {_CONTENT_MARKER} 记在这里。Alice 负责 JarvisMind"
    )
    body = client.get(
        f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers
    ).text
    assert _CONTENT_MARKER not in body


def test_preview_of_a_credential_memory_is_not_eligible(client, auth_headers, ready):
    memory_id = _insert_memory(ready, "Alice 负责 JarvisMind。密码 " + "hunter2")
    data = client.get(
        f"/api/v1/jkg/extraction/preview/{memory_id}", headers=auth_headers
    ).json()
    assert data["eligible"] is False
    assert data["nodes"] == []
    assert data["edges"] == []
    assert "hunter2" not in str(data)


def test_preview_requires_a_token(client):
    assert client.get("/api/v1/jkg/extraction/preview/x").status_code == 401


# ===========================================================================
# E · 路由表：POST 只出现在已登记的管理动作路径上
# ===========================================================================
# P0-1 再加一条：``/worker/run``。它不是新的写记忆 API，而是「让派生图跟权威
# 表对齐」的第五个维护入口（与 ``/sync/tick`` 共用同一个 ``JkgLifecycleSync``，
# 额外加上租约与记账）。仍然逐条列名，不接受任意 POST。
def test_only_the_documented_admin_paths_accept_post(client):
    schema = client.get("/openapi.json").json()
    post_paths = sorted(
        path
        for path, methods in schema["paths"].items()
        if "/jkg/" in path and "post" in methods
    )
    assert post_paths == [
        "/api/v1/jkg/init/tasks",
        "/api/v1/jkg/init/tasks/{task_id}/cancel",
        "/api/v1/jkg/init/tasks/{task_id}/pause",
        "/api/v1/jkg/init/tasks/{task_id}/resume",
        "/api/v1/jkg/init/tasks/{task_id}/retry",
        "/api/v1/jkg/init/tasks/{task_id}/retry-failed",
        "/api/v1/jkg/init/tasks/{task_id}/run",
        "/api/v1/jkg/sync/reconcile",
        "/api/v1/jkg/sync/tick",
        "/api/v1/jkg/worker/run",
    ]


def test_no_jkg_path_accepts_put_patch_or_delete(client):
    schema = client.get("/openapi.json").json()
    for path, methods in schema["paths"].items():
        if "/jkg/" not in path:
            continue
        assert set(methods).isdisjoint({"put", "patch", "delete"}), (path, methods)
