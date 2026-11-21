"""P2 JMR · 隔离库动态集成测试（十四条验收标准的 DB 主体）。

对齐 JarvisMind-2.0-P2-JMR-Architecture-Design.md v1.2：
  §5 公共 API 契约、§12 标准 1/2/3/4/5/14、§20 契约对齐点。

硬性环境规则：
  * 只用 settings / manager / client fixture 的隔离 SQLite 库；
  * fixture 必须显式 install_p1_schema(manager)（build() 不建表，D-13）；
  * 不裸调 DatabaseManager / sqlite3.connect / load_settings；
  * 标准 5 / 14 的摘要用独立新连接 ReadOnlyProjection.content_digest()；
  * 不 skip / xfail 任何用例。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from jarvismind.database.migrations import MIGRATIONS
from jarvismind.governance.projection import ReadOnlyProjection
from jarvismind.governance.schema import install_p1_schema
from jarvismind.jmr import JMRService, build_jmr_service
from jarvismind.jmr.query import JMRQueryService
from jarvismind.jmr.provenance import RULES

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"
TS3 = "2026-03-01T00:00:00+00:00"

DERIVED_TABLES = ("memory_merges", "memory_corrections", "memory_summaries")

# memory_records 测试数据：显式给齐 11 列（§20.3 规则 2），status=ACTIVE，
# created_at 为可排序 ISO 字符串；按 memory_type 分组隔离四类检测。
_INSERT_SQL = (
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, :importance, :confidence, :status, "
    ":created_at, :updated_at, :source_id, :metadata_json, :search_text)"
)


def _row(memory_id, content, memory_type, created_at):
    return {
        "id": memory_id,
        "memory_type": memory_type,
        "content": content,
        "importance": 5.0,
        "confidence": 0.9,
        "status": "ACTIVE",
        "created_at": created_at,
        "updated_at": created_at,
        "source_id": None,
        "metadata_json": None,
        "search_text": content,
    }


def _dup_dataset():
    return [
        _row("M-DUP-A", "Bifrost 网关采用 PostgreSQL 主从架构", "JMR_DUP", TS1),
        _row(
            "M-DUP-B",
            "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
            "JMR_DUP",
            TS2,
        ),
    ]


def _status_dataset():
    return [
        _row("M-ST-1", "Phoenix 项目当前状态为进行中", "JMR_STATUS", TS1),
        _row("M-ST-2", "Phoenix 项目当前状态为已完成", "JMR_STATUS", TS2),
    ]


def _full_dataset():
    """四类分组隔离；稳定 ASCII 标识互不相同，避免跨组误判。8 条 < 200。"""
    return [
        *_dup_dataset(),
        *_status_dataset(),
        _row("M-CF-1", "Nebula 缓存层使用 Redis Cluster 三主三从部署", "JMR_CONFLICT", TS1),
        _row("M-CF-2", "Nebula 缓存层改用 Memcached 单机方案降低成本", "JMR_CONFLICT", TS2),
        _row("M-RL-1", "Atlas 日志平台采集 Nginx 访问日志", "JMR_RELATED", TS1),
        _row("M-RL-2", "Atlas 日志平台采集应用错误日志", "JMR_RELATED", TS2),
    ]


def _seed(manager, specs):
    with manager.session() as session:
        for spec in specs:
            session.execute(text(_INSERT_SQL), spec)


def _scalar(manager, sql, params=None):
    with manager.session() as session:
        return session.execute(text(sql), params or {}).scalar()


def _rows(manager, sql, params=None):
    with manager.session() as session:
        return [tuple(r) for r in session.execute(text(sql), params or {})]


def _table_count(manager, table, where=""):
    return int(_scalar(manager, f"SELECT COUNT(*) FROM {table} {where}") or 0)


def _derived_rows(manager):
    """返回 [(table, id, status, metadata_json, ...extra)] 供标准 4/5 断言。"""
    out = []
    for table in DERIVED_TABLES:
        with manager.session() as session:
            for row in session.execute(text(f"SELECT * FROM {table}")).mappings():
                out.append((table, dict(row)))
    return out


def _content_digest(manager):
    """独立新连接摘要（每次 content_digest() 都重开 mode=ro 连接）。"""
    return ReadOnlyProjection(manager).content_digest()


def _pending_total(manager):
    return sum(
        _table_count(manager, t, "WHERE status='PENDING'") for t in DERIVED_TABLES
    )


@pytest.fixture()
def jmr_env(manager, settings):
    """显式安装 P1 表；不 build_governance（默认未激活）。"""
    install_p1_schema(manager)
    return manager, settings


# ==========================================================================
# 构建契约（§20.1 / D-13）
# ==========================================================================
def test_build_entries_do_not_create_p1_tables(manager):
    """build_jmr_service 与 JMRService.build 都不自动建表（§20.3 规则 1）。"""
    with manager.session() as session:
        before = {
            r[0]
            for r in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "p1_tasks" not in before
    assert "p1_task_events" not in before

    build_jmr_service(manager=manager)
    JMRService.build(manager=manager)

    with manager.session() as session:
        after = {
            r[0]
            for r in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "p1_tasks" not in after
    assert "p1_task_events" not in after
    with manager.session() as session:
        assert (
            session.execute(
                text("SELECT value FROM system_settings WHERE key='p1.schema_version'")
            ).scalar()
            is None
        )


# ==========================================================================
# 标准 1 · 重复记忆识别（DB 层）
# ==========================================================================
def test_std1_duplicate_produces_one_pending_merge(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _dup_dataset())
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD1", run_id="R-STD1")
    assert result.status == "SUCCEEDED"
    assert result.created["MERGE"] == 1

    with manager.session() as session:
        rows = session.execute(
            text(
                "SELECT id, status, merged_memory_ids_json, target_memory_id, "
                "metadata_json FROM memory_merges"
            )
        ).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "PENDING"
    assert row["target_memory_id"] is None
    assert set(json.loads(row["merged_memory_ids_json"])) == {"M-DUP-A", "M-DUP-B"}
    meta = json.loads(row["metadata_json"])
    assert meta["rule"] == "MERGE"


# ==========================================================================
# 标准 2 · 状态变化识别（DB 层）
# ==========================================================================
def test_std2_status_change_produces_pending_correction_with_snapshots(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _status_dataset())
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD2", run_id="R-STD2")
    assert result.status == "SUCCEEDED"
    assert result.created["STATUS_CHANGE"] == 1

    with manager.session() as session:
        rows = session.execute(
            text(
                "SELECT status, before_state_json, after_state_json, "
                "chosen_memory_id, resolved_at, metadata_json "
                "FROM memory_corrections"
            )
        ).mappings().all()
    status_rows = [r for r in rows if json.loads(r["metadata_json"])["rule"] == "STATUS_CHANGE"]
    assert len(status_rows) == 1
    row = status_rows[0]
    assert row["status"] == "PENDING"
    assert row["chosen_memory_id"] is None
    assert row["resolved_at"] is None
    before = json.loads(row["before_state_json"])
    after = json.loads(row["after_state_json"])
    for snapshot in (before, after):
        for key in ("memory_id", "content", "status", "created_at", "updated_at", "state_token"):
            assert key in snapshot
    assert before["created_at"] == TS1
    assert after["created_at"] == TS2
    assert before["state_token"] == "IN_PROGRESS"
    assert after["state_token"] == "DONE"


# ==========================================================================
# 标准 3 · 冲突识别 + DB 级互斥（关闭 P1-1）
# ==========================================================================
def test_std3_conflict_produces_correction_and_db_level_exclusion(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD3", run_id="R-STD3")
    assert result.status == "SUCCEEDED"

    with manager.session() as session:
        corrections = session.execute(
            text("SELECT memory_id_a, memory_id_b, metadata_json FROM memory_corrections")
        ).mappings().all()
        merges = session.execute(
            text("SELECT merged_memory_ids_json FROM memory_merges")
        ).mappings().all()
    conflict_pairs = set()
    for r in corrections:
        metadata = json.loads(r["metadata_json"])
        if metadata["rule"] == "CONFLICT":
            assert metadata["conflict_class"] == "WORDING_DIFF"
            assert metadata["confidence"] == 0.55
            assert metadata["source_memory_ids"] == sorted(
                (r["memory_id_a"], r["memory_id_b"])
            )
            conflict_pairs.add(tuple(sorted((r["memory_id_a"], r["memory_id_b"]))))
    assert conflict_pairs  # 至少 1 个冲突建议
    query = JMRQueryService(manager)
    assert query.list_conflicts(conflict_class="WORDING_DIFF").total == len(conflict_pairs)
    for unapproved_class in ("FACT_CONFLICT", "TIME_UPDATE", "INSUFFICIENT_INFO"):
        assert query.list_conflicts(conflict_class=unapproved_class).total == 0

    merge_pairs = set()
    for r in merges:
        ids = sorted(json.loads(r["merged_memory_ids_json"]))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                merge_pairs.add((ids[i], ids[j]))
    # 强判据：同一 (id_a,id_b) 不得同时出现在 merges 与 corrections
    assert conflict_pairs.isdisjoint(merge_pairs)


def test_conflict_with_missing_or_unapproved_class_is_not_written(jmr_env, monkeypatch):
    """A CONFLICT without the approved class must fail closed, not become WORDING_DIFF."""
    manager, _ = jmr_env
    _seed(
        manager,
        [
            _row("M-CF-MISSING-1", "Nebula 缓存层使用 Redis Cluster 三主三从部署", "JMR_CONFLICT", TS1),
            _row("M-CF-MISSING-2", "Nebula 缓存层改用 Memcached 单机方案降低成本", "JMR_CONFLICT", TS2),
        ],
    )
    service = build_jmr_service(manager=manager)
    monkeypatch.setattr(service, "_detect", lambda records: _detect_with_class(records, None))
    result = service.run(task_id="T-CLASS-MISSING", run_id="R-CLASS-MISSING")
    assert result.status == "SUCCEEDED"
    assert result.created["CONFLICT"] == 0
    assert _table_count(manager, "memory_corrections") == 0

    monkeypatch.setattr(service, "_detect", lambda records: _detect_with_class(records, "FACT_CONFLICT"))
    result = service.run(task_id="T-CLASS-UNAPPROVED", run_id="R-CLASS-UNAPPROVED")
    assert result.status == "SUCCEEDED"
    assert result.created["CONFLICT"] == 0
    assert _table_count(manager, "memory_corrections") == 0


def _detect_with_class(records, conflict_class):
    from jarvismind.jmr.detectors import ConflictPair, find_conflict_pairs

    candidates = find_conflict_pairs(records)
    conflicts = tuple(
        ConflictPair(
            memory_id_a=pair.memory_id_a,
            memory_id_b=pair.memory_id_b,
            reason=pair.reason,
            confidence=pair.confidence,
            conflict_class=conflict_class,
        )
        for pair in candidates
    )
    clusters = ()
    changes = ()
    related = ()
    summary = ("记忆反思总结", "summary", 0.6)
    return clusters, changes, conflicts, related, summary


def test_std3_status_change_pair_is_not_reported_as_conflict(jmr_env):
    """v1.2 优先级链：状态变化对只能落 STATUS_CHANGE，不得落 CONFLICT。"""
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-STD3B", run_id="R-STD3B")

    with manager.session() as session:
        rows = session.execute(
            text("SELECT memory_id_a, memory_id_b, metadata_json FROM memory_corrections")
        ).mappings().all()
    target = {"M-ST-1", "M-ST-2"}
    for r in rows:
        if {r["memory_id_a"], r["memory_id_b"]} == target:
            assert json.loads(r["metadata_json"])["rule"] != "CONFLICT"


# ==========================================================================
# 标准 4 · 来源追溯（独立 SQL 反查）
# ==========================================================================
def test_std4_derived_metadata_sources_exist_in_memory_records(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-STD4", run_id="R-STD4")

    derived = _derived_rows(manager)
    assert derived  # 确实产出了派生行
    with manager.session() as session:
        real_ids = {
            r[0] for r in session.execute(text("SELECT id FROM memory_records"))
        }
    for table, row in derived:
        meta = json.loads(row["metadata_json"])
        for key in (
            "source_memory_ids",
            "lineage",
            "task_id",
            "run_id",
            "rule",
            "algorithm_version",
            "projection_digest",
        ):
            assert key in meta, f"{table} 缺固定键 {key}"
        assert meta["rule"] in RULES
        assert meta["task_id"] == "T-STD4"
        assert meta["run_id"] == "R-STD4"
        sources = meta["source_memory_ids"]
        assert sources and set(sources).issubset(real_ids)
        lineage = meta["lineage"]
        items = lineage if isinstance(lineage, list) else [lineage]
        for entry in items:
            assert entry["authority"] == "JME"
            assert entry["table"] == "memory_records"
        assert meta["projection_digest"]


def test_std4_derived_rows_are_always_pending_with_null_resolution(jmr_env):
    """§20.2 派生行恒值：PENDING + 四个确认/裁决列 NULL。"""
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-STD4B", run_id="R-STD4B")

    with manager.session() as session:
        merges = session.execute(
            text("SELECT status, target_memory_id FROM memory_merges")
        ).mappings().all()
        corrections = session.execute(
            text("SELECT status, chosen_memory_id, resolved_at FROM memory_corrections")
        ).mappings().all()
        summaries = session.execute(
            text("SELECT status, confirmed_at FROM memory_summaries")
        ).mappings().all()
    assert merges and corrections and summaries
    assert all(r["status"] == "PENDING" and r["target_memory_id"] is None for r in merges)
    assert all(
        r["status"] == "PENDING"
        and r["chosen_memory_id"] is None
        and r["resolved_at"] is None
        for r in corrections
    )
    assert all(r["status"] == "PENDING" and r["confirmed_at"] is None for r in summaries)


# ==========================================================================
# 标准 5 · 派生数据与原始数据隔离（独立新连接摘要 + 逐字比对）
# ==========================================================================
def test_std5_original_memory_is_unchanged_after_run(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    before_digest = _content_digest(manager)  # 独立新连接
    before_rows = _rows(manager, "SELECT content FROM memory_records ORDER BY id")
    before_count = _table_count(manager, "memory_records")

    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD5", run_id="R-STD5")
    assert result.status == "SUCCEEDED"

    after_digest = _content_digest(manager)  # 再开一条独立新连接
    after_rows = _rows(manager, "SELECT content FROM memory_records ORDER BY id")
    assert after_digest == before_digest
    assert after_rows == before_rows  # 逐字比对，不用缓存的 digest_of_records 自比
    assert _table_count(manager, "memory_records") == before_count
    # 派生行只落在三张派生表
    assert _derived_rows(manager)


def test_std5_related_is_summary_metadata_key_not_standalone_row(jmr_env):
    """§20.1 / P2-2：RELATED 不是 rule、不是独立行，只是 SUMMARY 行内的键。"""
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-STD5B", run_id="R-STD5B")

    rules_in_db = set()
    summary_meta = []
    for _table, row in _derived_rows(manager):
        meta = json.loads(row["metadata_json"])
        rules_in_db.add(meta.get("rule"))
        if meta.get("rule") == "SUMMARY":
            summary_meta.append(meta)
    assert rules_in_db.issubset(set(RULES))
    assert "RELATED" not in rules_in_db
    assert summary_meta
    assert any(
        "related_memory_ids" in m and "related_groups" in m for m in summary_meta
    )


# ==========================================================================
# run() 结果契约（§20.1）
# ==========================================================================
def test_run_result_created_keys_and_status_are_frozen(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-RESULT", run_id="R-RESULT")
    assert set(result.created) == {"MERGE", "CONFLICT", "STATUS_CHANGE", "SUMMARY"}
    assert all(isinstance(v, int) and v >= 0 for v in result.created.values())
    assert result.status == "SUCCEEDED"
    assert result.memory_rows == len(_full_dataset())
    assert result.projection_digest
    assert result.task_id == "T-RESULT"
    assert result.run_id == "R-RESULT"


def test_read_derived_returns_rule_keyed_mapping(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-READ", run_id="R-READ")
    derived = service.read_derived()
    assert isinstance(derived, dict)
    assert set(derived).issubset(set(RULES))
    merge_only = service.read_derived(rule="MERGE")
    assert isinstance(merge_only, dict)
    assert set(merge_only).issubset({"MERGE"})


# ==========================================================================
# void_and_rebuild · VOID 复活（D-8 / 标准 10 部分）
# ==========================================================================
def test_void_and_rebuild_restores_pending_row_count(jmr_env):
    manager, _ = jmr_env
    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-REBUILD-1", run_id="R-REBUILD-1")
    before = _pending_total(manager)
    assert before > 0

    rebuilt = service.void_and_rebuild(task_id="T-REBUILD-2", run_id="R-REBUILD-2")
    assert rebuilt.status == "SUCCEEDED"
    after = _pending_total(manager)
    assert after == before  # VOID 行被 upsert 复活，不出现零条有效行
    # 逻辑作废不物理删除：总行数不减
    total = sum(_table_count(manager, t) for t in DERIVED_TABLES)
    assert total >= before


# ==========================================================================
# 标准 14 · 不破坏数据库及迁移策略（基线在 install_p1_schema 之后取）
# ==========================================================================
def test_std14_run_does_not_change_sqlite_master_or_migrations(manager):
    install_p1_schema(manager)
    # 基线在 install_p1_schema 之后取
    with manager.session() as session:
        baseline = {
            r[0] for r in session.execute(text("SELECT name FROM sqlite_master"))
        }

    _seed(manager, _full_dataset())
    service = build_jmr_service(manager=manager)
    result = service.run(task_id="T-STD14", run_id="R-STD14")
    assert result.status == "SUCCEEDED"

    with manager.session() as session:
        after = {
            r[0] for r in session.execute(text("SELECT name FROM sqlite_master"))
        }
    assert after == baseline  # run() 前后 sqlite_master 不增不减（无 DDL / 无 DROP）
    assert _rows(manager, "SELECT version FROM schema_migrations ORDER BY version") == [
        (1,), (2,), (3,), (4,), (5,), (6,)
    ]
    assert len(MIGRATIONS) == 6
    assert MIGRATIONS[-1].version == 6
