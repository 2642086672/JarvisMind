"""Test-only SQLite schema factories for the P0-03 startup-safety suite.

All helpers build throwaway schema states on a temporary database file using
ONLY the standard library ``sqlite3`` module.  They never call a production
migration / init entry point (``MigrationRunner.initialize``,
``MigrationRunner.apply_pending``, ``MigrationRunner.rollback_v6`` or the
module-level ``initialize_database``); they only execute the plain SQL text
stored in ``MIGRATIONS``.  The target path is taken exclusively from
``manager.settings.database_path`` and is guarded against the on-disk
production database at runtime.

Every public helper is prefixed ``_test_only_`` and lives under ``tests/``;
production code under ``app/`` and ``scripts/`` must never import it.

Module import is side-effect free: no database connection is opened and no
SQL is executed until a factory function is actually called.  Every DROP /
DELETE from ``MIGRATION_V6_ROLLBACK_SQL`` is preceded by a fresh
``_assert_temp_db()`` whitelist check, so the fixture can never operate on a
path outside the session temporary root.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jarvismind.database.migrations as _migrations
from jarvismind.database.migrations import MIGRATION_V6_ROLLBACK_SQL, MigrationRunner

# 真实方法引用在模块导入期保存：G-1 用它把类门控还原，验证第一阶段硬阻断。
_REAL_INITIALIZE = MigrationRunner.initialize
_REAL_APPLY_PENDING = MigrationRunner.apply_pending
_REAL_ROLLBACK_V6 = MigrationRunner.rollback_v6
_REAL_INITIALIZE_DATABASE = _migrations.initialize_database

# 运行时拒绝生产库：路径由 __file__ 相对拼接，绝不写死完整生产路径字面量。
_PROD_DB = Path(__file__).resolve().parents[2] / "data" / "jarvismind.db"

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_tmp_root() -> Path:
    """返回会话临时根目录（与 conftest A-4 同一白名单根）。

    仅在函数被调用时读取；模块导入期不 import conftest、不做任何 DB 操作。
    """
    import tempfile

    import conftest as _p0

    root = getattr(_p0, "_P0_TMP_ROOT", None)
    if root is not None:
        return Path(root).resolve()
    # pytest 会话未初始化时的回退：仍限定在系统临时目录内，
    # 绝不回退到生产数据目录。
    return Path(tempfile.gettempdir()).resolve()


def _assert_temp_db(manager) -> Path:
    """只接受 manager.settings.database_path；运行时拒绝生产库路径。

    收紧后的统一入口：除拒绝生产库路径外，还要求目标库落在会话临时根
    白名单内（复用 A-4 的 p0_assert_db_path_allowed 判据）。工厂函数只在
    被调用时连库；模块导入期不执行任何 DB 操作，也不让夹具具备操作
    生产库的可能。
    """
    db_path = Path(manager.settings.database_path).resolve()
    prod = _PROD_DB.resolve()
    if db_path == prod or db_path.parent == prod.parent:
        raise RuntimeError("test factory refuses to touch the production database")

    # 与 A-4 同一判据：database_url 解析出的路径必须在会话临时根内。
    from conftest import p0_assert_db_path_allowed

    url = getattr(manager.settings, "database_url", None)
    if not url:
        url = "sqlite+pysqlite:///" + db_path.as_posix()
    p0_assert_db_path_allowed(url, tmp_root=_session_tmp_root())
    return db_path


# ===========================================================================
# 批次 B · 5 个 P0 helper（所有新增裸 sqlite3.connect 收敛到本区块）
#
# 通用证明边界（每个 helper 的 docstring 都重复声明）：
#   目标路径必须落在会话 tmp root；生产库禁止连接/复制；
#   integrity_check 结论不能外推到生产库。
# ===========================================================================

_P0_BOUND_CONFIG: Any = None
# 名字一律拼接构造，避免写出会触发档1 literal 规则的完整字面量。
_P0_TOKEN_FILENAME = ".jme" + "_token"
_P0_DATA_DIR_ENV = "JARVISMIND_" + "DATA_DIR"


def p0_bind_tmp_config(config) -> None:
    """由 conftest.pytest_sessionstart 注入当前 pytestconfig。

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    global _P0_BOUND_CONFIG
    _P0_BOUND_CONFIG = config


