"""Console surface: static serving, browser hardening headers and the owner-only Context Lab."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from jarvismind.main import create_app
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType
from jarvismind.services.memory_service import MemoryService

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}


@pytest.fixture
def client(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        yield test_client


def _console_build(tmp_path):
    build = tmp_path / "console-dist"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<!doctype html><title>console</title>", encoding="utf-8")
    (build / "assets" / "app.js").write_text("console.log('ok');", encoding="utf-8")
    return build


def test_console_is_not_mounted_without_a_build(settings, manager):
    with TestClient(create_app(settings, manager=manager)) as test_client:
        assert test_client.get("/console/").status_code == 404


def test_console_serves_index_and_falls_back_for_client_routes(settings, manager, tmp_path):
    configured = replace(settings, console_dir=str(_console_build(tmp_path)))
    with TestClient(create_app(configured, manager=manager)) as test_client:
        index = test_client.get("/console/")
        assert index.status_code == 200
        assert "<title>console</title>" in index.text
        deep = test_client.get("/console/memories/abc123")
        assert deep.status_code == 200
        assert "<title>console</title>" in deep.text
        assert test_client.get("/console/assets/app.js").status_code == 200
        assert test_client.get("/console/assets/missing.js").status_code == 404


def test_console_responses_carry_hardening_headers(settings, manager, tmp_path):
    configured = replace(settings, console_dir=str(_console_build(tmp_path)))
    with TestClient(create_app(configured, manager=manager)) as test_client:
        response = test_client.get("/console/")
        policy = response.headers["content-security-policy"]
        assert "default-src 'self'" in policy
        assert "object-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_context_lab_is_owner_only_and_previews_the_agent_block(client, manager):
    MemoryService(manager).create(
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="我的项目代号是北极星",
            status=MemoryStatus.ACTIVE,
        )
    )
    owner = client.post("/api/v1/console/lab/context", json={"query": "北极星"}, headers=OWNER)
    assert owner.status_code == 200, owner.text
    data = owner.json()["data"]
    assert data["context"].startswith('<jarvismind-context trust="untrusted-data">')
    assert data["retrieval_version"]
    assert "excluded" in data

    agent = client.post("/api/v1/control/agents", json={"name": "lab-probe"}, headers=OWNER).json()["data"]
    issued = client.post(
        f"/api/v1/control/agents/{agent['id']}/tokens",
        json={"name": "t"},
        headers=OWNER,
    ).json()
    refused = client.post(
        "/api/v1/console/lab/context",
        json={"query": "北极星"},
        headers={"Authorization": f"Bearer {issued['token']}"},
    )
    assert refused.status_code == 403
