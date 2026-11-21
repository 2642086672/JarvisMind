"""Extraction quality metrics (JKG-EXTRACTION-QUALITY / P1-2).

Purpose
-------
Turn a labelled corpus (``tests/_fixtures/jkg_extraction_eval.py``) into numbers
that a review can act on: precision, recall, coverage, duplicate rate, invalid
relation rate and — the one this module exists for — a **standalone count of
cases where a must-not-extract body produced a candidate**.

Precision-first design
----------------------
The metrics are deliberately asymmetric:

* An extra candidate lowers ``entity_precision`` / ``relation_precision``.
* A missing candidate lowers ``entity_recall`` / ``relation_recall``.
* A candidate produced from a body labelled ``must_not_extract`` counts in
  ``false_positive_cases``. That number is reported on its own so a test can
  assert ``== 0`` without having to reason about weighted rates: a 95% precision
  still means "we invented facts in one body out of twenty".

Rationale: a wrong node in a personal knowledge graph is permanent and merges
into a real entity's neighbourhood, while a missing node is an absence the
retry path can still fill. Trading recall for precision is the intended
direction, so nothing here rewards recall at precision's expense.

Privacy
-------
``case_id`` and counts only. A report never carries a memory body, a captured
name, or a matched value. Reason codes are the extractor's stable codes, which
are already designed not to echo text. This is why ``EvalCaseResult`` stores
no strings from the input beyond the identifier the *labeller* chose.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .extraction import RULE_VERSION, extract_candidates


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalCaseResult:
    """Per-case outcome. Counts only — no captured text, ever.

    ``missed_*`` means "the label expected it, the extractor did not produce
    it". ``spurious_*`` means "the extractor produced it, the label did not
    want it". ``duplicate_names`` counts names emitted more than once in this
    case (impossible for the current single-pass extractor, which de-duplicates
    by ``(type, key)``; the metric exists so a future multi-pass change cannot
    regress it silently).
    """

    case_id: str
    category: str
    eligible: bool
    expected_eligible: bool
    false_positive: bool
    expected_nodes: int = 0
    produced_nodes: int = 0
    #: Raw emitted node objects, before de-duplication. The denominator of
    #: ``duplicate_entity_rate``: a duplicate is an emission that repeats a name
    #: already emitted, so the honest base is what was emitted, not what survived
    #: the de-duplication.
    emitted_nodes: int = 0
    matched_nodes: int = 0
    missed_nodes: int = 0
    spurious_nodes: int = 0
    expected_edges: int = 0
    produced_edges: int = 0
    matched_edges: int = 0
    missed_edges: int = 0
    spurious_edges: int = 0
    duplicate_names: int = 0
    invalid_edges: int = 0
    reason_codes: Tuple[str, ...] = ()
    reason_ok: bool = True

    @property
    def node_ok(self) -> bool:
        return self.missed_nodes == 0 and self.spurious_nodes == 0

    @property
    def edge_ok(self) -> bool:
        return self.missed_edges == 0 and self.spurious_edges == 0

    @property
    def passed(self) -> bool:
        return (
            self.node_ok
            and self.edge_ok
            and self.eligible == self.expected_eligible
            and self.reason_ok
            and self.duplicate_names == 0
            and self.invalid_edges == 0
        )

    def as_dict(self) -> Dict[str, Any]:
        """Report row. Deliberately excludes body and every captured name."""
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "false_positive": self.false_positive,
            "eligible": self.eligible,
            "expected_eligible": self.expected_eligible,
            "nodes": {
                "expected": self.expected_nodes,
                "produced": self.produced_nodes,
                "matched": self.matched_nodes,
                "missed": self.missed_nodes,
                "spurious": self.spurious_nodes,
            },
            "edges": {
                "expected": self.expected_edges,
                "produced": self.produced_edges,
                "matched": self.matched_edges,
                "missed": self.missed_edges,
                "spurious": self.spurious_edges,
            },
            "duplicate_names": self.duplicate_names,
            "invalid_edges": self.invalid_edges,
            "reason_codes": list(self.reason_codes),
            "reason_ok": self.reason_ok,
        }


@dataclass(frozen=True)
class CategoryReport:
    """Aggregate for one labelled category."""

    category: str
    cases: int
    expected_nodes: int
    produced_nodes: int
    matched_nodes: int
    expected_edges: int
    produced_edges: int
    matched_edges: int
    cases_with_output: int
    false_positive_cases: int

    @property
    def entity_precision(self) -> Optional[float]:
        return _ratio(self.matched_nodes, self.produced_nodes)

    @property
    def entity_recall(self) -> Optional[float]:
        return _ratio(self.matched_nodes, self.expected_nodes)

    @property
    def relation_precision(self) -> Optional[float]:
        return _ratio(self.matched_edges, self.produced_edges)

    @property
    def relation_recall(self) -> Optional[float]:
        return _ratio(self.matched_edges, self.expected_edges)


@dataclass(frozen=True)
class EvalReport:
    """Whole-corpus metrics.

    One instance is the *evidence* for a rule change. It is comparable across
    rule versions only when ``rule_version`` matches; a version bump with no
    report change is exactly the failure mode the P1-3 gate exists to prevent.
    """

    rule_version: str
    cases: int

    matched_nodes: int = 0
    produced_nodes: int = 0
    expected_nodes: int = 0
    matched_edges: int = 0
    produced_edges: int = 0
    expected_edges: int = 0

    cases_with_output: int = 0
    #: Cases labelled ``must_not_extract`` that produced at least one candidate.
    #: Asserted ``== 0`` by the regression test; it is a count, never a rate.
    false_positive_cases: int = 0
    #: Cases whose body the privacy gate must refuse and did.
    governance_rejections: int = 0
    #: Cases the privacy gate must refuse and did *not*.
    governance_leaks: int = 0

    duplicate_entities: int = 0
    total_entities: int = 0
    invalid_relations: int = 0
    total_relations: int = 0

    details: Tuple[EvalCaseResult, ...] = ()

    # -- headline rates ----------------------------------------------------
    @property
    def entity_precision(self) -> Optional[float]:
        return _ratio(self.matched_nodes, self.produced_nodes)

    @property
    def entity_recall(self) -> Optional[float]:
        return _ratio(self.matched_nodes, self.expected_nodes)

    @property
    def entity_f1(self) -> Optional[float]:
        return _f1(self.entity_precision, self.entity_recall)

    @property
    def relation_precision(self) -> Optional[float]:
        return _ratio(self.matched_edges, self.produced_edges)

    @property
    def relation_recall(self) -> Optional[float]:
        return _ratio(self.matched_edges, self.expected_edges)

    @property
    def relation_f1(self) -> Optional[float]:
        return _f1(self.relation_precision, self.relation_recall)

    @property
    def coverage(self) -> Optional[float]:
        """Share of cases that produced at least one expected candidate.

        A case whose label is "extract nothing" cannot be *covered*; counting it
        as covered would let a corpus of noise cases report 100% coverage. The
        denominator is therefore the number of cases with a non-empty label,
        which is the only population coverage can honestly be computed over.
        """
        return _ratio(self.cases_with_output, self.expectant_cases)

    @property
    def expectant_cases(self) -> int:
        """Cases whose label is non-empty (i.e. coverage is defined for them)."""
        return sum(
            1
            for detail in self.details
            if detail.expected_nodes or detail.expected_edges
        )

    @property
    def duplicate_entity_rate(self) -> Optional[float]:
        return _ratio(self.duplicate_entities, self.total_entities)

    @property
    def invalid_relation_rate(self) -> Optional[float]:
        return _ratio(self.invalid_relations, self.total_relations)

    @property
    def governance_rejection_rate(self) -> Optional[float]:
        """Share of must-refuse cases that were actually refused.

        The denominator is the cases the label says must be refused, not every
        case in the corpus: dividing by all cases would report 8% for a gate
        that works perfectly, which reads as a failure.
        """
        return _ratio(self.governance_rejections, self.governance_labeled)

    @property
    def governance_labeled(self) -> int:
        return sum(1 for detail in self.details if not detail.expected_eligible)

    # -- groupings ---------------------------------------------------------
    def by_category(self) -> Dict[str, CategoryReport]:
        return _category_reports(self.details)

    def failures(self) -> Tuple[EvalCaseResult, ...]:
        return tuple(detail for detail in self.details if not detail.passed)

    def false_positive_case_ids(self) -> Tuple[str, ...]:
        return tuple(detail.case_id for detail in self.details if detail.false_positive)

    def reason_histogram(self) -> Dict[str, int]:
        counts: Counter = Counter()
        for detail in self.details:
            for code in detail.reason_codes:
                counts[code] += 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe report. No body text and no captured name appears here."""
        return {
            "rule_version": self.rule_version,
            "cases": self.cases,
            "entity": {
                "precision": self.entity_precision,
                "recall": self.entity_recall,
                "f1": self.entity_f1,
                "matched": self.matched_nodes,
                "produced": self.produced_nodes,
                "expected": self.expected_nodes,
            },
            "relation": {
                "precision": self.relation_precision,
                "recall": self.relation_recall,
                "f1": self.relation_f1,
                "matched": self.matched_edges,
                "produced": self.produced_edges,
                "expected": self.expected_edges,
            },
            "coverage": self.coverage,
            "expectant_cases": self.expectant_cases,
            "cases_with_output": self.cases_with_output,
            "false_positive_cases": self.false_positive_cases,
            "false_positive_case_ids": list(self.false_positive_case_ids()),
            "governance_rejection_rate": self.governance_rejection_rate,
            "governance_rejections": self.governance_rejections,
            "governance_leaks": self.governance_leaks,
            "duplicate_entity_rate": self.duplicate_entity_rate,
            "duplicate_entities": self.duplicate_entities,
            "total_entities": self.total_entities,
            "invalid_relation_rate": self.invalid_relation_rate,
            "invalid_relations": self.invalid_relations,
            "total_relations": self.total_relations,
            "reason_histogram": self.reason_histogram(),
            "categories": {
                name: {
                    "cases": report.cases,
                    "entity_precision": report.entity_precision,
                    "entity_recall": report.entity_recall,
                    "relation_precision": report.relation_precision,
                    "relation_recall": report.relation_recall,
                    "cases_with_output": report.cases_with_output,
                    "false_positive_cases": report.false_positive_cases,
                }
                for name, report in self.by_category().items()
            },
            "details": [detail.as_dict() for detail in self.details],
        }


