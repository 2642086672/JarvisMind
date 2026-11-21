"""Unit tests for MCE decision conflict resolution (no database)."""
from __future__ import annotations

from jarvismind.mce.conflict_resolver import (
    REASON_CYCLE_IGNORED,
    REASON_EXPLICIT,
    REASON_SAME_KEY,
    REASON_TENTATIVE_CANNOT_SUPERSEDE,
    resolve_conflicts,
)
from jarvismind.mce.models import ContextItem, ContextKind, DecisionStatus


def _decision(
    memory_id: str,
    *,
    key: str | None = None,
    status: DecisionStatus = DecisionStatus.CONFIRMED,
    supersedes: tuple[str, ...] = (),
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> ContextItem:
    return ContextItem(
        memory_id=memory_id,
        kind=ContextKind.DECISION,
        content=f"decision {memory_id}",
        summary=None,
        importance=5.0,
        confidence=0.8,
        created_at=updated_at,
        updated_at=updated_at,
        decision_key=key,
        decision_status=status,
        supersedes=supersedes,
    )


def test_same_key_newer_confirmed_wins():
    old = _decision("old", key="storage", updated_at="2026-01-01T00:00:00+00:00")
    new = _decision("new", key="storage", updated_at="2026-02-01T00:00:00+00:00")

    result = resolve_conflicts([old, new])

    assert [item.memory_id for item in result.confirmed] == ["new"]
    assert [item.memory_id for item in result.superseded] == ["old"]
    assert result.superseded[0].superseded_by == "new"
    assert result.superseded[0].injected is False
    assert REASON_SAME_KEY in result.reasons


def test_independent_keys_are_not_suppressed():
    left = _decision("left", key="storage")
    right = _decision("right", key="ui")

    result = resolve_conflicts([left, right])

    assert {item.memory_id for item in result.confirmed} == {"left", "right"}
    assert result.superseded == ()


def test_unkeyed_decisions_are_conservatively_kept():
    left = _decision("left")
    right = _decision("right")

    result = resolve_conflicts([left, right])

    assert {item.memory_id for item in result.confirmed} == {"left", "right"}


def test_explicit_supersedes_edge_wins():
    old = _decision("old", updated_at="2026-01-01T00:00:00+00:00")
    new = _decision(
        "new",
        supersedes=("old",),
        updated_at="2026-02-01T00:00:00+00:00",
    )

    result = resolve_conflicts([old, new])

    assert [item.memory_id for item in result.confirmed] == ["new"]
    assert result.superseded[0].superseded_by == "new"
    assert result.superseded[0].decision_reasons == (REASON_EXPLICIT,)


def test_tentative_cannot_suppress_confirmed():
    confirmed = _decision("confirmed", key="a")
    tentative = _decision(
        "tentative",
        key="b",
        status=DecisionStatus.TENTATIVE,
        supersedes=("confirmed",),
    )

    result = resolve_conflicts([confirmed, tentative])

    assert [item.memory_id for item in result.confirmed] == ["confirmed"]
    assert [item.memory_id for item in result.tentative] == ["tentative"]
    assert REASON_TENTATIVE_CANNOT_SUPERSEDE in result.reasons


def test_declared_superseded_and_deprecated_are_not_confirmed():
    superseded = _decision("superseded", status=DecisionStatus.SUPERSEDED)
    deprecated = _decision("deprecated", status=DecisionStatus.DEPRECATED)

    result = resolve_conflicts([superseded, deprecated])

    assert result.confirmed == ()
    assert {item.memory_id for item in result.superseded} == {
        "superseded",
        "deprecated",
    }


def test_cycle_is_ignored_and_does_not_hang():
    a = _decision("a", key="a", supersedes=("b",))
    b = _decision("b", key="b", supersedes=("a",))

    result = resolve_conflicts([a, b])

    # Dropping the cyclic edges is the conservative answer: both rows remain
    # visible, and the caller can see why.
    assert {item.memory_id for item in result.confirmed} == {"a", "b"}
    assert REASON_CYCLE_IGNORED in result.reasons


def test_duplicate_ids_are_collapsed():
    first = _decision("same", key="a")
    second = _decision("same", key="a")

    result = resolve_conflicts([first, second])

    assert len(result.confirmed) + len(result.tentative) + len(result.superseded) == 1


def test_unknown_status_is_treated_as_tentative():
    unknown = _decision("unknown", status=DecisionStatus.UNKNOWN)

    result = resolve_conflicts([unknown])

    assert result.confirmed == ()
    assert [item.memory_id for item in result.tentative] == ["unknown"]
    assert result.tentative[0].decision_status is DecisionStatus.TENTATIVE
