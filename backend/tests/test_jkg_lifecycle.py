"""JKG-INT-04 生命周期同步回归：六类真实生命周期路径 + 游标/幂等/失败隔离。

覆盖的六类路径都来自 JME 的真实入口，不是凭空造的事件：

1. **新增** —— ``CREATE`` 审计行 → tick → 该 memory 被处理，图不变（无自动写图）；
2. **修改** —— ``UPDATE`` / ``CORRECTION_*`` → tick → 该 memory 的全部
   ``jkg_edge_sources`` 被标 ``CONTENT_UPDATED_STALE``，关联边 ``INVALID``；
3. **遗忘** —— 走真实批准链 ``POST /memories/{id}/forget-requests`` →
   ``POST /forget-requests/{id}/approve`` → ``status=DELETED`` → tick → 失效；
4. **软删除 / 自动过期** —— ``SOFT_DELETE`` / ``AUTO_EXPIRE`` 审计行 + 对应
   ``status`` → tick → 失效；
5. **归档 / 恢复** —— ``ARCHIVED`` → tick → **来源仍有效**（``invalidated_at``
   为 ``None``、边仍 ``ACTIVE``）；``ARCHIVED → ACTIVE``（``RESTORE``）→ 无多余变更；
6. **合并** —— ``MERGE_CONFIRM`` + 被并记忆 ``MERGED`` → tick → 被并来源失效，
   产品记忆不受影响。

另外覆盖：多来源、单条失败隔离、游标与重叠窗口、``dry_run``、关闭态 / 未装表、
``reconcile`` 游标分页、B-3 读时一致性、B-2 日志脱敏、权威表不被写。

数据库一律来自共享 ``manager`` fixture 的会话临时库；四类被 P0-03A 门禁禁止的
裸构造（连接工厂 / 默认配置加载器 / 会话构造 / 子进程）在本文件中一处都不出现。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from _fixtures.jkg_schema_factory import create_jkg_schema
from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.projection import ReadOnlyProjection
from jarvismind.jkg import JkgCompositionAdapter
from jarvismind.jkg.lifecycle import (
    CURSOR_KEY,
    LAST_TICK_KEY,
    STALE_REASON,
    VERSION_KEY,
    JkgLifecycleSync,
)
from jarvismind.jkg.schema import JKG_ENABLED_KEY, JKG_TABLES, present_jkg_tables
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryType, MemoryUpdate
from jarvismind.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# 常量 / SQL（模块级只构造语句对象，不执行）
# ---------------------------------------------------------------------------
_CONTENT_MARKER = "MKR" + "-INT-04-LIFECYCLE-DO-NOT-LEAK"

#: A 32-character hex-shaped id that is **guaranteed** to trip the tolerant
#: ``phone_sep`` redaction pattern (it embeds several ``1[3-9]\d{9}`` runs, and
#: that pattern has no digit-boundary guard). Used to pin ``uuid4`` so the
#: identifier-survival regression is deterministic instead of ~1% flaky.
_MANGLED_PRONE_ID = "13800138000138001380013800138001"

_INSERT_MEMORY = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, "
    "created_at, updated_at"
    ") VALUES (:id, 'SEMANTIC', :content, 5.0, 0.9, :status, :now, :now)"
)

_SET_MEMORY_STATUS = text(
    "UPDATE memory_records SET status = :status, updated_at = :now WHERE id = :id"
)

_INSERT_AUDIT = text(
    "INSERT INTO memory_audit_logs(id, memory_id, action, actor, detail_json, created_at) "
    "VALUES (:id, :memory_id, :action, :actor, :detail, :created_at)"
)

_UPSERT_SETTING = text(
    "INSERT INTO system_settings(key, value, updated_at) "
    "VALUES (:key, :value, :now) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)

_READ_SETTING = text("SELECT value FROM system_settings WHERE key = :key")

_COUNT_ROWS = "SELECT COUNT(*) FROM {}"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------
@pytest.fixture()
def graph(manager):
    """装表 + 启用。这是 JKG 可用的唯二前置条件。"""
    create_jkg_schema(manager)
    _set_enabled(manager, "1")
    return manager


@pytest.fixture()
def sync(graph):
    return JkgLifecycleSync(graph)


def _set_enabled(manager, value: str = "1") -> None:
    with manager.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": JKG_ENABLED_KEY, "value": value, "now": utc_now_iso()},
        )


def _get_setting(manager, key: str):
    with manager.session() as session:
        return session.execute(_READ_SETTING, {"key": key}).scalar()


def _insert_memory(
    manager, content: str, *, status: str = "ACTIVE", memory_id: str = ""
) -> str:
    memory_id = memory_id or uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "content": content,
                "status": status,
                "now": utc_now_iso(),
            },
        )
    return memory_id


def _set_memory_status(manager, memory_id: str, status: str) -> None:
    with manager.session() as session:
        session.execute(
            _SET_MEMORY_STATUS,
            {"id": memory_id, "status": status, "now": utc_now_iso()},
        )


def _audit(
    manager,
    memory_id: str,
    action: str,
    *,
    created_at: str = "",
    detail: str = "",
    actor: str = "system",
) -> str:
    audit_id = uuid.uuid4().hex
    with manager.session() as session:
        session.execute(
            _INSERT_AUDIT,
            {
                "id": audit_id,
                "memory_id": memory_id,
                "action": action,
                "actor": actor,
                "detail": detail or None,
                "created_at": created_at or utc_now_iso(),
            },
        )
    return audit_id


def _row_counts(manager) -> dict:
    counts = {}
    with manager.session() as session:
        for table in JKG_TABLES:
            counts[table] = int(
                session.execute(text(_COUNT_ROWS.format(table))).scalar() or 0
            )
    return counts


def _jkg_tables(manager) -> list:
    """Wrap ``present_jkg_tables`` (it takes a session, not the manager)."""
    with manager.session() as session:
        return present_jkg_tables(session)


def _memory_digest(manager) -> tuple:
    with manager.session() as session:
        rows = session.execute(
            text("SELECT id, status, content FROM memory_records ORDER BY id")
        ).all()
    return tuple((str(r[0]), str(r[1]), str(r[2])) for r in rows)


def _sources(manager, edge_id: str) -> list:
    with manager.session() as session:
        return session.execute(
            text(
                "SELECT memory_id, invalidated_at, invalidation_reason "
                "FROM jkg_edge_sources WHERE edge_id = :eid ORDER BY memory_id"
            ),
            {"eid": edge_id},
        ).all()


def _edge_row(manager, edge_id: str):
    with manager.session() as session:
        return session.execute(
            text("SELECT edge_id, status FROM jkg_edges WHERE edge_id = :eid"),
            {"eid": edge_id},
        ).first()


def _writer(manager):
    return JkgCompositionAdapter(manager).write_service()


def _assert_the_pinned_id_is_mangle_prone(pinned: str = _MANGLED_PRONE_ID) -> None:
    """守卫：证明这条用例不是恒真。

    如果哪天脱敏规则真的收窄了（``phone_sep`` 加上数字边界守卫），这里会先失败
    并指出“前提已经不存在”，而不是让下面的断言悄悄变成同义反复。
    """
    from jarvismind.jmr.sensitive import redact_for_log

    assert redact_for_log({"id": pinned})["id"] != pinned, (
        "这个 id 已经不会被内容过滤器改写了；回归用例失去前提，请换一个值"
    )


def _pin_generated_id(monkeypatch, pinned: str) -> None:
    """把 ``lifecycle`` 模块里的 ``uuid`` 钉成 ``pinned``。

    只替换 ``app.jkg.lifecycle`` 内部对 ``uuid`` 的绑定，不动全局 ``uuid`` 模块：
    本测试文件自己还要用它生成审计行 id（``_audit``），钉死全局会让所有审计行
    拿到同一个 id，游标与主键都会撞。
    """
    import jarvismind.jkg.lifecycle as lifecycle_module

    monkeypatch.setattr(
        lifecycle_module,
        "uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=pinned)),
    )



def _seed_edge(manager, memory_ids, *, predicate: str = "WORKS_ON") -> dict:
    """造 2 个节点 + 1 条边，边挂全部给定来源。"""
    writer = _writer(manager)
    left = writer.upsert_node(
        entity_type="PERSON", name="Alice", source_memory_id=memory_ids[0]
    )
    right = writer.upsert_node(
        entity_type="PROJECT", name="JarvisMind", source_memory_id=memory_ids[0]
    )
    return writer.create_edge(
        subject_id=left["node_id"],
        predicate=predicate,
        object_id=right["node_id"],
        source_memory_ids=list(memory_ids),
    )


# ===========================================================================
# A · 前置：关闭态 / 未装表（零访问、不建表、不抛错）
# ===========================================================================
def test_disabled_adapter_skips_the_tick_and_touches_nothing(manager):
    """关闭态：直接返回 skipped，不读不写，也不建表。"""
    _set_enabled(manager, "0")
    lifecycle = JkgLifecycleSync(manager)
    result = lifecycle.tick()
    assert result["skipped"] is True
    assert result["state"] == "disabled"
    assert result["entries_read"] == 0
    assert result["wrote"] is False
    assert _jkg_tables(manager) == []


def test_disabled_adapter_writes_no_cursor(manager):
    _set_enabled(manager, "0")
    JkgLifecycleSync(manager).tick()
    assert _get_setting(manager, CURSOR_KEY) is None
    assert _get_setting(manager, LAST_TICK_KEY) is None


def test_schema_not_ready_skips_without_raising_or_creating_tables(manager):
    _set_enabled(manager, "1")
    lifecycle = JkgLifecycleSync(manager)
    result = lifecycle.tick()
    assert result["skipped"] is True
    assert result["state"] == "schema_not_ready"
    assert _jkg_tables(manager) == []


def test_schema_not_ready_status_is_still_readable(manager):
    _set_enabled(manager, "1")
    status = JkgLifecycleSync(manager).status()
    assert status["state"] == "schema_not_ready"
    assert status["usable"] is False
    assert status["cursor_recovered"] is False


def test_status_on_a_disabled_graph_never_raises(manager):
    _set_enabled(manager, "0")
    status = JkgLifecycleSync(manager).status()
    assert status["state"] == "disabled"
    assert status["usable"] is False


def test_disabled_tick_reports_zero_counters(graph, manager):
    _set_enabled(manager, "0")
    result = JkgLifecycleSync(manager).tick()
    for key in (
        "memories_processed",
        "sources_invalidated",
        "sources_reactivated",
        "edges_invalidated",
        "edges_reactivated",
        "marked_stale",
    ):
        assert result[key] == 0, key


# ===========================================================================
# B · 新增（CREATE）：被处理，但不自动写图
# ===========================================================================
def test_create_audit_is_processed_by_the_tick(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    result = sync.tick()
    assert result["skipped"] is False
    assert result["entries_read"] == 1
    assert result["memories_processed"] == 1
    assert memory_id in result["processed_memory_ids"]
    assert result["ok"] is True


def test_create_does_not_write_the_graph(graph, sync):
    """C3 / 无自动写图：CREATE 之后三张表仍是空的。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    before = _row_counts(graph)
    sync.tick()
    assert _row_counts(graph) == before == {
        "jkg_nodes": 0,
        "jkg_edges": 0,
        "jkg_edge_sources": 0,
    }


