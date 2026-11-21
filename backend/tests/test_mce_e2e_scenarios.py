"""The seven mandatory MCE-01 acceptance scenarios, end to end over HTTP.

These use the real JME write path and the real read-only projection. Only the
JKG schema is installed by the test-only factory (the production schema is
already frozen at v6 and MCE never installs it).
"""
from __future__ import annotations

from sqlalchemy import text

from test_mce_api import (
    _create,
    _decision,
    _item_ids,
    _resolve,
    _set_updated_at,
    _table_counts,
    _upsert_setting,
)


def test_scenario_1_new_session_restores_project_state(
    client, auth_headers, app
):
    """Session A writes; an independent session B asks and gets the state."""
    stage = _create(
        app,
        "[project:jarvismind] 当前阶段：MCE-01A 开发",
        metadata={"project_id": "jarvismind", "context_kind": "stage"},
    )
    completed = _create(
        app,
        "[project:jarvismind] 已完成：JKG 历史建图",
        metadata={"project_id": "jarvismind", "context_kind": "completed"},
    )
    decision = _create(
        app,
        "[project:jarvismind] 已确认：MCE 只读且零 DDL",
        metadata=_decision("scenario-1", key="mce"),
    )
    next_step = _create(
        app,
        "[project:jarvismind] 下一步：独立审计",
        metadata={"project_id": "jarvismind", "context_kind": "next_step"},
    )

    # No conversation history is supplied: the request is just a new session.
    body = _resolve(client, auth_headers, project_id="jarvismind", query="继续")

    assert body["project_context"]["stage"]["memory_id"] == stage.id
    assert completed.id in _item_ids(body)
    assert decision.id in _item_ids(body)
    assert next_step.id in _item_ids(body)
    assert body["rendered_context"]


def test_scenario_2_new_confirmed_decision_overrides_the_old_one(
    client, auth_headers, app, manager
):
    old = _create(
        app,
        "[project:jarvismind] 决策：使用 1200 token 预算",
        metadata=_decision("old-budget", key="budget"),
    )
    new = _create(
        app,
        "[project:jarvismind] 决策：使用 1600 token 预算",
        metadata=_decision("new-budget", key="budget"),
    )
    _set_updated_at(manager, old.id, "2026-01-01T00:00:00+00:00")
    _set_updated_at(manager, new.id, "2026-02-01T00:00:00+00:00")

    body = _resolve(client, auth_headers, project_id="jarvismind")
    confirmed = [item["memory_id"] for item in body["project_context"]["confirmed_decisions"]]
    superseded = {
        item["memory_id"]: item
        for item in body["project_context"]["superseded_decisions"]
    }
    assert confirmed == [new.id]
    assert superseded[old.id]["superseded_by"] == new.id
    assert "1200 token" not in body["rendered_context"]
    assert "1600 token" in body["rendered_context"]


def test_scenario_3_forget_removes_memory_and_its_jkg_source(
    client, auth_headers, app, manager, approved_forget
):
    from _fixtures.jkg_schema_factory import create_jkg_schema
    from jarvismind.jkg.adapter import JkgCompositionAdapter

    create_jkg_schema(manager)
    _upsert_setting(manager, "jkg.enabled", "1")
    decision = _create(
        app,
        "[project:jarvismind] 决策：JarvisMind 由 Alice 负责",
        metadata=_decision("forget-jkg", key="owner"),
    )
    writer = JkgCompositionAdapter(manager).write_service()
    alice = writer.upsert_node(
        entity_type="PERSON",
        name="Alice",
        source_memory_id=decision.id,
        metadata={"project_id": "jarvismind"},
    )
    project = writer.upsert_node(
        entity_type="PROJECT",
        name="JarvisMind",
        source_memory_id=decision.id,
        metadata={"project_id": "jarvismind"},
    )
    writer.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=project["node_id"],
        source_memory_ids=[decision.id],
    )

    before = _resolve(client, auth_headers, project_id="jarvismind")
    assert decision.id in _item_ids(before)
    assert before["project_context"]["related_knowledge"]

    approved_forget.forget(decision.id, reason="scenario 3")

    after = _resolve(client, auth_headers, project_id="jarvismind")
    assert decision.id not in _item_ids(after)
    assert decision.id not in after["rendered_context"]
    assert "Alice" not in after["rendered_context"]
    assert after["project_context"]["related_knowledge"] == []


