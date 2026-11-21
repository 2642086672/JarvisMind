"""SQLite backup and restore tests."""
import sqlite3
from pathlib import Path

import pytest

from jarvismind.database.backup import (
    BackupError,
    _remove_stale_wal,
    create_backup,
    restore_backup,
    verify_backup,
)
from jarvismind.schemas.memory import MemoryCreate, MemoryType
from jarvismind.services.memory_service import MemoryService


def _add_memory(manager, content):
    return MemoryService(manager).create(
        MemoryCreate(memory_type=MemoryType.SEMANTIC, content=content)
    )


def _count_memories(manager):
    return MemoryService(manager).list(page_size=1000)[1]


def test_create_backup_and_verify(settings, manager):
    _add_memory(manager, "first")
    dest = settings.data_dir / "test-backup.db"
    info = create_backup(settings.database_path, dest)
    assert dest.exists()
    assert info["integrity"] == "ok"
    assert info["counts"]["memory_records"] == 1
    assert info["schema_version"] >= 2
    verified = verify_backup(dest)
    assert verified["counts"]["memory_records"] == 1


def test_verify_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 10)
    with pytest.raises(BackupError):
        verify_backup(bad)


def test_verify_rejects_missing_tables(tmp_path):
    path = tmp_path / "empty.db"
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(BackupError):
        verify_backup(path)


def test_restore_without_force_refuses(settings, manager):
    _add_memory(manager, "existing")
    backup = settings.data_dir / "b.db"
    create_backup(settings.database_path, backup)
    with pytest.raises(BackupError):
        restore_backup(backup, settings.database_path, force=False)


def test_restore_with_force_makes_safety_copy(settings, manager):
    _add_memory(manager, "before restore")
    backup = settings.data_dir / "b.db"
    create_backup(settings.database_path, backup)

    _add_memory(manager, "after backup")
    assert _count_memories(manager) == 2

    # Restore is an offline operation: close pooled connections first. This
    # matches scripts/restore_db.py, which opens no engine at all. Leaving a
    # live connection open while _remove_stale_wal() unlinks -wal/-shm works on
    # Windows (unlink fails) but breaks SQLite on macOS (unlink succeeds).
    manager.dispose()

    result = restore_backup(
        backup,
        settings.database_path,
        force=True,
        safety_dir=settings.data_dir / "backups",
    )
    assert result["safety_backup"]["counts"]["memory_records"] == 2
    assert result["integrity_after_restore"] == "ok"

    records, total = MemoryService(manager).list(page_size=1000)
    assert total == 1
    assert records[0].content == "before restore"


def test_remove_stale_wal_deletes_side_files(settings, manager):
    _add_memory(manager, "data")
    manager.dispose()

    wal = Path(str(settings.database_path) + "-wal")
    shm = Path(str(settings.database_path) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"")

    removed = _remove_stale_wal(settings.database_path)
    assert set(removed) == {str(wal), str(shm)}
    assert not wal.exists()
    assert not shm.exists()


def test_restore_leaves_no_stale_side_files(settings, manager):
    _add_memory(manager, "data")
    backup = settings.data_dir / "b.db"
    create_backup(settings.database_path, backup)

    # Restore is offline: no live engine may hold the WAL/SHM side files.
    manager.dispose()

    restore_backup(
        backup,
        settings.database_path,
        force=True,
        safety_dir=settings.data_dir / "backups",
    )

    assert not Path(str(settings.database_path) + "-wal").exists()
    assert not Path(str(settings.database_path) + "-shm").exists()
    assert _count_memories(manager) == 1


def test_backup_restore_roundtrip_preserves_search(settings, manager):
    _add_memory(manager, "机器学习与深度学习")
    backup = settings.data_dir / "b.db"
    create_backup(settings.database_path, backup)

    service = MemoryService(manager)
    results, total = service.search(query="机器学习")
    assert total == 1

    # Restore is offline: release the pooled connections before side-file
    # removal, matching the production restore script.
    manager.dispose()

    restore_backup(
        backup,
        settings.database_path,
        force=True,
        safety_dir=settings.data_dir / "backups",
    )
    results, total = MemoryService(manager).search(query="机器学习")
    assert total == 1
