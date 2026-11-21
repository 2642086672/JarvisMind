"""Deterministic decision conflict resolution for MCE-01.

Only two facts may suppress a decision:

1. an **explicit** ``supersedes_memory_id`` (or ``supersedes`` / ``replaces``)
   edge from a *confirmed* decision to another decision in the same project;
2. two **confirmed** decisions that share the same ``decision_key`` — the newer
   one wins.

Nothing else is inferred. Two independent decisions with no key stay
independent, even if their wording looks similar. That conservative rule is
deliberate: a false suppression silently removes a valid conclusion from every
future session, while a missed suppression only causes the model to see both
dates and can be corrected by a later confirmed decision.

Superseded rows are retained in the result (for de-duplication and to prevent
an old decision from being "revived" by a stale client), but they are never
rendered into the injectable context. Every suppression carries a machine
readable reason so an audit can explain it without reading memory content.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import ContextItem, DecisionStatus

REASON_EXPLICIT = "explicit_supersedes"
REASON_SAME_KEY = "newer_confirmed_same_key"
REASON_DECLARED_SUPERSEDED = "declared_superseded"
REASON_DECLARED_DEPRECATED = "declared_deprecated"
REASON_CYCLE_IGNORED = "supersede_cycle_ignored"
REASON_TENTATIVE_CANNOT_SUPERSEDE = "tentative_cannot_supersede"
REASON_UNKNOWN_STATUS = "unknown_status_treated_as_tentative"


@dataclass(frozen=True)
class ConflictResolution:
    confirmed: Tuple[ContextItem, ...] = ()
    tentative: Tuple[ContextItem, ...] = ()
    superseded: Tuple[ContextItem, ...] = ()
    reasons: Tuple[str, ...] = ()


def _timestamp(item: ContextItem) -> str:
    return item.updated_at or item.created_at or ""


def _newest_first(item: ContextItem) -> tuple:
    return (_timestamp(item), item.memory_id)


def _dedupe(items: Iterable[ContextItem]) -> List[ContextItem]:
    seen: Set[str] = set()
    unique: List[ContextItem] = []
    for item in items:
        if item.memory_id in seen:
            continue
        seen.add(item.memory_id)
        unique.append(item)
    return unique


def _cycles(edges: Dict[str, Set[str]]) -> Set[str]:
    """Return every node inside a directed cycle (iterative Tarjan SCC).

    Small graphs only (a project has tens of decisions, not millions). Tarjan
    is used instead of "indegree zero" peeling because peeling also leaves the
    downstream nodes of a cycle behind; marking those as cyclic would drop a
    legitimate decision. Only a strongly connected component of size > 1 (or a
    self-loop) is a real cycle.
    """
    nodes = set(edges)
    for targets in edges.values():
        nodes.update(targets)
    index = 0
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    cyclic: Set[str] = set()

    for start in sorted(nodes):
        if start in indices:
            continue
        call_stack: List[Tuple[str, int]] = [(start, 0)]
        while call_stack:
            node, offset = call_stack[-1]
            if offset == 0 and node not in indices:
                indices[node] = index
                low[node] = index
                index += 1
                stack.append(node)
                on_stack.add(node)
            neighbours = sorted(edges.get(node, ()))
            descended = False
            while offset < len(neighbours):
                neighbour = neighbours[offset]
                offset += 1
                if neighbour not in indices:
                    call_stack[-1] = (node, offset)
                    call_stack.append((neighbour, 0))
                    descended = True
                    break
                if neighbour in on_stack:
                    low[node] = min(low[node], indices[neighbour])
            if descended:
                continue
            if low[node] == indices[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in edges.get(node, ()):
                    cyclic.update(component)
            call_stack.pop()
            if call_stack:
                parent = call_stack[-1][0]
                low[parent] = min(low[parent], low[node])

    return cyclic


def _normalise_status(item: ContextItem) -> DecisionStatus:
    return item.decision_status or DecisionStatus.UNKNOWN


def resolve_conflicts(decisions: Sequence[ContextItem]) -> ConflictResolution:
    """Resolve confirmed / tentative / superseded decisions deterministically."""
    unique = _dedupe(decisions)
    if not unique:
        return ConflictResolution()

    by_id = {item.memory_id: item for item in unique}
    reasons: Set[str] = set()

    # Explicit edges are only accepted from a confirmed source. A tentative
    # proposal cannot silently retire an already-confirmed decision.
    edges: Dict[str, Set[str]] = {}
    for item in unique:
        if _normalise_status(item) is not DecisionStatus.CONFIRMED:
            if item.supersedes:
                reasons.add(REASON_TENTATIVE_CANNOT_SUPERSEDE)
            continue
        for target in item.supersedes:
            if target in by_id and target != item.memory_id:
                edges.setdefault(item.memory_id, set()).add(target)

    cycle_nodes = _cycles(edges)
    if cycle_nodes:
        reasons.add(REASON_CYCLE_IGNORED)
        for node in list(edges):
            if node in cycle_nodes:
                edges.pop(node, None)

    # target id -> (source id, reason)
    suppressed: Dict[str, Tuple[str, str]] = {}
    for source, targets in edges.items():
        for target in targets:
            current = suppressed.get(target)
            if current is None or _newest_first(by_id[source]) > _newest_first(
                by_id[current[0]]
            ):
                suppressed[target] = (source, REASON_EXPLICIT)

    # Declared state and same-key conflicts.
    for item in unique:
        status = _normalise_status(item)
        if status is DecisionStatus.SUPERSEDED:
            suppressed.setdefault(item.memory_id, ("", REASON_DECLARED_SUPERSEDED))
        elif status is DecisionStatus.DEPRECATED:
            suppressed.setdefault(item.memory_id, ("", REASON_DECLARED_DEPRECATED))

    confirmed_candidates = [
        item
        for item in unique
        if _normalise_status(item) is DecisionStatus.CONFIRMED
        and item.memory_id not in suppressed
    ]
    by_key: Dict[str, List[ContextItem]] = {}
    for item in confirmed_candidates:
        if item.decision_key:
            by_key.setdefault(item.decision_key, []).append(item)
    for key, group in by_key.items():
        ordered = sorted(group, key=_newest_first, reverse=True)
        winner = ordered[0]
        for loser in ordered[1:]:
            suppressed[loser.memory_id] = (winner.memory_id, REASON_SAME_KEY)
            reasons.add(REASON_SAME_KEY)
        # A tentative row under the same key is not a current conclusion once
        # a confirmed decision exists; keep it for traceability, not injection.
        for item in unique:
            if (
                item.decision_key == key
                and _normalise_status(item) is DecisionStatus.TENTATIVE
                and item.memory_id not in suppressed
            ):
                suppressed[item.memory_id] = (winner.memory_id, REASON_SAME_KEY)

    confirmed: List[ContextItem] = []
    tentative: List[ContextItem] = []
    superseded: List[ContextItem] = []
    for item in unique:
        status = _normalise_status(item)
        suppression = suppressed.get(item.memory_id)
        if suppression is not None:
            source, reason = suppression
            superseded.append(
                replace(
                    item,
                    decision_status=(
                        DecisionStatus.SUPERSEDED
                        if status is not DecisionStatus.DEPRECATED
                        else DecisionStatus.DEPRECATED
                    ),
                    superseded_by=source or item.superseded_by,
                    decision_reasons=tuple(sorted(set(item.decision_reasons + (reason,)))),
                    injected=False,
                )
            )
            if source:
                reasons.add(reason)
            continue
        if status is DecisionStatus.CONFIRMED:
            confirmed.append(
                replace(
                    item,
                    decision_status=DecisionStatus.CONFIRMED,
                    injected=True,
                )
            )
        elif status is DecisionStatus.TENTATIVE:
            tentative.append(
                replace(
                    item,
                    decision_status=DecisionStatus.TENTATIVE,
                    injected=True,
                )
            )
        else:
            # Unknown rows are shown as tentative. They must never be mistaken
            # for a settled decision by a downstream renderer.
            reasons.add(REASON_UNKNOWN_STATUS)
            tentative.append(
                replace(
                    item,
                    decision_status=DecisionStatus.TENTATIVE,
                    decision_reasons=tuple(
                        sorted(set(item.decision_reasons + (REASON_UNKNOWN_STATUS,)))
                    ),
                    injected=True,
                )
            )

    confirmed.sort(key=_newest_first, reverse=True)
    tentative.sort(key=_newest_first, reverse=True)
    superseded.sort(key=_newest_first, reverse=True)
    return ConflictResolution(
        confirmed=tuple(confirmed),
        tentative=tuple(tentative),
        superseded=tuple(superseded),
        reasons=tuple(sorted(reasons)),
    )


__all__ = [
    "ConflictResolution",
    "REASON_CYCLE_IGNORED",
    "REASON_DECLARED_DEPRECATED",
    "REASON_DECLARED_SUPERSEDED",
    "REASON_EXPLICIT",
    "REASON_SAME_KEY",
    "REASON_TENTATIVE_CANNOT_SUPERSEDE",
    "REASON_UNKNOWN_STATUS",
    "resolve_conflicts",
]
