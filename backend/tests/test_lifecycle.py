"""JME-5 lifecycle tests: summaries, corrections, merges, expiry, archive, forget."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jarvismind.repositories.memory_repository import MemoryRepository


def _create(client, headers, content="test memory", mtype="SEMANTIC", **kw):
    resp = client.post(
        "/api/v1/memories",
        json={"memory_type": mtype, "content": content, **kw},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _audit_actions(client, headers, memory_id):
    body = client.get(
        f"/api/v1/memories/{memory_id}/audit-logs?sort_order=asc", headers=headers
    ).json()
    return [entry["action"] for entry in body["data"]]


# ------------------------------------------------------------------ summaries
def test_create_summary_is_pending_and_retained(client, auth_headers):
    first = _create(client, auth_headers, "用户偏好简洁回答")
    second = _create(client, auth_headers, "用户使用 Python 开发")
    resp = client.post(
        "/api/v1/memories/summaries",
        json={
            "memory_ids": [first["id"], second["id"]],
            "summary_type": "COLLECTION",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    summary = resp.json()["data"]
    assert summary["status"] == "PENDING"
    assert summary["memory_ids"] == [first["id"], second["id"]]
    assert summary["content"]
    # Originals are untouched.
    assert client.get(
        f"/api/v1/memories/{first['id']}", headers=auth_headers
    ).json()["data"]["content"] == "用户偏好简洁回答"


def test_category_summary_requires_memory_type(client, auth_headers):
    first = _create(client, auth_headers, "profile note", mtype="PROFILE")
    resp = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first["id"]], "summary_type": "CATEGORY"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_approve_summary_creates_reflection_memory(client, auth_headers):
    first = _create(client, auth_headers, "用户偏好简洁回答")
    summary_id = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first["id"]], "summary_type": "COLLECTION"},
        headers=auth_headers,
    ).json()["data"]["id"]
    resp = client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "approve"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "CONFIRMED"
    listed = client.get(
        "/api/v1/memories?memory_type=REFLECTION", headers=auth_headers
    ).json()
    assert listed["meta"]["total"] == 1
    assert listed["data"][0]["summary"] == resp.json()["data"]["title"]


def test_reject_summary(client, auth_headers):
    first = _create(client, auth_headers, "some note")
    summary_id = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first["id"]], "summary_type": "COLLECTION"},
        headers=auth_headers,
    ).json()["data"]["id"]
    resp = client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "reject", "reason": "low quality"},
        headers=auth_headers,
    )
    assert resp.json()["data"]["status"] == "REJECTED"
    assert resp.json()["data"]["reject_reason"] == "low quality"


def test_summary_unknown_memory_returns_404(client, auth_headers):
    resp = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": ["nope"], "summary_type": "COLLECTION"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# --------------------------------------------------------------- corrections
def test_detect_and_resolve_correction(client, auth_headers):
    left = _create(client, auth_headers, "用户偏好简洁直接的回答风格")
    right = _create(client, auth_headers, "用户偏好冗长详细的回答风格")
    resp = client.post(
        "/api/v1/memories/corrections/detect",
        json={"memory_ids": [left["id"], right["id"]]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    corrections = resp.json()["data"]
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction["status"] == "PENDING"
    assert set((correction["memory_id_a"], correction["memory_id_b"])) == {
        left["id"],
        right["id"],
    }

    resolved = client.post(
        f"/api/v1/memories/corrections/{correction['id']}/resolve",
        json={
            "chosen_memory_id": left["id"],
            "corrected_content": "用户偏好简洁直接的回答风格（已确认）",
            "reason": "newer statement",
        },
        headers=auth_headers,
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()["data"]
    assert body["status"] == "RESOLVED"
    assert body["chosen_memory_id"] == left["id"]
    # JW-02 FIXED THE MEANING OF THIS FIELD. ``after_state`` used to record the
    # content AFTER the rewrite (i.e. the new value), so the "before" it was
    # meant to preserve never existed anywhere. It now captures the real prior
    # value, which is what makes a rewrite reviewable/reversible.
    assert body["after_state"][left["id"]]["content"] == (
        "用户偏好简洁直接的回答风格"
    )
    # The new content is applied to the memory itself.
    assert client.get(
        f"/api/v1/memories/{left['id']}", headers=auth_headers
    ).json()["data"]["content"].endswith("（已确认）")
    # The losing memory is retained unchanged.
    assert client.get(
        f"/api/v1/memories/{right['id']}", headers=auth_headers
    ).json()["data"]["status"] == "ACTIVE"


def test_correction_reject(client, auth_headers):
    left = _create(client, auth_headers, "用户偏好简洁直接的回答风格")
    right = _create(client, auth_headers, "用户偏好冗长详细的回答风格")
    correction_id = client.post(
        "/api/v1/memories/corrections/detect",
        json={"memory_ids": [left["id"], right["id"]]},
        headers=auth_headers,
    ).json()["data"][0]["id"]
    resp = client.post(
        f"/api/v1/memories/corrections/{correction_id}/reject",
        json={"reason": "not a real conflict"},
        headers=auth_headers,
    )
    assert resp.json()["data"]["status"] == "REJECTED"


def test_correction_requires_valid_chosen(client, auth_headers):
    left = _create(client, auth_headers, "用户偏好简洁直接的回答风格")
    right = _create(client, auth_headers, "用户偏好冗长详细的回答风格")
    correction_id = client.post(
        "/api/v1/memories/corrections/detect",
        json={"memory_ids": [left["id"], right["id"]]},
        headers=auth_headers,
    ).json()["data"][0]["id"]
    resp = client.post(
        f"/api/v1/memories/corrections/{correction_id}/resolve",
        json={"chosen_memory_id": "bogus-id", "reason": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/v1/memories/corrections/{correction_id}/resolve",
        json={"corrected_content": "missing chosen id"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


# -------------------------------------------------------------------- merges
def test_merge_preview_and_confirm(client, auth_headers):
    first = _create(client, auth_headers, "项目 A 使用 SQLite 作为存储")
    second = _create(client, auth_headers, "项目 A 使用 SQLite 作为存储引擎")
    preview = client.post(
        "/api/v1/memories/merges/preview",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["new_content"]

    confirmed = client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()["data"]
    assert body["status"] == "CONFIRMED"
    merged_id = body["target_memory_id"]
    assert merged_id
    # Merged memory is active and searchable.
    merged = client.get(
        f"/api/v1/memories/{merged_id}", headers=auth_headers
    ).json()["data"]
    assert merged["status"] == "ACTIVE"
    assert merged["memory_type"] == "SEMANTIC"
    # Originals are retained as MERGED, never deleted.
    for original in (first, second):
        record = client.get(
            f"/api/v1/memories/{original['id']}", headers=auth_headers
        ).json()["data"]
        assert record["status"] == "MERGED"
        assert record["merged_into"] == merged_id
        assert record["content"] == original["content"]


def test_merged_memories_hidden_from_active_list(client, auth_headers):
    first = _create(client, auth_headers, "项目 A 使用 SQLite 作为存储")
    second = _create(client, auth_headers, "项目 A 使用 SQLite 作为存储引擎")
    client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    )
    active = client.get("/api/v1/memories", headers=auth_headers).json()
    ids = {item["id"] for item in active["data"]}
    assert first["id"] not in ids
    assert second["id"] not in ids


def test_merge_requires_two_memories(client, auth_headers):
    only = _create(client, auth_headers, "only one")
    resp = client.post(
        "/api/v1/memories/merges/confirm",
        json={"memory_ids": [only["id"]]},
        headers=auth_headers,
    )
    assert resp.status_code == 422


# -------------------------------------------------------------------- expiry
def _soon(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_expiring_and_expired_lists(client, auth_headers):
    soon = _create(
        client, auth_headers, "即将过期的记忆", expires_at=_soon(2)
    )
    later = _create(
        client, auth_headers, "很久以后才过期", expires_at=_soon(30)
    )
    expired = _create(
        client,
        auth_headers,
        "已经过期的记忆",
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    expiring = client.get(
        "/api/v1/memories/expiring?days=7", headers=auth_headers
    ).json()
    ids = {item["memory_id"] for item in expiring["data"]}
    assert soon["id"] in ids
    assert later["id"] not in ids
    assert all(item["status"] == "EXPIRING" for item in expiring["data"])

    expired_body = client.get("/api/v1/memories/expired", headers=auth_headers).json()
    expired_ids = {item["memory_id"] for item in expired_body["data"]}
    assert expired["id"] in expired_ids
    assert soon["id"] not in expired_ids


def test_extend_expiry_keeps_memory_active(client, auth_headers):
    record = _create(
        client, auth_headers, "需要延期的记忆", expires_at=_soon(1)
    )
    new_expiry = _soon(30)
    resp = client.post(
        f"/api/v1/memories/{record['id']}/expiry-review",
        json={"action": "EXTENDED", "new_expires_at": new_expiry},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "EXTENDED"
    current = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert current["status"] == "ACTIVE"
    assert current["expires_at"] == new_expiry
    assert record["id"] not in {
        item["memory_id"]
        for item in client.get(
            "/api/v1/memories/expiring?days=7", headers=auth_headers
        ).json()["data"]
    }


def test_expired_memory_is_not_auto_deleted(client, auth_headers):
    record = _create(
        client,
        auth_headers,
        "过期但必须保留的记忆",
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    body = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert body["status"] == "ACTIVE"


# ------------------------------------------------------------------ archives
def test_archive_and_restore(client, auth_headers):
    record = _create(client, auth_headers, "待归档的记忆")
    archived = client.post(
        f"/api/v1/memories/{record['id']}/archive",
        json={"reason": "outdated", "source": "test"},
        headers=auth_headers,
    )
    assert archived.status_code == 200, archived.text
    body = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert body["status"] == "ARCHIVED"

    records = client.get(
        "/api/v1/memories/archive-records", headers=auth_headers
    ).json()
    assert records["meta"]["total"] == 1
    assert records["data"][0]["reason"] == "outdated"
    assert records["data"][0]["previous_status"] == "ACTIVE"
    assert records["data"][0]["restored_at"] is None

    restored = client.post(
        f"/api/v1/memories/{record['id']}/restore", headers=auth_headers
    )
    assert restored.status_code == 200
    assert restored.json()["data"] is True
    body = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert body["status"] == "ACTIVE"
    records = client.get(
        "/api/v1/memories/archive-records", headers=auth_headers
    ).json()
    assert records["data"][0]["restored_at"] is not None


def test_archive_twice_rejected(client, auth_headers):
    record = _create(client, auth_headers, "archive twice")
    client.post(
        f"/api/v1/memories/{record['id']}/archive",
        json={"reason": "first"},
        headers=auth_headers,
    )
    resp = client.post(
        f"/api/v1/memories/{record['id']}/archive",
        json={"reason": "second"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_archive_sets_vector_status_removed(client, auth_headers, manager):
    record = _create(client, auth_headers, "archive vector check")
    with manager.session() as session:
        assert (
            MemoryRepository(session).get_by_id(record["id"]).vector_status
            == "INDEXED"
        )
    client.post(
        f"/api/v1/memories/{record['id']}/archive",
        json={"reason": "issue A"},
        headers=auth_headers,
    )
    with manager.session() as session:
        assert (
            MemoryRepository(session).get_by_id(record["id"]).vector_status
            == "REMOVED"
        )


def test_soft_delete_sets_vector_status_removed(
    client, auth_headers, manager, approved_forget
):
    """JW-01: the vector point is still removed, now via the approved flow."""
    record = _create(client, auth_headers, "delete vector check")
    approved_forget.forget(record["id"])
    with manager.session() as session:
        assert (
            MemoryRepository(session).get_by_id(record["id"]).vector_status
            == "REMOVED"
        )


# ------------------------------------------------------------------- forget
def test_forget_requires_confirmation(client, auth_headers):
    """JW-01: a bare ``confirm`` flag no longer authorises anything.

    Previously this case returned 422 because ``confirm`` was absent against a
    schema that did not require it. It now returns 403 ``forget_approval_required``
    because the endpoint demands a real approved forget request - the memory
    stays ACTIVE in both cases, which is the property this test protects.
    """
    record = _create(client, auth_headers, "需要确认才能遗忘")
    resp = client.post(
        "/api/v1/memories/forget",
        json={"memory_ids": [record["id"]], "reason": "stale"},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    body = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert body["status"] == "ACTIVE"


def test_forget_by_id_removes_vector(client, auth_headers, manager, approved_forget):
    """JW-01: same end state (DELETED + vector REMOVED), approved path only."""
    record = _create(client, auth_headers, "将被遗忘的记忆")
    approved_forget.approve(record["id"], reason="stale")

    current = client.get(
        f"/api/v1/memories/{record['id']}", headers=auth_headers
    ).json()["data"]
    assert current["status"] == "DELETED"
    with manager.session() as session:
        db_record = MemoryRepository(session).get_by_id(record["id"])
        assert db_record.status == "DELETED"
        assert db_record.forgotten_at is not None
        assert db_record.forget_reason == "stale"
        assert db_record.vector_status == "REMOVED"


def test_forget_refuses_full_wipe_without_ids(client, auth_headers):
    """A ``memory_type`` selector cannot be used to wipe a whole type.

    Before JW-01 this was a 422 from the service's full-wipe guard; now the
    request never reaches that guard because no approved request identifies a
    single memory. Both outcomes refuse the wipe.
    """
    for index in range(3):
        _create(client, auth_headers, f"bulk memory {index}")
    resp = client.post(
        "/api/v1/memories/forget",
        json={"memory_type": "SEMANTIC", "confirm": True},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    # Nothing was deleted.
    assert (
        client.get(
            "/api/v1/memories?status=ACTIVE", headers=auth_headers
        ).json()["meta"]["total"]
        == 3
    )


def test_forget_by_keyword(client, auth_headers):
    """A keyword selector cannot forget anything without per-memory approval."""
    keep = _create(client, auth_headers, "保留的普通记忆")
    drop = _create(client, auth_headers, "包含特殊标记 ZZZFORGET 的记忆")
    resp = client.post(
        "/api/v1/memories/forget",
        json={"query": "ZZZFORGET", "confirm": True},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    # Both memories survive: keyword-based bulk forgetting is no longer a path.
    for memory in (keep, drop):
        assert (
            client.get(
                f"/api/v1/memories/{memory['id']}", headers=auth_headers
            ).json()["data"]["status"]
            == "ACTIVE"
        )


def test_forget_bulk_with_approval_is_refused(client, auth_headers, approved_forget):
    """Even WITH a valid approved request, a multi-memory call is refused.

    One approval authorises exactly one memory, so a caller cannot reuse a single
    token to delete a batch.
    """
    first = _create(client, auth_headers, "批量遗忘目标一")
    second = _create(client, auth_headers, "批量遗忘目标二")
    created = approved_forget.request(first["id"])
    resp = client.post(
        "/api/v1/memories/forget",
        json={
            "memory_ids": [first["id"], second["id"]],
            "forget_request_id": created["id"],
            "confirmation_token": created["confirmation_token"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forget_approval_required"
    # Neither memory was touched.
    for memory in (first, second):
        assert (
            client.get(
                f"/api/v1/memories/{memory['id']}", headers=auth_headers
            ).json()["data"]["status"]
            == "ACTIVE"
        )


# -------------------------------------------------------------------- audit
def test_lifecycle_audit_actions(client, auth_headers):
    first = _create(client, auth_headers, "审计测试记忆一")
    second = _create(client, auth_headers, "审计测试记忆二")
    summary_id = client.post(
        "/api/v1/memories/summaries",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    ).json()["data"]["id"]
    client.post(
        f"/api/v1/memories/summaries/{summary_id}/review",
        json={"decision": "approve"},
        headers=auth_headers,
    )
    actions = _audit_actions(client, auth_headers, first["id"])
    assert "SUMMARY_CREATE" in actions
    assert "SUMMARY_CONFIRM" in actions

    client.post(
        f"/api/v1/memories/{first['id']}/archive",
        json={"reason": "audit"},
        headers=auth_headers,
    )
    actions = _audit_actions(client, auth_headers, first["id"])
    assert "ARCHIVE" in actions
    client.post(
        f"/api/v1/memories/{first['id']}/restore", headers=auth_headers
    )
    actions = _audit_actions(client, auth_headers, first["id"])
    assert "RESTORE" in actions


def test_audit_entries_have_no_token(client, auth_headers):
    record = _create(client, auth_headers, "token leak check")
    client.post(
        f"/api/v1/memories/{record['id']}/archive",
        json={"reason": "check"},
        headers=auth_headers,
    )
    text = client.get(
        f"/api/v1/memories/{record['id']}/audit-logs", headers=auth_headers
    ).text.lower()
    assert "token" not in text
    assert "select " not in text


# --------------------------------------------------------------------- auth
def test_lifecycle_requires_token(client):
    for path in (
        "/api/v1/memories/summaries",
        "/api/v1/memories/corrections",
        "/api/v1/memories/merges",
        "/api/v1/memories/expiring",
        "/api/v1/memories/expired",
        "/api/v1/memories/archive-records",
        "/api/v1/memories/forget-candidates",
        "/api/v1/memories/export",
    ):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/v1/memories/forget", json={}).status_code == 401


def test_reconcile_repairs_stale_vector_status(
    client, auth_headers, manager, approved_forget
):
    record = _create(client, auth_headers, "reconcile target")
    approved_forget.forget(record["id"])
    # Simulate the JME-4 issue: the row still claims INDEXED after the point
    # has been removed from Qdrant.
    with manager.session() as session:
        MemoryRepository(session).update_vector_status(
            record["id"], "INDEXED"
        )
    resp = client.post(
        "/api/v1/memories/index/reconcile", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["marked_removed"] == 1
    with manager.session() as session:
        assert (
            MemoryRepository(session).get_by_id(record["id"]).vector_status
            == "REMOVED"
        )


def test_merge_confirm_rejects_mismatched_merge_id(client, auth_headers):
    first = _create(client, auth_headers, "合并校验记忆甲")
    second = _create(client, auth_headers, "合并校验记忆乙")
    other = _create(client, auth_headers, "合并校验记忆丙")
    client.post(
        "/api/v1/memories/merges/preview",
        json={"memory_ids": [first["id"], second["id"]]},
        headers=auth_headers,
    )
    merge_id = client.get(
        "/api/v1/memories/merges", headers=auth_headers
    ).json()["data"][0]["id"]
    resp = client.post(
        f"/api/v1/memories/merges/confirm?merge_id={merge_id}",
        json={"memory_ids": [first["id"], other["id"]]},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    # Nothing was merged.
    assert client.get(
        f"/api/v1/memories/{first['id']}", headers=auth_headers
    ).json()["data"]["status"] == "ACTIVE"