# ---------------------------------------------------------------------------
# Arithmetic helpers
#
# ``None`` rather than 0.0 when the denominator is empty. A precision of 0%
# means "everything we produced was wrong"; "we produced nothing" is a different
# statement and must not be reported as the first one.
# ---------------------------------------------------------------------------
def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return numerator / denominator


def _f1(precision: Optional[float], recall: Optional[float]) -> Optional[float]:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# Scoring one case
# ---------------------------------------------------------------------------
def _expected_node_set(case: Any) -> set:
    return {(str(name), str(entity_type)) for name, entity_type in case.expected_nodes}


def _expected_edge_set(case: Any) -> set:
    return {
        (str(subject), str(predicate), str(target))
        for subject, predicate, target in case.expected_edges
    }


def _produced_node_set(result: Any) -> set:
    return {(node.name, node.entity_type) for node in result.nodes}


def _produced_edge_set(result: Any) -> set:
    """Resolve edge keys back to the canonical names actually captured.

    The edge itself only carries normalized keys, so the display name is looked
    up through the node set. When an endpoint is missing (a one-sided clause)
    the key is used as-is, which makes the edge spurious or invalid rather than
    silently matching a label.
    """
    names = {
        (node.entity_type, node.normalized_key): node.name for node in result.nodes
    }
    produced = set()
    for edge in result.edges:
        subject = names.get((edge.subject_type, edge.subject_key), edge.subject_key)
        target = names.get((edge.object_type, edge.object_key), edge.object_key)
        produced.add((subject, edge.predicate, target))
    return produced


