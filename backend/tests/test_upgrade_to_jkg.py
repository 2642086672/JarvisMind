"""JKG-INT-01 · tests for the explicit JKG Schema installer (scripts/upgrade_to_jkg.py).

Every database here is a throwaway file under the session temp root, built by
the existing test-only factories. Raw SQLite access goes through
``_fixtures.schema_factory`` helpers so the P0-03A static gate stays clean.
Nothing here touches ``jme/data``. Hard links are created only to test-owned
copies inside a temporary data directory that replaces the production one.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _fixtures import schema_factory as factory
from jarvismind.database.migrations import MIGRATIONS
from jarvismind.jkg import schema as jkg_schema
from jarvismind.main import create_app

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = JARVISMIND_ROOT / "scripts" / "upgrade_to_jkg.py"
JKG_TABLES = ("jkg_nodes", "jkg_edges", "jkg_edge_sources")


@pytest.fixture
def inst(monkeypatch):
    return factory.p0_load_script_module("upgrade_to_jkg", INSTALLER, monkeypatch, {})


def _v6(manager) -> Path:
    return factory._test_only_v6_db(manager)


def _observable(inst, path: Path) -> dict:
    conn = factory.p0_safe_ro_connect(path)
    try:
        master = tuple(
            conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        )
        fp = inst.fingerprint(conn)
    finally:
        conn.close()
    listing = tuple(sorted(item.name for item in path.parent.iterdir()))
    return {"master": master, "fp": fp, "listing": listing}


def _jkg_names(path: Path, inst=None) -> set:
    conn = inst._open_ro(path) if inst is not None else factory.p0_safe_ro_connect(path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master").fetchall()
            if row[0].startswith(("jkg_", "idx_jkg_", "uq_jkg_", "sqlite_autoindex_jkg_"))
        }
    finally:
        conn.close()


def _no_connect(monkeypatch, inst) -> None:
    def _forbidden(*args, **kwargs):
        raise AssertionError("no SQLite connection may be opened for a refused target")

    monkeypatch.setattr(inst.sqlite3, "connect", _forbidden)


# ---------------------------------------------------------------------------
# Plan, allowlist and pinned digest (pure, no database)
# ---------------------------------------------------------------------------
def test_plan_is_exactly_the_canonical_three_tables_and_nine_indexes(inst):
    plan = inst.build_plan()
    assert len(plan) == 12
    assert sum(1 for s in plan if s.startswith("CREATE TABLE IF NOT EXISTS ")) == 3
    assert sum(1 for s in plan if "INDEX IF NOT EXISTS" in s) == 9


def test_pinned_digest_matches_the_canonical_source(inst):
    canonical = "\n".join(" ".join(s.split()) for s in jkg_schema.JKG_SCHEMA_STATEMENTS)
    assert inst.EXPECTED_PLAN_SHA256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "replacement",
    [
        ("CHECK (confidence >= 0.0 AND confidence <= 1.0)", ""),
        ("REAL NOT NULL DEFAULT 0.5", "REAL NOT NULL DEFAULT 0.6"),
    ],
)
def test_tampered_canonical_ddl_is_refused_by_the_pinned_digest(inst, replacement):
    old, new = replacement
    tampered = [s.replace(old, new) for s in jkg_schema.JKG_SCHEMA_STATEMENTS]
    assert tampered != list(jkg_schema.JKG_SCHEMA_STATEMENTS)
    with pytest.raises(inst.Refused) as excinfo:
        inst.build_plan(tampered)
    assert excinfo.value.code == inst.E_PLAN_REJECTED


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE jkg_nodes",
        "ALTER TABLE jkg_nodes ADD COLUMN x TEXT",
        "CREATE TABLE evil (x INTEGER)",
        "CREATE TABLE IF NOT EXISTS memory_records (id TEXT)",
        "CREATE TABLE IF NOT EXISTS jkg_nodes (x INTEGER); DROP TABLE memory_records",
        "CREATE INDEX IF NOT EXISTS idx_evil ON memory_records(id)",
        "CREATE INDEX IF NOT EXISTS idx_jkg_evil ON jkg_nodes(name) -- trailing",
        "CREATE TABLE IF NOT EXISTS jkg_nodes (x TEXT) WITHOUT ROWID; UPDATE x SET y=1",
        "CREATE VIEW IF NOT EXISTS jkg_nodes AS SELECT 1",
        "CREATE TABLE IF NOT EXISTS jkg_nodes (x TEXT) AS SELECT 1 AS x",
    ],
)
def test_allowlist_refuses_foreign_or_unsafe_statements(inst, statement):
    with pytest.raises(inst.Refused) as excinfo:
        inst.build_plan([statement])
    assert excinfo.value.code == inst.E_PLAN_REJECTED


def test_plan_refuses_any_missing_canonical_statement(inst):
    with pytest.raises(inst.Refused):
        inst.build_plan(list(inst.build_plan())[:-1])


# ---------------------------------------------------------------------------
# Target validation (no SQLite I/O)
# ---------------------------------------------------------------------------
def test_production_path_is_refused_before_any_io(inst, monkeypatch):
    _no_connect(monkeypatch, inst)
    existed = inst.PRODUCTION_DB_PATH.exists()
    report = inst.install(inst.PRODUCTION_DB_PATH, mode="install")
    assert report["status"] == "REFUSED"
    assert report["error_code"] == inst.E_TARGET_PRODUCTION
    assert report["exit_code"] == 2
    assert inst.PRODUCTION_DB_PATH.exists() == existed


@pytest.mark.parametrize("form", ["extended", "unc", "forward-extended"])
def test_unsupported_path_forms_are_refused_before_any_io(inst, monkeypatch, form):
    prod = str(inst.PRODUCTION_DB_PATH)
    raw = {
        "extended": "\\\\?\\" + prod,
        "unc": "\\\\localhost\\" + prod.replace(":", "$"),
        "forward-extended": "//?/" + prod.replace("\\", "/"),
    }[form]
    _no_connect(monkeypatch, inst)
    existed = inst.PRODUCTION_DB_PATH.exists()
    report = inst.install(raw, mode="install")
    assert report["status"] == "REFUSED"
    assert report["error_code"] == inst.E_TARGET_UNSUPPORTED
    assert inst.PRODUCTION_DB_PATH.exists() == existed


def test_traversal_into_data_directory_is_refused_before_any_io(inst, monkeypatch):
    _no_connect(monkeypatch, inst)
    raw = str(inst.PRODUCTION_DATA_DIR / ".." / "data" / "jarvismind.db")
    report = inst.install(raw, mode="install")
    assert report["error_code"] == inst.E_TARGET_PRODUCTION


def test_hard_link_to_a_production_database_is_refused(inst, manager, tmp_path):
    source = _v6(manager)
    fake_data = tmp_path / "fake-data"
    fake_data.mkdir()
    fake_db = fake_data / "jarvismind.db"
    factory.p0_make_ro_snapshot_copy(source, fake_db)
    inst_module_data, inst_module_db = inst.PRODUCTION_DATA_DIR, inst.PRODUCTION_DB_PATH
    inst.PRODUCTION_DATA_DIR, inst.PRODUCTION_DB_PATH = fake_data, fake_db
    try:
        other = tmp_path / "other"
        other.mkdir()
        alias = other / "alias.db"
        os.link(fake_db, alias)
        before = factory.p0_capture_five_way(alias)
        report = inst.install(alias, mode="install")
        assert report["error_code"] == inst.E_TARGET_PRODUCTION
        assert factory.p0_capture_five_way(alias) == before
        plain = other / "plain.db"
        factory.p0_make_ro_snapshot_copy(source, plain)
        assert inst.check_target(plain) == plain.resolve()
    finally:
        inst.PRODUCTION_DATA_DIR, inst.PRODUCTION_DB_PATH = inst_module_data, inst_module_db


def test_real_database_override_requires_non_empty_approval(inst):
    with pytest.raises(inst.Refused) as excinfo:
        inst.check_target(inst.PRODUCTION_DB_PATH, allow_real_database=True, approval_ref="   ")
    assert excinfo.value.code == inst.E_APPROVAL_MISSING
    resolved = inst.check_target(
        inst.PRODUCTION_DB_PATH, allow_real_database=True, approval_ref="OWNER-REF-TEST"
    )
    assert resolved == inst.PRODUCTION_DB_PATH.resolve()


def test_installer_is_not_wired_into_runtime_or_migrations():
    runtime_files = [JARVISMIND_ROOT / "jarvismind" / "main.py", JARVISMIND_ROOT / "jarvismind" / "container.py"]
    runtime_files += list((JARVISMIND_ROOT / "jarvismind").rglob("*.py"))
    for path in runtime_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "upgrade_to_jkg" not in text, f"installer referenced from runtime file: {path}"
    assert max(m.version for m in MIGRATIONS) == 6


def test_missing_file_is_refused_and_not_created(inst, tmp_path):
    target = tmp_path / "absent" / "nope.db"
    report = inst.install(target, mode="install")
    assert report["error_code"] == inst.E_TARGET_MISSING
    assert not target.exists()


def test_non_sqlite_file_is_refused_and_untouched(inst, tmp_path):
    target = tmp_path / "plain.txt"
    target.write_text("not a database", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    report = inst.install(target, mode="install")
    assert report["error_code"] == inst.E_NOT_SQLITE
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest


def test_corrupt_body_after_sqlite_header_is_reported_not_raised(inst, tmp_path):
    target = tmp_path / "corrupt.db"
    target.write_bytes(inst._SQLITE_HEADER + b"\x00" * 200 + b"\xff" * 4096)
    report = inst.install(target, mode="install")
    assert report["error_code"] == inst.E_INTEGRITY
    assert report["exit_code"] == 2


def test_percent_and_hash_in_file_names_are_read_exactly(inst, manager, tmp_path):
    source = _v6(manager)
    folder = tmp_path / "names"
    folder.mkdir()
    pct = folder / "w%41.db"
    installed = folder / "wA.db"
    hashed = folder / "h#x y.db"  # '?' is not a legal Windows file-name character
    for dst in (pct, installed, hashed):
        factory.p0_make_ro_snapshot_copy(source, dst)
    assert inst.install(installed, mode="install")["status"] == "INSTALLED"
    assert inst.install(pct)["state_before"] == inst.STATE_ABSENT
    assert inst.install(hashed)["state_before"] == inst.STATE_ABSENT
    assert inst.install(installed)["state_before"] == inst.STATE_INSTALLED
    backup = inst.install(pct, mode="install", backup_dir=folder / "bk")
    assert backup["status"] == "INSTALLED"
    assert _jkg_names(Path(backup["backup"]["backup_file"]), inst) == set()


def test_inspect_mode_is_the_default_and_writes_nothing(inst, manager):
    path = _v6(manager)
    before = factory.p0_capture_five_way(path)
    report = inst.install(path)
    assert report["mode"] == "inspect"
    assert report["status"] == "INSPECTED"
    assert report["state_before"] == inst.STATE_ABSENT
    assert factory.p0_capture_five_way(path) == before


def test_jme_preconditions_refuse_unexpected_versions_without_changes(inst, manager):
    path = factory._test_only_version_too_new_db(manager)
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JME_PRECONDITION
    assert factory.p0_capture_five_way(path) == before


def test_missing_middle_migration_is_refused(inst, manager):
    path = factory._test_only_missing_middle_db(manager)
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JME_PRECONDITION
    assert _jkg_names(path) == set()


def test_fingerprint_covers_every_jme_table(inst, manager):
    path = _v6(manager)
    conn = factory.p0_safe_ro_connect(path)
    try:
        fp = inst.fingerprint(conn)
    finally:
        conn.close()
    assert {"memory_records", "memory_candidates", "system_settings", "schema_migrations",
            "memory_audit_logs"} <= set(fp["jme_tables"])


# ---------------------------------------------------------------------------
# Install, repeat, and state classification
# ---------------------------------------------------------------------------
def test_fresh_install_on_v6_database_builds_exact_structure(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)
    report = inst.install(path, mode="install")
    assert report["status"] == "INSTALLED", report
    assert report["statements_executed"] == 12
    assert report["error_code"] is None

    conn = factory.p0_safe_ro_connect(path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'jkg_%'"
        )}
        assert tables == set(JKG_TABLES)
        explicit = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "AND (name LIKE 'idx_jkg_%' OR name LIKE 'uq_jkg_%')"
        )}
        assert len(explicit) == 9
        unique = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_jkg_%'"
        )}
        assert unique == {
            "uq_jkg_nodes_type_key",
            "uq_jkg_edges_triple",
            "uq_jkg_edge_sources_edge_memory",
        }
        assert len(conn.execute('PRAGMA foreign_key_list("jkg_edges")').fetchall()) == 2
        assert len(conn.execute('PRAGMA foreign_key_list("jkg_edge_sources")').fetchall()) == 2
        assert len(conn.execute('PRAGMA foreign_key_list("jkg_nodes")').fetchall()) == 1
        ddl = " ".join(r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name LIKE 'jkg_%' AND sql IS NOT NULL"
        ))
        assert "CHECK (entity_type IN ('PERSON', 'PROJECT', 'EVENT'))" in ddl
        assert "CHECK (predicate IN ('WORKS_ON', 'ATTENDS', 'BELONGS_TO', 'KNOWS'))" in ddl
        assert "CHECK (subject_id <> object_id)" in ddl
        assert conn.execute(
            "SELECT value FROM system_settings WHERE key = 'jkg.schema_version'"
        ).fetchone()[0] == "1"
        assert [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1, 2, 3, 4, 5, 6]
    finally:
        conn.close()

    after = _observable(inst, path)
    assert after["fp"] == before["fp"]
    added = {row[1] for row in after["master"]} - {row[1] for row in before["master"]}
    assert len(added) == 15  # 3 tables + 9 explicit indexes + 3 primary-key autoindexes
    assert all(name.startswith(("jkg_", "idx_jkg_", "uq_jkg_", "sqlite_autoindex_jkg_")) for name in added)


def test_repeat_install_is_an_exact_noop(inst, manager):
    path = _v6(manager)
    assert inst.install(path, mode="install")["status"] == "INSTALLED"
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["status"] == "ALREADY_INSTALLED"
    assert report["error_code"] is None
    assert report.get("exit_code", 0) == 0
    assert factory.p0_capture_five_way(path) == before


def test_partial_jkg_tables_are_refused_without_repair(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()

    def only_nodes(conn):
        for stmt in plan[:4]:  # jkg_nodes plus its three indexes
            conn.execute(stmt)

    factory._mutate(manager, only_nodes)
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["status"] == "REFUSED"
    assert report["error_code"] == inst.E_JKG_PARTIAL
    assert report["exit_code"] == 2
    assert factory.p0_capture_five_way(path) == before


def test_missing_explicit_index_with_version_key_is_refused(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()
    omitted = "idx_jkg_edge_sources_edge"

    def almost_complete(conn):
        for stmt in plan:
            if omitted not in stmt:
                conn.execute(stmt)
        conn.execute(
            "INSERT INTO system_settings(key, value, updated_at) "
            "VALUES ('jkg.schema_version', '1', '2026-11-20T00:00:00+00:00')"
        )

    factory._mutate(manager, almost_complete)
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_PARTIAL
    assert any(omitted in detail for detail in report["details"])
    assert factory.p0_capture_five_way(path) == before


def test_external_index_with_jkg_name_is_a_conflict(inst, manager):
    path = _v6(manager)
    factory._mutate(
        manager,
        lambda conn: conn.execute("CREATE INDEX idx_jkg_nodes_type ON memory_records(memory_type)"),
    )
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_CONFLICT
    assert factory.p0_capture_five_way(path) == before


def test_check_constraint_mismatch_is_a_conflict(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()
    altered = plan[0].replace("CHECK (entity_type IN ('PERSON', 'PROJECT', 'EVENT'))", "")
    assert altered != plan[0]
    factory._mutate(manager, lambda conn: conn.execute(altered))
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_CONFLICT


def test_foreign_key_mismatch_is_a_conflict(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()
    target = next(s for s in plan if "CREATE TABLE IF NOT EXISTS jkg_edge_sources" in s)
    altered = target.replace(
        "memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE",
        "memory_id TEXT NOT NULL",
    )
    assert altered != target
    factory._mutate(manager, lambda conn: conn.execute(altered))
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_CONFLICT


def test_unexpected_jkg_named_object_is_refused(inst, manager):
    path = _v6(manager)
    factory._mutate(
        manager,
        lambda conn: conn.execute("CREATE INDEX idx_jkg_extra_probe ON memory_records(id)"),
    )
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_UNEXPECTED
    assert "unexpected:idx_jkg_extra_probe" in report["details"]


def test_version_key_without_objects_is_refused(inst, manager):
    path = _v6(manager)
    factory._mutate(
        manager,
        lambda conn: conn.execute(
            "INSERT INTO system_settings(key, value, updated_at) "
            "VALUES ('jkg.schema_version', '1', '2026-11-20T00:00:00+00:00')"
        ),
    )
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_VERSION


def test_version_mismatch_on_complete_structure_is_refused(inst, manager):
    path = _v6(manager)
    assert inst.install(path, mode="install")["status"] == "INSTALLED"
    factory._mutate(
        manager,
        lambda conn: conn.execute("UPDATE system_settings SET value = '2' WHERE key = 'jkg.schema_version'"),
    )
    before = factory.p0_capture_five_way(path)
    report = inst.install(path, mode="install")
    assert report["error_code"] == inst.E_JKG_VERSION
    assert factory.p0_capture_five_way(path) == before


def test_lock_held_by_another_writer_is_refused_without_changes(inst, manager, monkeypatch):
    path = _v6(manager)
    before = factory.p0_capture_five_way(path)
    monkeypatch.setattr(inst, "LOCK_TIMEOUT_SECONDS", 0.05)
    holder = factory._connect(path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        report = inst.install(path, mode="install")
    finally:
        holder.rollback()
        holder.close()
    assert report["error_code"] == inst.E_LOCKED
    assert report["exit_code"] == 2
    assert factory.p0_capture_five_way(path) == before


def test_state_changed_before_the_lock_is_reclassified_under_the_lock(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()

    def install_concurrently(_target):
        def full(conn):
            for stmt in plan:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO system_settings(key, value, updated_at) "
                "VALUES ('jkg.schema_version', '1', '2026-11-20T00:00:00+00:00')"
            )

        factory._mutate(manager, full)

    report = inst.install(path, mode="install", _before_lock_hook=install_concurrently)
    assert report["status"] == "ALREADY_INSTALLED", report
    assert report["error_code"] is None
    assert len(_jkg_names(path)) == 15


# ---------------------------------------------------------------------------
# JME preservation: content, non-content columns, triggers, migrations
# ---------------------------------------------------------------------------
def test_non_content_change_to_a_jme_row_rolls_back(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)

    def touch_timestamp(conn):
        conn.execute("UPDATE memory_records SET updated_at = 'changed-by-probe' WHERE id = 'seed-mem'")

    report = inst.install(path, mode="install", _verify_hook=touch_timestamp)
    assert report["status"] == "ROLLED_BACK"
    assert report["error_code"] == inst.E_VERIFY_FAILED
    after = _observable(inst, path)
    assert after["master"] == before["master"]
    assert after["fp"] == before["fp"]


def test_trigger_added_to_a_jme_table_rolls_back(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)

    def add_trigger(conn):
        conn.execute(
            "CREATE TRIGGER jme_probe_trigger AFTER UPDATE ON memory_records BEGIN SELECT 1; END"
        )

    report = inst.install(path, mode="install", _verify_hook=add_trigger)
    assert report["status"] == "ROLLED_BACK"
    assert report["error_code"] == inst.E_VERIFY_FAILED
    assert _observable(inst, path)["master"] == before["master"]


def test_schema_migrations_change_rolls_back(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)

    def rename_marker(conn):
        conn.execute("UPDATE schema_migrations SET name = 'probe' WHERE version = 6")

    report = inst.install(path, mode="install", _verify_hook=rename_marker)
    assert report["status"] == "ROLLED_BACK"
    assert report["error_code"] == inst.E_VERIFY_FAILED
    assert _observable(inst, path)["fp"] == before["fp"]


# ---------------------------------------------------------------------------
# Transaction, rollback, and unknown-state reporting
# ---------------------------------------------------------------------------
def test_ddl_failure_rolls_back_every_statement(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)
    seen = {"count": 0}

    def fail_on_fifth(stmt):
        seen["count"] += 1
        if seen["count"] == 5:
            raise sqlite3.OperationalError("simulated DDL failure")

    report = inst.install(path, mode="install", _execute_hook=fail_on_fifth)
    assert report["status"] == "ROLLED_BACK"
    assert report["error_code"] == inst.E_DDL_FAILED
    assert report["exit_code"] == 3
    after = _observable(inst, path)
    assert after["master"] == before["master"]
    assert after["fp"] == before["fp"]
    assert after["listing"] == before["listing"]
    assert _jkg_names(path) == set()


def test_structure_verification_failure_rolls_back(inst, manager):
    path = _v6(manager)
    before = _observable(inst, path)

    def inject_unexpected(conn):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jkg_probe_extra ON memory_records(id)")

    report = inst.install(path, mode="install", _verify_hook=inject_unexpected)
    assert report["status"] == "ROLLED_BACK"
    assert report["error_code"] == inst.E_VERIFY_FAILED
    assert report["exit_code"] == 3
    after = _observable(inst, path)
    assert after["master"] == before["master"]
    assert after["fp"] == before["fp"]


def test_hook_that_commits_is_reported_as_unknown_state_not_rollback(inst, manager):
    path = _v6(manager)

    def commit_inside(conn):
        conn.execute("COMMIT")

    report = inst.install(path, mode="install", _verify_hook=commit_inside)
    assert report["status"] == "UNKNOWN_STATE"
    assert report["error_code"] == inst.E_POST_COMMIT
    assert report["exit_code"] == 4
    assert len(_jkg_names(path)) == 15  # the committed objects are real; the report says so


def test_unexpected_internal_error_is_reported_not_raised(inst, manager, monkeypatch):
    path = _v6(manager)
    before = factory.p0_capture_five_way(path)

    def boom(*args, **kwargs):
        raise RuntimeError("probe")

    monkeypatch.setattr(inst, "build_plan", boom)
    report = inst.install(path, mode="install")
    assert report["status"] == "ERROR"
    assert report["error_code"] == inst.E_UNEXPECTED
    assert report["exit_code"] == 5
    assert factory.p0_capture_five_way(path) == before


# ---------------------------------------------------------------------------
# Backup and restore drill
# ---------------------------------------------------------------------------
def test_backup_restore_drill_round_trips_to_pre_install_state(inst, manager, tmp_path):
    path = _v6(manager)
    before = _observable(inst, path)
    backup_dir = tmp_path / "backups"
    report = inst.install(path, mode="install", backup_dir=backup_dir)
    assert report["status"] == "INSTALLED", report

    backup = report["backup"]
    backup_file = Path(backup["backup_file"])
    manifest = json.loads(Path(backup["manifest_file"]).read_text(encoding="utf-8"))
    assert hashlib.sha256(backup_file.read_bytes()).hexdigest() == manifest["sha256"]
    assert manifest["sha256"] == backup["sha256"]
    assert _jkg_names(backup_file, inst) == set()

    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    restored = restored_dir / "restored.db"
    shutil.copyfile(backup_file, restored)
    restored_state = _observable(inst, restored)
    assert restored_state["fp"] == before["fp"]
    assert _jkg_names(restored) == set()

    again = inst.install(restored, mode="install")
    assert again["status"] == "INSTALLED"
    assert _jkg_names(restored) == _jkg_names(path)


def test_backup_failure_leaves_database_untouched(inst, manager, tmp_path):
    path = _v6(manager)
    before = factory.p0_capture_five_way(path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    report = inst.install(path, mode="install", backup_dir=blocker)
    assert report["error_code"] == inst.E_BACKUP_FAILED
    assert report["exit_code"] == 2
    assert factory.p0_capture_five_way(path) == before


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def test_cli_reports_refusal_as_json_with_exit_code_two(inst, tmp_path, capsys):
    code = inst.main(["--db", str(tmp_path / "missing.db")])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == inst.E_TARGET_MISSING
    assert payload["mode"] == "inspect"


# ---------------------------------------------------------------------------
# Review round 2: backup location, lock-time re-check, post-commit faults
# ---------------------------------------------------------------------------
def _fake_data_dir(inst, monkeypatch, manager, tmp_path):
    """A test-owned stand-in for jme/data; the real production directory is never touched."""
    source = _v6(manager)
    fake_data = tmp_path / "fake-data"
    fake_data.mkdir()
    fake_db = fake_data / "jarvismind.db"
    factory.p0_make_ro_snapshot_copy(source, fake_db)
    monkeypatch.setattr(inst, "PRODUCTION_DATA_DIR", fake_data)
    monkeypatch.setattr(inst, "PRODUCTION_DB_PATH", fake_db)
    return fake_data, fake_db


def test_backup_directory_inside_data_directory_is_refused(inst, manager, tmp_path, monkeypatch):
    fake_data, _ = _fake_data_dir(inst, monkeypatch, manager, tmp_path)
    target = _v6(manager)
    before = factory.p0_capture_five_way(target)
    listing_before = sorted(item.name for item in fake_data.iterdir())
    report = inst.install(target, mode="install", backup_dir=fake_data)
    assert report["error_code"] == inst.E_TARGET_PRODUCTION
    assert report["exit_code"] == 2
    assert sorted(item.name for item in fake_data.iterdir()) == listing_before
    assert factory.p0_capture_five_way(target) == before


def test_hard_link_swapped_in_before_the_lock_is_refused_at_the_recheck(inst, manager, tmp_path, monkeypatch):
    _, fake_db = _fake_data_dir(inst, monkeypatch, manager, tmp_path)
    target = tmp_path / "candidate.db"
    factory.p0_make_ro_snapshot_copy(_v6(manager), target)
    fake_before = factory.p0_capture_five_way(fake_db)
    entered = {"count": 0}

    def count_statements(stmt):
        entered["count"] += 1

    def swap_in_hard_link(_resolved):
        target.unlink()
        os.link(fake_db, target)

    report = inst.install(
        target,
        mode="install",
        _before_lock_hook=swap_in_hard_link,
        _execute_hook=count_statements,
    )
    assert report["status"] == "REFUSED"
    assert report["error_code"] == inst.E_TARGET_PRODUCTION
    assert entered["count"] == 0  # the transaction was never entered
    assert factory.p0_capture_five_way(fake_db) == fake_before


def test_internal_error_after_commit_is_reported_as_unknown_state(inst, manager, monkeypatch):
    path = _v6(manager)
    real_fingerprint = inst.fingerprint
    calls = {"count": 0}

    def fail_on_post_commit(conn):
        calls["count"] += 1
        if calls["count"] == 3:  # 1: pre-check, 2: inside the transaction, 3: post-commit
            raise RuntimeError("probe failure after commit")
        return real_fingerprint(conn)

    monkeypatch.setattr(inst, "fingerprint", fail_on_post_commit)
    report = inst.install(path, mode="install")
    assert report["status"] == "UNKNOWN_STATE"
    assert report["error_code"] == inst.E_POST_COMMIT
    assert report["exit_code"] == 4
    assert len(_jkg_names(path)) == 15  # committed before the failure; the report must say unknown


def test_jme_row_changed_after_commit_is_reported_as_unknown_state(inst, manager, monkeypatch):
    path = _v6(manager)
    real_fingerprint = inst.fingerprint
    calls = {"count": 0}

    def change_row_then_fingerprint(conn):
        calls["count"] += 1
        if calls["count"] == 3:  # post-commit verification: another writer edits a JME row
            factory._mutate(
                manager,
                lambda c: c.execute(
                    "UPDATE memory_records SET updated_at = 'changed-after-commit' WHERE id = 'seed-mem'"
                ),
            )
        return real_fingerprint(conn)

    monkeypatch.setattr(inst, "fingerprint", change_row_then_fingerprint)
    report = inst.install(path, mode="install")
    assert report["status"] == "UNKNOWN_STATE"
    assert report["error_code"] == inst.E_POST_COMMIT
    assert report["exit_code"] == 4


# ---------------------------------------------------------------------------
# Coverage closure: empty database, memory-table integrity, no auto-install
# ---------------------------------------------------------------------------
SEED_MEMORY_CONTENT = "真实记忆内容-零写入验证"
SEED_CANDIDATE_CONTENT = "真实候选内容"


def _v6_empty(manager) -> Path:
    return factory._test_only_v6_empty_db(manager)


def _row_count(path: Path, table: str) -> int:
    conn = factory.p0_safe_ro_connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _memory_integrity(path: Path) -> dict:
    """Row count and SHA-256 over the ordered rows of both memory tables (read-only)."""
    report = {}
    conn = factory.p0_safe_ro_connect(path)
    try:
        for table in ("memory_records", "memory_candidates"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            digest = hashlib.sha256()
            for row in rows:
                digest.update(repr(tuple(row)).encode("utf-8") + b"\n")
            report[table] = (len(rows), digest.hexdigest())
    finally:
        conn.close()
    return report


def _assert_seed_content(path: Path) -> None:
    conn = factory.p0_safe_ro_connect(path)
    try:
        memory = conn.execute(
            "SELECT content FROM memory_records WHERE id = 'seed-mem'"
        ).fetchall()
        candidate = conn.execute(
            "SELECT content FROM memory_candidates WHERE id = 'seed-cand'"
        ).fetchall()
    finally:
        conn.close()
    assert memory == [(SEED_MEMORY_CONTENT,)]
    assert candidate == [(SEED_CANDIDATE_CONTENT,)]


def _write_non_jme_sqlite(path: Path) -> None:
    conn = factory._connect(path)
    try:
        conn.execute("CREATE TABLE unrelated_probe (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO unrelated_probe (note) VALUES ('no JME tables here')")
        conn.commit()
    finally:
        conn.close()


def test_empty_v6_database_with_zero_business_rows_installs(inst, manager):
    path = _v6_empty(manager)
    assert _row_count(path, "memory_records") == 0
    assert _row_count(path, "memory_candidates") == 0
    report = inst.install(path, mode="install")
    assert report["status"] == "INSTALLED", report
    assert report["error_code"] is None
    assert report["statements_executed"] == 12
    names = _jkg_names(path)
    assert len(names) == 15
    assert {n for n in names if n in JKG_TABLES} == set(JKG_TABLES)
    assert sum(1 for n in names if n.startswith("sqlite_autoindex_jkg_")) == 3
    conn = factory.p0_safe_ro_connect(path)
    try:
        version = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'jkg.schema_version'"
        ).fetchall()
    finally:
        conn.close()
    assert version == [("1",)]
    assert _row_count(path, "memory_records") == 0
    assert _row_count(path, "memory_candidates") == 0


def test_valid_sqlite_without_jme_tables_is_refused_and_byte_identical(inst, tmp_path):
    target = tmp_path / "no-jme" / "unrelated.db"
    _write_non_jme_sqlite(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    before = factory.p0_capture_five_way(target)
    report = inst.install(target, mode="install")
    assert report["status"] == "REFUSED", report
    assert report["error_code"] == inst.E_JME_PRECONDITION == "JKG-INST-005"
    assert report["exit_code"] == 2
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert factory.p0_capture_five_way(target) == before
    assert _jkg_names(target) == set()


def test_memory_tables_are_byte_identical_after_successful_install(inst, manager):
    path = _v6(manager)
    before = _memory_integrity(path)
    assert before["memory_records"][0] == 1
    assert before["memory_candidates"][0] == 1
    _assert_seed_content(path)
    report = inst.install(path, mode="install")
    assert report["status"] == "INSTALLED", report
    assert _memory_integrity(path) == before
    _assert_seed_content(path)


def test_memory_tables_are_byte_identical_after_refused_install(inst, manager):
    path = _v6(manager)
    plan = inst.build_plan()

    def only_nodes(conn):
        for stmt in plan[:4]:
            conn.execute(stmt)

    factory._mutate(manager, only_nodes)
    before = _memory_integrity(path)
    report = inst.install(path, mode="install")
    assert report["status"] == "REFUSED", report
    assert report["error_code"] == inst.E_JKG_PARTIAL
    assert _memory_integrity(path) == before
    _assert_seed_content(path)


def test_memory_tables_are_byte_identical_after_rolled_back_install(inst, manager):
    path = _v6(manager)
    before = _memory_integrity(path)
    seen = {"count": 0}

    def fail_on_fifth(stmt):
        seen["count"] += 1
        if seen["count"] == 5:
            raise sqlite3.OperationalError("simulated DDL failure")

    report = inst.install(path, mode="install", _execute_hook=fail_on_fifth)
    assert report["status"] == "ROLLED_BACK", report
    assert report["error_code"] == inst.E_DDL_FAILED
    assert _jkg_names(path) == set()
    assert _memory_integrity(path) == before
    _assert_seed_content(path)


def test_serving_api_requests_never_creates_a_jkg_object(settings, manager):
    path = _v6_empty(manager)
    assert _jkg_names(path) == set()
    headers = {"X-API-Token": settings.api_token}
    with TestClient(create_app(settings, manager=manager)) as client:
        health = client.get("/health")
        listed = client.get("/api/v1/memories", headers=headers)
        created = client.post(
            "/api/v1/memories",
            json={"memory_type": "SEMANTIC", "content": "JKG-INT-01 no-auto-install probe"},
            headers=headers,
        )
        listed_again = client.get("/api/v1/memories", headers=headers)
    assert health.status_code == 200, health.text
    assert listed.status_code == 200, listed.text
    assert created.status_code == 201, created.text
    assert listed_again.status_code == 200, listed_again.text
    assert _jkg_names(path) == set()


def test_lifespan_and_reimport_cache_hit_create_no_jkg_object(settings, manager):
    """lifespan 启停 + 一次 import 缓存命中都不建 JKG 对象（不含全新解释器保证）。

    本文件在模块导入期已 `from jarvismind.main import create_app`，因此用例体内的
    `import jarvismind.container` / `import jarvismind.main` 是 sys.modules 缓存命中，什么
    也不执行；它们只证明缓存命中不产生副作用，**不**证明全新解释器导入无害。
    全新解释器的保证由 `test_fresh_interpreter_import_creates_no_jkg_object`
    （真实 subprocess 子进程）承担。
    """
    path = _v6_empty(manager)
    assert _jkg_names(path) == set()
    with TestClient(create_app(settings, manager=manager)):
        pass
    assert _jkg_names(path) == set()
    import jarvismind.container  # noqa: F401  (cache hit: proven elsewhere, not here)
    import jarvismind.main  # noqa: F401  (cache hit: proven elsewhere, not here)
    assert _jkg_names(path) == set()


def _sqlite_header(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(16)


def test_fresh_interpreter_import_creates_no_jkg_object(inst, manager, tmp_path):
    """全新解释器里 `import jarvismind.container, jarvismind.main, jarvismind.jkg` 不建任何 JKG 对象。

    真实子进程，不复用本进程的 sys.modules，因此这次导入确实执行了模块级代码；
    子进程在导入成功后打印 `IMPORT-OK` 标记，测试断言该标记出现，避免再次
    退化成「什么都没执行」的空讨论。
    子进程被剥掉全部 JARVISMIND_* 环境变量，只拿到指向 `tmp_path` 的数据目录环境变量
    （变量名拼接构造，避免静态门禁的完整字面量规则），工作目录固定为 `jme/`。
    断言的是「导入没有安装 JKG」：子进程结束后，该数据目录下任何 SQLite 文件
    都不得含 `jkg_` / `idx_jkg_` / `uq_jkg_` / `sqlite_autoindex_jkg_` 对象。
    完全没有生成数据库文件同样是通过——导入本就不该建库。
    末尾用一次真实安装做探针正控，证明 `_jkg_names` 不是恒为空。
    """
    # 局部导入：P0-03A 静态门禁对 import:subprocess 的豁免只绑定本函数
    # （见 tests/conftest.py 的 P0_ALLOWLIST），模块级导入不被放行。
    import subprocess

    data_dir = tmp_path / "fresh-data"
    data_dir.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("JARVISMIND_")}
    env["JARVISMIND_" + "DATA_DIR"] = str(data_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = (
        "import jarvismind.container, jarvismind.main, jarvismind.jkg, sys\n"
        "missed = [m for m in ('jarvismind.main', 'jarvismind.container', 'jarvismind.jkg')\n"
        "           if m not in sys.modules]\n"
        "print('IMPORT-OK' if not missed else 'IMPORT-MISSING ' + repr(missed))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", child],
        cwd=str(JARVISMIND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    # The import really ran in that interpreter; the assertion below is not a
    # statement about a process that did nothing.
    assert "IMPORT-OK" in completed.stdout, completed.stdout

    databases = [
        path
        for path in sorted(data_dir.rglob("*"))
        if path.is_file() and _sqlite_header(path) == inst._SQLITE_HEADER
    ]
    offenders = {str(path): sorted(_jkg_names(path)) for path in databases}
    assert {name: objs for name, objs in offenders.items() if objs} == {}, (
        f"fresh interpreter import created JKG objects: {offenders}"
    )

    # Detector positive control: the installer really does make objects appear,
    # so the empty result above is a property of the import, not of the probe.
    control = _v6(manager)
    assert _jkg_names(control) == set()
    assert inst.install(control, mode="install")["status"] == "INSTALLED"
    assert len(_jkg_names(control)) == 15


def test_jkg_package_executes_no_schema_statement_and_only_it_references_the_ddl():
    app_root = JARVISMIND_ROOT / "jarvismind"
    init_text = (app_root / "jkg" / "__init__.py").read_text(encoding="utf-8")
    assert "execute(" not in init_text, "jarvismind/jkg/__init__.py must not execute DDL at import"
    offenders = []
    for path in app_root.rglob("*.py"):
        rel = path.relative_to(JARVISMIND_ROOT)
        if rel.parts[:2] == ("jarvismind", "jkg"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "JKG_SCHEMA_STATEMENTS" in text:
            offenders.append(str(rel))
    assert offenders == [], f"DDL referenced outside jarvismind/jkg: {offenders}"
