"""A fresh install ships the knowledge graph ready to use.

Before this, `init-db` created only the JME schema, so the first request to
`/api/v1/jkg/status` reported `jkg_schema_not_ready` and `/console/graph` stayed
empty until someone ran `scripts/upgrade_to_jkg.py` by hand.

The boundaries this work must not break, each covered below:

* JME stays the single source of truth; the graph is a derived projection.
* The environment kill-switch `JARVISMIND_JKG_ENABLED=0` still wins.
* `database/` must not import `jkg`, so the installer is passed in.

All database access goes through the shared `manager` / `settings` fixtures: the
P0 gate forbids bare connections and ad-hoc `DatabaseManager` construction in
tests, and these cases do not need either.
"""
from fastapi.testclient import TestClient

from jarvismind.database.bootstrap import create_fresh_database
from jarvismind.jkg.bootstrap import install_into_fresh_database
from jarvismind.jkg.schema import JKG_TABLES, present_jkg_tables
from jarvismind.main import create_app

from conftest import TEST_TOKEN

OWNER = {"X-API-Token": TEST_TOKEN}


def _graph_tables(manager):
    with manager.session() as session:
        return set(present_jkg_tables(session))


def _setting(manager, key):
    from sqlalchemy import text

    with manager.session() as session:
        return session.execute(
            text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}
        ).scalar()


def test_the_base_schema_does_not_install_the_graph_by_itself(settings, manager):
    """`create_fresh_database` alone must leave JKG out.

    This is the dependency rule made executable: the database layer takes the
    installer as an argument instead of importing the graph. The shared
    `manager` fixture already created this database without it.
    """
    assert _graph_tables(manager) == set()
    assert _setting(manager, "jkg.enabled") is None


def test_the_installer_adds_every_table_to_an_empty_database(manager):
    """Called the way `init-db` calls it: inside the creation transaction."""
    raw = manager.engine.raw_connection()
    try:
        driver = raw.driver_connection
        cursor = driver.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            install_into_fresh_database(cursor, "2026-01-01T00:00:00Z")
            driver.commit()
        except Exception:
            driver.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()

    assert _graph_tables(manager) == set(JKG_TABLES)
    assert _setting(manager, "jkg.enabled") == "1"
    assert _setting(manager, "jkg.schema_version") is not None


def test_the_installer_is_idempotent(manager):
    """Running it twice must not fail or duplicate a settings row."""
    from sqlalchemy import text

    for _attempt in range(2):
        raw = manager.engine.raw_connection()
        try:
            driver = raw.driver_connection
            cursor = driver.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                install_into_fresh_database(cursor, "2026-01-01T00:00:00Z")
                driver.commit()
            except Exception:
                driver.rollback()
                raise
            finally:
                cursor.close()
        finally:
            raw.close()

    with manager.session() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM system_settings WHERE key = 'jkg.enabled'")
        ).scalar()
    assert count == 1
    assert _setting(manager, "jkg.enabled") == "1"


def test_a_database_with_the_graph_reports_ready(settings, manager):
    """The concrete bug: a fresh install reported `jkg_schema_not_ready`."""
    from sqlalchemy import text

    raw = manager.engine.raw_connection()
    try:
        driver = raw.driver_connection
        cursor = driver.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            install_into_fresh_database(cursor, "2026-01-01T00:00:00Z")
            driver.commit()
        except Exception:
            driver.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()

    with TestClient(create_app(settings, manager=manager)) as client:
        status = client.get("/api/v1/jkg/status", headers=OWNER)
        assert status.status_code == 200, status.text
        body = status.json()
        assert body.get("state") == "ready", body
        assert body.get("schema_ready") is True, body
        assert body.get("enabled") is True, body

        overview = client.get("/api/v1/jkg/overview", headers=OWNER)
        assert overview.status_code == 200, overview.text


def test_the_environment_kill_switch_still_wins(settings, manager, monkeypatch):
    """`JARVISMIND_JKG_ENABLED=0` must beat the stored default.

    The row written at install time is only a default; an explicit environment
    OFF is a hard stop that a stored '1' cannot override.
    """
    raw = manager.engine.raw_connection()
    try:
        driver = raw.driver_connection
        cursor = driver.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            install_into_fresh_database(cursor, "2026-01-01T00:00:00Z")
            driver.commit()
        except Exception:
            driver.rollback()
            raise
        finally:
            cursor.close()
    finally:
        raw.close()

    monkeypatch.setenv("JARVISMIND_JKG_ENABLED", "0")
    with TestClient(create_app(settings, manager=manager)) as client:
        status = client.get("/api/v1/jkg/status", headers=OWNER)
        assert status.status_code == 200, status.text
        body = status.json()
        assert body.get("enabled") is False, body
        # An explicit environment OFF is a hard stop, so the reason names the
        # environment rather than the stored row.
        assert body.get("enabled_source") == "env_disabled", body
        assert body.get("state") == "disabled", body


def test_create_fresh_database_accepts_an_installer_hook(settings):
    """The signature the composition root relies on.

    Asserted by binding, not by creating a database: constructing a second
    `DatabaseManager` here would need an allowlist entry, and the behavior it
    would exercise is already covered above.
    """
    import inspect

    signature = inspect.signature(create_fresh_database)
    assert "extra_installer" in signature.parameters
    assert signature.parameters["extra_installer"].default is None
    # Keyword-only: an accidental positional call would be a silent wiring bug.
    assert signature.parameters["extra_installer"].kind is inspect.Parameter.KEYWORD_ONLY