def score_case(case: Any, result: Any) -> EvalCaseResult:
    """Score one already-extracted case against its label."""
    expected_nodes = _expected_node_set(case)
    expected_edges = _expected_edge_set(case)
    produced_nodes = _produced_node_set(result)
    produced_edges = _produced_edge_set(result)

    matched_nodes = expected_nodes & produced_nodes
    matched_edges = expected_edges & produced_edges

    # Counted over the *emitted node list*, not over the de-duplicated set: the
    # current extractor accumulates into a dict keyed by (type, key), so it
    # cannot emit a duplicate today. The metric exists to catch a future
    # multi-pass change that loses that property, which a set would hide.
    name_counts: Counter = Counter(node.name for node in result.nodes)
    duplicate_names = sum(count - 1 for count in name_counts.values() if count > 1)

    # A relation is invalid when an endpoint is not among *this case's*
    # produced entities. It means the edge points at something that was never
    # emitted as a node — a dangling edge, which the writer would reject.
    node_names = {name for name, _entity_type in produced_nodes}
    invalid_edges = sum(
        1
        for subject, _predicate, target in produced_edges
        if subject not in node_names or target not in node_names
    )

    reason_codes = tuple(rejection.reason_code for rejection in result.rejections)
    expected_reason = getattr(case, "expected_skip_reason", None)
    reason_ok = expected_reason is None or expected_reason in reason_codes

    produced_anything = bool(produced_nodes or produced_edges)
    must_not_extract = bool(getattr(case, "must_not_extract", False))
    false_positive = must_not_extract and produced_anything

    return EvalCaseResult(
        case_id=str(case.case_id),
        category=str(getattr(case, "category", "")),
        eligible=bool(result.eligible),
        expected_eligible=bool(case.expected_eligible),
        false_positive=false_positive,
        expected_nodes=len(expected_nodes),
        produced_nodes=len(produced_nodes),
        emitted_nodes=len(result.nodes),
        matched_nodes=len(matched_nodes),
        missed_nodes=len(expected_nodes - produced_nodes),
        spurious_nodes=len(produced_nodes - expected_nodes),
        expected_edges=len(expected_edges),
        produced_edges=len(produced_edges),
        matched_edges=len(matched_edges),
        missed_edges=len(expected_edges - produced_edges),
        spurious_edges=len(produced_edges - expected_edges),
        duplicate_names=duplicate_names,
        invalid_edges=invalid_edges,
        reason_codes=reason_codes,
        reason_ok=reason_ok,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _category_reports(details: Sequence[EvalCaseResult]) -> Dict[str, CategoryReport]:
    grouped: Dict[str, List[EvalCaseResult]] = {}
    for detail in details:
        grouped.setdefault(detail.category, []).append(detail)

    reports: Dict[str, CategoryReport] = {}
    for category, rows in grouped.items():
        with_output = sum(
            1
            for row in rows
            if row.expected_nodes or row.expected_edges
            if row.matched_nodes or row.matched_edges
        )
        reports[category] = CategoryReport(
            category=category,
            cases=len(rows),
            expected_nodes=sum(row.expected_nodes for row in rows),
            produced_nodes=sum(row.produced_nodes for row in rows),
            matched_nodes=sum(row.matched_nodes for row in rows),
            expected_edges=sum(row.expected_edges for row in rows),
            produced_edges=sum(row.produced_edges for row in rows),
            matched_edges=sum(row.matched_edges for row in rows),
            cases_with_output=with_output,
            false_positive_cases=sum(1 for row in rows if row.false_positive),
        )
    return dict(sorted(reports.items()))


def evaluate(
    cases: Iterable[Any],
    *,
    extractor: Callable[..., Any] = extract_candidates,
) -> EvalReport:
    """Run ``extractor`` over ``cases`` and return an :class:`EvalReport`.

    ``extractor`` receives ``(body, memory_id=case_id)`` and must return an
    ``ExtractionResult``. It is injectable so the evaluator can be tested with a
    stub (see ``test_jkg_quality.py``) and so a future LLM extractor can be
    measured on the identical corpus — that comparison is the whole point of
    keeping the labels independent of the implementation.

    A case whose body the extractor refuses is still scored: ``eligible=False``
    with a non-empty label produces missed nodes and therefore hurts recall.
    That is correct. A body that must not be read has no correct extraction, so
    the two situations are kept apart by ``expected_eligible``, not conflated.
    """
    details: List[EvalCaseResult] = []
    for case in cases:
        result = extractor(case.body, memory_id=case.case_id)
        details.append(score_case(case, result))

    matched_nodes = sum(detail.matched_nodes for detail in details)
    produced_nodes = sum(detail.produced_nodes for detail in details)
    expected_nodes = sum(detail.expected_nodes for detail in details)
    matched_edges = sum(detail.matched_edges for detail in details)
    produced_edges = sum(detail.produced_edges for detail in details)
    expected_edges = sum(detail.expected_edges for detail in details)

    # "Produced at least one expected candidate" — a case that emitted only
    # spurious output has not been covered, it has been misread.
    cases_with_output = sum(
        1 for detail in details if detail.matched_nodes or detail.matched_edges
    )
    governance_rejections = sum(
        1
        for detail in details
        if not detail.expected_eligible and not detail.eligible
    )
    governance_leaks = sum(
        1
        for detail in details
        if not detail.expected_eligible and detail.eligible
    )

    return EvalReport(
        rule_version=RULE_VERSION,
        cases=len(details),
        matched_nodes=matched_nodes,
        produced_nodes=produced_nodes,
        expected_nodes=expected_nodes,
        matched_edges=matched_edges,
        produced_edges=produced_edges,
        expected_edges=expected_edges,
        cases_with_output=cases_with_output,
        false_positive_cases=sum(1 for detail in details if detail.false_positive),
        governance_rejections=governance_rejections,
        governance_leaks=governance_leaks,
        duplicate_entities=sum(detail.duplicate_names for detail in details),
        total_entities=sum(detail.emitted_nodes for detail in details),
        invalid_relations=sum(detail.invalid_edges for detail in details),
        total_relations=produced_edges,
        details=tuple(details),
    )


def evaluate_by_rule(cases: Iterable[Any]) -> Dict[str, Dict[str, Optional[float]]]:
    """Per-rule **precision** contribution.

    Attribution works from ``evidence.rule``, so a body with several clauses is
    credited to each rule separately rather than to the case as a whole.

    Only precision is reported, and that is not an oversight: a *missed* clause
    leaves no ``evidence.rule`` behind, so there is no way to attribute a miss to
    the rule that should have caught it. A recall number here would have to
    assume the answer ("the rule that matched nothing was responsible"), so the
    corpus instead carries explicit positive and negative examples per rule.
    """
    per_rule: Dict[str, Dict[str, int]] = {}

    def bucket(rule_id: str) -> Dict[str, int]:
        return per_rule.setdefault(
            rule_id,
            {
                "produced_nodes": 0,
                "produced_edges": 0,
                "expected_nodes": 0,
                "expected_edges": 0,
            },
        )

    for case in cases:
        result = extract_candidates(case.body, memory_id=case.case_id)
        expected_nodes = _expected_node_set(case)
        expected_edges = _expected_edge_set(case)
        produced_nodes = _produced_node_set(result)
        produced_edges = _produced_edge_set(result)
        for node in result.nodes:
            rule = getattr(node.evidence, "rule", "unknown") if node.evidence else "unknown"
            bucket(rule)["produced_nodes"] += 1
        for edge in result.edges:
            rule = getattr(edge.evidence, "rule", "unknown") if edge.evidence else "unknown"
            bucket(rule)["produced_edges"] += 1
        for node in result.nodes:
            if (node.name, node.entity_type) not in expected_nodes:
                continue
            rule = getattr(node.evidence, "rule", "unknown") if node.evidence else "unknown"
            bucket(rule)["expected_nodes"] += 1
        for edge in result.edges:
            rule = getattr(edge.evidence, "rule", "unknown") if edge.evidence else "unknown"
            subject = next(
                (n.name for n in result.nodes if n.normalized_key == edge.subject_key),
                edge.subject_key,
            )
            target = next(
                (n.name for n in result.nodes if n.normalized_key == edge.object_key),
                edge.object_key,
            )
            if (subject, edge.predicate, target) in expected_edges:
                bucket(rule)["expected_edges"] += 1

    report: Dict[str, Dict[str, Optional[float]]] = {}
    for rule_id, counts in sorted(per_rule.items()):
        # Produced == expected only for the *matched* subset here: a rule's
        # precision is matched/produced and its recall is matched/expected-label
        # for the cases it touched. Both are reported with their numerators so a
        # reader can recompute.
        report[rule_id] = {
            "produced_nodes": counts["produced_nodes"],
            "matched_nodes": counts["expected_nodes"],
            "produced_edges": counts["produced_edges"],
            "matched_edges": counts["expected_edges"],
            "entity_precision": _ratio(counts["expected_nodes"], counts["produced_nodes"]),
            "relation_precision": _ratio(counts["expected_edges"], counts["produced_edges"]),
        }
    return report


__all__ = [
    "EvalCaseResult",
    "CategoryReport",
    "EvalReport",
    "evaluate",
    "evaluate_by_rule",
    "score_case",
]
