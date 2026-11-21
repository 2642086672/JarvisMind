"""P0-03A-R3 · ``dry_run`` is a genuine read-only preview.

Covers: memory_sources row count, full table content, SQL DDL/DML event
count, main/WAL/SHM file state, dry-run vs real import difference, and
zero side effects when dry-run raises.  Every database is a session
tmp_path database.
"""
from __future__ import annotations

import hashlib
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
from jarvismind.services import initial_import as subject  # noqa: E402
from jarvismind.services.source_service import SourceService  # noqa: E402

_DDL = frozenset({
    "CREATE", "ALTER", "DROP", "TRUNCATE", "REINDEX", "VACUUM",
    "ANALYZE", "ATTACH", "DETACH",
})
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT"})


class _SqlCounter:
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


def _sha(path: Path):
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _triplet(db_path: Path):
    return tuple(
        _sha(p) for p in (
            db_path,
            Path(str(db_path) + "-wal"),
            Path(str(db_path) + "-shm"),
        )
    )


def _tree(root: Path):
    if not root.exists():
        return ()
    out = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            out.append((rel, "dir", None))
        else:
            out.append((rel, "file", _sha(path)))
    return tuple(out)


def _rows(manager, statement):
    with manager.session() as session:
        return tuple(
            tuple(str(value) for value in row)
            for row in session.execute(text(statement))
        )


# 内容主题互不相交，相似度实测 < 0.06（见测试证据报告），避免把
# 「去重逻辑生效」误判成「导入失败」。
_CONTENTS = (
    "舟山港集装箱吞吐量的年度统计口径需要核对。",
    "Redis 集群主从切换时的脑裂防护策略。",
    "PostgreSQL 分区表索引维护窗口安排在周日凌晨。",
    "铝合金铸件热处理工艺参数记录表。",
    "北欧极光观测最佳季节与拍摄曝光参数。",
)


def _document(n: int = 3):
    assert n <= len(_CONTENTS)
    return {
        "source_type": "IMPORT",
        "source_name": "p0-remediation-r3",
        "memories": [
            {
                "memory_type": "SEMANTIC",
                "content": _CONTENTS[i],
            }
            for i in range(n)
        ],
    }


def test_r3_dry_run_has_zero_persistent_side_effects(manager, tmp_path):
    """五维判据：记录数/表内容/SQL DDL-DML/三件套/tmp 目录树全部不变。"""
    db_path = Path(manager.settings.database_path)
    assert db_path.is_relative_to(tmp_path.resolve())

    # 预热：Engine 全局 connect 监听的 PRAGMA journal_mode=WAL 是
    # connection.py 的既有行为（批次 C 排除项），必须在快照窗口之外完成
    # 库头切换，否则「首次连接创建 -wal/-shm」会被误记到 dry-run 头上。
    sources_before = _rows(manager, "SELECT * FROM memory_sources ORDER BY id")
    records_before = _rows(manager, "SELECT * FROM memory_records ORDER BY id")
    five_before = factory.p0_capture_five_way(db_path)
    triplet_before = _triplet(db_path)
    tree_before = _tree(tmp_path)

    with _SqlCounter(manager.engine) as counter:
        result = subject.run_import(manager, _document(), dry_run=True)

    sources_after = _rows(manager, "SELECT * FROM memory_sources ORDER BY id")
    records_after = _rows(manager, "SELECT * FROM memory_records ORDER BY id")

    assert result["dry_run"] is True
    assert result["imported"] == 3
    assert result["total"] == 3
    assert result["skipped_identical"] == 0
    assert result["skipped_similar"] == 0
    assert result["by_type"] == {"SEMANTIC": 3}
    assert result["source_id"] is None
    assert result["source_is_preview"] is True
    assert result["source_type"] == "IMPORT"

    assert sources_after == sources_before, "dry-run 新增了 memory_sources 记录"
    assert records_after == records_before, "dry-run 新增了 memory_records 记录"
    assert counter.ddl == 0 and counter.dml == 0
    assert not [s for s in counter.statements
                if s.lstrip().split(None, 1)[0].upper() in _DDL | _DML]
    assert factory.p0_capture_five_way(db_path) == five_before
    assert _triplet(db_path) == triplet_before
    assert _tree(tmp_path) == tree_before


