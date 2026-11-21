"""ModuleProjection keyset scan / count (JKG-INT-05 / A1).

Every row lives in the shared ``manager`` fixture's temporary database. Rows are
inserted through the real manager session; no production database and no
direct sqlite3 connection is used.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from jarvismind.governance.projection import ReadOnlyProjection

_INSERT = text(
    "INSERT INTO memory_records("
    "id, memory_type, content, importance, confidence, status, created_at, updated_at"
    ") VALUES ("
    ":id, 'SEMANTIC', :content, 5.0, 0.9, :status, :created, :created"
    ")"
)


def _insert(manager, memory_id: str, created: str, status: str = "ACTIVE", content: str = "c") -> None:
    with manager.session() as session:
        session.execute(
            _INSERT,
            {"id": memory_id, "content": content, "status": status, "created": created},
        )


def _projection(manager):
    return ReadOnlyProjection(manager).for_module("JKG")


def _ids(rows) -> list:
    return [row["id"] for row in rows]


@pytest.fixture()
def seeded(manager):
    """Five rows; two share a timestamp to exercise the id tie-break."""
    _insert(manager, "m-03", "2026-01-03T00:00:00+00:00")
    _insert(manager, "m-01", "2026-01-01T00:00:00+00:00")
    _insert(manager, "m-02b", "2026-01-02T00:00:00+00:00")
    _insert(manager, "m-02a", "2026-01-02T00:00:00+00:00")
    _insert(manager, "m-04", "2026-01-04T00:00:00+00:00", status="ARCHIVED")
    return manager


# ---------------------------------------------------------------- pagination


def test_scan_orders_by_created_at_then_id(seeded):
    rows = _projection(seeded).scan_memories(limit=10)
    assert _ids(rows) == ["m-01", "m-02a", "m-02b", "m-03", "m-04"]


def test_keyset_pagination_visits_every_row_exactly_once(seeded):
    projection = _projection(seeded)
    collected = []
    cursor_created, cursor_id = "", ""
    for _ in range(10):
        page = projection.scan_memories(
            after_created_at=cursor_created, after_id=cursor_id, limit=2
        )
        if not page:
            break
        collected.extend(_ids(page))
        cursor_created, cursor_id = page[-1]["created_at"], page[-1]["id"]
    assert collected == ["m-01", "m-02a", "m-02b", "m-03", "m-04"]
    assert len(collected) == len(set(collected))


def test_keyset_tie_break_uses_id_within_same_timestamp(seeded):
    rows = _projection(seeded).scan_memories(
        after_created_at="2026-01-02T00:00:00+00:00", after_id="m-02a", limit=10
    )
    assert _ids(rows) == ["m-02b", "m-03", "m-04"]


# ------------------------------------------------------------------ boundary


def test_created_at_lte_bounds_the_snapshot(seeded):
    projection = _projection(seeded)
    bounded = projection.scan_memories(
        created_at_lte="2026-01-02T00:00:00+00:00", limit=10
    )
    assert _ids(bounded) == ["m-01", "m-02a", "m-02b"]
    assert projection.count_memories(created_at_lte="2026-01-02T00:00:00+00:00") == 3


def test_rows_inserted_after_snapshot_are_excluded(seeded):
    projection = _projection(seeded)
    snapshot = "2026-01-04T00:00:00+00:00"
    _insert(seeded, "m-late", "2026-01-09T00:00:00+00:00")
    assert "m-late" not in _ids(
        projection.scan_memories(created_at_lte=snapshot, limit=50)
    )
    assert projection.count_memories(created_at_lte=snapshot) == 5
    assert projection.count_memories() == 6


# ------------------------------------------------------------ status filter


def test_default_statuses_are_active_and_archived(manager):
    _insert(manager, "a-active", "2026-02-01T00:00:00+00:00", status="ACTIVE")
    _insert(manager, "a-archived", "2026-02-02T00:00:00+00:00", status="ARCHIVED")
    _insert(manager, "a-deleted", "2026-02-03T00:00:00+00:00", status="DELETED")
    _insert(manager, "a-merged", "2026-02-04T00:00:00+00:00", status="MERGED")
    projection = _projection(manager)
    assert _ids(projection.scan_memories(limit=50)) == ["a-active", "a-archived"]
    assert projection.count_memories() == 2


def test_explicit_status_filter_selects_only_requested_state(manager):
    _insert(manager, "s-active", "2026-02-01T00:00:00+00:00", status="ACTIVE")
    _insert(manager, "s-deleted", "2026-02-02T00:00:00+00:00", status="DELETED")
    projection = _projection(manager)
    assert _ids(projection.scan_memories(statuses=("DELETED",), limit=50)) == ["s-deleted"]
    assert projection.count_memories(statuses=("DELETED",)) == 1
    assert projection.count_memories(statuses=("ACTIVE", "DELETED")) == 2


# ------------------------------------------------------------- empty result


def test_empty_table_returns_empty_tuple_and_zero(manager):
    projection = _projection(manager)
    assert projection.scan_memories(limit=10) == ()
    assert projection.count_memories() == 0


def test_cursor_past_the_end_returns_empty(seeded):
    rows = _projection(seeded).scan_memories(
        after_created_at="2099-01-01T00:00:00+00:00", after_id="", limit=10
    )
    assert rows == ()


# ----------------------------------------------------------- limit clamping


def test_limit_below_one_is_clamped_to_one(seeded):
    assert len(_projection(seeded).scan_memories(limit=0)) == 1
    assert len(_projection(seeded).scan_memories(limit=-5)) == 1


def test_limit_above_cap_is_clamped_to_one_thousand(seeded):
    rows = _projection(seeded).scan_memories(limit=50_000)
    assert len(rows) == 5  # fewer rows than the cap; the cap is applied, not an error
    for index in range(1005):
        _insert(seeded, f"bulk-{index:05d}", f"2026-03-01T00:00:{index % 60:02d}+00:00")
    assert len(_projection(seeded).scan_memories(limit=50_000)) == 1000


# ------------------------------------------------- parameterisation / safety


@pytest.mark.parametrize(
    "statuses",
    [
        ("ACTIVE' OR '1'='1",),
        ("ACTIVE", "ARCHIVED", "DELETED"),
        (),
        "ACTIVE",
        ("unknown",),
    ],
)
def test_invalid_status_arguments_are_rejected(manager, statuses):
    projection = _projection(manager)
    with pytest.raises(ValueError):
        projection.scan_memories(statuses=statuses)
    with pytest.raises(ValueError):
        projection.count_memories(statuses=statuses)


def test_sql_metacharacters_in_cursor_are_bound_not_spliced(seeded):
    hostile = "x' OR 1=1 --"
    projection = _projection(seeded)
    # Bound as plain strings: the injected OR clause never becomes SQL, so the
    # row set is exactly what the keyset comparison dictates.
    assert _ids(
        projection.scan_memories(
            after_created_at="2026-01-02T00:00:00+00:00", after_id=hostile, limit=50
        )
    ) == ["m-03", "m-04"]
    assert projection.scan_memories(after_created_at=hostile, limit=50) == ()
    assert projection.count_memories(statuses=("ACTIVE",)) == 4


def test_content_with_quotes_is_returned_intact(manager):
    body = "it's \"quoted\" ; DROP TABLE memory_records; --"
    _insert(manager, "quoted", "2026-04-01T00:00:00+00:00", content=body)
    rows = _projection(manager).scan_memories(limit=10)
    assert rows[0]["content"] == body


# -------------------------------------------------------- read-only contract


def test_scan_and_count_are_not_write_methods(manager):
    report = _projection(manager).assert_read_only()
    assert report["write_methods"] == []
    assert report["write_probe_rejected"] is True
    assert report["read_only"] is True
    assert "scan_memories" not in report["write_methods"]
    assert "count_memories" not in report["write_methods"]


def test_scanned_rows_are_immutable(seeded):
    row = _projection(seeded).scan_memories(limit=1)[0]
    with pytest.raises(TypeError):
        row["content"] = "changed"


def test_existing_read_methods_still_work_after_extension(seeded):
    projection = _projection(seeded)
    assert projection.read_memory("m-01")["id"] == "m-01"
    assert projection.read_memory("missing") is None
    assert projection.read_memories(status="ACTIVE").total == 4