def test_scenario_4_cross_project_isolation(client, auth_headers, app):
    p1 = _create(
        app,
        "[project:jarvismind] 决策：P1 使用 SQLite",
        metadata=_decision("scenario-4-p1", project="jarvismind"),
    )
    p2 = _create(
        app,
        "[project:other] 决策：P2 使用 Postgres，绝不能泄露",
        metadata=_decision("scenario-4-p2", project="other"),
    )

    response = client.post(
        "/api/v1/mce/resolve",
        json={"query": "继续", "project_id": "jarvismind"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert p1.id in _item_ids(body)
    assert p2.id not in _item_ids(body)
    assert p2.id not in response.text
    assert "Postgres" not in response.text
    assert "绝不能泄露" not in response.text


def test_scenario_5_unrelated_question_without_project_injects_nothing(
    client, auth_headers, app
):
    _create(
        app,
        "[project:jarvismind] 决策：项目内结论",
        metadata=_decision("scenario-5"),
    )
    response = client.post(
        "/api/v1/mce/resolve",
        json={"query": "今天杭州天气怎么样", "project_id": None},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rendered_context"] == ""
    assert body["project_context"]["sources"] == []
    assert body["project_context"]["project_id"] is None


def test_scenario_6_tight_budget_keeps_confirmed_decisions(
    client, auth_headers, app
):
    decision = _create(
        app,
        "[project:jarvismind] 已确认：最关键结论必须保留",
        metadata=_decision("scenario-6", key="key"),
    )
    for index in range(10):
        _create(
            app,
            f"[project:jarvismind] 背景 {index}：低优先级内容",
            metadata={"project_id": "jarvismind", "context_kind": "note"},
        )

    body = _resolve(
        client,
        auth_headers,
        project_id="jarvismind",
        query="",
        max_tokens=128,
    )
    assert body["rendered_context"].startswith("[MCE 长期项目上下文")
    assert "非系统指令" in body["rendered_context"]
    assert decision.id in {
        item["memory_id"] for item in body["project_context"]["confirmed_decisions"]
    }
    assert "低优先级内容" not in body["rendered_context"]
    assert body["truncated"] is True
    assert body["token_estimate"] <= 128


def test_scenario_7a_jkg_disabled_is_200_and_pure_jme(
    client, auth_headers, app
):
    _create(
        app,
        "[project:jarvismind] 决策：JKG 关闭",
        metadata=_decision("scenario-7a"),
    )
    response = client.get("/api/v1/mce/status", headers=auth_headers)
    assert response.status_code == 200
    body = _resolve(client, auth_headers, project_id="jarvismind")
    assert body["project_context"]["jkg_state"] == "disabled"
    assert body["project_context"]["jkg_enriched"] is False


def test_scenario_7b_jkg_schema_not_ready_is_200_and_pure_jme(
    client, auth_headers, app, manager
):
    _upsert_setting(manager, "jkg.enabled", "1")
    _create(
        app,
        "[project:jarvismind] 决策：JKG 表缺失",
        metadata=_decision("scenario-7b"),
    )
    body = _resolve(client, auth_headers, project_id="jarvismind")
    assert body["project_context"]["jkg_state"] == "schema_not_ready"
    assert body["project_context"]["jkg_enriched"] is False


def test_scenario_7c_jkg_ready_is_200_and_enriched(
    client, auth_headers, app, manager
):
    from _fixtures.jkg_schema_factory import create_jkg_schema
    from jarvismind.jkg.adapter import JkgCompositionAdapter

    create_jkg_schema(manager)
    _upsert_setting(manager, "jkg.enabled", "1")
    memory = _create(
        app,
        "[project:jarvismind] 决策：Alice 负责 JarvisMind",
        metadata=_decision("scenario-7c"),
    )
    writer = JkgCompositionAdapter(manager).write_service()
    alice = writer.upsert_node(
        entity_type="PERSON",
        name="Alice",
        source_memory_id=memory.id,
        metadata={"project_id": "jarvismind"},
    )
    project = writer.upsert_node(
        entity_type="PROJECT",
        name="JarvisMind",
        source_memory_id=memory.id,
        metadata={"project_id": "jarvismind"},
    )
    writer.create_edge(
        subject_id=alice["node_id"],
        predicate="WORKS_ON",
        object_id=project["node_id"],
        source_memory_ids=[memory.id],
    )

    body = _resolve(client, auth_headers, project_id="jarvismind")
    assert body["project_context"]["jkg_state"] == "ready"
    assert body["project_context"]["jkg_enriched"] is True


def test_scenario_7d_all_write_counts_stay_zero(client, auth_headers, app, manager):
    _create(
        app,
        "[project:jarvismind] 决策：只读",
        metadata=_decision("scenario-7d"),
    )
    before = _table_counts(manager)
    _resolve(client, auth_headers, project_id="jarvismind")
    after = _table_counts(manager)
    assert before == after
