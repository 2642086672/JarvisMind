"""Space isolation, console API truthfulness, exports, backups and the CLI.

The isolation tests check that two spaces never share rows, vector paths or
overview caches. The console tests check that unsupported figures are reported
as not available instead of being invented, and that exports redact sensitive
text unless the owner explicitly asks for it.
"""
import json
from pathlib import Path

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


def _space_headers(space_id):
    return {**OWNER, "X-JarvisMind-Space": space_id}


def _create_space(client, space_id):
    response = client.post(
        "/api/v1/control/spaces",
        json={"id": space_id, "name": space_id.title()},
        headers=OWNER,
    )
    assert response.status_code == 201, response.text


def test_memory_written_to_one_space_is_invisible_in_another(client):
    _create_space(client, "space-a")
    _create_space(client, "space-b")
    created = client.post(
        "/api/v1/memories/extract",
        json={"conversation": "我的项目代号是北极星", "conversation_id": "c-a"},
        headers=_space_headers("space-a"),
    )
    assert created.status_code in (200, 201), created.text
    in_a = client.get("/api/v1/memories/search", params={"q": "北极星"}, headers=_space_headers("space-a"))
    in_b = client.get("/api/v1/memories/search", params={"q": "北极星"}, headers=_space_headers("space-b"))
    in_default = client.get("/api/v1/memories/search", params={"q": "北极星"}, headers=OWNER)
    assert in_a.status_code == 200 and in_b.status_code == 200 and in_default.status_code == 200
    assert in_b.json()["data"] == []
    assert in_default.json()["data"] == []


def test_each_space_has_its_own_database_and_vector_directory(client, settings):
    _create_space(client, "vec-a")
    _create_space(client, "vec-b")
    db_a = settings.data_dir / "spaces" / "vec-a" / "jarvismind.db"
    db_b = settings.data_dir / "spaces" / "vec-b" / "jarvismind.db"
    assert db_a.is_file() and db_b.is_file()
    assert db_a != db_b
    registry = client.app.state.spaces
    container_a = registry.container_for("vec-a")
    container_b = registry.container_for("vec-b")
    assert container_a.settings.qdrant_path != container_b.settings.qdrant_path
    assert str(settings.data_dir / "spaces" / "vec-a") in container_a.settings.qdrant_path
    assert container_a.manager is not container_b.manager


def test_shared_qdrant_override_is_not_inherited_by_other_spaces(settings, manager):
    from dataclasses import replace

    overridden = replace(settings, qdrant_path_override=str(settings.data_dir / "shared-qdrant"))
    with TestClient(create_app(overridden, manager=manager)) as overridden_client:
        _create_space_via(overridden_client, "qd-x")
        container = overridden_client.app.state.spaces.container_for("qd-x")
        assert "shared-qdrant" not in container.settings.qdrant_path


def _create_space_via(client, space_id):
    response = client.post(
        "/api/v1/control/spaces", json={"id": space_id, "name": space_id}, headers=OWNER
    )
    assert response.status_code == 201, response.text


def test_overview_cache_is_per_container_not_process_wide(client, manager):
    from jarvismind.services.memory_overview_cache import CachedOverview

    _create_space(client, "cache-a")
    registry = client.app.state.spaces
    default_cache = client.app.state.container._overview_cache_instance()
    space_cache = registry.container_for("cache-a")._overview_cache_instance()
    assert default_cache is not space_cache
    default_cache.put(
        CachedOverview(source_version="same-hash", generated_at="t", model="m", summaries={"x": "y"})
    )
    assert space_cache.get() is None


def test_console_dashboard_marks_unsupported_figures_instead_of_inventing_them(client):
    response = client.get("/api/v1/console/dashboard", headers=OWNER)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["memory"]["total"] >= 0
    assert data["pending"]["candidates"] >= 0
    assert any("dead-letter" in item.lower() or "dead letter" in item.lower() for item in data["not_available"])
    assert "dead_letter_queue" in client.get("/api/v1/console/ops/tasks", headers=OWNER).json()["data"]