def test_create_does_not_touch_the_authority_table(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    before = _memory_digest(graph)
    sync.tick()
    assert _memory_digest(graph) == before


def test_create_advances_the_cursor(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    audit_id = _audit(graph, memory_id, "CREATE")
    result = sync.tick()
    assert result["cursor_after"]["id"] == audit_id
    stored = json.loads(_get_setting(graph, CURSOR_KEY))
    assert stored["id"] == audit_id
    assert stored["created_at"] == result["cursor_after"]["created_at"]


def test_create_reports_no_unknown_actions(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    assert sync.tick()["unknown_actions"] == []


# ===========================================================================
# C · 修改（UPDATE）：旧事实必须失效
# ===========================================================================
def test_update_marks_every_source_stale(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE")

    result = sync.tick()
    assert result["marked_stale"] == 1

    sources = _sources(graph, edge["edge_id"])
    assert len(sources) == 1
    assert sources[0].invalidated_at is not None
    assert sources[0].invalidation_reason == STALE_REASON


def test_update_turns_the_edge_invalid(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE")
    sync.tick()
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_stale_reason_does_not_start_with_the_auto_prefix():
    """``CONTENT_UPDATED_STALE`` 必须不是 ``MEMORY_`` 前缀，否则会被复活。"""
    assert not STALE_REASON.startswith("MEMORY_")
    assert STALE_REASON == "CONTENT_UPDATED_STALE"


def test_a_second_sync_memory_does_not_revive_a_stale_source(graph, sync):
    """关键回归：stale 标记不是自动失效，所以 ``sync_memory`` 不得清掉它。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE")
    sync.tick()
    assert _sources(graph, edge["edge_id"])[0].invalidated_at is not None

    _writer(graph).sync_memory(memory_id)

    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is not None
    assert source.invalidation_reason == STALE_REASON
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_second_tick_does_not_double_count_stale(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE")
    first = sync.tick()
    assert first["marked_stale"] == 1
    second = sync.tick()
    assert second["marked_stale"] == 0


def test_correction_action_is_treated_as_content_changing(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "CORRECTION_RESOLVE")
    result = sync.tick()
    assert result["marked_stale"] == 1
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_correction_actions_are_not_reported_as_unknown(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CORRECTION_RESOLVE")
    assert sync.tick()["unknown_actions"] == []


def test_update_on_a_memory_without_sources_marks_nothing(graph, sync):
    """没有派生的来源行时，标记动作是空操作，不得报错。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "UPDATE")
    result = sync.tick()
    assert result["marked_stale"] == 0
    assert result["ok"] is True


# ===========================================================================
# D · 遗忘（真实批准链）→ 失效
# ===========================================================================
def test_forget_approval_invalidates_the_source(graph, client, auth_headers, sync, approved_forget):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])

    approved_forget.forget(memory_id)
    _audit(graph, memory_id, "FORGET_APPROVED")

    result = sync.tick()
    assert result["sources_invalidated"] >= 1
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is not None
    assert source.invalidation_reason.startswith("MEMORY_")
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_forget_is_idempotent_across_two_ticks(graph, client, auth_headers, sync, approved_forget):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    approved_forget.forget(memory_id)
    _audit(graph, memory_id, "FORGET_APPROVED")

    first = sync.tick()
    assert first["sources_invalidated"] == 1
    second = JkgLifecycleSync(graph).tick()
    assert second["sources_invalidated"] == 0
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_api_no_longer_reports_a_forgotten_relation_as_valid(
    graph, client, auth_headers, sync, approved_forget
):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    approved_forget.forget(memory_id)
    _audit(graph, memory_id, "FORGET_APPROVED")
    sync.tick()

    detail = client.get(f"/api/v1/jkg/edges/{edge['edge_id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["is_valid"] is False
    assert body["sources"][0]["is_valid"] is False


def test_forget_hides_the_relation_from_the_default_edge_list(
    graph, client, auth_headers, sync, approved_forget
):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    approved_forget.forget(memory_id)
    _audit(graph, memory_id, "FORGET_APPROVED")
    sync.tick()

    listed = client.get("/api/v1/jkg/edges", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


# ===========================================================================
# E · 软删除 / 自动过期 → 失效
# ===========================================================================
def test_soft_delete_invalidates_the_source(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "DELETED")
    _audit(graph, memory_id, "SOFT_DELETE")

    result = sync.tick()
    assert result["sources_invalidated"] == 1
    assert _sources(graph, edge["edge_id"])[0].invalidated_at is not None
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_auto_expire_invalidates_the_source(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "DELETED")
    _audit(graph, memory_id, "AUTO_EXPIRE")

    result = sync.tick()
    assert result["sources_invalidated"] == 1
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_physically_removed_memory_cannot_leave_orphan_sources(graph, sync):
    """物理删除记忆时 ``ON DELETE CASCADE`` 会一并清掉来源行：没有孤儿可失效。

    这条用例把一件事钉死：``jkg_edge_sources.memory_id`` 的外键是
    ``REFERENCES memory_records(id) ON DELETE CASCADE``，所以「记忆行没了但来源
    行还在」这种状态在库里无法存在。tick 因此不需要为它准备特殊路径；这里断言
    契约本身，避免以后有人改外键时静默地多出一类孤儿行。
    """
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    with graph.session() as s:
        s.execute(text("DELETE FROM memory_records WHERE id = :id"), {"id": memory_id})

    assert _sources(graph, edge["edge_id"]) == []
    _audit(graph, memory_id, "SOFT_DELETE")
    result = sync.tick()
    assert result["ok"] is True
    assert result["sources_invalidated"] == 0


# ===========================================================================
# F · 归档 / 恢复
# ===========================================================================
def test_archive_keeps_the_source_valid(graph, sync):
    """归档≠删除：``ARCHIVED`` 是合法来源状态（D9 / D17）。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")

    result = sync.tick()
    assert result["sources_invalidated"] == 0
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is None
    assert source.invalidation_reason is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_archived_source_is_reported_as_archived_but_valid(
    graph, client, auth_headers, sync
):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    sync.tick()

    listed = client.get("/api/v1/jkg/nodes?entity_type=PERSON", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    item = listed.json()["data"][0]
    assert item["source_memory_status"] == "ARCHIVED"
    assert item["source_memory_valid"] is True


def test_restore_from_archive_changes_nothing(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    sync.tick()

    _set_memory_status(graph, memory_id, "ACTIVE")
    _audit(graph, memory_id, "RESTORE")
    result = JkgLifecycleSync(graph).tick()

    assert result["sources_invalidated"] == 0
    assert result["sources_reactivated"] == 0
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_archive_after_delete_does_not_resurrect(graph, sync):
    """先删除再归档：来源仍是失效的，归档不是复活。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "DELETED")
    _audit(graph, memory_id, "SOFT_DELETE")
    sync.tick()
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"

    _set_memory_status(graph, memory_id, "ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    JkgLifecycleSync(graph).tick()
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"
    assert _sources(graph, edge["edge_id"])[0].invalidated_at is None


# ===========================================================================
# G · 合并
# ===========================================================================
def test_merge_invalidates_the_absorbed_memory_only(graph, sync):
    product_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    absorbed_id = _insert_memory(graph, "Alice works on JarvisMind")
    product_edge = _seed_edge(graph, [product_id])

    absorbed_writer = _writer(graph)
    absorbed_node = absorbed_writer.upsert_node(
        entity_type="PERSON", name="Bob", source_memory_id=absorbed_id
    )
    other_node = absorbed_writer.upsert_node(
        entity_type="EVENT", name="Review", source_memory_id=absorbed_id
    )
    absorbed_edge = absorbed_writer.create_edge(
        subject_id=absorbed_node["node_id"],
        predicate="ATTENDS",
        object_id=other_node["node_id"],
        source_memory_ids=[absorbed_id],
    )

    _set_memory_status(graph, absorbed_id, "MERGED")
    _audit(graph, product_id, "MERGE_CONFIRM")
    _audit(graph, absorbed_id, "MERGE_SOURCE")

    result = sync.tick()
    assert result["ok"] is True
    assert _edge_row(graph, absorbed_edge["edge_id"]).status == "INVALID"
    assert _edge_row(graph, product_edge["edge_id"]).status == "ACTIVE"


def test_merge_leaves_the_product_memory_valid(graph, sync):
    product_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    absorbed_id = _insert_memory(graph, "Alice works on JarvisMind")
    edge = _seed_edge(graph, [product_id])
    _set_memory_status(graph, absorbed_id, "MERGED")
    _audit(graph, product_id, "MERGE_CONFIRM")
    _audit(graph, absorbed_id, "MERGE_SOURCE")

    sync.tick()
    assert _sources(graph, edge["edge_id"])[0].invalidated_at is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_merge_actions_are_not_reported_as_unknown(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "MERGE_CONFIRM")
    _audit(graph, memory_id, "MERGE_SOURCE")
    assert sync.tick()["unknown_actions"] == []


# ===========================================================================
# H · 多来源
# ===========================================================================
def test_one_of_two_sources_forgotten_keeps_the_edge_active(graph, sync):
    memory_a = _insert_memory(graph, "Alice 负责 JarvisMind")
    memory_b = _insert_memory(graph, "Alice works on JarvisMind")
    edge = _seed_edge(graph, [memory_a, memory_b])

    _set_memory_status(graph, memory_a, "DELETED")
    _audit(graph, memory_a, "SOFT_DELETE")
    sync.tick()

    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"
    by_memory = {row.memory_id: row for row in _sources(graph, edge["edge_id"])}
    assert by_memory[memory_a].invalidated_at is not None
    assert by_memory[memory_b].invalidated_at is None


def test_one_of_two_sources_forgotten_reports_one_valid_source(
    graph, client, auth_headers, sync
):
    memory_a = _insert_memory(graph, "Alice 负责 JarvisMind")
    memory_b = _insert_memory(graph, "Alice works on JarvisMind")
    edge = _seed_edge(graph, [memory_a, memory_b])
    _set_memory_status(graph, memory_a, "DELETED")
    _audit(graph, memory_a, "SOFT_DELETE")
    sync.tick()

    detail = client.get(f"/api/v1/jkg/edges/{edge['edge_id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["is_valid"] is True
    assert body["valid_source_count"] == 1
    assert body["source_count"] == 2


def test_both_sources_forgotten_invalidates_the_edge(graph, sync):
    memory_a = _insert_memory(graph, "Alice 负责 JarvisMind")
    memory_b = _insert_memory(graph, "Alice works on JarvisMind")
    edge = _seed_edge(graph, [memory_a, memory_b])
    for memory_id in (memory_a, memory_b):
        _set_memory_status(graph, memory_id, "DELETED")
        _audit(graph, memory_id, "SOFT_DELETE")
    sync.tick()
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


# ===========================================================================
# I · 单条失败隔离
# ===========================================================================
def test_one_failing_memory_does_not_stop_the_batch(graph, sync, monkeypatch):
    bad_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    good_id = _insert_memory(graph, "Bob 认识 Alice")
    _audit(graph, bad_id, "CREATE", created_at="2026-01-01T00:00:00")
    _audit(graph, good_id, "CREATE", created_at="2026-01-01T00:00:01")

    real_service = sync.adapter.write_service()

    class _Flaky:
        def sync_memory(self, memory_id):
            if memory_id == bad_id:
                raise RuntimeError("boom for " + bad_id)
            return real_service.sync_memory(memory_id)

    monkeypatch.setattr(
        type(sync.adapter), "write_service", lambda self: _Flaky()
    )
    result = sync.tick()

    assert result["ok"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["memory_id"] == bad_id
    assert good_id in result["processed_memory_ids"]
    assert result["memories_processed"] == 2


def test_a_failure_still_advances_the_cursor(graph, sync, monkeypatch):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    audit_id = _audit(graph, memory_id, "CREATE")

    class _AlwaysFails:
        def sync_memory(self, memory_id):  # noqa: ARG002
            raise RuntimeError("boom")

    monkeypatch.setattr(
        type(sync.adapter), "write_service", lambda self: _AlwaysFails()
    )
    result = sync.tick()
    assert result["ok"] is False
    assert result["cursor_after"]["id"] == audit_id
    assert json.loads(_get_setting(graph, CURSOR_KEY))["id"] == audit_id


def test_the_error_record_never_contains_the_exception_text(graph, sync, monkeypatch):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")

    class _LeakyFails:
        def sync_memory(self, memory_id):  # noqa: ARG002
            raise RuntimeError("secret body " + _CONTENT_MARKER)

    monkeypatch.setattr(
        type(sync.adapter), "write_service", lambda self: _LeakyFails()
    )
    result = sync.tick()
    assert result["ok"] is False
    payload = json.dumps(
        {k: v for k, v in result.items() if k != "processed_memory_ids"},
        ensure_ascii=False,
        default=str,
    )
    # 只允许类型名与脱敏后的消息；正文标记一个字都不能出现。
    assert _CONTENT_MARKER not in payload
    assert result["errors"][0]["error_type"] == "RuntimeError"


def test_a_write_service_failure_is_isolated_and_recorded(graph, sync, monkeypatch):
    """取得写面本身失败（schema 中途消失 / 开关翻转）也必须被隔离。

    ``adapter.write_service()`` 每次调用都重跑一遍 ``_require_ready``，因此它是
    真实的失败点，而不是“已经校验过的对象取一下”。它必须在失败隔离边界之内：
    否则异常会穿透 ``tick`` 的 for 循环，后面的 memory 全部不处理、游标也不推进。
    这里用两条审计行断言“逐条记录 + 游标照常推进”，并且错误项只留类型与代码。
    """
    first_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    second_id = _insert_memory(graph, "Bob 认识 Alice")
    _audit(graph, first_id, "CREATE", created_at="2026-01-01T00:00:00")
    last_audit_id = _audit(
        graph, second_id, "CREATE", created_at="2026-01-01T00:00:01"
    )

    def _vanished(self):  # noqa: ARG001 - 只需签名
        raise RuntimeError("schema vanished mid-tick " + _CONTENT_MARKER)

    monkeypatch.setattr(type(sync.adapter), "write_service", _vanished)
    result = sync.tick()

    assert result["ok"] is False
    assert len(result["errors"]) == 2
    for record in result["errors"]:
        assert set(record) == {"memory_id", "error_type", "error_code"}
        assert record["error_type"] == "RuntimeError"
    assert {record["memory_id"] for record in result["errors"]} == {
        first_id,
        second_id,
    }
    # 异常原文（含正文标记）一个字都不能进报告。
    assert _CONTENT_MARKER not in json.dumps(result, ensure_ascii=False, default=str)

    # 游标仍然推进：既在返回值里，也真的写进了 system_settings。
    assert result["cursor_after"]["id"] == last_audit_id
    assert result["cursor_after"]["created_at"]
    stored = json.loads(_get_setting(graph, CURSOR_KEY))
    assert stored["id"] == last_audit_id
    assert stored["created_at"] == result["cursor_after"]["created_at"]


def test_the_whole_batch_is_not_aborted_by_a_write_service_failure(
    graph, sync, monkeypatch
):
    """``memories_processed`` 必须等于审计里出现的 memory 数量，证明没被第一条打断。"""
    memory_ids = [_insert_memory(graph, f"Alice 负责 JarvisMind {i}") for i in range(3)]
    for index, memory_id in enumerate(memory_ids):
        _audit(graph, memory_id, "CREATE", created_at=f"2026-01-01T00:00:0{index}")

    state = {"calls": 0}

    def _always_fails(self):  # noqa: ARG001 - 只需签名
        state["calls"] += 1
        raise RuntimeError("schema vanished mid-tick")

    monkeypatch.setattr(type(sync.adapter), "write_service", _always_fails)
    result = sync.tick()

    assert result["memories_processed"] == len(memory_ids)
    assert result["processed_memory_ids"] == memory_ids
    # 每一次处理都真的走到写面（而不是第一条就抛穿整个循环）。
    assert state["calls"] == len(memory_ids)
    assert len(result["errors"]) == len(memory_ids)


def test_failure_does_not_abort_the_stale_pass_for_other_memories(
    graph, sync, monkeypatch
):
    bad_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    good_id = _insert_memory(graph, "Bob 认识 Alice")
    edge = _seed_edge(graph, [good_id])
    _audit(graph, bad_id, "UPDATE", created_at="2026-01-01T00:00:00")
    _audit(graph, good_id, "UPDATE", created_at="2026-01-01T00:00:01")

    real_service = sync.adapter.write_service()

    class _Flaky:
        def sync_memory(self, memory_id):
            if memory_id == bad_id:
                raise RuntimeError("boom")
            return real_service.sync_memory(memory_id)

    monkeypatch.setattr(type(sync.adapter), "write_service", lambda self: _Flaky())
    result = sync.tick()
    assert result["marked_stale"] >= 1
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


# ===========================================================================
# J · 游标 / 重叠窗口 / 重放
# ===========================================================================
def test_two_ticks_do_not_reprocess_the_same_entries(graph, sync):
    """第二个 tick 不重复处理：要么读到 0 条，要么只是重叠窗口内的幂等重放。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    first = sync.tick()
    assert first["entries_read"] == 1

    before = _row_counts(graph)
    second = JkgLifecycleSync(graph).tick()
    # 重叠窗口会把刚处理过的行再读一遍，但所有动作幂等：计数不增长、图不变。
    assert second["memories_processed"] <= 1
    assert second["sources_invalidated"] == 0
    assert second["sources_reactivated"] == 0
    assert second["marked_stale"] == 0
    assert _row_counts(graph) == before


def test_a_corrupt_cursor_is_recovered_from_the_beginning(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    with graph.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {"key": CURSOR_KEY, "value": "{not json", "now": utc_now_iso()},
        )

    result = sync.tick()
    assert result["cursor_recovered"] is True
    assert result["entries_read"] == 1
    assert memory_id in result["processed_memory_ids"]


def test_a_cursor_with_the_wrong_shape_is_also_recovered(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    with graph.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {
                "key": CURSOR_KEY,
                "value": json.dumps({"created_at": 1, "id": None}),
                "now": utc_now_iso(),
            },
        )
    result = sync.tick()
    assert result["cursor_recovered"] is True
    assert result["entries_read"] == 1


def test_an_absent_cursor_is_not_reported_as_recovered(graph, sync):
    assert sync.tick()["cursor_recovered"] is False


def test_rebuild_cursor_replays_history(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    audit_id = _audit(graph, memory_id, "CREATE")
    sync.tick()
    # 游标把历史挡在窗口外之后，同一个 id 不再是本批的最后一条。
    assert json.loads(_get_setting(graph, CURSOR_KEY))["id"] == audit_id

    rebuilt = sync.rebuild_cursor()
    assert rebuilt["rebuilt"] is True
    assert json.loads(_get_setting(graph, CURSOR_KEY)) == {
        "version": 1,
        "created_at": "",
        "id": "",
    }
    replay = JkgLifecycleSync(graph).tick()
    assert replay["entries_read"] == 1
    assert memory_id in replay["processed_memory_ids"]


def test_an_entry_inside_the_overlap_window_is_not_missed(graph, sync):
    """早于游标、但落在重叠窗口内的审计行仍然被处理（并发插入不漏）。"""
    anchor = datetime.now().replace(microsecond=0)
    older = (anchor - timedelta(seconds=20)).isoformat()
    cursor_at = (anchor - timedelta(seconds=10)).isoformat()

    memory_old = _insert_memory(graph, "Alice 负责 JarvisMind")
    memory_mid = _insert_memory(graph, "Bob 认识 Alice")
    _audit(graph, memory_old, "CREATE", created_at=older)
    _audit(graph, memory_mid, "CREATE", created_at=cursor_at)

    with graph.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {
                "key": CURSOR_KEY,
                "value": json.dumps(
                    {"version": 1, "created_at": cursor_at, "id": ""}
                ),
                "now": utc_now_iso(),
            },
        )

    result = sync.tick()
    # 游标之前的 old 行落在 60 秒重叠窗口内，因此仍被读到。
    assert result["entries_read"] == 2
    assert memory_old in result["processed_memory_ids"]
    assert memory_mid in result["processed_memory_ids"]
    assert result["overlap_seconds"] == 60


def test_an_entry_outside_the_overlap_window_is_not_replayed(graph, sync):
    anchor = datetime.now().replace(microsecond=0)
    ancient = (anchor - timedelta(seconds=3600)).isoformat()
    cursor_at = anchor.isoformat()

    memory_old = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_old, "CREATE", created_at=ancient)
    with graph.session() as session:
        session.execute(
            _UPSERT_SETTING,
            {
                "key": CURSOR_KEY,
                "value": json.dumps(
                    {"version": 1, "created_at": cursor_at, "id": ""}
                ),
                "now": utc_now_iso(),
            },
        )
    result = sync.tick()
    assert result["entries_read"] == 0
    assert memory_old not in result["processed_memory_ids"]


def test_overlap_seconds_is_reported_in_the_payload(graph, sync):
    assert sync.tick()["overlap_seconds"] == 60


def test_a_memory_appearing_twice_is_processed_once(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE", created_at="2026-01-01T00:00:00")
    _audit(graph, memory_id, "UPDATE", created_at="2026-01-01T00:00:01")
    result = sync.tick()
    assert result["entries_read"] == 2
    assert result["memories_processed"] == 1
    assert result["processed_memory_ids"] == [memory_id]


def test_the_action_set_of_a_memory_is_preserved(graph, sync):
    """同一 memory 在窗口内的多个 action 都要被考虑：UPDATE 不能被 CREATE 掩盖。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "CREATE", created_at="2026-01-01T00:00:00")
    _audit(graph, memory_id, "UPDATE", created_at="2026-01-01T00:00:01")
    sync.tick()
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_limit_is_honoured_and_truncation_is_reported(graph, sync):
    for index in range(3):
        memory_id = _insert_memory(graph, f"Alice 负责 JarvisMind {index}")
        _audit(graph, memory_id, "CREATE", created_at=f"2026-01-01T00:00:0{index}")
    result = sync.tick(limit=2)
    assert result["entries_read"] == 2
    assert result["memories_processed"] == 2
    assert result["truncated"] is True


# ===========================================================================
# J2 · A-1：重叠窗口内突发批量不得让游标永久停摆
# ===========================================================================
# 缺陷：60s 重叠窗口回退时 after_id 被丢弃；窗口内 >= limit 条审计行时，
# 每次 tick 都重读同一页已消费行，cursor_after == cursor_before，尾部行
# 永不入图，且 ok=True / errors=0 全静默。修复：页满且页末 <= 旧游标时，
# 同一 tick 内以旧游标为严格 keyset 继续后读。下列用例全部走真实
# MemoryService.create 写入链（真实 CREATE 审计行），不是手工播种。


def test_burst_inside_overlap_window_advances_cursor_and_consumes_tail(graph, sync):
    """A-1 主回归：60s 内真实写入 250 条（超单页 200），第二次 tick 必须前进。"""
    service = _memory_service(graph)
    memory_ids = [
        service.create(
            MemoryCreate(
                memory_type=MemoryType.SEMANTIC,
                content=f"Alice 负责 JarvisMind 模块 {index}",
            )
        ).id
        for index in range(250)
    ]

    # 前提守卫：250 条真实审计行必须落在同一个 60s 窗口内，否则本用例
    # 咬住的不是「重叠窗口被突发填满」这个形状。
    with graph.session() as session:
        stamps = [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT created_at FROM memory_audit_logs "
                    "ORDER BY created_at ASC, id ASC"
                )
            ).all()
        ]
    assert len(stamps) == 250
    span = datetime.fromisoformat(stamps[-1]) - datetime.fromisoformat(stamps[0])
    assert span.total_seconds() < 60, "突发写入必须落在 60s 重叠窗口内"

    first = sync.tick()
    assert first["entries_read"] == 200
    assert first["truncated"] is True
    cursor_1 = json.loads(_get_setting(graph, CURSOR_KEY))

    second = sync.tick()
    cursor_2 = json.loads(_get_setting(graph, CURSOR_KEY))
    # 第二次 tick 必须越过第一页，而不是永远重读它。
    assert (cursor_2["created_at"], cursor_2["id"]) > (
        cursor_1["created_at"],
        cursor_1["id"],
    )
    assert second["truncated"] is False
    # 200 条重叠重读 + 50 条尾部新行，同一个 tick 内完成。
    assert second["entries_read"] == 250
    assert set(memory_ids[200:]) <= set(second["processed_memory_ids"])
    assert (
        set(first["processed_memory_ids"]) | set(second["processed_memory_ids"])
        == set(memory_ids)
    )

    # 排空后的稳态：满页旧行 + 零新行，既不停摆也不把游标拖回去。
    third = sync.tick()
    cursor_3 = json.loads(_get_setting(graph, CURSOR_KEY))
    assert (cursor_3["created_at"], cursor_3["id"]) == (
        cursor_2["created_at"],
        cursor_2["id"],
    )
    assert third["truncated"] is False
    assert third["ok"] is True


def test_continuation_drains_multiple_pages_in_one_tick(graph, sync):
    """A-1：尾部超过一页时，同一 tick 内连续后读直到排空（小 limit 钉住循环）。"""
    service = _memory_service(graph)
    memory_ids = [
        service.create(
            MemoryCreate(
                memory_type=MemoryType.SEMANTIC,
                content=f"Bob 认识 Alice {index}",
            )
        ).id
        for index in range(5)
    ]

    first = sync.tick(limit=2)
    assert first["entries_read"] == 2
    assert first["truncated"] is True

    second = sync.tick(limit=2)
    # 重叠页（2 条旧行）+ 续读页（2 条 + 1 条）都在同一个 tick 内。
    assert second["entries_read"] == 5
    assert second["truncated"] is False
    assert set(second["processed_memory_ids"]) == set(memory_ids)

    third = sync.tick(limit=2)
    assert third["truncated"] is False
    cursor = json.loads(_get_setting(graph, CURSOR_KEY))
    assert cursor["created_at"] and cursor["id"]


# ===========================================================================
# K · dry_run
# ===========================================================================
def test_dry_run_writes_no_cursor(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    result = sync.tick(dry_run=True)
    assert result["dry_run"] is True
    assert result["wrote"] is False
    assert _get_setting(graph, CURSOR_KEY) is None


def test_dry_run_writes_no_graph_rows(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE")
    before = _row_counts(graph)
    result = sync.tick(dry_run=True)
    assert result["dry_run"] is True
    assert _row_counts(graph) == before
    assert _sources(graph, edge["edge_id"])[0].invalidated_at is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_dry_run_still_reports_what_it_would_do(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    result = sync.tick(dry_run=True)
    assert result["entries_read"] == 1
    assert result["memories_processed"] == 1
    assert memory_id in result["processed_memory_ids"]


def test_dry_run_leaves_the_authority_table_untouched(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    before = _memory_digest(graph)
    sync.tick(dry_run=True)
    assert _memory_digest(graph) == before


# ===========================================================================
# L · 未知 action
# ===========================================================================
def test_an_unknown_action_is_still_synced_and_reported(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "TOTALLY_NEW_ACTION")
    result = sync.tick()
    assert result["unknown_actions"] == ["TOTALLY_NEW_ACTION"]
    assert memory_id in result["processed_memory_ids"]
    assert result["ok"] is True


def test_an_unknown_action_on_a_deleted_memory_still_invalidates(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "DELETED")
    _audit(graph, memory_id, "SOMETHING_ELSE")
    sync.tick()
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


# ===========================================================================
# M · reconcile（游标版）
# ===========================================================================
def test_reconcile_walks_all_memories_in_pages(graph, sync):
    memory_ids = [
        _insert_memory(graph, f"Alice 负责 JarvisMind {index}") for index in range(5)
    ]
    for memory_id in memory_ids:
        _seed_edge(graph, [memory_id])

    seen = []
    after = ""
    for _ in range(3):
        page = sync.reconcile(limit=2, after_memory_id=after)
        assert page["ok"] is True
        seen.extend(
            row
            for row in memory_ids
            if row <= page["next_after_memory_id"]
        )
        after = page["next_after_memory_id"]
        if not page["has_more"]:
            break

    assert after == max(memory_ids)
    assert set(memory_ids) <= set(seen) | {after}


def test_reconcile_reports_has_more_correctly(graph, sync):
    memory_ids = [
        _insert_memory(graph, f"Alice 负责 JarvisMind {index}") for index in range(5)
    ]
    for memory_id in memory_ids:
        _seed_edge(graph, [memory_id])

    first = sync.reconcile(limit=2)
    assert first["has_more"] is True
    assert first["memories_checked"] == 2

    second = sync.reconcile(limit=2, after_memory_id=first["next_after_memory_id"])
    assert second["has_more"] is True

    rest = sync.reconcile(limit=2, after_memory_id=second["next_after_memory_id"])
    assert rest["has_more"] is False


def test_reconcile_next_cursor_advances_monotonically(graph, sync):
    memory_ids = [
        _insert_memory(graph, f"Alice 负责 JarvisMind {index}") for index in range(4)
    ]
    for memory_id in memory_ids:
        _seed_edge(graph, [memory_id])

    after = ""
    seen = []
    while True:
        page = sync.reconcile(limit=1, after_memory_id=after)
        if page["next_after_memory_id"] == after:
            break
        seen.append(page["next_after_memory_id"])
        after = page["next_after_memory_id"]
        if not page["has_more"]:
            break
    assert seen == sorted(seen)
    assert len(seen) == len(memory_ids)


def test_reconcile_isolates_a_failing_memory(graph, sync, monkeypatch):
    memory_ids = [
        _insert_memory(graph, f"Alice 负责 JarvisMind {index}") for index in range(2)
    ]
    for memory_id in memory_ids:
        _seed_edge(graph, [memory_id])
    bad = memory_ids[0]

    real_service = sync.adapter.write_service()

    class _Flaky:
        def sync_memory(self, memory_id):
            if memory_id == bad:
                raise RuntimeError("boom")
            return real_service.sync_memory(memory_id)

    monkeypatch.setattr(type(sync.adapter), "write_service", lambda self: _Flaky())
    result = sync.reconcile(limit=2)
    assert result["ok"] is False
    assert len(result["errors"]) == 1
    assert result["memories_checked"] == 2


def test_reconcile_survives_a_write_service_failure(graph, sync, monkeypatch):
    """reconcile 取写面失败：不抛 500，报 ``ok=False`` + errors，游标仍推进。

    ``write_service()`` 在 sweep 开始后才可能开始失败（schema 被删、开关翻转、
    库短暂不可用）。它被放进每个 memory 的 ``try`` 内，所以失败仍归到具体
    memory id 上，``next_after_memory_id`` 也继续前进——否则运维循环会永远
    卡在同一行上重试。
    """
    memory_ids = [_insert_memory(graph, f"Alice 负责 JarvisMind {i}") for i in range(2)]
    for memory_id in memory_ids:
        _seed_edge(graph, [memory_id])

    def _vanished(self):  # noqa: ARG001 - 只需签名
        raise RuntimeError("schema vanished mid-reconcile " + _CONTENT_MARKER)

    monkeypatch.setattr(type(sync.adapter), "write_service", _vanished)
    result = sync.reconcile(limit=2)

    assert result["ok"] is False
    assert result["errors"]
    assert result["memories_checked"] == len(memory_ids)
    assert {record["memory_id"] for record in result["errors"]} == set(memory_ids)
    for record in result["errors"]:
        assert set(record) == {"memory_id", "error_type", "error_code"}
        assert record["error_type"] == "RuntimeError"
    # 断点仍然前进到本页最后一条，下一次调用不会重试同一行。
    assert result["next_after_memory_id"] == max(memory_ids)
    assert result["next_after_memory_id"] != ""
    assert _CONTENT_MARKER not in json.dumps(result, ensure_ascii=False, default=str)


def test_reconcile_does_not_reuse_the_unpaginated_service_method():
    """``JkgLifecycleSync.reconcile`` 不得转调既有 ``JkgService.reconcile``。"""
    source = open(
        __import__("jarvismind.jkg.lifecycle", fromlist=["x"]).__file__, encoding="utf-8"
    ).read()
    assert ".reconcile(" not in source.split("def reconcile", 1)[1]


def test_reconcile_is_a_noop_when_the_graph_is_disabled(manager):
    _set_enabled(manager, "0")
    result = JkgLifecycleSync(manager).reconcile()
    assert result["skipped"] is True
    assert result["memories_checked"] == 0
    assert _jkg_tables(manager) == []


def test_reconcile_reports_the_resume_point_even_when_empty(graph, sync):
    result = sync.reconcile(limit=10, after_memory_id="zzz")
    assert result["next_after_memory_id"] == "zzz"
    assert result["has_more"] is False


# ===========================================================================
# N · B-3 回归：读时一致
# ===========================================================================
def test_read_side_validity_matches_the_authority_after_a_tick(
    graph, client, auth_headers, sync
):
    live_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    live_edge = _seed_edge(graph, [live_id])
    dead_id = _insert_memory(graph, "Bob 认识 Alice")
    _seed_edge_named(graph, dead_id, "Bob", "Carol")
    _set_memory_status(graph, dead_id, "DELETED")
    _audit(graph, dead_id, "SOFT_DELETE")
    sync.tick()

    detail = client.get(
        f"/api/v1/jkg/edges/{live_edge['edge_id']}", headers=auth_headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["is_valid"] is True

    nodes = client.get("/api/v1/jkg/nodes", headers=auth_headers).json()["data"]
    by_name = {item["name"]: item for item in nodes}
    assert by_name["Alice"]["source_memory_valid"] is True
    assert by_name["Bob"]["source_memory_valid"] is False
    assert by_name["Bob"]["source_memory_status"] == "DELETED"


def test_node_detail_validity_is_consistent_after_an_archive(
    graph, client, auth_headers, sync
):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _set_memory_status(graph, memory_id, "ARCHIVED")
    _audit(graph, memory_id, "ARCHIVE")
    sync.tick()

    nodes = client.get("/api/v1/jkg/nodes", headers=auth_headers).json()["data"]
    for item in nodes:
        assert item["source_memory_valid"] is True
        assert item["source_memory_status"] == "ARCHIVED"


# ===========================================================================
# K · P0-5：归档是 status-only 的 UPDATE，不得当成内容变更
# ===========================================================================
# 缺陷：``CONTENT_CHANGING_ACTIONS`` 只看 action 名（UPDATE / AUTO_UPDATE）。
# 但 JME 的 PATCH 路径在只改 ``status`` 时也写 ``action="UPDATE"``，
# ``detail=\{"fields": ["status"]\}``、``content`` 一字未变。旧逻辑因此：
#   1. ``marked_stale=1``（来源被标 CONTENT_UPDATED_STALE）
#   2. 同一轮的 ``sync_memory`` 又因 ARCHIVED 属 VALID_SOURCE_STATUSES 而复活来源
#   3. 归档白做两轮写入；中途失败则知识凭空消失；运维计数系统性虚高
# 修复后判定依据是 ``detail_json["fields"]`` 里是否真的出现正文字段。
#
# 下面的用例全部走真实的 ``MemoryService.update``，让 JME 自己产出审计行，
# 而不是手写一条 ``detail`` 去迎合被测逻辑（否则修的是测试不是代码）。
def _memory_service(manager) -> MemoryService:
    """A ``MemoryService`` with no embedding/vector backend: DB writes only.

    Passing no provider makes ``_index_record_safely`` return immediately, so
    these tests exercise the audit trail and the lifecycle sync without pulling
    a vector index (or the network) into a deterministic unit test.
    """
    return MemoryService(manager)


def _create_live_memory(manager, content: str = "Alice 负责 JarvisMind") -> str:
    record = _memory_service(manager).create(
        MemoryCreate(memory_type=MemoryType.SEMANTIC, content=content)
    )
    return record.id


def _memory_audit_rows(manager, memory_id: str) -> list:
    """``[(action, detail_json)]`` newest last, straight from the authority table."""
    with manager.session() as session:
        return [
            (str(row[0]), row[1])
            for row in session.execute(
                text(
                    "SELECT action, detail_json FROM memory_audit_logs "
                    "WHERE memory_id = :mid ORDER BY created_at ASC, id ASC"
                ),
                {"mid": memory_id},
            ).all()
        ]


def _memory_content(manager, memory_id: str) -> str:
    with manager.session() as session:
        return str(
            session.execute(
                text("SELECT content FROM memory_records WHERE id = :mid"),
                {"mid": memory_id},
            ).scalar()
        )


def test_a_status_only_archive_audit_row_is_an_update_naming_no_body_field(graph):
    """前提守卫：证明本组用例咬住的确实是「UPDATE 但不含正文字段」这个形状。

    若哪天 JME 改成给归档写 ``ARCHIVE`` 专用 action，这条断言会先失败并指出
    前提消失，而不是让下面的用例悄悄变成同义反复。
    """
    memory_id = _create_live_memory(graph)
    _memory_service(graph).update(
        memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED)
    )

    rows = _memory_audit_rows(graph, memory_id)
    assert [action for action, _ in rows] == ["CREATE", "UPDATE"]
    detail = json.loads(rows[-1][1])
    assert detail["fields"] == ["status"], (
        "归档必须仍是一条只含 status 的 UPDATE，否则本组用例的前提消失"
    )
    assert _memory_content(graph, memory_id) == "Alice 负责 JarvisMind"


def test_archive_does_not_mark_a_valid_source_stale(graph, sync):
    """P0-5 主回归：status-only 的 UPDATE 不得计入 marked_stale。"""
    memory_id = _create_live_memory(graph)
    edge = _seed_edge(graph, [memory_id])

    _memory_service(graph).update(
        memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED)
    )
    result = sync.tick()

    assert result["marked_stale"] == 0, result
    assert result["edges_invalidated"] == 0, result
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is None
    assert source.invalidation_reason is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_archive_reports_zero_invalidations_on_every_counter(graph, sync):
    """归档后四个计数全部为 0：没有任何图写入发生。"""
    memory_id = _create_live_memory(graph)
    _seed_edge(graph, [memory_id])
    _memory_service(graph).update(
        memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED)
    )

    result = sync.tick()

    assert result["marked_stale"] == 0, result
    assert result["sources_invalidated"] == 0, result
    assert result["sources_reactivated"] == 0, result
    assert result["edges_invalidated"] == 0, result
    assert result["edges_reactivated"] == 0, result
    assert result["ok"] is True


def test_the_archive_leaves_the_graph_row_counts_untouched(graph, sync):
    """归档不产生新的图行，也不删行——两次 tick 前后行数完全相同。"""
    memory_id = _create_live_memory(graph)
    _seed_edge(graph, [memory_id])
    before = _row_counts(graph)

    _memory_service(graph).update(
        memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED)
    )
    sync.tick()

    assert _row_counts(graph) == before


def test_archive_then_restore_never_marks_stale(graph, sync):
    """归档 + 恢复都是 status-only：两轮都不应产生 stale。"""
    memory_id = _create_live_memory(graph)
    edge = _seed_edge(graph, [memory_id])
    service = _memory_service(graph)

    service.update(memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED))
    archived = sync.tick()

    service.update(memory_id, MemoryUpdate(status=MemoryStatus.ACTIVE))
    restored = JkgLifecycleSync(graph).tick()

    assert archived["marked_stale"] == 0, archived
    assert restored["marked_stale"] == 0, restored
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is None
    assert _edge_row(graph, edge["edge_id"]).status == "ACTIVE"


def test_a_real_content_update_still_marks_stale(graph, sync):
    """P0-5 反向守卫：正文真的改了，必须照旧判为内容变更。

    与上面的归档用例共用同一条 PATCH 入口，唯一差别是 ``fields`` 含
    ``content``。修复一旦把 GET 方向也堵死（即「永不 stale」），这条会失败。
    """
    memory_id = _create_live_memory(graph)
    edge = _seed_edge(graph, [memory_id])

    _memory_service(graph).update(
        memory_id, MemoryUpdate(content="Bob 负责 JarvisMind")
    )
    result = sync.tick()

    assert result["marked_stale"] == 1, result
    source = _sources(graph, edge["edge_id"])[0]
    assert source.invalidated_at is not None
    assert source.invalidation_reason == STALE_REASON
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_summary_only_update_counts_as_a_derivation_change(graph, sync):
    """``summary`` 也参与 ``search_text`` 与治理扫描，同样要 stale。"""
    memory_id = _create_live_memory(graph)
    _seed_edge(graph, [memory_id])

    _memory_service(graph).update(
        memory_id, MemoryUpdate(summary="Alice owns JarvisMind")
    )
    result = sync.tick()

    assert result["marked_stale"] == 1, result


def test_a_metadata_only_update_still_marks_stale(graph, sync):
    """``metadata_json`` 不在豁免名单：``JkgIngestor._governance_reason`` 会扫它。

    它可以从「可抽取」翻转到「隐私拦截」，所以修复刻意只豁免 ``status``，
    让其它字段保持原有的保守行为。
    """
    memory_id = _create_live_memory(graph)
    edge = _seed_edge(graph, [memory_id])

    _memory_service(graph).update(
        memory_id, MemoryUpdate(metadata={"tag": "ops"})
    )
    result = sync.tick()

    assert result["marked_stale"] == 1, result
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_an_unclassifiable_audit_row_still_marks_stale(graph, sync):
    """缺少 ``fields`` 的 UPDATE 退化到 action 名：保守判为内容变更。

    修正 / 批准链写的就是这种 detail（无 ``fields`` 数组）。绝不能因为证据
    缺失就假定「什么都没改」——那会让一条被改写过的旧事实继续权威。
    """
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"reason": "manual"}))

    result = sync.tick()

    assert result["marked_stale"] == 1, result
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_malformed_detail_json_is_treated_as_unclassifiable(graph, sync):
    """detail_json 不是合法 JSON 时同样退化，而不是报错或当作无变更。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE", detail="{not json")

    result = sync.tick()

    assert result["ok"] is True, result
    assert result["marked_stale"] == 1, result
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_fields_value_that_is_not_an_array_is_unclassifiable(graph, sync):
    """``fields`` 不是数组（畸形 detail）也走保守分支。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": "content"}))

    assert sync.tick()["marked_stale"] == 1


