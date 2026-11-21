"""JW-02: governance of the four remaining lifecycle write paths.

Coverage groups (task book §5):

  A. switch gate        - each entry can be administratively disabled
  B. import integrity   - fabricated status / actor / business records refused
  C. merge consistency  - no repeat confirmation, no partial write, source checks
  D. summary review     - single transaction, idempotent, audit complete
  E. correction resolve - single transaction, honest before-state, idempotent
  F. bypass checks      - no old/alternate path reaches the same write
  G. identity policy    - NOT_VERIFIED everywhere, non-human actors refused

Every case runs on the shared tmp manager via ``create_app``. No real database.
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jarvismind.core.lifecycle_policy import (
    NON_HUMAN_ACTOR_TOKENS,
    SERVER_ACTOR_LABEL,
)
from jarvismind.database.models import MemoryMerge
from jarvismind.main import create_app

HEADERS = {"X-API-Token": "test-token-0123456789"}

LONG_A = "我居住在浙江金华市婺城区，长期在快运公司客服中心从事仲裁理赔工作"
LONG_B = "我养了两只猫，一只橘猫一只狸花猫，每天早晚各喂一次猫粮和清水"
# Two members of a SEMANTIC CONFLICT pair. The local conflict detector needs a
# genuine contradiction (same subject, opposite predicate), so these are the
# strings the detector actually flags - verified by the pre-existing
# test_detect_and_resolve_correction.
LONG_C = "用户偏好简洁直接的回答风格"
LONG_D = "用户偏好冗长详细的回答风格"


def _client(settings, manager, **overrides):
    """Build an app with the given settings overrides."""
    return TestClient(create_app(dataclasses.replace(settings, **overrides), manager=manager))


@pytest.fixture()
def jw02_client(settings, manager):
    """All four lifecycle switches ON, so behaviour can be exercised."""
    with _client(
        settings,
        manager,
        lifecycle_import_enabled=True,
        lifecycle_merge_enabled=True,
        lifecycle_summary_review_enabled=True,
        lifecycle_correction_resolve_enabled=True,
    ) as client:
        yield client


@pytest.fixture()
def jw02_no_raise(settings, manager):
    """Same, but server exceptions surface as 500 instead of propagating."""
    with TestClient(
        create_app(
            dataclasses.replace(
                settings,
                lifecycle_import_enabled=True,
                lifecycle_merge_enabled=True,
                lifecycle_summary_review_enabled=True,
                lifecycle_correction_resolve_enabled=True,
            ),
            manager=manager,
        ),
        raise_server_exceptions=False,
    ) as client:
        yield client


def _import(client, pairs, extra=None, actor=None):
    body = {
        "document": {
            "schema_version": 5,
            "memories": [
                {"id": mid, "memory_type": "SEMANTIC", "content": content}
                for mid, content in pairs
            ],
            **(extra or {}),
        }
    }
    if actor is not None:
        body["actor"] = actor
    return client.post("/api/v1/memories/import", json=body, headers=HEADERS)


def _status(client, memory_id):
    return client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS).json()["data"]


def _audit(client, memory_id):
    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc&page_size=100",
        headers=HEADERS,
    ).json()
    return [(e["action"], e["actor"], e["detail"]) for e in body["data"]]


def _conflict(client, left, right):
    resp = client.post(
        "/api/v1/memories/corrections/detect",
        json={"memory_ids": [left, right]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert rows, "expected a detected conflict"
    return rows[0]["id"]


# ============================================================ A. switch gate
def test_import_is_off_by_default(settings, manager):
    """Import must be OFF at the product default: it fabricates authoritative rows."""
    with _client(settings, manager, lifecycle_import_enabled=False) as client:
        resp = client.post(
            "/api/v1/memories/import",
            json={"document": {"schema_version": 5, "memories": []}},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "lifecycle_write_disabled"


def test_import_switch_settings_default_is_false(settings):
    """The declared default (not just the test override) is False."""
    from jarvismind.config import Settings

    assert Settings().lifecycle_import_enabled is False


def test_merge_switch_can_disable_the_entry(settings, manager):
    with _client(settings, manager, lifecycle_merge_enabled=False) as client:
        _import(client, [("sw-a", LONG_A), ("sw-b", LONG_B)])
        resp = client.post(
            "/api/v1/memories/merges/confirm",
            json={"memory_ids": ["sw-a", "sw-b"], "new_content": "禁用的合并"},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "lifecycle_write_disabled"
        assert _status(client, "sw-a")["status"] == "ACTIVE"


def test_summary_review_switch_can_disable_the_entry(settings, manager):
    with _client(
        settings, manager, lifecycle_summary_review_enabled=False
    ) as client:
        _import(client, [("sr-a", LONG_A), ("sr-b", LONG_B)])
        sid = client.post(
            "/api/v1/memories/summaries",
            json={"memory_ids": ["sr-a", "sr-b"]},
            headers=HEADERS,
        ).json()["data"]["id"]
        resp = client.post(
            f"/api/v1/memories/summaries/{sid}/review",
            json={"decision": "approve"},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "lifecycle_write_disabled"
        # No REFLECTION memory was created.
        active = client.get(
            "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
        ).json()["data"]
        assert not [m for m in active if m["memory_type"] == "REFLECTION"]


def test_correction_resolve_switch_can_disable_the_entry(settings, manager):
    with _client(
        settings, manager, lifecycle_correction_resolve_enabled=False
    ) as client:
        _import(client, [("cr-a", LONG_C), ("cr-b", LONG_D)])
        cid = _conflict(client, "cr-a", "cr-b")
        resp = client.post(
            f"/api/v1/memories/corrections/{cid}/resolve",
            json={"chosen_memory_id": "cr-a", "corrected_content": "不该写入"},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "lifecycle_write_disabled"
        assert _status(client, "cr-a")["content"] == LONG_C


def test_switch_cannot_be_opened_by_a_request_field(jw02_client, settings, manager):
    """No query/header/body value may flip a switch off-by-default entry."""
    with _client(settings, manager, lifecycle_import_enabled=False) as client:
        for attempt in (
            {"params": {"lifecycle_import_enabled": "true"}},
            {"headers": {**HEADERS, "X-Lifecycle-Import-Enabled": "1"}},
        ):
            resp = client.post(
                "/api/v1/memories/import",
                json={
                    "document": {"schema_version": 5, "memories": []},
                    "lifecycle_import_enabled": True,
                },
                headers=attempt.get("headers", HEADERS),
                params=attempt.get("params"),
            )
            assert resp.status_code == 403, attempt


# ========================================================= B. import integrity
def test_import_cannot_forge_deleted_status(jw02_client):
    """A document must not be able to present a memory as already forgotten."""
    # _import builds plain ACTIVE rows, so the forged status must be sent
    # explicitly to exercise the policy.
    doc = {
        "schema_version": 5,
        "memories": [
            {
                "id": "imp-del2",
                "memory_type": "SEMANTIC",
                "content": "第二条试图以已删除状态导入的独特内容XYZ",
                "status": "DELETED",
                "forgotten_at": "2020-01-01T00:00:00+00:00",
                "forget_reason": "forged",
            }
        ],
    }
    r2 = jw02_client.post(
        "/api/v1/memories/import", json={"document": doc}, headers=HEADERS
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["status_forced_to_active"] == 1
    record = _status(jw02_client, "imp-del2")
    assert record["status"] == "ACTIVE"
    assert record["forgotten_at"] is None
    assert record["forget_reason"] is None


def test_import_cannot_forge_merged_status(jw02_client):
    doc = {
        "schema_version": 5,
        "memories": [
            {
                "id": "imp-merged",
                "memory_type": "SEMANTIC",
                "content": "试图伪装成已合并状态导入的独特记忆内容QQQ",
                "status": "MERGED",
                "merged_into": "some-other-id",
            }
        ],
    }
    r = jw02_client.post(
        "/api/v1/memories/import", json={"document": doc}, headers=HEADERS
    )
    assert r.status_code == 200, r.text
    record = _status(jw02_client, "imp-merged")
    assert record["status"] == "ACTIVE"
    assert record.get("merged_into") is None


def test_import_actor_is_server_label_with_claim_kept_apart(jw02_client):
    resp = _import(
        jw02_client,
        [("imp-actor", "验证操作人伪造是否被阻断的独特记忆内容")],
        actor="完全伪造的操作人",
    )
    data = resp.json()["data"]
    assert data["actor"] == SERVER_ACTOR_LABEL
    assert data["client_actor_claim"] == "完全伪造的操作人"
    assert data["authentication_level"] == "API_TOKEN_SHARED"
    assert data["authorization_verification_result"] == "NOT_VERIFIED"

    entries = _audit(jw02_client, "imp-actor")
    imports = [e for e in entries if e[0] == "IMPORT"]
    assert imports, entries
    action, actor, detail = imports[0]
    assert actor == SERVER_ACTOR_LABEL
    assert actor != "完全伪造的操作人"
    assert detail["client_actor_claim"] == "完全伪造的操作人"
    assert detail["authorization_verification_result"] == "NOT_VERIFIED"


def test_import_refuses_completed_lifecycle_records(jw02_client, manager):
    """Sections that assert a FINISHED event are refused outright.

    ``archive_records`` and ``lifecycle_reviews`` have no PENDING state: each row
    claims "this memory was archived" / "this expiry was reviewed". A document
    cannot be allowed to assert that. The pending-state sections (summaries /
    corrections / merges) are a different case - see the next test.
    """
    doc = {
        "schema_version": 5,
        "memories": [
            {
                "id": "imp-rec",
                "memory_type": "SEMANTIC",
                "content": "验证业务记录伪造是否被拒绝的独特记忆内容WWW",
            }
        ],
        "archive_records": [
            {"id": "forged-arch", "memory_id": "imp-rec", "previous_status": "ACTIVE"}
        ],
        "lifecycle_reviews": [
            {
                "id": "forged-review",
                "memory_id": "imp-rec",
                "review_type": "EXPIRY_REVIEW",
                "action": "REVIEWED",
            }
        ],
    }
    resp = jw02_client.post(
        "/api/v1/memories/import", json={"document": doc}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["imported_archive_records"] == 0
    assert data["imported_lifecycle_reviews"] == 0
    assert set(data["rejected_sections"]) == {
        "archive_records",
        "lifecycle_reviews",
    }
    # The memory itself still imported (only the fabricated bookkeeping dropped).
    assert data["imported_memories"] == 1

    with manager.session() as session:
        for table, ident in (
            ("memory_archive_records", "forged-arch"),
            ("memory_lifecycle_reviews", "forged-review"),
        ):
            count = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE id = :i"), {"i": ident}
            ).scalar()
            assert int(count) == 0, table


def test_import_downgrades_pending_state_sections(jw02_client, manager):
    """Pending work items import, but can never import as already-decided.

    This is the round-trip requirement: an export/import cycle must restore the
    work queue. What it must NOT do is let a document claim a human already
    approved any of it, so status is forced to PENDING and every *_by /
    decided-at field is dropped.
    """
    doc = {
        "schema_version": 5,
        "memories": [
            {
                "id": "imp-p1",
                "memory_type": "SEMANTIC",
                "content": "我常驻浙江金华婺城区，负责快运破损与遗失的定责理赔核对工作",
            },
            {
                "id": "imp-p2",
                "memory_type": "SEMANTIC",
                "content": "家里养了两只猫，一只橘猫一只狸花猫，用自动喂食器定时投喂",
            },
        ],
        "summaries": [
            {
                "id": "forged-sum",
                "memory_ids": ["imp-p1", "imp-p2"],
                "content": "伪造的已确认总结",
                "status": "CONFIRMED",
                "confirmed_by": "伪造审核人",
                "confirmed_at": "2020-01-01T00:00:00+00:00",
            }
        ],
        "corrections": [
            {
                "id": "forged-cor",
                "memory_id_a": "imp-p1",
                "memory_id_b": "imp-p2",
                "status": "RESOLVED",
                "chosen_memory_id": "imp-p1",
                "resolved_by": "伪造解决人",
            }
        ],
        "merges": [
            {
                "id": "forged-mrg",
                "status": "CONFIRMED",
                "target_memory_id": "imp-p1",
                "merged_memory_ids": ["imp-p1", "imp-p2"],
                "resolved_by": "伪造合并人",
            }
        ],
    }
    resp = jw02_client.post(
        "/api/v1/memories/import", json={"document": doc}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # Diagnostics: a pending-state row is skipped as an orphan only when its
    # memory_ids are not present, so surface the counters on failure.
    assert data["imported_memories"] == 2, data
    assert data["imported_summaries"] == 1, data
    assert data["imported_corrections"] == 1, data
    assert data["imported_merges"] == 1, data
    assert data["status_forced_to_pending"] == 3, data
    assert data["rejected_sections"] == [], data

    with manager.session() as session:
        assert (
            session.execute(
                text("SELECT status FROM memory_summaries WHERE id='forged-sum'")
            ).scalar()
            == "PENDING"
        )
        assert (
            session.execute(
                text("SELECT confirmed_by FROM memory_summaries WHERE id='forged-sum'")
            ).scalar()
            is None
        )
        assert (
            session.execute(
                text("SELECT status FROM memory_corrections WHERE id='forged-cor'")
            ).scalar()
            == "PENDING"
        )
        assert (
            session.execute(
                text("SELECT resolved_by FROM memory_corrections WHERE id='forged-cor'")
            ).scalar()
            is None
        )
        assert (
            session.execute(
                text("SELECT status FROM memory_merges WHERE id='forged-mrg'")
            ).scalar()
            == "PENDING"
        )
        assert (
            session.execute(
                text("SELECT target_memory_id FROM memory_merges WHERE id='forged-mrg'")
            ).scalar()
            is None
        )


def test_import_refuses_non_human_actor(jw02_client):
    for label in ("model", "llm", "background-task"):
        resp = _import(jw02_client, [], actor=label)
        assert resp.status_code == 403, label
        assert resp.json()["error"]["code"] == "non_human_actor_forbidden", label


# ======================================================== C. merge consistency
def test_merge_creates_one_product_and_retires_originals(jw02_client):
    _import(jw02_client, [("mc-a", LONG_A), ("mc-b", LONG_B)])
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["mc-a", "mc-b"], "new_content": "合并产物唯一内容ABC"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["status"] == "CONFIRMED"
    product_id = body["target_memory_id"]

    for mid in ("mc-a", "mc-b"):
        record = _status(jw02_client, mid)
        assert record["status"] == "MERGED"
        assert record["merged_into"] == product_id
    assert _status(jw02_client, product_id)["status"] == "ACTIVE"

    # Originals leave the active list.
    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    active_ids = {m["id"] for m in active}
    assert "mc-a" not in active_ids and "mc-b" not in active_ids


def test_repeat_merge_confirmation_is_refused(jw02_client):
    """The measured JW-02 defect: one preview used to produce TWO products."""
    _import(jw02_client, [("mr-a", LONG_A), ("mr-b", LONG_B)])
    preview = jw02_client.post(
        "/api/v1/memories/merges/preview",
        json={"memory_ids": ["mr-a", "mr-b"]},
        headers=HEADERS,
    )
    assert preview.status_code == 200, preview.text
    merge_id = jw02_client.get(
        "/api/v1/memories/merges", params={"status": "PENDING"}, headers=HEADERS
    ).json()["data"][0]["id"]

    first = jw02_client.post(
        f"/api/v1/memories/merges/confirm?merge_id={merge_id}",
        json={"memory_ids": ["mr-a", "mr-b"], "new_content": "第一次合并产物"},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text
    product_id = first.json()["data"]["target_memory_id"]

    second = jw02_client.post(
        f"/api/v1/memories/merges/confirm?merge_id={merge_id}",
        json={"memory_ids": ["mr-a", "mr-b"], "new_content": "第二次合并产物"},
        headers=HEADERS,
    )
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "already_resolved"
    assert second.json()["error"]["details"]["reason"] == "merge_already_resolved"

    # Exactly one product exists, and the originals still point at it.
    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    products = [m for m in active if "合并产物" in m["content"]]
    assert len(products) == 1, products
    assert products[0]["id"] == product_id
    assert _status(jw02_client, "mr-a")["merged_into"] == product_id


def test_merge_refuses_already_merged_sources(jw02_client):
    """A merged source can never be merged again into a second product."""
    _import(jw02_client, [("mm-a", LONG_A), ("mm-b", LONG_B), ("mm-c", LONG_C)])
    assert (
        jw02_client.post(
            "/api/v1/memories/merges/confirm",
            json={"memory_ids": ["mm-a", "mm-b"], "new_content": "第一组合并产物"},
            headers=HEADERS,
        ).status_code
        == 200
    )
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["mm-a", "mm-c"], "new_content": "第二组合并产物"},
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "merge_source_not_mergeable"
    # mm-c untouched, and only one product exists.
    assert _status(jw02_client, "mm-c")["status"] == "ACTIVE"
    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert len([m for m in active if "合并产物" in m["content"]]) == 1


def test_merge_refuses_deleted_sources(jw02_client):
    """A forgotten memory cannot be used as a merge source."""
    _import(jw02_client, [("md-a", LONG_A), ("md-b", LONG_B)])
    created = jw02_client.post(
        "/api/v1/memories/md-a/forget-requests",
        json={"reason": "jw02"},
        headers=HEADERS,
    ).json()["data"]
    assert (
        jw02_client.post(
            f"/api/v1/forget-requests/{created['id']}/approve",
            json={"confirmation_token": created["confirmation_token"]},
            headers=HEADERS,
        ).status_code
        == 200
    )
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["md-a", "md-b"], "new_content": "不该产生的合并产物"},
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert _status(jw02_client, "md-a")["status"] == "DELETED"
    assert _status(jw02_client, "md-b")["status"] == "ACTIVE"


def test_merge_failure_leaves_no_partial_write(
    jw02_no_raise, manager, monkeypatch
):
    """The measured defect: product committed while originals stayed ACTIVE."""
    _import(jw02_no_raise, [("mp-a", LONG_A), ("mp-b", LONG_B)])
    client = jw02_no_raise

    from jarvismind.repositories.lifecycle_repository import LifecycleRepository

    original = LifecycleRepository.create_merge

    def boom(self, merge):
        raise RuntimeError("injected failure after product insert")

    monkeypatch.setattr(LifecycleRepository, "create_merge", boom)
    resp = client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["mp-a", "mp-b"], "new_content": "失败合并的产物内容ZZZ"},
        headers=HEADERS,
    )
    monkeypatch.undo()

    assert resp.status_code >= 500, resp.text
    # Nothing was committed: no product, originals still ACTIVE.
    active = client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert not [m for m in active if "失败合并的产物内容" in m["content"]]
    assert _status(client, "mp-a")["status"] == "ACTIVE"
    assert _status(client, "mp-b")["status"] == "ACTIVE"
    with manager.session() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM memory_merges WHERE status='CONFIRMED'")
        ).scalar()
    assert int(count) == 0


def test_merge_rejects_unknown_and_single_memory(jw02_client):
    only = _import(jw02_client, [("mz-a", LONG_A)]).json()["data"]
    assert only["imported_memories"] == 1
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["mz-a"]},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["mz-a", "nope"]},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_merge_audit_records_identity_not_client_claim(jw02_client):
    _import(jw02_client, [("ma-a", LONG_A), ("ma-b", LONG_B)])
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={
            "memory_ids": ["ma-a", "ma-b"],
            "new_content": "审计用的合并产物内容",
            "actor": "伪造的合并操作人",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    product_id = resp.json()["data"]["target_memory_id"]

    entries = {e[0]: e for e in _audit(jw02_client, product_id)}
    assert "MERGE_CONFIRM" in entries
    _, actor, detail = entries["MERGE_CONFIRM"]
    assert actor == SERVER_ACTOR_LABEL
    assert detail["client_actor_claim"] == "伪造的合并操作人"
    assert detail["authorization_verification_result"] == "NOT_VERIFIED"


def test_merge_refuses_non_human_actor(jw02_client):
    _import(jw02_client, [("mh-a", LONG_A), ("mh-b", LONG_B)])
    resp = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={
            "memory_ids": ["mh-a", "mh-b"],
            "new_content": "不该产生的合并",
            "actor": "model",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "non_human_actor_forbidden"
    assert _status(jw02_client, "mh-a")["status"] == "ACTIVE"


# ========================================================= D. summary review
def test_summary_approve_creates_reflection_in_one_transaction(jw02_client):
    _import(jw02_client, [("sv-a", LONG_A), ("sv-b", LONG_B)])
    sid = jw02_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["sv-a", "sv-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]

    resp = jw02_client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "approve"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "CONFIRMED"

    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    reflections = [m for m in active if m["memory_type"] == "REFLECTION"]
    assert len(reflections) == 1
    # Source memories are untouched.
    assert _status(jw02_client, "sv-a")["status"] == "ACTIVE"
    assert _status(jw02_client, "sv-b")["status"] == "ACTIVE"


def test_summary_approve_is_idempotent(jw02_client):
    _import(jw02_client, [("si-a", LONG_A), ("si-b", LONG_B)])
    sid = jw02_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["si-a", "si-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]
    assert (
        jw02_client.post(
            f"/api/v1/memories/summaries/{sid}/review",
            json={"decision": "approve"},
            headers=HEADERS,
        ).status_code
        == 200
    )
    second = jw02_client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "approve"},
        headers=HEADERS,
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "already_resolved"
    assert second.json()["error"]["details"]["reason"] == "summary_already_reviewed"
    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert len([m for m in active if m["memory_type"] == "REFLECTION"]) == 1


def test_summary_reject_writes_no_memory(jw02_client):
    _import(jw02_client, [("sj-a", LONG_A), ("sj-b", LONG_B)])
    sid = jw02_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["sj-a", "sj-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]
    resp = jw02_client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "reject", "reason": "不需要"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "REJECTED"
    active = jw02_client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert not [m for m in active if m["memory_type"] == "REFLECTION"]
    entries = [e for e in _audit(jw02_client, "sj-a") if e[0] == "SUMMARY_REJECT"]
    assert entries
    assert entries[0][1] == SERVER_ACTOR_LABEL


def test_summary_review_failure_leaves_summary_pending(
    jw02_no_raise, manager, monkeypatch
):
    """A failure while writing the REFLECTION memory must not confirm the summary."""
    _import(jw02_no_raise, [("sf-a", LONG_A), ("sf-b", LONG_B)])
    client = jw02_no_raise
    sid = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["sf-a", "sf-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]

    from jarvismind.repositories.lifecycle_repository import LifecycleRepository

    original = LifecycleRepository.update_summary

    def boom(self, summary, values):
        raise RuntimeError("injected failure during summary confirm")

    monkeypatch.setattr(LifecycleRepository, "update_summary", boom)
    resp = client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "approve"},
        headers=HEADERS,
    )
    monkeypatch.undo()

    assert resp.status_code >= 500, resp.text
    detail = client.get(
        f"/api/v1/memories/summaries/{sid}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "PENDING"
    active = client.get(
        "/api/v1/memories?status=ACTIVE&page_size=100", headers=HEADERS
    ).json()["data"]
    assert not [m for m in active if m["memory_type"] == "REFLECTION"]


def test_summary_review_refuses_non_human_actor(jw02_client):
    _import(jw02_client, [("sh-a", LONG_A), ("sh-b", LONG_B)])
    sid = jw02_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["sh-a", "sh-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]
    resp = jw02_client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "approve", "actor": "llm"},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "non_human_actor_forbidden"
    detail = jw02_client.get(
        f"/api/v1/memories/summaries/{sid}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "PENDING"


# ==================================================== E. correction resolve
def test_correction_resolve_rewrites_in_one_transaction(jw02_client):
    _import(jw02_client, [("cv-a", LONG_C), ("cv-b", LONG_D)])
    cid = _conflict(jw02_client, "cv-a", "cv-b")
    resp = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "cv-a", "corrected_content": "改写后的正确内容"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "RESOLVED"
    assert _status(jw02_client, "cv-a")["content"] == "改写后的正确内容"
    # The losing memory keeps its content.
    assert _status(jw02_client, "cv-b")["content"] == LONG_D


def test_correction_before_state_records_the_real_previous_content(jw02_client):
    """The old code stored the POST-update value as "before"."""
    _import(jw02_client, [("cb-a", LONG_C), ("cb-b", LONG_D)])
    cid = _conflict(jw02_client, "cb-a", "cb-b")
    resp = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "cb-a", "corrected_content": "新内容"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    after_state = resp.json()["data"]["after_state"]
    assert after_state["cb-a"]["content"] == LONG_C, after_state


def test_correction_resolve_is_idempotent(jw02_client):
    _import(jw02_client, [("ci-a", LONG_C), ("ci-b", LONG_D)])
    cid = _conflict(jw02_client, "ci-a", "ci-b")
    first = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "ci-a", "corrected_content": "第一次改写"},
        headers=HEADERS,
    )
    assert first.status_code == 200
    second = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "ci-a", "corrected_content": "第二次改写"},
        headers=HEADERS,
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "already_resolved"
    assert second.json()["error"]["details"]["reason"] == "correction_already_resolved"
    assert _status(jw02_client, "ci-a")["content"] == "第一次改写"


def test_correction_resolve_without_content_changes_nothing(jw02_client):
    _import(jw02_client, [("cn-a", LONG_C), ("cn-b", LONG_D)])
    cid = _conflict(jw02_client, "cn-a", "cn-b")
    resp = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "cn-a"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert _status(jw02_client, "cn-a")["content"] == LONG_C


def test_correction_refuses_deleted_target(jw02_client):
    """A forgotten memory must not be rewritten by a correction."""
    _import(jw02_client, [("cd-a", LONG_C), ("cd-b", LONG_D)])
    cid = _conflict(jw02_client, "cd-a", "cd-b")
    created = jw02_client.post(
        "/api/v1/memories/cd-a/forget-requests",
        json={"reason": "jw02"},
        headers=HEADERS,
    ).json()["data"]
    assert (
        jw02_client.post(
            f"/api/v1/forget-requests/{created['id']}/approve",
            json={"confirmation_token": created["confirmation_token"]},
            headers=HEADERS,
        ).status_code
        == 200
    )
    resp = jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "cd-a", "corrected_content": "不该写入的内容"},
        headers=HEADERS,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "correction_target_not_editable"
    assert _status(jw02_client, "cd-a")["status"] == "DELETED"


def test_correction_resolve_failure_leaves_memory_untouched(
    jw02_no_raise, manager, monkeypatch
):
    _import(jw02_no_raise, [("cf-a", LONG_C), ("cf-b", LONG_D)])
    client = jw02_no_raise
    cid = _conflict(client, "cf-a", "cf-b")

    from jarvismind.repositories.lifecycle_repository import LifecycleRepository

    original = LifecycleRepository.update_correction

    def boom(self, correction, values):
        raise RuntimeError("injected failure after content rewrite")

    monkeypatch.setattr(LifecycleRepository, "update_correction", boom)
    resp = client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "cf-a", "corrected_content": "不该保留的改写"},
        headers=HEADERS,
    )
    monkeypatch.undo()

    assert resp.status_code >= 500, resp.text
    # The rewrite rolled back with the decision record.
    assert _status(client, "cf-a")["content"] == LONG_C
    detail = client.get(
        f"/api/v1/memories/corrections/{cid}", headers=HEADERS
    ).json()["data"]
    assert detail["status"] == "PENDING"


# =========================================================== F. bypass checks
def test_import_cannot_be_reached_through_another_route(jw02_client):
    """No alternate verb/path performs the import write."""
    for method, path in (
        ("put", "/api/v1/memories/import"),
        ("patch", "/api/v1/memories/import"),
        ("post", "/api/v1/memories/import/bulk"),
        ("post", "/api/v1/memories/import-now"),
    ):
        resp = getattr(jw02_client, method)(path, json={}, headers=HEADERS)
        assert resp.status_code in (404, 405), (method, path, resp.status_code)


def test_merge_confirm_cannot_bypass_source_validation(jw02_client):
    """merge_id pointing at a different memory set is still refused."""
    _import(
        jw02_client,
        [("by-a", LONG_A), ("by-b", LONG_B), ("by-c", LONG_C), ("by-d", LONG_D)],
    )
    assert (
        jw02_client.post(
            "/api/v1/memories/merges/preview",
            json={"memory_ids": ["by-a", "by-b"]},
            headers=HEADERS,
        ).status_code
        == 200
    )
    merge_id = jw02_client.get(
        "/api/v1/memories/merges", params={"status": "PENDING"}, headers=HEADERS
    ).json()["data"][0]["id"]
    resp = jw02_client.post(
        f"/api/v1/memories/merges/confirm?merge_id={merge_id}",
        json={"memory_ids": ["by-c", "by-d"], "new_content": "错配的合并"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    for mid in ("by-a", "by-b", "by-c", "by-d"):
        assert _status(jw02_client, mid)["status"] == "ACTIVE"


def test_forget_flow_still_requires_approval_after_jw02(jw02_client):
    """JW-01 must stay intact: an unapproved delete is still refused."""
    _import(jw02_client, [("fw-a", LONG_A)])
    resp = jw02_client.delete("/api/v1/memories/fw-a", headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    assert _status(jw02_client, "fw-a")["status"] == "ACTIVE"


def test_forget_approval_still_executes_after_jw02(jw02_client):
    _import(jw02_client, [("fa-a", LONG_A)])
    created = jw02_client.post(
        "/api/v1/memories/fa-a/forget-requests",
        json={"reason": "jw02 regression"},
        headers=HEADERS,
    ).json()["data"]
    resp = jw02_client.post(
        f"/api/v1/forget-requests/{created['id']}/approve",
        json={"confirmation_token": created["confirmation_token"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["memory_forgotten"] is True
    assert _status(jw02_client, "fa-a")["status"] == "DELETED"


# ========================================================== G. identity policy
def test_identity_policy_agrees_with_jw01():
    """The two policy implementations must not drift apart."""
    from jarvismind.services import forget_service

    assert SERVER_ACTOR_LABEL == forget_service.SERVER_ACTOR_LABEL
    assert NON_HUMAN_ACTOR_TOKENS == forget_service.NON_HUMAN_ACTOR_TOKENS


def test_every_lifecycle_entry_reports_not_verified(jw02_client):
    """No lifecycle path may claim a verified personal identity."""
    resp = _import(jw02_client, [("id-a", LONG_A)], actor="某人")
    data = resp.json()["data"]
    assert data["authentication_level"] == "API_TOKEN_SHARED"
    assert data["authorization_verification_result"] == "NOT_VERIFIED"

    _import(jw02_client, [("id-b", LONG_C)])
    merge = jw02_client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": ["id-a", "id-b"], "new_content": "身份验证用的合并产物"},
        headers=HEADERS,
    )
    assert merge.status_code == 200, merge.text
    product = merge.json()["data"]["target_memory_id"]
    entries = {e[0]: e for e in _audit(jw02_client, product)}
    assert (
        entries["MERGE_CONFIRM"][2]["authorization_verification_result"]
        == "NOT_VERIFIED"
    )


def test_actor_column_is_never_the_client_string(jw02_client, manager):
    """Across the three write paths, no identity column holds a client claim."""
    _import(jw02_client, [("ac-a", LONG_C), ("ac-b", LONG_D)])
    sid = jw02_client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["ac-a", "ac-b"]},
        headers=HEADERS,
    ).json()["data"]["id"]
    jw02_client.post(
        f"/api/v1/memories/summaries/{sid}/review",
        json={"decision": "approve", "actor": "伪造审核人"},
        headers=HEADERS,
    )
    cid = _conflict(jw02_client, "ac-a", "ac-b")
    jw02_client.post(
        f"/api/v1/memories/corrections/{cid}/resolve",
        json={"chosen_memory_id": "ac-a", "actor": "伪造解决人"},
        headers=HEADERS,
    )
    with manager.session() as session:
        confirmed_by = session.execute(
            text("SELECT confirmed_by FROM memory_summaries WHERE id = :i"),
            {"i": sid},
        ).scalar()
        resolved_by = session.execute(
            text("SELECT resolved_by FROM memory_corrections WHERE id = :i"),
            {"i": cid},
        ).scalar()
    assert confirmed_by == SERVER_ACTOR_LABEL
    assert resolved_by == SERVER_ACTOR_LABEL
    assert "伪造审核人" not in (confirmed_by or "")
    assert "伪造解决人" not in (resolved_by or "")
