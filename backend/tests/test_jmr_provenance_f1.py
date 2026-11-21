"""P2 JMR-02 P0 REMEDIATION-01 · F-1 回归测试（隔离库）。

缺陷 F-1：metadata_json 为合法 JSON 但非对象（数组/字符串/数字/布尔/null）时，
provenance.rule_of 调用 payload.get 抛 AttributeError，使 list_derived /
read_derived / void_derived / void_and_rebuild 整体崩溃。

修复口径：非对象 metadata 与非法 JSON 同等对待 -> 归入 corrupt 分区、
_jmr_corrupt 标记、永不丢弃、永不映射到有效规则、永不被 void 误伤。

硬性环境规则（同 test_jmr_service.py）：
  * 只用 manager / settings 的隔离 SQLite 库；
  * 必须显式 install_p1_schema(manager)；
  * 不 skip / xfail，不修改门禁。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from jarvismind.governance.schema import install_p1_schema
from jarvismind.jmr import build_jmr_service
from jarvismind.jmr.provenance import RULES, rule_of

TS1 = "2026-01-01T00:00:00+00:00"
TS2 = "2026-02-01T00:00:00+00:00"

DERIVED_TABLES = ("memory_merges", "memory_corrections", "memory_summaries")

# 非对象 JSON 形态（合法 JSON，但顶层不是 object）
NON_OBJECT_PAYLOADS = [
    "[]",
    '["MERGE"]',
    '"MERGE"',
    "42",
    "3.14",
    "true",
    "false",
    "null",
]

# 非法 JSON 形态
INVALID_JSON_PAYLOADS = [
    "{not json",
    "MERGE",
    "{'rule': 'MERGE'}",
]

_MEM_INSERT = (
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at, source_id, metadata_json, search_text"
    ") VALUES (:id, :memory_type, :content, 5.0, 0.9, 'ACTIVE', "
    ":created_at, :created_at, NULL, NULL, :content)"
)


def _seed_memories(manager):
    rows = [
        ("M-DUP-A", "Bifrost 网关采用 PostgreSQL 主从架构", "JMR_DUP", TS1),
        (
            "M-DUP-B",
            "Bifrost 网关采用 PostgreSQL 主从架构，并计划 Q3 迁移到 TiDB",
            "JMR_DUP",
            TS2,
        ),
    ]
    with manager.session() as session:
        for memory_id, content, mtype, created in rows:
            session.execute(
                text(_MEM_INSERT),
                {
                    "id": memory_id,
                    "content": content,
                    "memory_type": mtype,
                    "created_at": created,
                },
            )


# 各派生表 NOT NULL 必需列（migrations.py:233-295 逐一核对），
# 其余列显式置 NULL。metadata_json 可为任意值（含非法 JSON / 非对象 JSON）。
_DERIVED_REQUIRED = {
    "memory_summaries": {
        "title": "损坏行占位标题",
        "content": "损坏行占位内容",
        "summary_type": "SINGLE_TOPIC",
        "memory_ids_json": "[]",
        "engine": "local",
    },
    "memory_corrections": {
        "memory_id_a": "M-DUP-A",
        "memory_id_b": "M-DUP-B",
        "detected_at": TS1,
    },
    "memory_merges": {
        "merged_memory_ids_json": "[]",
    },
}


def _insert_derived_raw(manager, table, row_id, metadata_json, status="PENDING"):
    """直接写入一条带任意 metadata_json 的派生行（模拟历史损坏数据）。

    只填 NOT NULL 必需列；其余列显式 NULL，不触发 NOT NULL 约束失败。
    """
    columns = set(_derived_columns(manager, table))
    values = {
        "id": row_id,
        "status": status,
        "metadata_json": metadata_json,
    }
    # memory_corrections has no updated_at column (migrations.py:244-261),
    # so only the timestamps this table actually defines are written.
    for stamp in ("created_at", "updated_at"):
        if stamp in columns:
            values[stamp] = TS1
    values.update(_DERIVED_REQUIRED[table])
    names = list(values)
    placeholders = ", ".join(f":{c}" for c in names)
    sql = f"INSERT INTO {table}({', '.join(names)}) VALUES ({placeholders})"
    with manager.session() as session:
        session.execute(text(sql), values)


def _derived_columns(manager, table):
    with manager.session() as session:
        return [r[1] for r in session.execute(text(f"PRAGMA table_info({table})"))]


def _row_status(manager, table, row_id):
    with manager.session() as session:
        return session.execute(
            text(f"SELECT status FROM {table} WHERE id=:i"), {"i": row_id}
        ).scalar()


def _count(manager, table):
    with manager.session() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar())


@pytest.fixture()
def f1_env(manager, settings):
    install_p1_schema(manager)
    _seed_memories(manager)
    return manager, settings


# ==========================================================================
# 一、rule_of 单元契约（纯函数）
# ==========================================================================
@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS + INVALID_JSON_PAYLOADS)
def test_rule_of_never_raises_on_abnormal_metadata(payload):
    # F-1 原始复现点：修复前此处抛 AttributeError
    assert rule_of(payload) == ""


@pytest.mark.parametrize("rule", RULES)
def test_rule_of_normal_object_behaviour_unchanged(rule):
    payload = json.dumps({"rule": rule, "source_memory_ids": ["M-1"]})
    assert rule_of(payload) == rule


def test_rule_of_object_without_rule_returns_empty():
    assert rule_of(json.dumps({"other": 1})) == ""


def test_rule_of_object_with_unknown_rule_is_returned_as_is():
    # 未知规则名保持原语义：返回字符串，由调用方按 RULES 过滤
    assert rule_of(json.dumps({"rule": "BOGUS"})) == "BOGUS"


def test_rule_of_none_and_empty_return_empty():
    assert rule_of(None) == ""
    assert rule_of("") == ""


# ==========================================================================
# 二、F-1 原始缺陷：查询 / 读取 / 撤销 / 重建在损坏行存在时不崩溃
# ==========================================================================
@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_f1_list_derived_survives_non_object_row(f1_env, payload):
    manager, settings = f1_env
    _insert_derived_raw(manager, "memory_merges", "BAD-NONOBJ", payload)
    service = build_jmr_service(manager=manager)
    result = service.store.list_derived()
    corrupt_ids = [r["id"] for r in result["corrupt"]]
    assert "BAD-NONOBJ" in corrupt_ids  # 损坏行保持可见，不静默消失
    bad = next(r for r in result["corrupt"] if r["id"] == "BAD-NONOBJ")
    assert bad["_jmr_corrupt"] == "metadata_not_object"
    assert bad["_jmr_source_table"] == "memory_merges"


@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_f1_read_derived_excludes_non_object_row_from_rules(f1_env, payload):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_merges", "BAD-READ", payload)
    service = build_jmr_service(manager=manager)
    derived = service.read_derived()  # F-1 修复前此处 AttributeError
    assert set(derived) == set(RULES)
    for items in derived.values():
        assert all(item["id"] != "BAD-READ" for item in items)


@pytest.mark.parametrize("payload", NON_OBJECT_PAYLOADS)
def test_f1_void_derived_skips_non_object_row(f1_env, payload):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_summaries", "BAD-VOID", payload)
    service = build_jmr_service(manager=manager)
    changed = service.store.void_derived()  # F-1 修复前此处 AttributeError
    assert changed == 0
    assert _row_status(manager, "memory_summaries", "BAD-VOID") == "PENDING"


def test_f1_void_and_rebuild_completes_with_mixed_corrupt_rows(f1_env):
    manager, _ = f1_env
    # 同时注入 F-1 的全部非对象形态与非法 JSON，覆盖批量场景
    for idx, payload in enumerate(NON_OBJECT_PAYLOADS + INVALID_JSON_PAYLOADS):
        _insert_derived_raw(manager, "memory_corrections", f"BAD-MIX-{idx}", payload)
    service = build_jmr_service(manager=manager)
    service.run(task_id="T-F1-1", run_id="R-F1-1")
    rebuilt = service.void_and_rebuild(task_id="T-F1-2", run_id="R-F1-2")
    assert rebuilt.status == "SUCCEEDED"
    # 损坏行全部保留（不删除、不作废）
    for idx in range(len(NON_OBJECT_PAYLOADS) + len(INVALID_JSON_PAYLOADS)):
        assert _row_status(manager, "memory_corrections", f"BAD-MIX-{idx}") == "PENDING"


def test_f1_single_corrupt_row_does_not_break_valid_rows(f1_env):
    """单条损坏记录不得影响同批有效记录的分类与读取。"""
    manager, _ = f1_env
    valid = json.dumps({"rule": "MERGE", "source_memory_ids": ["M-DUP-A"]})
    _insert_derived_raw(manager, "memory_merges", "GOOD-1", valid)
    _insert_derived_raw(manager, "memory_merges", "BAD-ARR", "[1,2,3]")
    service = build_jmr_service(manager=manager)
    derived = service.read_derived(rule="MERGE")
    ids = [item["id"] for item in derived["MERGE"]]
    assert ids == ["GOOD-1"]
    corrupt = service.store.list_derived()["corrupt"]
    assert any(r["id"] == "BAD-ARR" for r in corrupt)


# ==========================================================================
# 三、非法 JSON 边界（与非对象同等处理，修复前已正确，修复后保持）
# ==========================================================================
@pytest.mark.parametrize("payload", INVALID_JSON_PAYLOADS)
def test_invalid_json_row_classified_as_corrupt_unparseable(f1_env, payload):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_summaries", "BAD-JSON", payload)
    service = build_jmr_service(manager=manager)
    corrupt = service.store.list_derived()["corrupt"]
    bad = next(r for r in corrupt if r["id"] == "BAD-JSON")
    assert bad["_jmr_corrupt"] == "metadata_json_unparseable"


def test_missing_metadata_row_classified_as_corrupt(f1_env):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_summaries", "BAD-NULL", None)
    service = build_jmr_service(manager=manager)
    corrupt = service.store.list_derived()["corrupt"]
    bad = next(r for r in corrupt if r["id"] == "BAD-NULL")
    assert bad["_jmr_corrupt"] == "metadata_missing"


def test_object_without_rule_classified_as_rule_missing(f1_env):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_summaries", "BAD-NORULE", "{}")
    service = build_jmr_service(manager=manager)
    corrupt = service.store.list_derived()["corrupt"]
    bad = next(r for r in corrupt if r["id"] == "BAD-NORULE")
    assert bad["_jmr_corrupt"] == "rule_missing_or_unknown"


@pytest.mark.parametrize("rule_value", [["MERGE"], 5, True, {"rule": "MERGE"}])
def test_non_string_rule_is_unknown_and_kept_corrupt(f1_env, rule_value):
    # 对象但 rule 非字符串：不得被字符串化成看似合法的规则名，也不得静默丢失
    manager, _ = f1_env
    payload = json.dumps({"rule": rule_value, "source_memory_ids": ["M-DUP-A"]})
    assert rule_of(payload) == ""
    _insert_derived_raw(manager, "memory_merges", "BAD-RULETYPE", payload)
    service = build_jmr_service(manager=manager)
    corrupt = service.store.list_derived()["corrupt"]
    bad = next(r for r in corrupt if r["id"] == "BAD-RULETYPE")
    assert bad["_jmr_corrupt"] == "rule_missing_or_unknown"


# ==========================================================================
# 四、正常对象行为一致性（修复前后必须一致）
# ==========================================================================
def test_normal_object_rows_keep_rule_bucketing(f1_env):
    manager, _ = f1_env
    merge_meta = json.dumps({"rule": "MERGE", "source_memory_ids": ["M-DUP-A"]})
    corr_meta = json.dumps({"rule": "CONFLICT", "source_memory_ids": ["M-DUP-B"]})
    _insert_derived_raw(manager, "memory_merges", "OK-MERGE", merge_meta)
    _insert_derived_raw(manager, "memory_corrections", "OK-CONF", corr_meta)
    service = build_jmr_service(manager=manager)

    by_table = service.store.list_derived()
    assert [r["id"] for r in by_table["merges"]] == ["OK-MERGE"]
    assert [r["id"] for r in by_table["corrections"]] == ["OK-CONF"]
    assert by_table["corrupt"] == []

    derived = service.read_derived()
    assert [r["id"] for r in derived["MERGE"]] == ["OK-MERGE"]
    assert [r["id"] for r in derived["CONFLICT"]] == ["OK-CONF"]


def test_normal_object_row_is_voided_by_rule_filter(f1_env):
    manager, _ = f1_env
    meta = json.dumps({"rule": "MERGE", "source_memory_ids": ["M-DUP-A"]})
    _insert_derived_raw(manager, "memory_merges", "OK-VOID", meta)
    service = build_jmr_service(manager=manager)
    assert service.store.void_derived(rule="MERGE") == 1
    assert _row_status(manager, "memory_merges", "OK-VOID") == "VOID"


def test_void_derived_does_not_touch_corrupt_rows_with_rule_filter(f1_env):
    manager, _ = f1_env
    _insert_derived_raw(manager, "memory_merges", "BAD-KEEP", '"MERGE"')
    service = build_jmr_service(manager=manager)
    assert service.store.void_derived(rule="MERGE") == 0
    assert _row_status(manager, "memory_merges", "BAD-KEEP") == "PENDING"


# ==========================================================================
# 五、隔离与完整性：损坏行不被删除，行数守恒
# ==========================================================================
def test_corrupt_rows_are_never_deleted(f1_env):
    manager, _ = f1_env
    before = sum(_count(manager, t) for t in DERIVED_TABLES)
    for idx, payload in enumerate(NON_OBJECT_PAYLOADS):
        _insert_derived_raw(manager, "memory_merges", f"KEEP-{idx}", payload)
    service = build_jmr_service(manager=manager)
    service.read_derived()
    service.store.void_derived()
    after = sum(_count(manager, t) for t in DERIVED_TABLES)
    assert after == before + len(NON_OBJECT_PAYLOADS)