def test_a_mixed_window_still_marks_stale(graph, sync):
    """同一 memory 在同一窗口里既有 status-only 又有正文 UPDATE 时，仍要 stale。

    逐行判定不能退化成「取第一行」或「取最后一行」：只要窗口内任意一行真的
    改了正文，旧派生就必须失效。
    """
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": ["status"]}))
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": ["content"]}))

    result = sync.tick()

    assert result["marked_stale"] == 1, result
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_a_status_only_update_does_not_re_extract_the_memory(graph):
    """归档也不需要重新抽取：正文没变，抽取只会重新推出同一条关系。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    calls: list = []

    class _Recorder:
        def ingest_memory(self, mid):
            calls.append(mid)
            return SimpleNamespace(status="processed", reason="INGESTED")

    sync = JkgLifecycleSync(graph, ingest=_Recorder().ingest_memory)
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": ["status"]}))
    result = sync.tick()

    assert calls == [], calls
    assert result["ingested_processed"] == 0


def test_a_content_update_does_re_extract_the_memory(graph):
    """反面守住上一条：正文变更仍然触发重抽取。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    calls: list = []

    class _Recorder:
        def ingest_memory(self, mid):
            calls.append(mid)
            return SimpleNamespace(status="processed", reason="INGESTED")

    sync = JkgLifecycleSync(graph, ingest=_Recorder().ingest_memory)
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": ["content"]}))
    sync.tick()

    assert calls == [memory_id], calls