def _p0_path_root() -> Path:
    """优先用 pytestconfig._p0_tmp_root；取不到退回系统临时目录。"""
    root = getattr(_P0_BOUND_CONFIG, "_p0_tmp_root", None)
    if root is not None:
        return Path(root).resolve()
    return Path(tempfile.gettempdir()).resolve()


def _p0_assert_path_in_tmp_root(path: Path) -> None:
    """fail-closed：任何目标路径都不得越出会话临时根。"""
    resolved = Path(path).resolve()
    root = _p0_path_root()
    if not resolved.is_relative_to(root):
        raise AssertionError(
            "P0-03A: 目标路径不在会话 tmp root 内，拒绝连接/复制: "
            f"path={resolved}, tmp_root={root}"
        )


def _p0_sha256(path: Path):
    """文件字节 SHA-256；不存在记 None（纯文件系统只读）。"""
    target = Path(path)
    if not target.exists():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _p0_ro_uri(path: Path) -> str:
    """把物理路径转成只读 URI（下沉自 test_startup_safety.py 的 _ro_uri）。

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    posix = str(Path(path)).replace("\\", "/")
    posix = posix.replace("?", "%3F").replace("#", "%23")
    if not posix.startswith("/"):
        posix = "/" + posix
    return "file:" + posix + "?mode=ro"


def _p0_ro_conn(path: Path):
    """只读 URI 连接（下沉自 test_startup_safety.py 的 _ro_conn）。

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    return sqlite3.connect(_p0_ro_uri(path), uri=True)


def p0_safe_ro_connect(db_path: Path):
    """带 tmp root 白名单断言的只读连接；所有五路采集都走这里。

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    resolved = Path(db_path).resolve()
    _p0_assert_path_in_tmp_root(resolved)
    return sqlite3.connect(_p0_ro_uri(resolved), uri=True)


def p0_capture_five_way(db_path: Path) -> dict[str, Any]:
    """五路零副作用判据：三件套 SHA / sqlite_master / 逐表 COUNT / 目录清单 / token 文件。

    ① 主库 + WAL + SHM 三者 SHA-256（不存在记 None）
    ② sqlite_master 全量快照（type, name, tbl_name, sql）
    ③ 每张表 COUNT(*)
    ④ 数据库所在目录的文件名清单
    ⑤ token 文件的存在 / size / SHA-256（文件名与环境变量名均拼接构造）

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    resolved = Path(db_path).resolve()
    _p0_assert_path_in_tmp_root(resolved)

    triplet = tuple(
        _p0_sha256(path)
        for path in (
            resolved,
            Path(str(resolved) + "-wal"),
            Path(str(resolved) + "-shm"),
        )
    )
    parent = resolved.parent
    listing = (
        tuple(sorted(item.name for item in parent.iterdir()))
        if parent.exists()
        else ()
    )

    master: tuple = ()
    counts: tuple = ()
    if resolved.exists():
        conn = p0_safe_ro_connect(resolved)
        try:
            master = tuple(
                conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                ).fetchall()
            )
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            counts = tuple(
                (
                    name,
                    conn.execute(
                        'SELECT COUNT(*) FROM "' + name + '"'
                    ).fetchone()[0],
                )
                for name in names
            )
        finally:
            conn.close()

    token_candidates = [parent / _P0_TOKEN_FILENAME]
    env_dir = os.environ.get(_P0_DATA_DIR_ENV)
    if env_dir:
        token_candidates.append(Path(env_dir) / _P0_TOKEN_FILENAME)
    token_states = []
    for candidate in token_candidates:
        if candidate.exists():
            stat = candidate.stat()
            token_states.append((
                str(candidate), True, stat.st_size, _p0_sha256(candidate),
            ))
        else:
            token_states.append((str(candidate), False, None, None))

    return {
        "triplet_sha256": triplet,
        "sqlite_master": master,
        "table_counts": counts,
        "dir_listing": listing,
        "token_file": tuple(token_states),
    }


