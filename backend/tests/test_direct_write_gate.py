"""JW-00: direct-write endpoint gate (POST / PATCH /memories).

Covers the nine required behaviours:
  1. default config rejects POST
  2. default config rejects PATCH
  3. explicit enable restores POST
  4. explicit enable restores PATCH
  5. candidate create/approve unaffected
  6. memory reads unaffected
  7. DELETE ("forget") still works
  8. malformed/absent config falls back to the safe default
  9. no other route bypasses the gate

JW-01 changed item 7's authorization path (an approved forget request is now
required). JW-03 changed item 7's sibling assertions for archive/restore: those
two write ``memory_records.status``, so on an approved memory they now require a
modification request. Both changes keep the original intent ("the feature still
works") and swap only the path that authorises it; each is documented at the
case that changed.

Fixtures: the shared ``settings`` fixture opts in (conftest sets
direct_write_enabled=True so the legacy regression keeps working). The
default-deny cases therefore derive a copy with ``dataclasses.replace`` that
turns the flag OFF, and build the app with ``create_app`` on the shared tmp
manager — the same construction pattern the P0-03A allowlist sanctions.
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from jarvismind.config import Settings, _env_bool
from jarvismind.main import create_app

HEADERS = {"X-API-Token": "test-token-0123456789"}
SAMPLE = {"memory_type": "SEMANTIC", "content": "jw00 direct write probe"}


@pytest.fixture()
def closed_client(settings, manager):
    """App with direct writes at the PRODUCT default (disabled)."""
    disabled = dataclasses.replace(settings, direct_write_enabled=False)
    with TestClient(create_app(disabled, manager=manager)) as client:
        yield client


@pytest.fixture()
def open_client(settings, manager):
    """App with direct writes explicitly enabled."""
    enabled = dataclasses.replace(settings, direct_write_enabled=True)
    with TestClient(create_app(enabled, manager=manager)) as client:
        yield client


def _approve_one(client, conversation_id: str, text: str) -> str:
    """Create a formal memory through the approval chain; return its id."""
    conv = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": f"user: {text}\nassistant: 好的。",
            "conversation_id": conversation_id,
            "mode": "local",
        },
        headers=HEADERS,
    )
    assert conv.status_code == 201, conv.text
    candidates = conv.json()["candidates"]
    assert candidates, conv.text
    resp = client.post(
        f"/api/v1/memories/candidates/{candidates[0]['id']}/approve",
        json={},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    listed = client.get("/api/v1/memories", headers=HEADERS).json()
    assert listed["data"], listed
    return listed["data"][0]["id"]


# ---------------------------------------------------------------- 1 & 2: deny
def test_default_config_rejects_post(closed_client):
    resp = closed_client.post("/api/v1/memories", json=SAMPLE, headers=HEADERS)
    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    assert error["code"] == "direct_write_disabled"
    assert error["details"]["endpoint"] == "POST /api/v1/memories"


def test_default_config_rejects_patch(closed_client):
    resp = closed_client.patch(
        "/api/v1/memories/any-id",
        json={"content": "nope"},
        headers=HEADERS,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "direct_write_disabled"


def test_rejected_post_persists_nothing(closed_client):
    assert closed_client.get("/api/v1/memories", headers=HEADERS).json()["meta"]["total"] == 0
    closed_client.post("/api/v1/memories", json=SAMPLE, headers=HEADERS)
    assert closed_client.get("/api/v1/memories", headers=HEADERS).json()["meta"]["total"] == 0


# ------------------------------------------------------ 3 & 4: enabled behaves
def test_enabled_post_still_works(open_client):
    resp = open_client.post("/api/v1/memories", json=SAMPLE, headers=HEADERS)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["content"] == SAMPLE["content"]
    assert data["status"] == "ACTIVE"


def test_enabled_patch_still_works(open_client):
    created = open_client.post(
        "/api/v1/memories", json=SAMPLE, headers=HEADERS
    ).json()["data"]
    resp = open_client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"content": "updated by jw00"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["content"] == "updated by jw00"


def test_token_still_required_when_enabled(open_client):
    resp = open_client.post(
        "/api/v1/memories", json=SAMPLE, headers={"X-API-Token": "wrong"}
    )
    assert resp.status_code == 401, resp.text


# ------------------------------------------------------- 5: approval unaffected
def test_candidate_extract_and_approve_work_when_closed(closed_client):
    memory_id = _approve_one(
        closed_client, "conv-jw00-ok", "我居住在浙江金华并长期从事客服理赔工作"
    )
    assert memory_id


def test_candidate_reject_works_when_closed(closed_client):
    conv = closed_client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "user: 我居住在浙江金华并长期从事客服理赔工作\nassistant: 好的。",
            "conversation_id": "conv-jw00-reject",
            "mode": "local",
        },
        headers=HEADERS,
    )
    assert conv.status_code == 201, conv.text
    candidates = conv.json()["candidates"]
    assert candidates, conv.text
    candidate_id = candidates[0]["id"]
    resp = closed_client.post(
        f"/api/v1/memories/candidates/{candidate_id}/reject",
        json={"reason": "jw00"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "REJECTED"


def test_candidate_listing_works_when_closed(closed_client):
    resp = closed_client.get("/api/v1/memories/candidates", headers=HEADERS)
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------ 6: reads unaffected
def test_memory_list_and_get_not_gated(closed_client):
    assert closed_client.get("/api/v1/memories", headers=HEADERS).status_code == 200
    # A missing id must be 404 (not 403): reads are not gated.
    assert closed_client.get(
        "/api/v1/memories/nonexistent", headers=HEADERS
    ).status_code == 404


def test_search_endpoints_not_gated(closed_client):
    assert closed_client.get(
        "/api/v1/memories/search", params={"q": "anything"}, headers=HEADERS
    ).status_code == 200
    assert closed_client.get(
        "/api/v1/memories/hybrid-search", params={"q": "probe"}, headers=HEADERS
    ).status_code == 200


# ------------------------------------------- 7: DELETE now requires approval
def test_delete_forget_no_longer_bypasses_approval(closed_client):
    """JW-01 CHANGED THIS CONTRACT DELIBERATELY.

    JW-00 asserted ``DELETE`` still worked when closed, because the iOS forget
    button depended on it. JW-01 closes that bypass: an unapproved DELETE is now
    refused with 403 ``forget_approval_required``, and the memory is untouched.
    The iOS client was switched to the two-step forget-request flow in the same
    change, so no client lost functionality.
    """
    memory_id = _approve_one(
        closed_client, "conv-jw00-del", "我居住在浙江金华并长期从事客服理赔工作"
    )
    resp = closed_client.delete(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forget_approval_required"
    # The memory must still be ACTIVE after the refused delete.
    current = closed_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
    assert current.status_code == 200
    assert current.json()["data"]["status"] == "ACTIVE"


def test_delete_with_approved_forget_request_still_works(closed_client):
    """The legacy DELETE remains usable as a transport for an approved request.

    This keeps the documented compatibility path honest: it is not removed, only
    made conditional on real approval.
    """
    memory_id = _approve_one(
        closed_client, "conv-jw00-del2", "我居住在浙江金华并长期从事客服理赔工作"
    )
    created = closed_client.post(
        f"/api/v1/memories/{memory_id}/forget-requests",
        json={"reason": "jw01"},
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    payload = created.json()["data"]
    resp = closed_client.delete(
        f"/api/v1/memories/{memory_id}",
        params={"forget_request_id": payload["id"]},
        headers={**HEADERS, "X-Forget-Confirmation-Token": payload["confirmation_token"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "DELETED"


def test_archive_and_restore_still_work_when_closed(closed_client):
    """Archive/restore stay usable when direct writes are closed.

    JW-03 CHANGED THE AUTHORIZATION PATH DELIBERATELY (contract, not intent).

    JW-00 asserted that archive/restore worked with no credentials at all. That
    was true, and it was the hole JW-03 closes: ``archive`` writes
    ``memory_records.status``, so on an APPROVED memory it was an unauthenticated
    write to authoritative data. The original intent - "closing direct writes
    must not break the iOS archive/restore buttons" - is preserved here by
    driving the same two operations through the JW-03 modification-request flow,
    which is what the client now uses. The assertion that they still succeed is
    unchanged; only the path that authorises them is.
    """
    memory_id = _approve_one(
        closed_client, "conv-jw00-arch", "我居住在浙江金华并长期从事客服理赔工作"
    )

    def _authorise(kind: str) -> dict:
        created = closed_client.post(
            f"/api/v1/memories/{memory_id}/modification-requests",
            json={"request_kind": kind},
            headers=HEADERS,
        )
        assert created.status_code == 201, created.text
        payload = created.json()["data"]
        approved = closed_client.post(
            f"/api/v1/modification-requests/{payload['request']['id']}/approve",
            json={"confirmation_token": payload["confirmation_token"]},
            headers=HEADERS,
        )
        assert approved.status_code == 200, approved.text
        return payload

    _authorise("ARCHIVE")
    assert (
        closed_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
        .json()["data"]["status"]
        == "ARCHIVED"
    )

    _authorise("RESTORE")
    assert (
        closed_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
        .json()["data"]["status"]
        == "ACTIVE"
    )


def test_archive_without_credentials_is_refused_when_closed(closed_client):
    """JW-03: the exact call JW-00 allowed is now refused on an approved memory.

    This is the regression test for the defect JW-03 exists to close, kept next
    to the case above so the contract change is visible in one place.
    """
    memory_id = _approve_one(
        closed_client, "conv-jw03-archgate", "我在快运公司客服中心负责仲裁理赔工作"
    )
    refused = closed_client.post(
        f"/api/v1/memories/{memory_id}/archive",
        json={"reason": "no credentials"},
        headers=HEADERS,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "approved_memory_protected"
    assert (
        closed_client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
        .json()["data"]["status"]
        == "ACTIVE"
    )


# -------------------------------------------------------- 8: safe default parse
def test_settings_default_is_disabled():
    assert Settings().direct_write_enabled is False


@pytest.mark.parametrize("raw", ["", "maybe", "yes please", "2", "null", "enabled"])
def test_env_bool_malformed_falls_back_to_false(monkeypatch, raw):
    monkeypatch.setenv("JARVISMIND_DIRECT_WRITE_ENABLED", raw)
    assert _env_bool("JARVISMIND_DIRECT_WRITE_ENABLED", False) is False


def test_env_bool_absent_falls_back_to_false(monkeypatch):
    monkeypatch.delenv("JARVISMIND_DIRECT_WRITE_ENABLED", raising=False)
    assert _env_bool("JARVISMIND_DIRECT_WRITE_ENABLED", False) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_bool_true_values(monkeypatch, raw):
    monkeypatch.setenv("JARVISMIND_DIRECT_WRITE_ENABLED", raw)
    assert _env_bool("JARVISMIND_DIRECT_WRITE_ENABLED", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_env_bool_false_values(monkeypatch, raw):
    monkeypatch.setenv("JARVISMIND_DIRECT_WRITE_ENABLED", raw)
    assert _env_bool("JARVISMIND_DIRECT_WRITE_ENABLED", False) is False


# ------------------------------------------------ 9: no other route bypasses it
def test_no_request_field_bypasses_the_gate(closed_client):
    resp = closed_client.post(
        "/api/v1/memories",
        json=SAMPLE,
        headers={**HEADERS, "X-Direct-Write": "1"},
        params={"direct_write": "1", "enabled": "true"},
    )
    assert resp.status_code == 403, resp.text


def test_alternative_write_verbs_do_not_exist(closed_client):
    for method, path in (
        ("put", "/api/v1/memories/any"),
        ("post", "/api/v1/memories/bulk"),
        ("post", "/api/v1/memories/batch"),
    ):
        resp = getattr(closed_client, method)(path, json=SAMPLE, headers=HEADERS)
        assert resp.status_code in (404, 405), (
            f"{method.upper()} {path} unexpectedly reachable: {resp.status_code}"
        )


def test_import_route_is_a_separate_known_gap_not_a_bypass_of_this_gate(closed_client):
    """JW-00 scope note: /import is a *different* direct-write entry.

    It is tracked as a known gap (audit R-01 / W8) and is NOT claimed to be
    closed by this task. This test pins that fact so a future change cannot
    silently pretend the gate covers it.
    """
    resp = closed_client.post(
        "/api/v1/memories/import",
        json={"memories": [], "sources": []},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