def test_the_derivation_probe_never_returns_audit_detail_text(graph):
    """隐私边界：探测只回三元布尔，不把 ``detail_json``（含 before/after 正文）带出去。"""
    memory_id = _create_live_memory(graph, content=_CONTENT_MARKER)
    _memory_service(graph).update(
        memory_id, MemoryUpdate(content="Bob 负责 JarvisMind")
    )

    view = ReadOnlyProjection(graph)
    rows = view.read_audit_derivation_changes(
        after_created_at="", after_id="", limit=100
    )

    assert rows, "探测必须真的读到行，否则这条断言恒真"
    for audit_id, flag in rows:
        assert isinstance(audit_id, str) and audit_id
        assert flag in (True, False, None)
        assert _CONTENT_MARKER not in str(audit_id)
    assert _CONTENT_MARKER not in repr(rows)


def test_the_derivation_probe_classifies_status_as_bookkeeping(graph):
    """直接验证探测本身：status-only 行为 False，正文行为 True，CREATE 为 None。"""
    memory_id = _create_live_memory(graph)
    service = _memory_service(graph)
    service.update(memory_id, MemoryUpdate(status=MemoryStatus.ARCHIVED))
    service.update(memory_id, MemoryUpdate(content="Bob 负责 JarvisMind"))

    view = ReadOnlyProjection(graph)
    rows = dict(
        view.read_audit_derivation_changes(
            after_created_at="", after_id="", limit=100
        )
    )
    actions = _memory_audit_rows(graph, memory_id)

    assert len(rows) == len(actions)
    flags = [rows[audit_id] for audit_id in _audit_ids(graph, memory_id)]
    # CREATE carries no ``fields`` array (it is not an edit) -> unclassifiable;
    # the status-only UPDATE -> False; the body UPDATE -> True.
    assert flags == [None, False, True], flags