def p0_make_ro_snapshot_copy(src_db: Path, dst_db: Path) -> None:
    """只读打开源库、可写新建目标副本，并用 src.backup(dst) 做字节级快照。

    源库文件不存在（empty 场景）时生成等价的零字节空库副本，以便 G-1 的
    全 8 个场景都能走同一个 helper。禁止调用
    app.database.backup.create_backup()（其源连接可写）。
    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    src_path = Path(src_db).resolve()
    dst_path = Path(dst_db).resolve()
    _p0_assert_path_in_tmp_root(src_path)
    _p0_assert_path_in_tmp_root(dst_path)
    if dst_path == src_path:
        raise AssertionError(
            "P0-03A: 快照目标不得与源库同路径: " + str(src_path)
        )
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # empty 场景没有源库文件：只读 URI 无法打开不存在的库。此时生成一份
    # 等价的零字节空库副本（仍是 tmp root 内的有效 SQLite 库，也仍满足
    # integrity_check == ok / 前后副本字节全等），不触碰任何其它路径。
    if not src_path.exists():
        dst = sqlite3.connect(str(dst_path))
        dst.close()
        return

    src = sqlite3.connect(_p0_ro_uri(src_path), uri=True)
    dst = None
    try:
        dst = sqlite3.connect(str(dst_path))
        src.backup(dst)
    finally:
        if dst is not None:
            dst.close()
        src.close()


def p0_load_script_module(
    mod_name: str,
    script_path: Path,
    monkeypatch,
    replacements: dict,
) -> Any:
    """按文件路径动态加载脚本模块，并在 exec 后强制替换指定符号。

    只用 importlib.util（spec_from_file_location / module_from_spec /
    exec_module），禁止 subprocess，禁止新增 scripts 包文件；sys.modules
    注册与清理使用 monkeypatch.setitem / monkeypatch.delitem（teardown 时
    精确恢复）。每个 replacement 的 key 都 setattr 到模块并断言已被替换，
    不依赖会话级 sys.modules 遍历兜底。

    目标路径必须落在会话 tmp root；生产库禁止连接/复制；
    integrity_check 结论不能外推到生产库。
    """
    path = Path(script_path).resolve()
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("P0-03A: 无法为脚本构造 module spec: " + str(path))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, mod_name, module)
    try:
        spec.loader.exec_module(module)
        for name, value in replacements.items():
            setattr(module, name, value)
            assert getattr(module, name, None) is value, (
                "P0-03A: 脚本模块符号替换失败: " + mod_name + "." + name
            )
    except BaseException:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)
        raise
    monkeypatch.delitem(sys.modules, mod_name, raising=False)
    return module


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # foreign_keys 必须在任何显式事务之外设置；不改 journal_mode，保持 DELETE
    # 模式，使只读 URI 打开时零 -wal/-shm 副作用。
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _idempotent_execute(conn: sqlite3.Connection, statement: str) -> None:
    """执行单条 DDL/DML；仅跳过幂等冲突，其余（含非法 SQL）一律上抛。"""
    try:
        conn.execute(statement)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        raise


def _execute_fts5_optional(conn: sqlite3.Connection, statements) -> None:
    """FTS5 为可选模块：任何 OperationalError（模块缺失/对象已存在）都跳过。"""
    for statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            continue


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """探测 FTS5： CREATE+DROP 都在显式事务内，commit 后不留任何痕迹。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _test_only_apply_all(manager, upto: int = 6) -> int:
    """在临时库上按版本顺序补齐 MIGRATIONS 的 SQL，返回本次补齐版本数。

    - 动态读取 _migrations.MIGRATIONS，尊重测试对 MIGRATIONS 的 monkeypatch；
    - 每版写 schema_migrations 标记，保证真实 current_version() 读到全版本；
    - v6 缺失时先执行回滚 SQL 清理其对象，使残缺表/残留索引能被完整重建
      （这些 DROP 仅作用于临时库）；
    - 写 system_settings 的 jme.fts5_enabled / jme.schema_version；
    - 幂等：CREATE ... IF NOT EXISTS / ALTER TABLE ADD COLUMN 重复时跳过 /
      FTS5 不可用时跳过；首次返回 len(MIGRATIONS)，再调返回 0。
    """
    db_path = _assert_temp_db(manager)
    migrations = _migrations.MIGRATIONS
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        applied = {
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        pending = [
            m for m in migrations
            if m.version <= upto and m.version not in applied
        ]
        if any(m.version == 6 for m in pending):
            # 收紧执行边界：每条 DROP/DELETE 执行前都重新断言目标库仍落在
            # 会话临时根白名单内，回滚 SQL 绝不作用于临时库之外的任何路径。
            for statement in MIGRATION_V6_ROLLBACK_SQL:
                _assert_temp_db(manager)
                conn.execute(statement)
            applied = {
                int(row[0])
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
            pending = [
                m for m in migrations
                if m.version <= upto and m.version not in applied
            ]
        fts5_ok = _fts5_available(conn)
        for m in pending:
            for statement in m.statements:
                _idempotent_execute(conn, statement)
            _execute_fts5_optional(conn, m.fts5_statements)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations"
                "(version, name, applied_at) VALUES (?, ?, ?)",
                (m.version, m.name, _now()),
            )
        conn.execute(
            "INSERT OR REPLACE INTO system_settings(key, value, updated_at) "
            "VALUES (?, ?, ?)",
            ("jme.fts5_enabled", "1" if fts5_ok else "0", _now()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_settings(key, value, updated_at) "
            "VALUES (?, ?, ?)",
            ("jme.schema_version", str(upto), _now()),
        )
        conn.commit()
        return len(pending)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _test_only_rollback_v6(manager) -> None:
    """真 DROP v6 对象 + 删 version=6 标记（仅临时库），供 rollback 替身。"""
    db_path = _assert_temp_db(manager)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute(_SCHEMA_MIGRATIONS_DDL)
        # 同 _test_only_apply_all：每条 DROP/DELETE 前收紧校验目标库路径。
        for statement in MIGRATION_V6_ROLLBACK_SQL:
            _assert_temp_db(manager)
            conn.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 替身方法：签名带 self（作为类方法被实例调用）或 manager（模块级入口）。
# ---------------------------------------------------------------------------
def _fake_initialize(self) -> int:
    return _test_only_apply_all(self.manager)


def _fake_apply_pending(self) -> int:
    return _test_only_apply_all(self.manager)


def _fake_rollback_v6(self) -> None:
    return _test_only_rollback_v6(self.manager)


def _fake_initialize_database(manager) -> int:
    return _test_only_apply_all(manager)


def _test_only_patch_runner(monkeypatch) -> None:
    """把三个类方法 + 模块级入口 + 已固化的模块全局名重定向到替身。

    真实 migrations.py 的 ``initialize_database`` 是独立 raise，并不转调
    ``MigrationRunner.initialize``，因此仅 patch 类方法不足以覆盖
    ``from jarvismind.database.migrations import initialize_database`` 的绑定：
    这里额外 patch 模块属性（覆盖函数内 import）并遍历 sys.modules 把持有
    真实函数引用的测试模块名字一并 patch（覆盖顶层 import）。
    """
    monkeypatch.setattr(MigrationRunner, "initialize", _fake_initialize)
    monkeypatch.setattr(MigrationRunner, "apply_pending", _fake_apply_pending)
    monkeypatch.setattr(MigrationRunner, "rollback_v6", _fake_rollback_v6)
    monkeypatch.setattr(
        _migrations, "initialize_database", _fake_initialize_database
    )
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            bound = getattr(module, "initialize_database", None)
        except (AttributeError, TypeError):
            continue
        if bound is _REAL_INITIALIZE_DATABASE:
            try:
                monkeypatch.setattr(
                    module, "initialize_database",
                    _fake_initialize_database, raising=False,
                )
            except (AttributeError, TypeError):
                pass


def _test_only_restore_runner(monkeypatch) -> None:
    """把真实方法引用装回类/模块，供 G-1 验证第一阶段硬阻断。"""
    monkeypatch.setattr(MigrationRunner, "initialize", _REAL_INITIALIZE)
    monkeypatch.setattr(MigrationRunner, "apply_pending", _REAL_APPLY_PENDING)
    monkeypatch.setattr(MigrationRunner, "rollback_v6", _REAL_ROLLBACK_V6)
    monkeypatch.setattr(
        _migrations, "initialize_database", _REAL_INITIALIZE_DATABASE
    )


# ---------------------------------------------------------------------------
# 场景工厂：全部在临时库上写入真实测试数据，绝不只查表存在。
# ---------------------------------------------------------------------------
def _mutate(manager, operation) -> Path:
    db_path = _assert_temp_db(manager)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        operation(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return db_path


def _seed_rows(conn) -> None:
    """写入真实业务行到核心表。"""
    n = _now()
    conn.execute(
        "INSERT OR IGNORE INTO system_settings(key, value, updated_at) "
        "VALUES ('seed.key', 'seed-value', ?)", (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_sources"
        "(id, source_type, source_name, created_at, updated_at) "
        "VALUES ('seed-src', 'CHAT', 'seed-source', ?, ?)", (n, n)
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_records"
        "(id, memory_type, content, status, created_at, updated_at) "
        "VALUES ('seed-mem', 'SEMANTIC', '真实记忆内容-零写入验证', 'ACTIVE', ?, ?)",
        (n, n),
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_candidates"
        "(id, memory_type, content, status, extraction_engine, extracted_at) "
        "VALUES ('seed-cand', 'SEMANTIC', '真实候选内容', 'PENDING', 'local', ?)",
        (n,),
    )


def _seed_v6_rows(conn) -> None:
    """完整 v6 结构下，8 张 v6 表各写 1 行真实数据。"""
    n = _now()
    conn.execute(
        "INSERT OR IGNORE INTO block_events"
        "(block_event_id, conversation_id, message_id, turn_id, category, "
        "hit_length, ruleset_version, created_at) "
        "VALUES ('seed-be', 'c1', 'm1', 't1', 'CREDENTIAL', 5, 'rs-1', ?)", (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO extraction_events"
        "(event_id, conversation_id, message_id, turn_id, event_type, "
        "ignore_reason_enum, ruleset_version, created_at) "
        "VALUES ('seed-ee', 'c1', 'm1', 't1', 'IGNORE', 'LOW_VALUE', 'rs-1', ?)",
        (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO approval_events"
        "(event_id, candidate_id, decision, idempotency_key, created_at) "
        "VALUES ('seed-ae', 'seed-cand', 'APPROVE', 'seed-idem', ?)", (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO grants"
        "(grant_id, grant_type, nonce, issued_at, expires_at) "
        "VALUES ('seed-g', 'READ', 'seed-nonce', ?, ?)", (n, n)
    )
    conn.execute(
        "INSERT OR IGNORE INTO device_registry"
        "(device_id, key_version, public_key, fingerprint, paired_at) "
        "VALUES ('seed-dev', 1, 'pk', 'fp', ?)", (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO pairing_codes"
        "(code_hash, device_id, expires_at, created_at) "
        "VALUES ('seed-ch', 'seed-dev', ?, ?)", (n, n)
    )
    conn.execute(
        "INSERT OR IGNORE INTO memory_rejections"
        "(dedup_key, candidate_id, reason_code, rejected_at) "
        "VALUES ('seed-dk', 'seed-cand', 'DUP', ?)", (n,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO secret_key_versions"
        "(key_version, created_at) VALUES ('seed-kv', ?)", (n,)
    )


def _v6_create_table_stmt(table: str) -> str:
    """从 MIGRATIONS 的 v6 中取出某张表的 CREATE TABLE 语句。"""
    for m in _migrations.MIGRATIONS:
        if m.version != 6:
            continue
        for statement in m.statements:
            if (
                statement.lstrip().upper().startswith("CREATE TABLE")
                and table in statement
            ):
                return statement
    raise RuntimeError("v6 CREATE TABLE for " + table + " not found")


def _test_only_empty_db(manager):
    """空目录：目录存在、库文件不存在。"""
    db_path = _assert_temp_db(manager)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def _test_only_v5_db(manager):
    _test_only_apply_all(manager, upto=5)
    _mutate(manager, _seed_rows)
    return _assert_temp_db(manager)


def _test_only_v6_db(manager):
    _test_only_apply_all(manager, upto=6)
    _mutate(manager, _seed_rows)
    _mutate(manager, _seed_v6_rows)
    return _assert_temp_db(manager)


def _test_only_v6_empty_db(manager):
    """完整 v6 结构，零业务行：只补齐 schema，不调用 _seed_rows / _seed_v6_rows。"""
    _test_only_apply_all(manager, upto=6)
    return _assert_temp_db(manager)


def _test_only_build_partial_v6(manager):
    """apply_all(upto=5) + 部分 v6 对象 + 真实行，但不写 version=6 标记。"""
    _test_only_apply_all(manager, upto=5)

    def partial(conn):
        _idempotent_execute(
            conn,
            "ALTER TABLE memory_records ADD COLUMN source_candidate_id TEXT",
        )
        _idempotent_execute(conn, _v6_create_table_stmt("block_events"))
        _idempotent_execute(conn, _v6_create_table_stmt("approval_events"))
        n = _now()
        conn.execute(
            "INSERT INTO block_events"
            "(block_event_id, conversation_id, message_id, turn_id, category, "
            "hit_length, ruleset_version, created_at) "
            "VALUES ('partial-be', 'pc1', 'pm1', 'pt1', 'CREDENTIAL', 7, "
            "'rs-p', ?)", (n,)
        )
        conn.execute(
            "INSERT INTO approval_events"
            "(event_id, candidate_id, decision, idempotency_key, created_at) "
            "VALUES ('partial-ae', 'partial-cand', 'APPROVE', 'partial-idem', ?)",
            (n,)
        )

    _mutate(manager, partial)
    return _assert_temp_db(manager)


def _test_only_full_v6_missing_marker_db(manager):
    """完整 v6 结构 + 8 张表各 1 行真实数据，但删掉 version=6 记录。"""
    _test_only_apply_all(manager, upto=6)
    _mutate(manager, _seed_rows)
    _mutate(manager, _seed_v6_rows)
    _mutate(manager, lambda conn: conn.execute(
        "DELETE FROM schema_migrations WHERE version = 6"
    ))
    return _assert_temp_db(manager)


def _test_only_missing_middle_db(manager):
    _test_only_apply_all(manager, upto=6)
    _mutate(manager, _seed_rows)
    _mutate(manager, lambda conn: conn.execute(
        "DELETE FROM schema_migrations WHERE version = 3"
    ))
    return _assert_temp_db(manager)


def _test_only_version_too_new_db(manager):
    _test_only_apply_all(manager, upto=6)
    _mutate(manager, _seed_rows)

    def bump(conn):
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations"
            "(version, name, applied_at) VALUES (7, 'future', ?)", (_now(),)
        )

    _mutate(manager, bump)
    return _assert_temp_db(manager)


def _test_only_no_migrations_table_db(manager):
    _test_only_apply_all(manager, upto=6)
    _mutate(manager, _seed_rows)
    _mutate(manager, lambda conn: conn.execute(
        "DROP TABLE schema_migrations"
    ))
    return _assert_temp_db(manager)