def test_export_redacts_sensitive_text_by_default(client, manager):
    MemoryService(manager).create(
        MemoryCreate(
            memory_type=MemoryType.PROFILE,
            content="我的体检报告显示需要长期吃药治疗",
            status=MemoryStatus.ACTIVE,
        )
    )
    MemoryService(manager).create(
        MemoryCreate(memory_type=MemoryType.SEMANTIC, content="我的公司在杭州", status=MemoryStatus.ACTIVE)
    )
    redacted = client.get("/api/v1/console/exports/memories", headers=OWNER).json()
    assert redacted["sensitive_included"] is False
    contents = [item["content"] for item in redacted["items"]]
    assert all("吃药" not in text for text in contents)
    assert any("[redacted: sensitive]" in text for text in contents)
    assert any("杭州" in text for text in contents)

    explicit = client.get(
        "/api/v1/console/exports/memories", params={"include_sensitive": "true"}, headers=OWNER
    ).json()
    assert explicit["sensitive_included"] is True
    assert any("吃药" in item["content"] for item in explicit["items"])



def test_export_csv_has_header_and_no_secret_token(client):
    response = client.get("/api/v1/console/exports/memories", params={"format": "csv"}, headers=OWNER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0].startswith("id,memory_type")
    assert TEST_TOKEN not in response.text


def test_settings_view_never_contains_secrets(client):
    body = client.get("/api/v1/console/settings", headers=OWNER).text
    assert TEST_TOKEN not in body
    assert "llm_api_key" not in body
    assert "api_key" not in body.lower()


def test_api_management_lists_v1_endpoints_and_rate_limits(client):
    data = client.get("/api/v1/console/api-management", headers=OWNER).json()["data"]
    paths = {item["path"] for item in data["endpoints"]}
    assert "/api/v1/agent/remember" in paths
    assert "/api/v1/control/agents" in paths
    assert data["rate_limits"]["window_seconds"] == 60


def test_backup_create_list_and_verify(client, settings):
    created = client.post("/api/v1/console/backups", headers=OWNER)
    assert created.status_code == 201, created.text
    name = created.json()["data"]["name"]
    backup_path = settings.data_dir / "backups" / name
    assert backup_path.is_file()
    listed = client.get("/api/v1/console/backups", headers=OWNER).json()
    assert any(item["name"] == name for item in listed["data"])
    assert listed["restore"]["available_via_api"] is False
    verified = client.post("/api/v1/console/backups/verify", json={"name": name}, headers=OWNER)
    assert verified.status_code == 200, verified.text
    assert verified.json()["data"]["ok"] is True


def test_backup_verify_rejects_path_traversal(client):
    response = client.post(
        "/api/v1/console/backups/verify",
        json={"name": "../../control.db"},
        headers=OWNER,
    )
    assert response.status_code == 200
    assert response.json()["data"]["ok"] is False


def test_cli_init_db_creates_new_database_and_refuses_existing(settings, tmp_path):
    from dataclasses import replace

    from jarvismind import cli

    fresh_dir = tmp_path / "fresh"
    fresh = replace(
        settings,
        data_dir=fresh_dir,
        database_url=f"sqlite+pysqlite:///{(fresh_dir / 'jarvismind.db').as_posix()}",
        qdrant_path_override=":memory:",
    )
    assert cli.main(["init-db"], settings=fresh) == 0
    assert (fresh_dir / "jarvismind.db").is_file()
    assert (fresh_dir / "control.db").is_file()
    assert cli.main(["init-db"], settings=fresh) == 1


def test_cli_version_prints_1_0_0(capsys):
    from jarvismind import cli

    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "1.0.0"


def test_init_db_legacy_entry_point_still_refuses(capsys):
    from jarvismind import init_db

    with pytest.raises(SystemExit) as exit_info:
        init_db.main()
    assert exit_info.value.code == 2