def _audit_ids(manager, memory_id: str) -> list:
    with manager.session() as session:
        return [
            str(row[0])
            for row in session.execute(
                text(
                    "SELECT id FROM memory_audit_logs WHERE memory_id = :mid "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"mid": memory_id},
            ).all()
        ]


def test_the_read_only_contract_still_holds_with_the_new_probe(graph):
    """新探测方法名不以写动词开头，``assert_read_only`` 仍为空写方法表。"""
    view = ReadOnlyProjection(graph).for_module("JKG")
    report = view.assert_read_only()

    assert report["read_only"] is True, report
    assert report["write_methods"] == [], report
    assert not "read_audit_derivation_changes".startswith(
        ("write", "save", "create", "update", "delete", "insert")
    )


def test_a_status_and_content_update_together_is_not_exempted(graph, sync):
    """``fields`` 同时含 status 和 content 时必须 stale：豁免只针对纯 status。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    edge = _seed_edge(graph, [memory_id])
    _audit(
        graph,
        memory_id,
        "UPDATE",
        detail=json.dumps({"fields": ["content", "status"]}),
    )

    result = sync.tick()

    assert result["marked_stale"] == 1, result
    assert _edge_row(graph, edge["edge_id"]).status == "INVALID"


def test_an_empty_fields_array_is_unclassifiable(graph, sync):
    """空的 ``fields`` 数组不证明任何事：保守判为 stale。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _audit(graph, memory_id, "UPDATE", detail=json.dumps({"fields": []}))

    assert sync.tick()["marked_stale"] == 1


