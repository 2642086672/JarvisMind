"""JKG-INT-05 HTTP 回归：历史初始化任务接口（``/api/v1/jkg/init/*``）。

覆盖
----
* 无 token 401、未知任务 404、非法请求体 422；
* JKG 关闭 / 图表缺失时预览与创建被拒绝（错误码沿用 ``JkgDisabledError`` 等）；
* 预览只读，报告图计数、``attributes_supported`` 与 ``p1_available``；
* 创建 / 列表 / 详情 / 状态过滤；
* run → pause → resume → run 到完成，图节点、边、来源真实写入；
* run → cancel 后任务终止、不可再运行；
* 单条失败隔离、failures 分页、retry-failed 派生重试任务；
* 同一时刻只运行一个任务；
* P1 表缺失时的 409 映射；
* ``POST /sync/tick`` 返回四个新计数字段。

记忆一律经 ``POST /api/v1/memories`` 创建。数据库是共享 ``manager`` 的会话临时库；
JKG 表只经 ``create_jkg_schema`` 安装，P1 表只经 ``install_p1_schema`` 安装。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jkg.extraction import RULE_VERSION as RULES_VERSION
from jarvismind.jkg.ingestor import IngestOutcome, JkgIngestor
from jarvismind.jkg.schema import JKG_ENABLED_KEY

URL = "/api/v1/jkg/init"

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


def _enable(manager, value: str = "1") -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


@pytest.fixture()
def ready(manager):
    install_p1_schema(manager)
    create_jkg_schema(manager)
    _enable(manager, "1")
    return manager


def _remember(client, headers, count: int) -> list:
    ids = []
    for index in range(count):
        response = client.post(
            "/api/v1/memories",
            json={
                "memory_type": "SEMANTIC",
                "content": f"张{index:02d} 负责 项目{index:02d}。",
                "importance": 5.0,
                "confidence": 0.9,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["data"]["id"])
    return ids


def _create(client, headers, **body) -> dict:
    response = client.post(f"{URL}/tasks", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _run(client, headers, task_id: str, max_batches: int | None = None) -> dict:
    params = {} if max_batches is None else {"max_batches": max_batches}
    response = client.post(
        f"{URL}/tasks/{task_id}/run", params=params, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def _action(client, headers, task_id: str, action: str):
    return client.post(f"{URL}/tasks/{task_id}/{action}", headers=headers)


# ===========================================================================
# A · 鉴权、未知任务、请求体校验
# ===========================================================================
@pytest.mark.parametrize("path", ["/preview", "/tasks", "/tasks/x"])
def test_init_get_routes_require_a_token(client, path):
    assert client.get(f"{URL}{path}").status_code == 401


def test_init_create_requires_a_token(client):
    response = client.post(f"{URL}/tasks", json={"batch_size": 1})
    assert response.status_code == 401


def test_unknown_task_is_404(client, auth_headers, ready):
    response = client.get(f"{URL}/tasks/no-such-task", headers=auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "jkg_init_task_not_found"


def test_unknown_task_is_404_for_run_and_failures(client, auth_headers, ready):
    run = client.post(f"{URL}/tasks/no-such-task/run", headers=auth_headers)
    assert run.status_code == 404, run.text
    failures = client.get(f"{URL}/tasks/no-such-task/failures", headers=auth_headers)
    assert failures.status_code == 404, failures.text


@pytest.mark.parametrize(
    "body",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"mode": "retry"},
        {"unexpected": True},
    ],
)
def test_create_rejects_invalid_bodies(client, auth_headers, ready, body):
    response = client.post(f"{URL}/tasks", json=body, headers=auth_headers)
    assert response.status_code == 422, response.text


# ===========================================================================
# B · 关闭态与缺表：预览、创建被拒绝
# ===========================================================================
def test_disabled_graph_refuses_preview_and_create(client, auth_headers, manager):
    install_p1_schema(manager)
    create_jkg_schema(manager)
    _enable(manager, "0")

    preview = client.get(f"{URL}/preview", headers=auth_headers)
    assert preview.status_code == 503, preview.text
    assert preview.json()["error"]["code"] == "jkg_disabled"

    created = client.post(f"{URL}/tasks", json={}, headers=auth_headers)
    assert created.status_code == 503, created.text
    assert created.json()["error"]["code"] == "jkg_disabled"


def test_missing_jkg_schema_refuses_create(client, auth_headers, manager):
    install_p1_schema(manager)
    _enable(manager, "1")
    response = client.post(f"{URL}/tasks", json={}, headers=auth_headers)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "jkg_schema_not_ready"


# ===========================================================================
# C · 预览（只读）
# ===========================================================================
def test_preview_counts_eligible_memories_and_reports_p1(client, auth_headers, ready):
    _remember(client, auth_headers, 3)
    response = client.get(f"{URL}/preview", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["eligible_memories"] == 3
    assert data["graph"] == {"nodes": 0, "edges": 0, "sources": 0}
    assert data["attributes_supported"] is False
    assert data["p1_available"] is True
    assert data["rules_version"] == RULES_VERSION
    assert "WORKS_ON" in data["supported_relations"]


def test_preview_writes_no_task(client, auth_headers, ready):
    _remember(client, auth_headers, 2)
    client.get(f"{URL}/preview", headers=auth_headers)
    assert client.get(f"{URL}/tasks", headers=auth_headers).json()["total"] == 0


# ===========================================================================
# D · 创建 / 列表 / 详情
# ===========================================================================
def test_create_defaults_to_a_pending_full_task(client, auth_headers, ready):
    _remember(client, auth_headers, 3)
    task = _create(client, auth_headers)
    assert task["status"] == "PENDING"
    assert task["mode"] == "full"
    assert task["batch_size"] == 20
    assert task["progress"]["total"] == 3
    assert task["progress"]["percent"] == 0
    assert task["failures"] == []
    assert task["failed_total"] == 0

    listed = client.get(f"{URL}/tasks", headers=auth_headers).json()
    assert listed["total"] == 1
    assert listed["data"][0]["task_id"] == task["task_id"]

    fetched = client.get(f"{URL}/tasks/{task['task_id']}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["task_id"] == task["task_id"]


def test_list_filters_by_status(client, auth_headers, ready):
    _remember(client, auth_headers, 2)
    _create(client, auth_headers)

    pending = client.get(
        f"{URL}/tasks", params={"status": "PENDING"}, headers=auth_headers
    ).json()
    assert pending["total"] == 1

    completed = client.get(
        f"{URL}/tasks", params={"status": "COMPLETED"}, headers=auth_headers
    ).json()
    assert completed["total"] == 0
    assert completed["data"] == []


def test_list_rejects_an_unknown_status(client, auth_headers, ready):
    response = client.get(
        f"{URL}/tasks", params={"status": "BOGUS"}, headers=auth_headers
    )
    assert response.status_code == 422, response.text


# ===========================================================================
# E · run / pause / resume 到完成
# ===========================================================================
def test_run_pause_resume_then_complete(client, auth_headers, ready):
    _remember(client, auth_headers, 3)
    task_id = _create(client, auth_headers, batch_size=1)["task_id"]

    first = _run(client, auth_headers, task_id)
    assert first["status"] == "RUNNING"
    assert first["progress"]["scanned"] == 1

    paused = _action(client, auth_headers, task_id, "pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"

    blocked = client.post(f"{URL}/tasks/{task_id}/run", headers=auth_headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "jkg_init_invalid_state"

    resumed = _action(client, auth_headers, task_id, "resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"

    done = _run(client, auth_headers, task_id, max_batches=20)
    assert done["status"] == "COMPLETED"
    assert done["progress"]["percent"] == 100
    assert done["progress"]["scanned"] == 3
    assert done["finished_at"]

    overview = client.get("/api/v1/jkg/overview", headers=auth_headers).json()
    assert overview["counts"]["nodes"] > 0
    assert overview["counts"]["edges"] > 0
    assert overview["counts"]["sources"] > 0


def test_run_rejects_max_batches_out_of_range(client, auth_headers, ready):
    task_id = _create(client, auth_headers)["task_id"]
    for value in (0, 21):
        response = client.post(
            f"{URL}/tasks/{task_id}/run",
            params={"max_batches": value},
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text


# ===========================================================================
# F · cancel 后终止
# ===========================================================================
def test_cancel_after_one_batch_is_terminal(client, auth_headers, ready):
    _remember(client, auth_headers, 3)
    task_id = _create(client, auth_headers, batch_size=1)["task_id"]
    assert _run(client, auth_headers, task_id)["status"] == "RUNNING"

    cancelled = _action(client, auth_headers, task_id, "cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["finished_at"]

    blocked = client.post(f"{URL}/tasks/{task_id}/run", headers=auth_headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "jkg_init_invalid_state"

    fetched = client.get(f"{URL}/tasks/{task_id}", headers=auth_headers).json()
    assert fetched["status"] == "CANCELLED"
    assert fetched["progress"]["scanned"] == 1


# ===========================================================================
# G · 失败隔离、failures 分页、retry-failed
# ===========================================================================
def test_failed_memory_is_isolated_and_can_be_retried(
    client, auth_headers, ready, monkeypatch
):
    ids = _remember(client, auth_headers, 3)
    failing = {ids[1]}
    original = JkgIngestor.ingest_memory

    def flaky(self, memory_id, *, init_task_id=None):
        if memory_id in failing:
            return IngestOutcome("failed", "FAKE_FAIL", 0, 0, 0, 0, 0)
        return original(self, memory_id, init_task_id=init_task_id)

    monkeypatch.setattr(JkgIngestor, "ingest_memory", flaky)
    task_id = _create(client, auth_headers)["task_id"]
    done = _run(client, auth_headers, task_id, max_batches=20)
    assert done["status"] == "COMPLETED"
    assert done["failed_total"] == 1
    assert done["progress"]["failed"] == 1
    assert done["failures"] == [
        {"memory_id": ids[1], "error_type": "ingest_failed", "error_code": "FAKE_FAIL"}
    ]

    listing = client.get(f"{URL}/tasks/{task_id}/failures", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["task_id"] == task_id
    assert body["failed_total"] == 1
    assert len(body["failures"]) == 1
    assert body["limit"] == 100
    assert body["offset"] == 0

    past_end = client.get(
        f"{URL}/tasks/{task_id}/failures",
        params={"limit": 1, "offset": 1},
        headers=auth_headers,
    ).json()
    assert past_end["failed_total"] == 1
    assert past_end["failures"] == []
    assert past_end["offset"] == 1

    monkeypatch.setattr(JkgIngestor, "ingest_memory", original)
    retry = client.post(f"{URL}/tasks/{task_id}/retry-failed", headers=auth_headers)
    assert retry.status_code == 201, retry.text
    retry_task = retry.json()
    assert retry_task["mode"] == "retry"
    assert retry_task["source_task_id"] == task_id
    assert retry_task["progress"]["total"] == 1

    finished = _run(client, auth_headers, retry_task["task_id"], max_batches=20)
    assert finished["status"] == "COMPLETED"
    assert finished["progress"]["scanned"] == 1
    assert finished["progress"]["failed"] == 0


def test_failures_of_a_task_without_failures_is_an_empty_page(
    client, auth_headers, ready
):
    task_id = _create(client, auth_headers)["task_id"]
    body = client.get(f"{URL}/tasks/{task_id}/failures", headers=auth_headers).json()
    assert body == {
        "task_id": task_id,
        "failed_total": 0,
        "failures": [],
        "limit": 100,
        "offset": 0,
    }


def test_failures_reject_out_of_range_paging(client, auth_headers, ready):
    task_id = _create(client, auth_headers)["task_id"]
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        response = client.get(
            f"{URL}/tasks/{task_id}/failures", params=params, headers=auth_headers
        )
        assert response.status_code == 422, (params, response.text)


def test_retry_failed_is_refused_for_a_task_without_failures(
    client, auth_headers, ready
):
    _remember(client, auth_headers, 2)
    task_id = _create(client, auth_headers)["task_id"]
    _run(client, auth_headers, task_id, max_batches=20)
    response = client.post(f"{URL}/tasks/{task_id}/retry-failed", headers=auth_headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "jkg_init_invalid_state"


# ===========================================================================
# H · 同一时刻只运行一个任务
# ===========================================================================
def test_only_one_task_runs_at_a_time(client, auth_headers, ready):
    _remember(client, auth_headers, 3)
    first = _create(client, auth_headers, batch_size=1)["task_id"]
    second = _create(client, auth_headers, batch_size=1)["task_id"]

    assert _run(client, auth_headers, first)["status"] == "RUNNING"

    blocked = client.post(f"{URL}/tasks/{second}/run", headers=auth_headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "jkg_init_conflict"
    assert client.get(f"{URL}/tasks/{second}", headers=auth_headers).json()[
        "status"
    ] == "PENDING"


# ===========================================================================
# I · P1 缺失：预览仍答复，创建 / 列表 / 运行 报 409
# ===========================================================================
def test_missing_p1_schema_is_reported_per_endpoint(client, auth_headers, manager):
    create_jkg_schema(manager)
    _enable(manager, "1")

    preview = client.get(f"{URL}/preview", headers=auth_headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["p1_available"] is False

    created = client.post(f"{URL}/tasks", json={}, headers=auth_headers)
    assert created.status_code == 409, created.text
    assert created.json()["error"]["code"] == "p1_schema_missing"

    listed = client.get(f"{URL}/tasks", headers=auth_headers)
    assert listed.status_code == 409, listed.text
    assert listed.json()["error"]["code"] == "p1_schema_missing"


# ===========================================================================
# J · POST /sync/tick 的四个新计数字段
# ===========================================================================
def test_sync_tick_reports_the_four_ingest_counters(client, auth_headers, ready):
    _remember(client, auth_headers, 2)
    response = client.post("/api/v1/jkg/sync/tick", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    for key in (
        "ingested_processed",
        "ingested_skipped",
        "ingested_failed",
        "governance_rejected",
    ):
        assert isinstance(data[key], int), key
        assert not isinstance(data[key], bool), key
        assert data[key] >= 0, key
    assert data["run_id"]
    assert data["ok"] is True
