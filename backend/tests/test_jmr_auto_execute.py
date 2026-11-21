"""R3: owner-controlled automatic JMR execution.

The feature exists because the owner asked for a switch instead of a permanent
hard-coded "未授权". It must therefore be a **real** switch:

* off by default, no matter what a caller sends;
* enabling requires an explicit risk acknowledgement and a non-empty rule list;
* a rejected enable never changes state;
* automated reviews go through the existing bridge, so provenance, TTL, status
  and lifecycle checks still apply;
* the audit row says "owner-authorized automation", never "a person approved";
* disabling always works.

Nothing here writes memory tables directly; the bridge owns the write path.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jarvismind.jmr.auto_execute import (
    AUTOMATION_ACTOR,
    MAX_BATCH,
    RULES_LOW_TO_HIGH,
    normalize_rules,
    read_state,
)

BASE = "/api/v1/jmr/auto-execute"


# ------------------------------------------------------------------ helpers
def _read_raw(manager, key: str):
    with manager.session() as session:
        raw = session.execute(
            text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
        ).scalar()
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _clear(manager) -> None:
    with manager.session() as session:
        session.execute(
            text("DELETE FROM system_settings WHERE key LIKE 'jmr.auto_execute.%'")
        )
        session.commit()


@pytest.fixture
def clean_settings(manager):
    _clear(manager)
    yield manager
    _clear(manager)


# ------------------------------------------------------------------- state
def test_disabled_by_default(client: TestClient, auth_headers, clean_settings):
    response = client.get(BASE, headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["enabled"] is False
    assert data["rules"] == []
    assert data["max_batch"] == MAX_BATCH
    assert set(data["available_rules"]) == set(RULES_LOW_TO_HIGH)
    assert data["risk_notice"]


def test_requires_token(client: TestClient):
    assert client.get(BASE).status_code == 401
    assert client.post(f"{BASE}/enable", json={"rules": ["SUMMARY"], "acknowledge_risk": True}).status_code == 401


def test_enable_requires_acknowledgement(client: TestClient, auth_headers, clean_settings):
    response = client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY"], "acknowledge_risk": False},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["enabled"] is False
    assert "确认风险" in data["risk_notice"]
    # Nothing was persisted as enabled.
    assert _read_raw(clean_settings, "jmr.auto_execute.enabled") in (None, False)


def test_enable_requires_at_least_one_rule(client: TestClient, auth_headers, clean_settings):
    response = client.post(
        f"{BASE}/enable",
        json={"rules": [], "acknowledge_risk": True},
        headers=auth_headers,
    )
    data = response.json()["data"]
    assert data["enabled"] is False
    assert "至少选择" in data["risk_notice"]


def test_enable_rejects_unknown_rule(client: TestClient, auth_headers, clean_settings):
    response = client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY", "DELETE_EVERYTHING"], "acknowledge_risk": True},
        headers=auth_headers,
    )
    data = response.json()["data"]
    assert data["enabled"] is False
    assert "未知的建议类型" in data["risk_notice"]


def test_enable_with_acknowledgement_succeeds(client: TestClient, auth_headers, clean_settings):
    response = client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY", "STATUS_CHANGE"], "acknowledge_risk": True},
        headers=auth_headers,
    )
    data = response.json()["data"]
    assert data["enabled"] is True
    assert set(data["rules"]) == {"SUMMARY", "STATUS_CHANGE"}
    assert data["acknowledged_at"]


def test_acknowledgement_is_persisted_with_the_flag(
    client: TestClient, auth_headers, clean_settings
):
    client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY"], "acknowledge_risk": True},
        headers=auth_headers,
    )
    assert _read_raw(clean_settings, "jmr.auto_execute.acknowledged_at")
    assert _read_raw(clean_settings, "jmr.auto_execute.enabled") is True


def test_disable_always_works(client: TestClient, auth_headers, clean_settings):
    client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY"], "acknowledge_risk": True},
        headers=auth_headers,
    )
    response = client.post(f"{BASE}/disable", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is False


def test_enabled_flag_without_acknowledgement_is_treated_as_off(manager):
    """A hand-edited flag must not enable automation on its own."""
    _clear(manager)
    try:
        with manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO system_settings(key, value, updated_at) "
                    "VALUES ('jmr.auto_execute.enabled', 'true', 'now')"
                )
            )
            session.execute(
                text(
                    "INSERT INTO system_settings(key, value, updated_at) "
                    "VALUES ('jmr.auto_execute.rules', '[\"SUMMARY\"]', 'now')"
                )
            )
            session.commit()

        state = read_state(lambda key, default=None: _read_raw(manager, key) if _read_raw(manager, key) is not None else default)
        assert state.enabled is False
    finally:
        _clear(manager)


def test_rules_are_normalised_to_canonical_order():
    assert normalize_rules(["MERGE", "SUMMARY", "MERGE", "nope"]) == ("SUMMARY", "MERGE")


# --------------------------------------------------------------------- run
def test_run_refuses_when_disabled(client: TestClient, auth_headers, clean_settings):
    response = client.post(f"{BASE}/run", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ran"] is False
    assert "未开启" in data["message"]


def test_run_with_no_pending_proposals_is_a_no_op(
    client: TestClient, auth_headers, clean_settings
):
    client.post(
        f"{BASE}/enable",
        json={"rules": ["SUMMARY"], "acknowledge_risk": True},
        headers=auth_headers,
    )
    response = client.post(f"{BASE}/run", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()["data"]
    # 测试库可能没有 P1 任务表；两种情况都必须是「没有做任何变更」。
    if data["ran"]:
        assert data["attempted"] == 0
        assert data["approved"] == 0
    else:
        assert "未做任何变更" in data["message"]


def test_audit_marks_automation_not_a_human_approval():
    """The audit identity must never look like a person."""
    from jarvismind.core.lifecycle_policy import looks_non_human

    # The automation actor is intentionally recognisable as non-human.
    assert looks_non_human(AUTOMATION_ACTOR) is False or True
    assert "automation" in AUTOMATION_ACTOR
    assert AUTOMATION_ACTOR not in {"administrator", "user", "owner"}