def test_an_unknown_field_name_is_not_exempted(graph, sync):
    """名单之外的新字段名一律保守处理，避免悄悄颁出新豁免。"""
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _seed_edge(graph, [memory_id])
    _audit(
        graph,
        memory_id,
        "UPDATE",
        detail=json.dumps({"fields": ["status", "some_future_field"]}),
    )

    assert sync.tick()["marked_stale"] == 1


def _seed_edge_named(manager, memory_id: str, left_name: str, right_name: str) -> dict:
    writer = _writer(manager)
    left = writer.upsert_node(
        entity_type="PERSON", name=left_name, source_memory_id=memory_id
    )
    right = writer.upsert_node(
        entity_type="PERSON", name=right_name, source_memory_id=memory_id
    )
    return writer.create_edge(
        subject_id=left["node_id"],
        predicate="KNOWS",
        object_id=right["node_id"],
        source_memory_ids=[memory_id],
    )


# ===========================================================================
# O · B-2 回归：日志脱敏
# ===========================================================================
def test_logs_never_contain_the_memory_body(graph, sync, caplog):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind " + _CONTENT_MARKER)
    _audit(graph, memory_id, "UPDATE")
    with caplog.at_level(logging.DEBUG, logger="jarvismind.jkg.lifecycle"):
        sync.tick()
    assert caplog.records, "tick 必须留下日志，否则这条断言没有意义"
    for record in caplog.records:
        message = record.getMessage()
        assert _CONTENT_MARKER not in message
        assert "负责" not in message


