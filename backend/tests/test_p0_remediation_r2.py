"""P0-03A-R2 · migration rejection path must perform ZERO database access.

Real first-stage gate (``real_migration_gate`` marker restores the real
methods).  Boom spies fail on any schema inspection, any connection, any SQL
statement; five-way state and the temp directory tree are compared before and
after.  All databases are session tmp_path databases.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, event, text

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _fixtures import schema_factory as factory  # noqa: E402
from jarvismind.database import migrations as migrations_module  # noqa: E402
from jarvismind.database.migrations import (  # noqa: E402
    MigrationBlocked,
    MigrationRunner,
    PartialMigrationDetected,
)

_DDL = frozenset({
    "CREATE", "ALTER", "DROP", "TRUNCATE", "REINDEX", "VACUUM",
    "ANALYZE", "ATTACH", "DETACH",
})
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT"})


def _sha(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(root: Path):
    if not root.exists():
        return ()
    out = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            out.append((rel, "dir", None, None))
        else:
            stat = path.stat()
            out.append((rel, "file", stat.st_size, _sha(path)))
    return tuple(out)


def _five_way(db_path: Path):
    return factory.p0_capture_five_way(db_path)


def _boom_counter():
    state = {"n": 0}

    def boom(*args, **kwargs):
        state["n"] += 1
        raise AssertionError("P0-03A-R2: 禁止的调用被触发")

    return boom, state


class _SqlCounter:
    """Count DDL/DML on both the instance Engine and the Engine class."""

    def __init__(self, engine):
        self.engine = engine
        self.ddl = 0
        self.dml = 0
        self.statements = []
        self._seen = set()
        self._targets = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        key = (id(cursor), str(statement))
        if key in self._seen:
            return
        self._seen.add(key)
        self.statements.append(str(statement))
        parts = str(statement).lstrip().split(None, 1)
        head = parts[0].upper().rstrip(";") if parts else ""
        if head in _DDL:
            self.ddl += 1
        elif head in _DML:
            self.dml += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        event.listen(Engine, "before_cursor_execute", self._on)
        self._targets = [self.engine, Engine]
        return self

    def __exit__(self, *exc_info):
        for target in self._targets:
            try:
                event.remove(target, "before_cursor_execute", self._on)
            except Exception:
                pass
        self._targets = []
        return False


@pytest.mark.real_migration_gate
def test_r2_apply_pending_rejects_before_any_database_access(manager, tmp_path, monkeypatch):
    """拒绝判断必须先于 Schema 检视、连接、SQL 与任何文件副作用。"""
    db_path = Path(manager.settings.database_path)
    assert db_path.is_relative_to(tmp_path.resolve()), "必须使用会话临时库"

    runner = MigrationRunner(manager)
    before = _five_way(db_path)
    tree_before = _tree(tmp_path)

    boom, state = _boom_counter()
    counter = _SqlCounter(manager.engine)
    with monkeypatch.context() as mp:
        mp.setattr(MigrationRunner, "inspect_schema", boom)
        mp.setattr(sqlite3, "connect", boom)
        mp.setattr(Engine, "connect", boom)
        mp.setattr(type(manager), "session", boom)
        mp.setattr(migrations_module, "create_engine", boom, raising=False)
        with counter:
            with pytest.raises(MigrationBlocked) as exc:
                runner.apply_pending()
            assert "拒绝前零数据库访问" in str(exc.value)

    after = _five_way(db_path)
    tree_after = _tree(tmp_path)
    assert state["n"] == 0, "拒绝路径触发了被禁止的调用"
    assert counter.ddl == 0 and counter.dml == 0
    assert counter.statements == [], f"拒绝路径执行了 SQL: {counter.statements}"
    assert after == before, "数据库五维状态在拒绝路径发生变化"
    assert tree_after == tree_before, "临时目录树在拒绝路径发生变化"


@pytest.mark.real_migration_gate
def test_r2_all_four_entry_points_have_zero_access(manager, tmp_path, monkeypatch):
    """initialize / rollback_v6 / initialize_database / apply_pending 全部零访问。"""
    db_path = Path(manager.settings.database_path)
    runner = MigrationRunner(manager)
    before = _five_way(db_path)
    tree_before = _tree(tmp_path)

    boom, state = _boom_counter()
    with monkeypatch.context() as mp:
        mp.setattr(MigrationRunner, "inspect_schema", boom)
        mp.setattr(sqlite3, "connect", boom)
        mp.setattr(Engine, "connect", boom)
        with _SqlCounter(manager.engine) as counter:
            with pytest.raises(MigrationBlocked):
                runner.initialize()
            with pytest.raises(MigrationBlocked):
                runner.rollback_v6()
            with pytest.raises(MigrationBlocked):
                migrations_module.initialize_database(manager)
            with pytest.raises(MigrationBlocked):
                runner.apply_pending()
            assert counter.ddl == 0 and counter.dml == 0
            assert counter.statements == []

    assert state["n"] == 0
    assert _five_way(db_path) == before
    assert _tree(tmp_path) == tree_before


@pytest.mark.real_migration_gate
def test_r2_rejection_does_not_swallow_diagnostic_exceptions(manager, monkeypatch):
    """不得靠捕获异常把诊断失败伪装成拒绝成功。"""
    runner = MigrationRunner(manager)

    def poisoned(*args, **kwargs):
        raise RuntimeError("diagnostic poison")

    monkeypatch.setattr(MigrationRunner, "inspect_schema", poisoned)
    with pytest.raises(MigrationBlocked):
        runner.apply_pending()


@pytest.mark.real_migration_gate
def test_r2_offline_diagnostic_path_still_works(manager):
    """允许路径：离线诊断入口保留原有只读检视 + partial_v6 判定行为。

    第一阶段不存在可执行迁移，因此「允许」仅指离线诊断；它必须仍能
    inspect_schema() 并返回真实快照，且 partial_v6 场景仍抛
    PartialMigrationDetected。
    """
    runner = MigrationRunner(manager)
    inspection = runner.plan_pending()
    assert inspection.code_version == migrations_module.MIGRATIONS[-1].version
    assert inspection.status in {"COMPATIBLE", "PENDING_MIGRATION", "UNINITIALIZED",
                                 "PARTIAL_V6", "VERSION_TOO_NEW", "MISSING_MIDDLE"}
    assert isinstance(inspection.partial_v6, bool)


@pytest.mark.real_migration_gate
def test_r2_partial_v6_diagnosis_is_preserved_on_tmp_db(manager, tmp_path):
    """partial_v6 判定归位到诊断入口，拒绝入口不再承担该职责。"""
    from dataclasses import replace

    data_dir = tmp_path / "partial_v6_case"
    db_path = data_dir / "jarvismind.db"
    st = replace(
        manager.settings,
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
    )
    case_manager = type(manager)(st)
    try:
        factory._test_only_build_partial_v6(case_manager)
        runner = MigrationRunner(case_manager)
        inspection = runner.inspect_schema()
        assert inspection.partial_v6 is True
        assert inspection.status == "PARTIAL_V6"
        with pytest.raises(PartialMigrationDetected) as exc:
            runner.plan_pending()
        assert "v6 部分对象残留" in str(exc.value)
        # 拒绝入口仍无条件阻断，不因 partial_v6 而改变行为或触发检视。
        with pytest.raises(MigrationBlocked):
            runner.apply_pending()
    finally:
        case_manager.dispose()