def test_r3_dry_run_differs_from_real_import(manager, tmp_path):
    """正式导入必须真的写入；dry-run 必须一行都不写。"""
    document = _document(2)
    dry = subject.run_import(manager, document, dry_run=True)
    sources_after_dry = _rows(manager, "SELECT id FROM memory_sources")
    records_after_dry = _rows(manager, "SELECT id FROM memory_records")

    real = subject.run_import(manager, document, dry_run=False)
    sources_after_real = _rows(manager, "SELECT id FROM memory_sources")
    records_after_real = _rows(manager, "SELECT id FROM memory_records")

    assert dry["source_id"] is None
    assert dry["source_is_preview"] is True
    assert dry["imported"] == real["imported"] == 2, "预览统计必须与真实导入一致"
    assert dry["by_type"] == real["by_type"]
    assert real["source_id"] is not None
    assert real["source_is_preview"] is False
    assert len(sources_after_real) == len(sources_after_dry) + 1
    assert len(records_after_real) == len(records_after_dry) + 2


def test_r3_dry_run_validation_and_dedup_are_preserved(manager):
    """不得用「跳过整个导入流程」实现 dry-run：校验与去重仍生效。"""
    document = _document(2)
    document["memories"].append(dict(document["memories"][0]))
    result = subject.run_import(manager, document, dry_run=True)
    assert result["total"] == 3
    assert result["imported"] == 2
    assert result["skipped_identical"] == 1
    assert result["by_type"] == {"SEMANTIC": 2}
    assert _rows(manager, "SELECT id FROM memory_sources") == ()


def test_r3_dry_run_exception_leaves_zero_side_effects(manager, tmp_path):
    """dry-run 中途抛异常时，也不得留下任何持久化改动。"""
    db_path = Path(manager.settings.database_path)
    document = _document(2)
    document["memories"][1]["memory_type"] = "NOT_A_MEMORY_TYPE"
    # 同样先预热连接池，把 WAL 模式初始化排除在 dry-run 观察窗口之外。
    _rows(manager, "SELECT COUNT(*) FROM memory_sources")
    five_before = factory.p0_capture_five_way(db_path)
    tree_before = _tree(tmp_path)

    with _SqlCounter(manager.engine) as counter:
        with pytest.raises(ValueError):
            subject.run_import(manager, document, dry_run=True)
        assert counter.ddl == 0 and counter.dml == 0, (
            "异常路径也不得执行 DDL/DML"
        )

    assert factory.p0_capture_five_way(db_path) == five_before
    assert _tree(tmp_path) == tree_before
    assert _rows(manager, "SELECT id FROM memory_sources") == ()
    assert _rows(manager, "SELECT id FROM memory_records") == ()


def test_r3_dry_run_reuses_existing_source_without_insert(manager):
    """已有 source 时 dry-run 返回真实 id，但仍不新增行。"""
    SourceService(manager).create(
        subject.SourceCreate(
            source_type="IMPORT", source_name="p0-remediation-r3",
        )
    )
    before = _rows(manager, "SELECT id FROM memory_sources")
    result = subject.run_import(manager, _document(1), dry_run=True)
    assert result["source_id"] is not None
    assert result["source_is_preview"] is False
    assert _rows(manager, "SELECT id FROM memory_sources") == before


def test_r3_find_source_is_select_only(manager):
    """_find_source 只做 SELECT；无匹配时返回 None 而不是 INSERT。"""
    service = SourceService(manager)
    before = _rows(manager, "SELECT id FROM memory_sources")
    with _SqlCounter(manager.engine) as counter:
        found = subject._find_source(
            service, "IMPORT", "p0-remediation-r3-missing-source"
        )
    assert found is None
    assert counter.dml == 0 and counter.ddl == 0
    assert _rows(manager, "SELECT id FROM memory_sources") == before