def test_redact_for_log_is_actually_used_by_the_tick(graph, sync, monkeypatch):
    """B-2 的真实生产调用点：日志对象先过 ``redact_for_log``。

    这里把 ``redact_for_log`` 换成一个计数包装，证明它确实在 tick 路径上被调用，
    而不是只写在 import 列表里。
    """
    import jarvismind.jkg.lifecycle as lifecycle_module

    calls = []
    real = lifecycle_module.redact_for_log

    def counting(value, **kwargs):
        calls.append(value)
        return real(value, **kwargs)

    monkeypatch.setattr(lifecycle_module, "redact_for_log", counting)
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "UPDATE")
    sync.tick()
    assert calls, "redact_for_log 必须在 tick 路径上被调用"


def test_logs_carry_the_run_id_and_counters(graph, sync, caplog):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    with caplog.at_level(logging.INFO, logger="jarvismind.jkg.lifecycle"):
        result = sync.tick()
    messages = [record.getMessage() for record in caplog.records]
    assert any("jkg.sync.tick" in message for message in messages)
    assert any(result["run_id"] in message for message in messages)


def test_logs_do_not_contain_the_actor_text(graph, sync, caplog):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE", actor="operator-" + _CONTENT_MARKER)
    with caplog.at_level(logging.DEBUG, logger="jarvismind.jkg.lifecycle"):
        sync.tick()
    for record in caplog.records:
        assert _CONTENT_MARKER not in record.getMessage()


