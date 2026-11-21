"""Database initialization, migration, FTS5, and error-handling tests."""
import pytest
from sqlalchemy import text

from jarvismind.config import Settings
from jarvismind.database.connection import DatabaseManager
from jarvismind.database.migrations import MIGRATIONS, MigrationRunner
from jarvismind.schemas.memory import MemoryCreate, MemoryType
from jarvismind.services.memory_service import MemoryService

EXPECTED_TABLES = (
    "schema_migrations",
    "system_settings",
    "memory_records",
    "memory_sources",
    "memory_audit_logs",
)


def _table_names(engine) -> set:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    return {row[0] for row in rows}


def test_fresh_database_creates_schema(settings):
    manager = DatabaseManager(settings)
    try:
        applied = MigrationRunner(manager).initialize()
        assert applied == len(MIGRATIONS)
        assert MigrationRunner(manager).current_version() == len(MIGRATIONS)
        tables = _table_names(manager.engine)
        for expected in EXPECTED_TABLES:
            assert expected in tables
    finally:
        manager.dispose()


def test_migrations_are_idempotent(settings):
    manager = DatabaseManager(settings)
    try:
        assert MigrationRunner(manager).initialize() == len(MIGRATIONS)
        assert MigrationRunner(manager).initialize() == 0
        assert MigrationRunner(manager).current_version() == MIGRATIONS[-1].version
    finally:
        manager.dispose()


def test_foreign_keys_enabled(settings):
    manager = DatabaseManager(settings)
    try:
        MigrationRunner(manager).initialize()
        with manager.engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        manager.dispose()


def test_wal_mode_enabled(settings):
    manager = DatabaseManager(settings)
    try:
        MigrationRunner(manager).initialize()
        with manager.engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
    finally:
        manager.dispose()


def _trigger_names(engine) -> set:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger'")
        ).all()
    return {row[0] for row in rows}


def test_fts5_virtual_table_created(settings):
    manager = DatabaseManager(settings)
    try:
        MigrationRunner(manager).initialize()
        tables = _table_names(manager.engine)
        if "memory_records_fts" not in tables:
            pytest.skip("SQLite build without FTS5")
        assert "memory_records" in tables
        triggers = _trigger_names(manager.engine)
        assert "memory_records_ai" in triggers
        assert "memory_records_au" in triggers
        assert "memory_records_ad" in triggers
    finally:
        manager.dispose()


def test_fts_index_tracks_writes(settings):
    manager = DatabaseManager(settings)
    try:
        MigrationRunner(manager).initialize()
        if "memory_records_fts" not in _table_names(manager.engine):
            pytest.skip("SQLite build without FTS5")
        service = MemoryService(manager)
        service.create(
            MemoryCreate(
                memory_type=MemoryType.SEMANTIC,
                content="the quick brown fox jumps over the lazy dog",
            )
        )
        with manager.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT rowid FROM memory_records_fts WHERE memory_records_fts MATCH 'fox'")
            ).all()
        assert rows
    finally:
        manager.dispose()


def test_bad_database_path_raises(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    broken = Settings(
        env="test",
        data_dir=blocker,
        api_token="t",
        database_url=f"sqlite+pysqlite:///{(blocker / 'jarvismind.db').as_posix()}",
    )
    manager = None
    try:
        with pytest.raises(Exception):
            manager = DatabaseManager(broken)
            MigrationRunner(manager).initialize()
    finally:
        if manager is not None:
            manager.dispose()


def test_api_returns_503_when_table_missing(
    client, auth_headers, manager, sample_memory_payload
):
    with manager.engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("DROP TABLE memory_records"))

    resp = client.post(
        "/api/v1/memories", json=sample_memory_payload, headers=auth_headers
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "database_error"
    assert "traceback" not in resp.text.lower()


def test_database_file_is_created_under_data_dir(settings):
    manager = DatabaseManager(settings)
    try:
        MigrationRunner(manager).initialize()
        assert settings.database_path.exists()
    finally:
        manager.dispose()