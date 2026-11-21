"""P0-03 startup-safety regression suite.

G-1 真实第一阶段门控（跳过 autouse 替身）、G-2 create_app 零连接、
G-3 静态隔离、G-4 会话守卫，以及自动 DROP 阻断专项。8 个场景各自使用
tmp_path 下的独立库并写入真实测试数据，验证「零写入、零 DROP」。

批次 B 追加 14 例（P1-01~P1-05），并在 P1-06 / P1-07 原地增强既有断言：
所有新增裸 sqlite3.connect 都收敛到 schema_factory 的具名 helper，本文件
只保留 _ro_conn 薄封装；脚本模块一律用 factory.p0_load_script_module 动态
加载并显式替换 5 个符号，不依赖会话级 sys.modules 遍历兜底。
"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import sqlite3
import sys
import types
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

JARVISMIND_ROOT = Path(__file__).resolve().parents[1]
if str(JARVISMIND_ROOT) not in sys.path:
    sys.path.insert(0, str(JARVISMIND_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _fixtures import schema_factory as factory  # noqa: E402

import jarvismind.database.connection as connection_module  # noqa: E402
from jarvismind.database import migrations as migrations_module  # noqa: E402
from jarvismind.database.connection import DatabaseManager  # noqa: E402
from jarvismind.database.migrations import (  # noqa: E402
    MigrationBlocked,
    MigrationRunner,
    PartialMigrationDetected,
)
from jarvismind.main import create_app  # noqa: E402
from jarvismind.services.memory_service import MemoryService  # noqa: E402

_IMPORT_SCRIPT = JARVISMIND_ROOT / "scripts" / "import_memories.py"
_EXPORT_SCRIPT = JARVISMIND_ROOT / "scripts" / "export_memories.py"

# 符号名拼接构造：避免 G-3 静态隔离的原始文本命中，也避免档1 call 规则误判。
_P0_LOAD_SETTINGS = "load_" + "settings"
_P0_CONFIGURE_LOGGING = "configure_" + "logging"
_P0_INIT_DB = "initialize_" + "database"
_P0_REBUILD_INDEX = "rebuild_" + "search_index"

_DDL_FIRST_WORDS = frozenset({
    "CREATE", "ALTER", "DROP", "TRUNCATE", "REINDEX", "VACUUM",
    "ANALYZE", "ATTACH", "DETACH",
})
_DML_FIRST_WORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT",
})


# ---------------------------------------------------------------------------
# A/B/C 判据辅助：sqlite_master 快照、PRAGMA schema_version（只读）、
# 关键表内容校验和、主库 + WAL + SHM SHA-256、目录清单。
# 注意：PRAGMA data_version 不单独作为零写证明；这里也不采集它。
# ---------------------------------------------------------------------------
def _ro_uri(path: Path) -> str:
    """薄封装：只读 URI 构造已下沉到 factory._p0_ro_uri。"""
    return factory._p0_ro_uri(path)


def _ro_conn(path: Path):
    """薄封装：只读连接已下沉到 factory._p0_ro_conn（R-1 symbols 精确指向此名）。"""
    return factory._p0_ro_conn(path)


def _sha256(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _triplet(db_path: Path):
    return (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    )


def _file_state(path: Path):
    if not path.exists():
        return (path.name, False, None, None, None)
    stat = path.stat()
    return (
        path.name, True, stat.st_size, stat.st_mtime_ns, _sha256(path),
    )


_KEY_TABLES = (
    "system_settings", "memory_sources", "memory_records", "memory_candidates",
    "memory_summaries", "memory_corrections", "memory_merges",
    "memory_archive_records", "memory_lifecycle_reviews",
    "schema_migrations", "block_events", "extraction_events",
    "approval_events", "grants", "device_registry", "pairing_codes",
    "memory_rejections", "secret_key_versions",
)


def _key_table_checksums(conn):
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    out = []
    for table in _KEY_TABLES:
        if table not in existing:
            continue
        rows = conn.execute('SELECT * FROM "' + table + '"').fetchall()
        out.append((
            table,
            len(rows),
            hashlib.sha256(repr(rows).encode("utf-8")).hexdigest(),
        ))
    return tuple(out)


def _capture_state(db_path: Path):
    files = tuple(_file_state(p) for p in _triplet(db_path))
    parent = db_path.parent
    listing = (
        tuple(sorted(p.name for p in parent.iterdir()))
        if parent.exists() else ()
    )
    master = ()
    schema_version = None
    checksums = ()
    if db_path.exists():
        with closing(_ro_conn(db_path)) as conn:
            master = tuple(conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall())
            schema_version = conn.execute(
                "PRAGMA schema_version"
            ).fetchone()[0]
            checksums = _key_table_checksums(conn)
    return {
        "files": files,
        "dir_listing": listing,
        "sqlite_master": master,
        "schema_version": schema_version,
        "table_checksums": checksums,
    }


def _snapshot_tree(root: Path):
    """P1-01：目录树快照（相对路径 + 类型 + size/mtime_ns/sha256）。"""
    if not root.exists():
        return ()
    out = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            out.append((rel, "dir", None, None, None))
        else:
            stat = path.stat()
            out.append((
                rel, "file", stat.st_size, stat.st_mtime_ns, _sha256(path),
            ))
    return tuple(out)


def _migration_row_count(db_path: Path) -> int:
    """只读查询 schema_migrations 行数（表不存在时返回 -1）。"""
    with closing(_ro_conn(db_path)) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return -1
        return int(conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0])


class _SqlCounter:
    """P1-06：双挂 before_cursor_execute，按 SQL 首词分类 DDL/DML。

    不能证明（检测边界，六条必须逐字保留）：
    ① 抓不到绕开 SQLAlchemy 的原始 sqlite3.connect 写入
    ② 抓不到全局 connect 监听里的 PRAGMA journal_mode=WAL
    ③ 抓不到只打开连接不执行语句产生的 -wal/-shm / mtime / 文件头变化
    ④ 抓不到其他进程/线程上非该 Engine 的连接
    ⑤ 抓不到 PRAGMA 内部写副作用
    ⑥ 不能替代 sqlite_master 快照 + 文件 SHA-256 + PRAGMA schema_version + P1-07 的 integrity_check

    实例级与类级 Engine 监听会对同一语句各触发一次，用 (id(cursor),
    statement) 去重；未识别首词只记入 statements，绝不默认当成安全读。
    """

    def __init__(self, engine):
        self.engine = engine
        self.ddl = 0
        self.dml = 0
        self.statements = []
        self._seen = set()
        self._callback = None
        self._targets = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        key = (id(cursor), str(statement))
        if key in self._seen:
            return
        self._seen.add(key)
        self.statements.append(str(statement))
        parts = str(statement).lstrip().split(None, 1)
        head = parts[0].upper().rstrip(";") if parts else ""
        if head in _DDL_FIRST_WORDS:
            self.ddl += 1
        elif head in _DML_FIRST_WORDS:
            self.dml += 1

    def __enter__(self):
        self._callback = self._on_execute
        event.listen(self.engine, "before_cursor_execute", self._callback)
        event.listen(Engine, "before_cursor_execute", self._callback)
        self._targets = [self.engine, Engine]
        return self

    def __exit__(self, *exc_info):
        for target in self._targets:
            try:
                event.remove(target, "before_cursor_execute", self._callback)
            except Exception:
                pass
        self._targets = []
        return False


class _NoDisposeManager:
    """P1-03 受控替身：session 委托真实 manager，dispose 为空操作。

    空操作是必要的：快照窗口内保持连接池状态稳定，避免最后一次连接关闭
    触发 WAL checkpoint 改写 jarvismind.db 头部，造成「前后 SHA 全等」假阴性。
    """

    def __init__(self, real):
        self._real = real
        self.settings = real.settings

    def session(self):
        return self._real.session()

    def dispose(self):
        return None


# ---------------------------------------------------------------------------
# 8 个场景，每个用 tmp_path 独立库 + 真实测试数据。
# ---------------------------------------------------------------------------
_SCENARIOS = {
    "empty": factory._test_only_empty_db,
    "v5": factory._test_only_v5_db,
    "v6": factory._test_only_v6_db,
    "partial_v6": factory._test_only_build_partial_v6,
    "full_v6_missing_marker": factory._test_only_full_v6_missing_marker_db,
    "missing_middle": factory._test_only_missing_middle_db,
    "version_too_new": factory._test_only_version_too_new_db,
    "no_migrations_table": factory._test_only_no_migrations_table_db,
}


def _scenario_manager(tmp_path, settings, name):
    data_dir = tmp_path / name
    db_path = data_dir / "jarvismind.db"
    st = replace(
        settings,
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
    )
    manager = DatabaseManager(st)
    return manager, db_path


def _assert_all_entries_blocked(manager, counter=None):
    """四个生产入口全部抛阻断；P1-06：窗口后断言 ddl==0 and dml==0。

    自动覆盖 G-1 的 8 场景与 auto_drop 的 2 场景。调用方可以传入自己
    持有的 counter，以覆盖更大的 before/after 窗口；未传时本函数自建
    并在退出时精确摘除监听。
    """
    own = counter is None
    if own:
        counter = _SqlCounter(manager.engine)
    window = counter if own else nullcontext()
    with window:
        runner = MigrationRunner(manager)
        with pytest.raises(MigrationBlocked):
            runner.initialize()
        with pytest.raises(MigrationBlocked):
            migrations_module.initialize_database(manager)
        with pytest.raises(MigrationBlocked):
            runner.rollback_v6()
        with pytest.raises((MigrationBlocked, PartialMigrationDetected)):
            runner.apply_pending()
        assert counter.ddl == 0 and counter.dml == 0
    return counter


def _boom_counter():
    """返回 (boom 函数, 计数字典)：调用即计数并抛 AssertionError。"""
    state = {"n": 0}

    def boom(*args, **kwargs):
        state["n"] += 1
        raise AssertionError("P0-03A: 禁止的调用被触发")

    return boom, state


def _export_fixtures(tmp_path, settings):
    """P1-03 公共前置：tmp 库 + 种子行 + 预热连接池 + 受控 Settings/mgr。"""
    manager = DatabaseManager(settings)
    factory._test_only_apply_all(manager)
    factory._mutate(manager, factory._seed_rows)
    # 预热：让全局 connect 监听的 PRAGMA（含 journal_mode）在任何快照之前
    # 完成库头切换，之后只做前后全等比较。
    with manager.session() as session:
        session.execute(text("SELECT 1"))
    db_path = Path(settings.database_path)
    controlled = replace(settings, data_dir=tmp_path / "data")

    def guarded_mgr(st):
        assert Path(st.data_dir).resolve().is_relative_to(tmp_path.resolve())
        assert Path(st.database_path).resolve().is_relative_to(tmp_path.resolve())
        return _NoDisposeManager(manager)

    return manager, db_path, controlled, guarded_mgr


# --------------------------------------------------------------- G-1
@pytest.mark.real_migration_gate
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_g1_real_gate_blocks_all_writes(tmp_path, settings, scenario):
    """P1-07：G-1 窗口生成 before/after 两份只读副本。

    证明边界：副本只校验临时场景库；生产库禁止连接/复制，结论永远不能
    外推到生产库。既有 after == before 同时自证副本生成未改动原库。
    """
    # 跳过 autouse 替身后，类/模块上必须是真实方法引用。
    assert MigrationRunner.initialize is factory._REAL_INITIALIZE
    assert MigrationRunner.apply_pending is factory._REAL_APPLY_PENDING
    assert MigrationRunner.rollback_v6 is factory._REAL_ROLLBACK_V6
    assert (
        migrations_module.initialize_database
        is factory._REAL_INITIALIZE_DATABASE
    )

    builder = _SCENARIOS[scenario]
    manager, db_path = _scenario_manager(tmp_path, settings, scenario)
    builder(manager)

    before = _capture_state(db_path)
    copy_before = tmp_path / (scenario + "-copy-before.db")
    factory.p0_make_ro_snapshot_copy(db_path, copy_before)
    _assert_all_entries_blocked(manager)
    after = _capture_state(db_path)
    copy_after = tmp_path / (scenario + "-copy-after.db")
    factory.p0_make_ro_snapshot_copy(db_path, copy_after)
    # sqlite_master 快照、关键表行数/校验和、主库 + WAL SHA-256、目录清单全等
    assert after == before, scenario

    for copy in (copy_before, copy_after):
        with closing(factory.p0_safe_ro_connect(copy)) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _sha256(copy_before) is not None
    assert _sha256(copy_before) == _sha256(copy_after)
    manager.dispose()


# ----------------------------------------------- 自动 DROP 阻断专项
@pytest.mark.real_migration_gate
@pytest.mark.parametrize(
    "scenario", ["partial_v6", "full_v6_missing_marker"]
)
def test_auto_drop_is_blocked(tmp_path, settings, scenario):
    """预置 block_events / approval_events 真实行，迁移入口不得 DROP 任何对象。

    特别覆盖「完整 v6 结构 + 缺 version=6 标记」不得被误判为需要全量 DROP。
    P1-06：before/after 之间用同一 counter 再做一次显式 DDL/DML 断言。
    """
    builder = _SCENARIOS[scenario]
    manager, db_path = _scenario_manager(tmp_path, settings, scenario)
    builder(manager)

    before = _capture_state(db_path)
    counter = _SqlCounter(manager.engine)
    with counter:
        _assert_all_entries_blocked(manager, counter=counter)
        after = _capture_state(db_path)
        assert counter.ddl == 0 and counter.dml == 0
    assert after == before, scenario

    with closing(_ro_conn(db_path)) as conn:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "block_events" in names
        assert "approval_events" in names
        # 原有记录仍在
        assert conn.execute(
            "SELECT COUNT(*) FROM block_events"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM approval_events"
        ).fetchone()[0] == 1
        # 原有列仍在
        block_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(block_events)")
        }
        assert {"hit_length", "category"} <= block_cols
        approval_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(approval_events)")
        }
        assert "candidate_id" in approval_cols
        memory_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_records)")
        }
        assert "source_candidate_id" in memory_cols
    manager.dispose()


# --------------------------------------------------------------- G-2
def test_g2_create_app_opens_no_connection(settings, monkeypatch):
    """任何 session/connect 调用都会抛 AssertionError；create_app 仍成功返回。"""
    def boom(*args, **kwargs):
        raise AssertionError("create_app attempted a database connection")

    mgr = DatabaseManager(settings)
    monkeypatch.setattr(DatabaseManager, "session", boom)
    monkeypatch.setattr(Engine, "connect", boom)
    application = create_app(settings, manager=mgr)
    assert application is not None
    mgr.dispose()


# --------------------------------------------------------------- G-3
def test_g3_static_isolation():
    """静态隔离：检查用的禁止串全部拼接/构造生成，避免自指。"""
    tests_root = Path(__file__).resolve().parent
    jme_root = tests_root.parent
    app_root = jme_root / "jarvismind"
    scripts_root = jme_root / "scripts"

    sep = os.sep
    _banned_prod_native = sep.join(("jme", "data", "jarvismind.db"))
    _banned_prod_posix = "/".join(("jme", "data", "jarvismind.db"))
    _banned_prod_abs = sep.join(("JarvisMind", "jme", "data", "jarvismind.db"))
    _banned_env = "JARVISMIND_" + "DATA_DIR"
    _banned_call = "load_" + "settings("
    _banned_symbol = "test_" + "only"

    # tests/** 不得硬编码生产数据库路径、不得出现环境变量名、不得调 load_settings。
    for py in sorted(tests_root.rglob("*.py")):
        content = py.read_text(encoding="utf-8")
        assert _banned_prod_native not in content, str(py)
        assert _banned_prod_posix not in content, str(py)
        assert _banned_prod_abs not in content, str(py)
        assert _banned_env not in content, str(py)
        assert _banned_call not in content, str(py)

    # app/** 与 scripts/** 不得引用任何 test_only 符号。
    for py in sorted(
        list(app_root.rglob("*.py")) + list(scripts_root.rglob("*.py"))
    ):
        content = py.read_text(encoding="utf-8")
        assert _banned_symbol not in content, str(py)


# --------------------------------------------------------------- G-4
def test_g4_session_guard_enabled_and_stable(pytestconfig):
    """断言 conftest 会话守卫已启用，且当前三件套状态与 sessionstart 一致。"""
    start = getattr(pytestconfig, "_p003_guard_snapshot", None)
    probe = getattr(pytestconfig, "_p003_guard_probe", None)
    assert start is not None, "pytest_sessionstart 守卫未启用"
    assert probe is not None, "会话守卫探针未注册"
    assert probe() == start, "生产库三件套状态与 sessionstart 快照不一致"


# =====================================================================
# P1-01 · init_db.main() 运行时拒绝（直接调用，零子进程）
# =====================================================================
def test_init_db_main_exits_2_with_policy_text(capsys):
    """退出码 2 + 逐字命中五条禁止项与独立审批文案。"""
    import jarvismind.init_db as mod

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    for item in (
        "- 自动创建数据库",
        "- 自动初始化数据库结构",
        "- 自动执行数据库迁移",
        "- 自动回滚",
        "- 自动删除或重建数据库对象",
    ):
        assert item in out
    assert "必须等待后续独立审批" in out


def test_init_db_main_has_zero_file_side_effects(tmp_path, monkeypatch):
    """patch 窗口严格限定在 pytest.raises 内；前后快照在窗口之外采集。"""
    import jarvismind.init_db as mod

    before = _snapshot_tree(tmp_path)
    boom, counter = _boom_counter()
    with monkeypatch.context() as mp:
        mp.setattr(sqlite3, "connect", boom)
        mp.setattr(connection_module, "create_engine", boom)
        mp.setattr(Path, "write_text", boom)
        mp.setattr(Path, "mkdir", boom)
        mp.setattr(builtins, "open", boom)
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
    after = _snapshot_tree(tmp_path)
    assert counter["n"] == 0
    assert before == after


# =====================================================================
# P1-02 · import 脚本 --dry-run 早返回（均不加 real_migration_gate）
# =====================================================================
def _load_import_script(monkeypatch, mod_name, replacements):
    """按路径动态加载 import 脚本，5 个符号全部显式替换。"""
    monkeypatch.setattr(
        sys, "argv", ["import_memories.py", "--dry-run"]
    )
    return factory.p0_load_script_module(
        mod_name, _IMPORT_SCRIPT, monkeypatch, replacements
    )


def test_import_script_dry_run_five_way_zero_side_effects(
    tmp_path, settings, monkeypatch
):
    """五路判据前后全等 + 5 个 boom 零触发（状态面）。"""
    manager = DatabaseManager(settings)
    factory._test_only_apply_all(manager)
    factory._mutate(manager, factory._seed_rows)
    db_path = Path(settings.database_path)

    monkeypatch.chdir(tmp_path)
    boom, counter = _boom_counter()
    mod = _load_import_script(
        monkeypatch,
        "_p0_import_probe_five_way",
        {
            _P0_LOAD_SETTINGS: boom,
            _P0_CONFIGURE_LOGGING: boom,
            "load_document": boom,
            "DatabaseManager": boom,
            "run_import": boom,
        },
    )
    before = factory.p0_capture_five_way(db_path)
    assert mod.main() == 0
    after = factory.p0_capture_five_way(db_path)
    assert after == before
    assert counter["n"] == 0
    manager.dispose()


def test_import_script_dry_run_never_reaches_config_or_db(
    tmp_path, monkeypatch, capsys
):
    """只验调用链短路：早返回在配置/DB/文档读取之前（调用链面）。"""
    monkeypatch.chdir(tmp_path)
    boom, counter = _boom_counter()
    mod = _load_import_script(
        monkeypatch,
        "_p0_import_probe_chain",
        {
            _P0_LOAD_SETTINGS: boom,
            _P0_CONFIGURE_LOGGING: boom,
            "load_document": boom,
            "DatabaseManager": boom,
            "run_import": boom,
        },
    )
    assert mod.main() == 0
    assert counter["n"] == 0
    out = capsys.readouterr().out
    assert "不执行任何数据库操作、不读取配置、不创建任何文件" in out


# =====================================================================
# P1-03 · export 脚本：不初始化、不重建索引、只落在 tmp 库
# =====================================================================
def _load_export_script(monkeypatch, mod_name, controlled, guarded_mgr):
    """动态加载 export 脚本，显式替换受控配置 / 日志 / 连接工厂 3 个符号。"""
    replacements = {
        _P0_LOAD_SETTINGS: lambda: controlled,
        _P0_CONFIGURE_LOGGING: lambda *args, **kwargs: None,
        "DatabaseManager": guarded_mgr,
    }
    mod = factory.p0_load_script_module(
        mod_name, _EXPORT_SCRIPT, monkeypatch, replacements
    )
    # 另外 2 个符号：脚本顶层根本没有这两个名字，用 raising=False 装成 boom。
    assert not hasattr(mod, _P0_INIT_DB)
    assert not hasattr(mod, _P0_REBUILD_INDEX)
    boom, counter = _boom_counter()
    monkeypatch.setattr(mod, _P0_INIT_DB, boom, raising=False)
    monkeypatch.setattr(mod, _P0_REBUILD_INDEX, boom, raising=False)
    return mod, counter


def test_export_script_never_initializes_or_rebuilds(
    tmp_path, settings, monkeypatch
):
    """sqlite_master / schema_version / 迁移表行数 / 主库 SHA 前后全等，DDL/DML=0。"""
    manager, db_path, controlled, guarded_mgr = _export_fixtures(
        tmp_path, settings
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_memories.py",
            "--output",
            str(tmp_path / "exports" / "memories.json"),
        ],
    )
    mod, counter = _load_export_script(
        monkeypatch,
        "_p0_export_probe_no_init",
        controlled,
        guarded_mgr,
    )

    before = _capture_state(db_path)
    before_rows = _migration_row_count(db_path)
    ddl_dml = {"ddl": 0, "dml": 0}

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        parts = str(statement).lstrip().split(None, 1)
        head = parts[0].upper().rstrip(";") if parts else ""
        if head in _DDL_FIRST_WORDS:
            ddl_dml["ddl"] += 1
        elif head in _DML_FIRST_WORDS:
            ddl_dml["dml"] += 1

    event.listen(manager.engine, "before_cursor_execute", on_execute)
    try:
        assert mod.main() == 0
    finally:
        try:
            event.remove(manager.engine, "before_cursor_execute", on_execute)
        except Exception:
            pass
    after = _capture_state(db_path)

    assert ddl_dml == {"ddl": 0, "dml": 0}
    assert after["sqlite_master"] == before["sqlite_master"]
    assert after["schema_version"] == before["schema_version"]
    assert after["table_checksums"] == before["table_checksums"]
    assert after["files"][0][4] == before["files"][0][4]  # jarvismind.db SHA-256
    assert _migration_row_count(db_path) == before_rows
    assert counter["n"] == 0
    manager.dispose()


def test_export_script_runs_only_on_tmp_db(tmp_path, settings, monkeypatch):
    """显式 --output + chdir(tmp_path)，产物在 tmp 内且 CWD 之外无新增文件。"""
    manager, db_path, controlled, guarded_mgr = _export_fixtures(
        tmp_path, settings
    )
    output = tmp_path / "exports" / "memories.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["export_memories.py", "--output", str(output)]
    )
    mod, counter = _load_export_script(
        monkeypatch,
        "_p0_export_probe_tmp_only",
        controlled,
        guarded_mgr,
    )
    expected = MemoryService(manager).export_memories(status="ACTIVE")
    outside_before = tuple(sorted(p.name for p in JARVISMIND_ROOT.iterdir()))

    assert mod.main() == 0
    assert output.exists()
    assert output.resolve().is_relative_to(tmp_path.resolve())
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["format"] == "jme-export"
    assert document["counts"]["memories"] == len(expected["memories"])
    assert document["counts"]["sources"] == len(expected["sources"])
    assert counter["n"] == 0
    assert tuple(sorted(p.name for p in JARVISMIND_ROOT.iterdir())) == outside_before
    manager.dispose()


# =====================================================================
# P1-04 · health 当前行为特征刻画（不是零连接期望）
#
# 若未来批次 C/T-D 把 health 拆成 /livez 零 DB，本组断言必须同步修改，
# 不得静默跳过。
# =====================================================================
def test_health_connect_and_session_call_counts(settings, monkeypatch):
    """若未来批次 C/T-D 把 health 拆成 /livez 零 DB，本组断言必须同步修改，不得静默跳过。

    冷启动一次 /health：全局 connect 事件（物理连接）== 1，QueuePool 复用
    使后续 session 不再新建物理连接；manager.session >= 1。对照组把
    session / Engine.connect 换 boom，证明 G-2 只覆盖构造期。
    """
    manager = DatabaseManager(settings)
    factory._test_only_apply_all(manager)
    counts = {"engine_connect": 0, "connect_calls": 0, "session": 0}
    real_connect = Engine.connect
    real_session = DatabaseManager.session

    def spy_connect(self, *args, **kwargs):
        counts["connect_calls"] += 1
        return real_connect(self, *args, **kwargs)

    def spy_session(self, *args, **kwargs):
        counts["session"] += 1
        return real_session(self, *args, **kwargs)

    def on_connect(dbapi_connection, connection_record):
        counts["engine_connect"] += 1

    monkeypatch.setattr(Engine, "connect", spy_connect)
    monkeypatch.setattr(DatabaseManager, "session", spy_session)
    event.listen(Engine, "connect", on_connect)
    try:
        with TestClient(
            create_app(settings=settings, manager=manager)
        ) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert counts["engine_connect"] == 1
        assert counts["connect_calls"] >= 1
        assert counts["session"] >= 1

        def boom(*args, **kwargs):
            raise AssertionError("health 请求期仍尝试打开数据库连接")

        monkeypatch.setattr(Engine, "connect", boom)
        monkeypatch.setattr(DatabaseManager, "session", boom)
        with TestClient(
            create_app(settings=settings, manager=manager)
        ) as client:
            with pytest.raises(AssertionError):
                client.get("/health")
    finally:
        try:
            event.remove(Engine, "connect", on_connect)
        except Exception:
            pass
        manager.dispose()


def test_health_does_not_create_schema_migrations(settings, tmp_path):
    """若未来批次 C/T-D 把 health 拆成 /livez 零 DB，本组断言必须同步修改，不得静默跳过。

    自建空库 manager（绝不使用 client fixture，否则 conftest 的 manager
    fixture 会建全表造成假阴性）。「无建表」与「零文件副作用」分开写：
    connection.py 的全局 connect PRAGMA 会改库头并可能产生 -wal/-shm，
    因此这里不断言三件套 SHA-256 全等。
    """
    data_dir = tmp_path / "empty_health"
    db_path = data_dir / "jarvismind.db"
    st = replace(
        settings,
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
    )
    manager = DatabaseManager(st)
    factory._test_only_empty_db(manager)
    before = _capture_state(db_path)
    try:
        with TestClient(create_app(settings=st, manager=manager)) as client:
            resp = client.get("/health")
            # 快照在 TestClient 上下文内采集：连接池仍存活，只读 URI 可打开。
            after = _capture_state(db_path)
            with closing(factory.p0_safe_ro_connect(db_path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                pragma_version = conn.execute(
                    "PRAGMA schema_version"
                ).fetchone()[0]
            assert "schema_migrations" not in tables
            assert pragma_version == 0
            assert resp.json()["schema_version"] == 0
        assert after["sqlite_master"] == before["sqlite_master"]
    finally:
        manager.dispose()


def test_health_fires_global_connect_pragmas(settings, monkeypatch):
    """若未来批次 C/T-D 把 health 拆成 /livez 零 DB，本组断言必须同步修改，不得静默跳过。

    三条 PRAGMA 走原始 DBAPI cursor，before_cursor_execute 抓不到；这里用
    sqlalchemy.event.contains 确认全局监听仍在，再用 insert=True 的 spy
    设置 set_trace_callback 捕获原始语句。
    """
    assert event.contains(
        Engine, "connect", connection_module._set_sqlite_pragmas
    )
    records = []

    def spy(dbapi_connection, connection_record):
        dbapi_connection.set_trace_callback(records.append)

    event.listen(Engine, "connect", spy, insert=True)
    manager = DatabaseManager(settings)
    factory._test_only_apply_all(manager)
    try:
        with TestClient(
            create_app(settings=settings, manager=manager)
        ) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        for statement in (
            "PRAGMA foreign_keys=ON",
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
        ):
            assert statement in records
        assert any("journal_mode=WAL" in item for item in records)
    finally:
        try:
            event.remove(Engine, "connect", spy)
        except Exception:
            pass
        manager.dispose()


def test_health_degraded_still_opens_session(settings, monkeypatch):
    """若未来批次 C/T-D 把 health 拆成 /livez 零 DB，本组断言必须同步修改，不得静默跳过。

    verify_connection 返回 False 时状态为 degraded，但 current_version()
    仍会打开 manager.session()，即 degraded 不等于零 DB 接触。
    """
    manager = DatabaseManager(settings)
    factory._test_only_apply_all(manager)
    monkeypatch.setattr(manager, "verify_connection", lambda: False)
    counts = {"session": 0}
    real_session = DatabaseManager.session

    def spy_session(self, *args, **kwargs):
        counts["session"] += 1
        return real_session(self, *args, **kwargs)

    monkeypatch.setattr(DatabaseManager, "session", spy_session)
    try:
        with TestClient(
            create_app(settings=settings, manager=manager)
        ) as client:
            resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["database_connected"] is False
        assert counts["session"] >= 1
    finally:
        manager.dispose()


# =====================================================================
# P1-05 · autouse 替身 / 延迟 import 绑定边界
# =====================================================================
def test_g5_lazy_import_resolves_to_stand_in():
    """用例体内延迟 import 拿到的是模块属性原地 patch 后的替身。"""
    from jarvismind.database.migrations import initialize_database as f

    assert f is factory._fake_initialize_database


def test_g5_new_module_imported_after_setup_is_covered(monkeypatch):
    """setup 之后动态注册的新模块，函数内 from-import 仍绑定到替身。

    如实标注：手工把 _REAL_INITIALIZE_DATABASE 固化到新模块（写成别名）
    不在覆盖范围，归入下一例 test_g5_alias_binding_boundary_is_documented_and_banned。
    """
    probe = types.ModuleType("_p0_dynamic_lazy_probe")
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    exec(
        "from jarvismind.database.migrations import initialize_database",
        probe.__dict__,
    )
    assert probe.initialize_database is factory._fake_initialize_database


def test_g5_alias_binding_boundary_is_documented_and_banned(settings, tmp_path):
    """检测边界、非已修复：别名绑定漏网于 sys.modules 遍历，但漏网不等于写库。

    _test_only_patch_runner 只遍历名为 initialize_database 的模块属性；
    以别的属性名固化真实函数引用时会漏网。真实入口无条件抛
    MigrationBlocked，因此漏网仍不会写库。
    """
    alias = factory._REAL_INITIALIZE_DATABASE
    assert alias is not factory._fake_initialize_database

    probe = types.ModuleType("_p0_alias_boundary_probe")
    probe.alias = alias
    sys.modules[probe.__name__] = probe
    mp = pytest.MonkeyPatch()
    try:
        factory._test_only_patch_runner(mp)
        assert probe.alias is alias
        assert probe.alias is not factory._fake_initialize_database
    finally:
        mp.undo()
        sys.modules.pop(probe.__name__, None)

    manager, _ = _scenario_manager(tmp_path, settings, "alias_boundary")
    factory._test_only_empty_db(manager)
    try:
        with pytest.raises(MigrationBlocked):
            alias(manager)
    finally:
        manager.dispose()


def test_g5_autouse_fixture_precedes_lazy_fixture():
    """autouse _migration_runner_sandbox 先于用例体内延迟 import 完成 patch。"""
    assert MigrationRunner.initialize is factory._fake_initialize
    assert MigrationRunner.apply_pending is factory._fake_apply_pending
    assert MigrationRunner.rollback_v6 is factory._fake_rollback_v6
    assert (
        migrations_module.initialize_database
        is factory._fake_initialize_database
    )