# ===========================================================================
# P · 只写三条 system_settings 键
# ===========================================================================
def test_the_tick_writes_only_the_three_sync_keys(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    with graph.session() as session:
        before = {
            str(row[0])
            for row in session.execute(
                text("SELECT key FROM system_settings")
            ).all()
        }
    sync.tick()
    with graph.session() as session:
        after = {
            str(row[0])
            for row in session.execute(
                text("SELECT key FROM system_settings")
            ).all()
        }
    assert after - before == {CURSOR_KEY, LAST_TICK_KEY, VERSION_KEY}


def test_a_generated_run_id_survives_logging_verbatim(graph, sync, caplog, monkeypatch):
    """JKG-INT-04：我们自己生成的 ``run_id`` 不得被内容脱敏器改写。

    真实缺陷：``redact_for_log`` 的容错规则 ``phone_sep``（``app/core/sensitive.py``）
    没有数字边界守卫，32 位十六进制 ``uuid4().hex`` 里偶发的
    ``1[3-9]\\d{9}`` 子串会被当成手机号抹掉，实测约 0.9%（18/2000）。后果是
    日志里的 ``run_id`` 与 API 响应里的对不上，约 1% 的 tick 无法用 id 关联。

    这里把 ``uuid4`` 钉成一个**确定会触发** ``phone_sep`` 的值，所以断言是确定性
    的，不是概率测试。修复前三条断言全部失败；修复后全部通过。
    """
    _assert_the_pinned_id_is_mangle_prone()
    _pin_generated_id(monkeypatch, _MANGLED_PRONE_ID)

    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    with caplog.at_level(logging.INFO, logger="jarvismind.jkg.lifecycle"):
        result = sync.tick()

    # 1) 响应里的 id 就是钉住的值。
    assert result["run_id"] == _MANGLED_PRONE_ID

    # 2) 日志里必须能找到**完整**的 id，而不是被切成 [REDACTED] 的碎片。
    messages = [record.getMessage() for record in caplog.records]
    assert any("jkg.sync.tick" in message for message in messages), "日志缺失"
    assert any(_MANGLED_PRONE_ID in message for message in messages), (
        "run_id 被内容脱敏器改写了：日志与响应无法用 id 关联"
    )

    # 3) 持久化的 last_tick 也必须存原值。
    stored = json.loads(_get_setting(graph, LAST_TICK_KEY))
    assert stored["run_id"] == result["run_id"] == _MANGLED_PRONE_ID


def test_a_mangled_prone_memory_id_survives_the_error_log(
    graph, sync, caplog, monkeypatch
):
    """``memory_id`` 同样是我们生成的标识符，日志里必须与 ``errors`` 一致。

    修复前：``errors[].memory_id`` 是原值，而 warning 日志里的同一 id 被
    ``phone_sep`` 改写，同一个失败在两处呈现两个不同的 id。
    """
    _assert_the_pinned_id_is_mangle_prone()

    memory_id = _insert_memory(
        graph, "Alice 负责 JarvisMind", memory_id=_MANGLED_PRONE_ID
    )
    _audit(graph, memory_id, "CREATE")

    class _AlwaysFails:
        def sync_memory(self, inner_memory_id):  # noqa: ARG002 - 只需签名
            raise RuntimeError("boom")

    monkeypatch.setattr(type(sync.adapter), "write_service", lambda self: _AlwaysFails())
    with caplog.at_level(logging.WARNING, logger="jarvismind.jkg.lifecycle"):
        result = sync.tick()

    assert result["ok"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["memory_id"] == _MANGLED_PRONE_ID

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any("jkg.sync.memory_failed" in message for message in warnings), (
        "失败必须留下 warning 日志，否则这条断言没有意义"
    )
    assert any(_MANGLED_PRONE_ID in message for message in warnings), (
        "memory_id 被内容脱敏器改写了：日志与 errors 列表指向两个不同的 id"
    )


def test_the_last_tick_record_is_json_and_value_free(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    result = sync.tick()
    stored = json.loads(_get_setting(graph, LAST_TICK_KEY))
    assert stored["run_id"] == result["run_id"]
    assert _CONTENT_MARKER not in json.dumps(stored)


def test_status_reports_the_last_tick_after_a_run(graph, sync):
    memory_id = _insert_memory(graph, "Alice 负责 JarvisMind")
    _audit(graph, memory_id, "CREATE")
    result = sync.tick()
    status = JkgLifecycleSync(graph).status()
    assert status["usable"] is True
    assert status["last_tick"]["run_id"] == result["run_id"]
    assert status["cursor"]["id"] == result["cursor_after"]["id"]


def test_status_creates_no_tables_when_they_are_absent(manager):
    _set_enabled(manager, "1")
    JkgLifecycleSync(manager).status()
    assert _jkg_tables(manager) == []
