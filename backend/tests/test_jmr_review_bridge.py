"""JMR-03 review bridge tests.

Covers: generic lifecycle endpoints cannot review JMR rows; a real human
approve goes through the existing lifecycle service; source changes and TTL
invalidate the proposal; review audit evidence exists; non-human actors are
refused; repeated review is refused without a second authoritative write.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from jarvismind.governance.audit_bus import AuditBus
from jarvismind.governance import build_governance, deactivate_governance
from jarvismind.governance.schema import install_p1_schema

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"

_INSERT = (
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, :importance, :confidence, :status, "
    ":created_at, :updated_at, :source_id, :metadata_json, :search_text)"
)


def _row(memory_id, content, memory_type, created_at):
    return {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "importance": 5.0,
        "confidence": 0.9,
        "status": "ACTIVE",
        "created_at": created_at,
        "updated_at": created_at,
        "source_id": None,
        "metadata_json": None,
        "search_text": content,
    }


def _seed(manager):
    specs = [
        _row("M-DUP-A", "Bifrost 网关采用 PostgreSQL 主从架构", "JMR_DUP", TS1),
        _row(
            "M-DUP-B",
            "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
            "JMR_DUP",
            TS2,
        ),
        _row("M-ST-1", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS1),
        _row("M-ST-2", "Phoenix 项目当前状态为已完成", "JMR_STATUS", TS2),
        _row("M-CF-1", "Nebula 缓存层使用 Redis Cluster 三主三从部署", "JMR_CONFLICT", TS1),
        _row("M-CF-2", "Nebula 缓存层改用 Memcached 单机方案降低成本", "JMR_CONFLICT", TS2),
    ]
    with manager.session() as session:
        for spec in specs:
            session.execute(text(_INSERT), spec)


def _run(client, auth_headers, task_id):
    resp = client.post(
        "/api/v1/jmr/runs",
        json={"task_id": task_id, "run_id": "R-" + task_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "SUCCEEDED"


def _proposal(manager, rule):
    tables = {
        "SUMMARY": "memory_summaries",
        "MERGE": "memory_merges",
        "CONFLICT": "memory_corrections",
        "STATUS_CHANGE": "memory_corrections",
    }
    table = tables[rule]
    with manager.session() as session:
        rows = session.execute(
            text(f"SELECT id, metadata_json FROM {table}")
        ).mappings().all()
    for row in rows:
        meta = json.loads(row["metadata_json"] or "{}")
        if meta.get("rule") == rule:
            return row["id"], meta
    raise AssertionError(f"no {rule} proposal")


def _table_count(manager, table):
    with manager.session() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def _audit_actions(manager):
    return [e["action"] for e in AuditBus(manager).p1_entries(limit=1000)]


def test_generic_summary_review_is_refused_for_jmr_row(
    client, auth_headers, manager
):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-GUARD")
    summary_id, _ = _proposal(manager, "SUMMARY")
    resp = client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "approve", "actor": "user"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "jmr_review_required"
    # The row is still PENDING: the generic endpoint did not silently approve it.
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_summaries WHERE id=:i"),
            {"i": summary_id},
        ).scalar()
    assert status == "PENDING"


def test_generic_merge_confirm_is_refused_for_jmr_row(client, auth_headers, manager):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-GUARD-MERGE")
    merge_id, meta = _proposal(manager, "MERGE")
    resp = client.post(
        "/api/v1/memories/merges/confirm",
        params={"merge_id": merge_id},
        json={
            "memory_ids": meta["source_memory_ids"],
            "actor": "user",
            "reason": "should be refused",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "jmr_review_required"
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_merges WHERE id=:i"), {"i": merge_id}
        ).scalar()
    assert status == "PENDING"


def test_approve_summary_through_bridge_creates_one_reflection_memory(
    client, auth_headers, manager
):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-OK")
    summary_id, _ = _proposal(manager, "SUMMARY")
    before = _table_count(manager, "memory_records")

    resp = client.post(
        f"/api/v1/jmr/proposals/{summary_id}/review",
        json={"decision": "approve", "actor": "user", "reason": "reviewed"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["authoritative"] is False
    assert data["audit_id"]
    assert data["review"]["source"] == "jmr_review_bridge"

    assert _table_count(manager, "memory_records") == before + 1
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_summaries WHERE id=:i"),
            {"i": summary_id},
        ).scalar()
    assert status == "CONFIRMED"
    assert "P1:JMR_REVIEW:APPROVED" in _audit_actions(manager)

    # A second review must not create a second REFLECTION memory.
    again = client.post(
        f"/api/v1/jmr/proposals/{summary_id}/review",
        json={"decision": "approve", "actor": "user"},
        headers=auth_headers,
    )
    assert again.status_code == 409, again.text
    assert _table_count(manager, "memory_records") == before + 1


def test_reject_conflict_through_bridge_does_not_rewrite_memory(
    client, auth_headers, manager
):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-REJECT")
    conflict_id, _ = _proposal(manager, "CONFLICT")
    with manager.session() as session:
        before = [tuple(r) for r in session.execute(
            text("SELECT id, content, status, updated_at FROM memory_records ORDER BY id")
        )]

    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "reject", "actor": "user", "reason": "not a conflict"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "REJECTED"
    with manager.session() as session:
        after = [tuple(r) for r in session.execute(
            text("SELECT id, content, status, updated_at FROM memory_records ORDER BY id")
        )]
        status = session.execute(
            text("SELECT status FROM memory_corrections WHERE id=:i"),
            {"i": conflict_id},
        ).scalar()
    assert after == before  # reject never rewrites source memories
    assert status == "REJECTED"


def test_changed_source_voids_proposal_and_denies_review(
    client, auth_headers, manager
):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-STALE")
    conflict_id, meta = _proposal(manager, "CONFLICT")
    source_id = meta["source_memory_ids"][0]
    with manager.session() as session:
        session.execute(
            text(
                "UPDATE memory_records SET content=:c, updated_at=:u WHERE id=:i"
            ),
            {
                "c": "Nebula 缓存层方案已经彻底改变",
                "u": "2026-09-01T00:00:00+00:00",
                "i": source_id,
            },
        )

    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "approve", "actor": "user"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "jmr_source_stale"
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_corrections WHERE id=:i"),
            {"i": conflict_id},
        ).scalar()
    assert status == "VOID"
    assert "P1:JMR_REVIEW:DENIED" in _audit_actions(manager)


def test_expired_proposal_is_voided_and_not_reviewable(
    client, auth_headers, manager
):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-TTL")
    conflict_id, meta = _proposal(manager, "CONFLICT")
    meta["expires_at"] = "2000-01-01T00:00:00+00:00"
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_corrections SET metadata_json=:m WHERE id=:i"),
            {"m": json.dumps(meta, ensure_ascii=False, sort_keys=True), "i": conflict_id},
        )

    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "reject", "actor": "user"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "jmr_proposal_expired"
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_corrections WHERE id=:i"),
            {"i": conflict_id},
        ).scalar()
    assert status == "VOID"


def test_review_requires_api_token(client, auth_headers, manager):
    """The review endpoint must not be reachable without the shared token."""
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-TOKEN")
    conflict_id, _ = _proposal(manager, "CONFLICT")

    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "reject", "actor": "user"},
    )
    assert resp.status_code == 401, resp.text
    with manager.session() as session:
        status = session.execute(
            text("SELECT status FROM memory_corrections WHERE id=:i"),
            {"i": conflict_id},
        ).scalar()
    assert status == "PENDING"


def test_human_like_actor_is_only_a_claim_not_verified_identity(
    client, auth_headers, manager
):
    """A shared token authenticates a client, not a person.

    ``actor`` is persisted only as ``client_actor_claim``; the audit actor is
    the server-written bridge principal and the verification result is fixed
    to NOT_VERIFIED. This test pins that separation in place.
    """
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-CLAIM")
    conflict_id, _ = _proposal(manager, "CONFLICT")

    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "reject", "actor": "administrator", "reason": "claim"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "REJECTED"

    entries = [
        entry
        for entry in AuditBus(manager).p1_entries(limit=1000)
        if entry["action"] == "P1:JMR_REVIEW:REJECTED"
    ]
    assert entries, "expected a JMR review audit row"
    entry = entries[-1]
    assert entry["actor"] == "svc:jmr-review-bridge"
    extra = entry["detail"]["extra"]
    assert extra["client_actor_claim"] == "administrator"
    assert extra["authentication_level"] == "API_TOKEN_SHARED"
    assert extra["authorization_verification_result"] == "NOT_VERIFIED"


def test_non_human_actor_is_refused_by_bridge(client, auth_headers, manager):
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-ACTOR")
    conflict_id, _ = _proposal(manager, "CONFLICT")
    resp = client.post(
        f"/api/v1/jmr/proposals/{conflict_id}/review",
        json={"decision": "approve", "actor": "system"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "non_human_actor_forbidden"


def test_governed_summary_approve_through_bridge_succeeds(
    client, auth_headers, manager, settings
):
    """The bridge must cross the active P1 write gate via the service boundary."""
    install_p1_schema(manager)
    _seed(manager)
    _run(client, auth_headers, "T-BRIDGE-GOV")
    summary_id, _ = _proposal(manager, "SUMMARY")
    before = _table_count(manager, "memory_records")
    build_governance(manager, settings, enforce=True)
    try:
        resp = client.post(
            f"/api/v1/jmr/proposals/{summary_id}/review",
            json={"decision": "approve", "actor": "user", "reason": "governed"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "APPROVED"
        assert _table_count(manager, "memory_records") == before + 1
    finally:
        deactivate_governance()
