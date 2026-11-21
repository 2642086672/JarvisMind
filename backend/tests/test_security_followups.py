"""Regression tests for the second independent audit.

They cover forget requests that only the owner may decide, sensitive metadata,
per-space backups, the exact token lifetime, archiving a space that an agent
still uses, and refused reads that must not touch the memory.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvismind.control.service import _lifetime_of
from jarvismind.main import create_app
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from jarvismind.services.memory_service import MemoryService

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}
REDACTED = "[redacted: sensitive]"


@pytest.fixture
def client(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        yield test_client


def _store(manager, content, memory_type="SEMANTIC", metadata=None):
    return MemoryService(manager).create(
        MemoryCreate(
            memory_type=MemoryType(memory_type),
            content=content,
            status=MemoryStatus.ACTIVE,
            metadata=metadata,
        )
    )


def _agent_headers(client, permissions):
    agent = client.post(
        "/api/v1/control/agents",
        json={"name": "audit-probe", "permissions": permissions},
        headers=OWNER,
    ).json()["data"]
    token = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "probe"},
        headers=OWNER,
    ).json()["token"]
    return agent, {"Authorization": f"Bearer {token}"}


def test_owner_route_refuses_to_reissue_an_agent_pending_request(client, manager):
    record = _store(manager, "我的办公室位于二楼东侧")
    _agent, headers = _agent_headers(client, ["read", "delete"])
    agent_request = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests", json={}, headers=headers
    )
    assert agent_request.status_code == 201, agent_request.text
    request_id = agent_request.json()["data"]["id"]

    owner_attempt = client.post(
        f"/api/v1/memories/{record.id}/forget-requests", json={}, headers=OWNER
    )
    assert owner_attempt.status_code == 409
    details = owner_attempt.json()["error"]["details"]
    assert details["code"] == "agent_request_pending"
    assert details["forget_request_id"] == request_id

    repeat = client.post(
        f"/api/v1/agent/memories/{record.id}/forget-requests", json={}, headers=headers
    )
    assert repeat.status_code == 201, repeat.text
    assert repeat.json()["data"]["id"] == request_id
    assert repeat.json()["data"]["reused_existing"] is True
    assert "confirmation_token" not in repeat.text

    approved = client.post(
        f"/api/v1/forget-requests/{request_id}/approve", json={}, headers=OWNER
    )
    assert approved.status_code == 200, approved.text
    assert MemoryService(manager).get(record.id, touch=False).status == "DELETED"


def test_sensitive_metadata_is_withheld_from_agents_and_exports(client, manager):
    record = _store(
        manager,
        "我整理了这周的待办事项",
        "PROFILE",
        metadata={
            "health": "确诊2型糖尿病",
            "home": "杭州市西湖区文三路100号",
            "salary": "月薪32000元",
        },
    )
    _agent, headers = _agent_headers(client, ["read", "search", "context"])

    fetched = client.get(f"/api/v1/agent/memories/{record.id}", headers=headers)
    assert fetched.status_code == 403
    assert fetched.json()["error"]["details"]["code"] == "sensitive_access_denied"
    assert "糖尿病" not in fetched.text

    listing = client.get("/api/v1/agent/memories", headers=headers)
    assert record.id not in listing.text
    assert "糖尿病" not in listing.text
    assert "文三路" not in listing.text

    exported = client.get("/api/v1/console/exports/memories", headers=OWNER).json()
    item = next(entry for entry in exported["items"] if entry["id"] == record.id)
    assert item["sensitive"] is True
    assert item["content"] == REDACTED
    assert item["metadata"] is None
    assert "糖尿病" not in str(exported)

    explicit = client.get(
        "/api/v1/console/exports/memories",
        params={"include_sensitive": "true"},
        headers=OWNER,
    ).json()
    included = next(entry for entry in explicit["items"] if entry["id"] == record.id)
    assert included["metadata"]["health"] == "确诊2型糖尿病"


def test_backups_are_written_to_and_verified_within_the_selected_space(client, settings):
    created = client.post("/api/v1/control/spaces", json={"id": "alpha-team", "name": "Alpha"}, headers=OWNER)
    assert created.status_code == 201, created.text
    space_headers = {**OWNER, "X-JarvisMind-Space": "alpha-team"}
    space_backup = client.post("/api/v1/console/backups", headers=space_headers)
    assert space_backup.status_code == 201, space_backup.text
    space_name = space_backup.json()["data"]["name"]
    assert (settings.data_dir / "spaces" / "alpha-team" / "backups" / space_name).is_file()

    # The default space has written nothing yet, so the space's file must be unknown there.
    # Names are timestamped, so checks against the default space must run before its own
    # backups exist; otherwise an identical name in the same second would be ambiguous.
    assert client.get("/api/v1/console/backups", headers=OWNER).json()["data"] == []
    foreign = client.post("/api/v1/console/backups/verify", json={"name": space_name}, headers=OWNER).json()["data"]
    assert foreign["ok"] is False
    assert foreign["reason"] == "not_found"
    own = client.post(
        "/api/v1/console/backups/verify", json={"name": space_name}, headers=space_headers
    ).json()["data"]
    assert own["ok"] is True
    in_space = client.get("/api/v1/console/backups", headers=space_headers).json()["data"]
    assert [item["name"] for item in in_space] == [space_name]

    first = client.post("/api/v1/console/backups", headers=OWNER)
    second = client.post("/api/v1/console/backups", headers=OWNER)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    names = [first.json()["data"]["name"], second.json()["data"]["name"]]
    assert names[0] != names[1]
    listed = {item["name"] for item in client.get("/api/v1/console/backups", headers=OWNER).json()["data"]}
    assert listed == set(names)
    assert (
        client.post("/api/v1/console/backups/verify", json={"name": names[1]}, headers=OWNER).json()["data"]["ok"]
        is True
    )
    still_in_space = client.get("/api/v1/console/backups", headers=space_headers).json()["data"]
    assert [item["name"] for item in still_in_space] == [space_name]


def test_rotation_keeps_the_exact_original_lifetime():
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = SimpleNamespace(
        created_at=created.isoformat(),
        expires_at=(created + timedelta(days=1, hours=12)).isoformat(),
    )
    assert _lifetime_of(row) == timedelta(days=1, hours=12)


def test_space_used_as_an_agent_default_cannot_be_archived(client):
    created = client.post("/api/v1/control/spaces", json={"id": "delta-space", "name": "Delta"}, headers=OWNER)
    assert created.status_code == 201, created.text
    agent = client.post(
        "/api/v1/control/agents",
        json={"name": "delta-agent", "default_space_id": "delta-space"},
        headers=OWNER,
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["data"]["id"]

    blocked = client.patch("/api/v1/control/spaces/delta-space", json={"status": "archived"}, headers=OWNER)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["details"]["code"] == "space_in_use"

    grant = client.put(
        f"/api/v1/control/agents/{agent_id}/grants/default",
        json={"access": "read"},
        headers=OWNER,
    )
    assert grant.status_code == 200, grant.text
    moved = client.patch(
        f"/api/v1/control/agents/{agent_id}", json={"default_space_id": "default"}, headers=OWNER
    )
    assert moved.status_code == 200, moved.text
    archived = client.patch("/api/v1/control/spaces/delta-space", json={"status": "archived"}, headers=OWNER)
    assert archived.status_code == 200, archived.text


def test_refused_agent_read_does_not_touch_the_memory(client, manager):
    record = _store(manager, "我的体检报告显示需要长期吃药治疗", "PROFILE")
    _agent, headers = _agent_headers(client, ["read"])
    before = MemoryService(manager).get(record.id, touch=False).accessed_at
    refused = client.get(f"/api/v1/agent/memories/{record.id}", headers=headers)
    assert refused.status_code == 403
    after = MemoryService(manager).get(record.id, touch=False).accessed_at
    assert after == before


def test_metadata_scan_exempts_only_generated_identifiers():
    from jarvismind.control.sensitivity import metadata_is_protected

    ordinary = '{"candidate_id": "9135fd15577749719d4a533982e65785", "engine": "auto-memory", "extracted": true}'
    assert metadata_is_protected(ordinary) is False
    assert metadata_is_protected('{"candidate_id": "13800138000abcdef0123456789abcde"}') is False
    assert metadata_is_protected('{"contact_id": "13800138000"}') is True
    assert metadata_is_protected('{"contact": 13800138000}') is True
    assert metadata_is_protected('{"outer": {"inner_id": "13800138000"}}') is True
    assert metadata_is_protected('{"health": "确诊2型糖尿病"}') is True
    assert metadata_is_protected('{"note": "月薪32000元"}') is True
    assert metadata_is_protected("not json at all 确诊糖尿病") is True
