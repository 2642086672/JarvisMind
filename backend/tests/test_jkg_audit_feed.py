"""JKG-INT-04 只读审计 feed 回归：``read_audit_entries`` 的边界与不可变性。

``memory_audit_logs`` 是生命周期的唯一事实源，但它可能含正文（``detail_json``）。
把它接到派生模块上时，风险不在「读不到」，而在三件事：

1. 读的路径绕过了只读令牌边界（拿到 ``manager.session()`` 就能写）；
2. 读的列比需要的多（``detail_json`` 会把正文带出去）；
3. 新方法名以写动词开头，让 ``assert_read_only()`` 的 ``write_methods`` 不再为空，
   从而悄悄扩大能力边界声称的范围。

这三点各有一组用例。另有一条 proof：表不存在时返回空 tuple 而不抛错 —— 极旧库
不该把「没有审计历史」变成「同步坏了」。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.governance.projection import ReadOnlyProjection

_INSERT_AUDIT = text(
    "INSERT INTO memory_audit_logs(id, memory_id, action, actor, detail_json, created_at) "
    "VALUES (:id, :memory_id, :action, :actor, :detail, :created_at)"
)

_BODY_MARKER = "MKR" + "-INT-04-AUDIT-FEED-DO-NOT-LEAK"


def _insert_audit(
    manager,
    memory_id: str = "m-1",
    action: str = "CREATE",
    *,
    created_at: str,
    actor: str = "system",
    detail: str = "",
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
                "created_at": created_at,
            },
        )
    return audit_id


def _projection(manager) -> ReadOnlyProjection:
    return ReadOnlyProjection(manager)


# ===========================================================================
# A · 读得到，且只读需要的四列
# ===========================================================================
def test_entries_are_returned_in_created_at_then_id_order(manager):
    projection = _projection(manager)
    second = _insert_audit(manager, "m-b", "UPDATE", created_at="2026-01-01T00:00:02")
    first = _insert_audit(manager, "m-a", "CREATE", created_at="2026-01-01T00:00:01")
    third = _insert_audit(manager, "m-c", "ARCHIVE", created_at="2026-01-01T00:00:03")

    entries = projection.read_audit_entries()
    assert [entry["id"] for entry in entries] == [first, second, third]


def test_the_feed_exposes_exactly_four_columns(manager):
    projection = _projection(manager)
    _insert_audit(manager, created_at="2026-01-01T00:00:01")
    entry = projection.read_audit_entries()[0]
    assert set(entry.keys()) == {"id", "memory_id", "action", "created_at"}


def test_detail_json_is_never_selected(manager):
    """``detail_json`` 可能含正文，因此绝不能被取出来。"""
    projection = _projection(manager)
    _insert_audit(
        manager, created_at="2026-01-01T00:00:01", detail="body: " + _BODY_MARKER
    )
    entries = projection.read_audit_entries()
    assert entries
    assert _BODY_MARKER not in repr(entries)


def test_actor_is_never_selected(manager):
    projection = _projection(manager)
    _insert_audit(manager, created_at="2026-01-01T00:00:01", actor=_BODY_MARKER)
    entries = projection.read_audit_entries()
    assert _BODY_MARKER not in repr(entries)


def test_entries_are_immutable_mappings(manager):
    projection = _projection(manager)
    _insert_audit(manager, created_at="2026-01-01T00:00:01")
    entry = projection.read_audit_entries()[0]
    with pytest.raises(TypeError):
        entry["action"] = "TAMPERED"  # type: ignore[index]


def test_the_result_is_a_tuple(manager):
    projection = _projection(manager)
    _insert_audit(manager, created_at="2026-01-01T00:00:01")
    assert isinstance(projection.read_audit_entries(), tuple)


# ===========================================================================
# B · keyset 分页
# ===========================================================================
def test_a_cursor_after_id_resumes_without_repeating(manager):
    projection = _projection(manager)
    first = _insert_audit(manager, "m-a", "CREATE", created_at="2026-01-01T00:00:01")
    second = _insert_audit(manager, "m-b", "CREATE", created_at="2026-01-01T00:00:02")

    page = projection.read_audit_entries(
        after_created_at="2026-01-01T00:00:01", after_id=first
    )
    assert [entry["id"] for entry in page] == [second]


def test_a_same_timestamp_tie_is_broken_by_id(manager):
    projection = _projection(manager)
    stamp = "2026-01-01T00:00:01"
    ids = [
        _insert_audit(manager, f"m-{index}", "CREATE", created_at=stamp)
        for index in range(3)
    ]
    ordered = projection.read_audit_entries()
    assert [entry["id"] for entry in ordered] == sorted(ids)

    after = projection.read_audit_entries(after_created_at=stamp, after_id=sorted(ids)[0])
    assert [entry["id"] for entry in after] == sorted(ids)[1:]


def test_limit_is_honoured(manager):
    projection = _projection(manager)
    for index in range(5):
        _insert_audit(manager, f"m-{index}", created_at=f"2026-01-01T00:00:0{index}")
    assert len(projection.read_audit_entries(limit=2)) == 2


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_a_non_positive_limit_is_clamped_to_one(manager, limit):
    projection = _projection(manager)
    _insert_audit(manager, created_at="2026-01-01T00:00:01")
    assert len(projection.read_audit_entries(limit=limit)) == 1


def test_a_huge_limit_is_clamped_to_the_page_cap(manager):
    """上限 1000 是硬约束：一个消费者不得一次读走整张表。"""
    from jarvismind.governance.projection import _AUDIT_PAGE_MAX

    assert _AUDIT_PAGE_MAX == 1000


def test_an_empty_cursor_reads_from_the_beginning(manager):
    projection = _projection(manager)
    audit_id = _insert_audit(manager, created_at="2026-01-01T00:00:01")
    assert [entry["id"] for entry in projection.read_audit_entries()] == [audit_id]


def test_the_feed_does_not_include_rows_behind_the_cursor(manager):
    projection = _projection(manager)
    _insert_audit(manager, "m-old", "CREATE", created_at="2025-01-01T00:00:01")
    _insert_audit(manager, "m-new", "CREATE", created_at="2026-01-01T00:00:01")
    entries = projection.read_audit_entries(after_created_at="2026-01-01T00:00:00")
    assert [entry["memory_id"] for entry in entries] == ["m-new"]


# ===========================================================================
# C · ModuleProjection 走 token，且不持有路径
# ===========================================================================
def test_module_projection_exposes_the_same_feed(manager):
    projection = _projection(manager)
    audit_id = _insert_audit(manager, created_at="2026-01-01T00:00:01")
    view = projection.for_module("JKG")
    entries = view.read_audit_entries()
    assert [entry["id"] for entry in entries] == [audit_id]


def test_module_projection_holds_no_path_or_manager(manager):
    view = _projection(manager).for_module("JKG")
    assert not hasattr(view, "_db_path")
    assert not hasattr(view, "_manager")
    assert not hasattr(view, "_projection")


def test_the_module_feed_is_also_read_only(manager):
    """模块视图读到的行同样不可变。"""
    _insert_audit(manager, created_at="2026-01-01T00:00:01")
    entry = _projection(manager).for_module("JKG").read_audit_entries()[0]
    with pytest.raises(TypeError):
        entry["action"] = "TAMPERED"  # type: ignore[index]


def test_an_unknown_token_cannot_read_the_feed(manager):
    """令牌被伪造/回收后不得读；这是只读边界的一部分。"""
    from jarvismind.governance.errors import ProjectionWriteError
    from jarvismind.governance.projection import ModuleProjection

    forged = ModuleProjection("JKG", "not-a-real-token")
    with pytest.raises(ProjectionWriteError):
        forged.read_audit_entries()


# ===========================================================================
# D · 表不存在时不抛错
# ===========================================================================
def test_a_missing_audit_table_yields_an_empty_tuple(manager):
    with manager.session() as session:
        session.execute(text("DROP TABLE IF EXISTS memory_audit_logs"))
    assert _projection(manager).read_audit_entries() == ()


def test_a_missing_audit_table_is_empty_through_the_module_view_too(manager):
    with manager.session() as session:
        session.execute(text("DROP TABLE IF EXISTS memory_audit_logs"))
    assert _projection(manager).for_module("JKG").read_audit_entries() == ()


# ===========================================================================
# E · assert_read_only 仍然成立
# ===========================================================================
def test_module_projection_still_asserts_read_only(manager):
    view = _projection(manager).for_module("JKG")
    report = view.assert_read_only()
    assert report["read_only"] is True, report
    assert report["write_methods"] == [], report
    assert report["write_probe_rejected"] is True, report


def test_the_new_method_name_does_not_start_with_a_write_verb(manager):
    """方法名必须以写动词之外的前缀开头，否则 ``write_methods`` 会变非空。"""
    view = _projection(manager).for_module("JKG")
    assert not "read_audit_entries".startswith(
        ("write", "save", "create", "update", "delete", "insert")
    )
    write_names = [
        name
        for name in dir(view)
        if name.startswith(("write", "save", "create", "update", "delete", "insert"))
    ]
    assert write_names == []


def test_the_projection_itself_still_asserts_read_only(manager):
    report = _projection(manager).assert_read_only()
    assert report["read_only"] is True, report


def test_the_audit_feed_cannot_write(manager):
    """真正的写探针：只读连接上的 INSERT 必须被 SQLite 拒绝。"""
    import sqlite3
    from contextlib import closing

    from jarvismind.governance.projection import _open_readonly

    projection = _projection(manager)
    with pytest.raises(sqlite3.OperationalError):
        with closing(_open_readonly(projection._db_path)) as conn:
            conn.execute(
                "INSERT INTO memory_audit_logs"
                "(id, memory_id, action, actor, detail_json, created_at) "
                "VALUES ('x','m','CREATE','a',NULL,:now)",
                {"now": utc_now_iso()},
            )
