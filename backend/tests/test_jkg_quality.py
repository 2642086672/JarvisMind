"""Extraction quality metrics and evaluation-corpus regression (P1-1 / P1-2 / P1-3).

Two independent layers, deliberately kept apart:

``TestMetricArithmetic``
    The evaluator is fed *fake* cases and *fake* extraction results whose correct
    score is known by construction. Nothing here touches the real extractor, so a
    broken precision formula cannot hide behind a rule that happens to be right.

``TestEvaluationCorpus``
    The real extractor is run over the hand-labelled corpus. These assertions are
    lower bounds and absolutes, never "output equals current output": a rule
    change that improves the numbers passes, and a change that degrades them
    fails. The one absolute is ``false_positive_cases == 0``.

``TestAssertivenessGuards`` / ``TestCaptureGuards``
    Positive and negative cases per guard, so a guard cannot be "fixed" by being
    deleted without a test going red.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import pytest

from _fixtures.jkg_extraction_eval import CATEGORIES, EVAL_CASES, cases_in
from jarvismind.jkg.extraction import (
    REASON_ADVERBIAL_CAPTURE,
    REASON_CLASSIFICATION_PHRASE,
    REASON_FRAGMENT_CAPTURE,
    REASON_INTERROGATIVE_CLAUSE,
    REASON_NEGATED_CLAUSE,
    REASON_NON_NAMING_CAPTURE,
    REASON_PRONOUN_NAME,
    REASON_SELF_REFERENCE,
    RULE_VERSION,
    extract_candidates,
)
from jarvismind.jkg.quality import evaluate


# ---------------------------------------------------------------------------
# Stubs: a controllable extractor, so metric arithmetic is tested in isolation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _StubNode:
    name: str
    entity_type: str
    normalized_key: str


@dataclass(frozen=True)
class _StubEdge:
    subject_key: str
    predicate: str
    object_key: str
    subject_type: str
    object_type: str


@dataclass(frozen=True)
class _StubRejection:
    reason_code: str


@dataclass(frozen=True)
class _StubResult:
    nodes: Tuple[_StubNode, ...]
    edges: Tuple[_StubEdge, ...]
    rejections: Tuple[_StubRejection, ...]
    eligible: bool = True


@dataclass(frozen=True)
class _StubCase:
    case_id: str
    body: str
    category: str = "stub"
    expected_nodes: Tuple[Tuple[str, str], ...] = ()
    expected_edges: Tuple[Tuple[str, str, str], ...] = ()
    expected_eligible: bool = True
    expected_skip_reason: Optional[str] = None
    must_not_extract: bool = False


def _node(name: str, entity_type: str = "PERSON") -> _StubNode:
    return _StubNode(name=name, entity_type=entity_type, normalized_key=name.casefold())


def _edge(
    subject: str,
    predicate: str,
    target: str,
    subject_type: str = "PERSON",
    object_type: str = "PROJECT",
) -> _StubEdge:
    return _StubEdge(
        subject_key=subject.casefold(),
        predicate=predicate,
        object_key=target.casefold(),
        subject_type=subject_type,
        object_type=object_type,
    )


def _stub_extractor(
    nodes: Sequence[_StubNode] = (),
    edges: Sequence[_StubEdge] = (),
    *,
    eligible: bool = True,
    reasons: Sequence[str] = (),
):
    """Return an extractor that yields the same result for every case."""

    def _run(_body: str, *, memory_id: str = "") -> _StubResult:
        return _StubResult(
            nodes=tuple(nodes),
            edges=tuple(edges),
            rejections=tuple(_StubRejection(code) for code in reasons),
            eligible=eligible,
        )

    return _run


# ---------------------------------------------------------------------------
# Evaluator arithmetic
# ---------------------------------------------------------------------------
class TestMetricArithmetic:
    def test_perfect_case_scores_one(self):
        case = _StubCase(
            case_id="perfect",
            body="ignored",
            # Both endpoints of the edge are labelled: a relation needs its
            # target node, so producing it is correct, not over-extraction.
            expected_nodes=(("张三", "PERSON"), ("北极星", "PROJECT")),
            expected_edges=(("张三", "WORKS_ON", "北极星"),),
        )
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三"), _node("北极星", "PROJECT")],
                edges=[_edge("张三", "WORKS_ON", "北极星")],
            ),
        )
        assert report.entity_precision == 1.0
        assert report.entity_recall == 1.0
        assert report.relation_precision == 1.0
        assert report.relation_recall == 1.0
        assert report.false_positive_cases == 0
        assert report.failures() == ()

    def test_pure_over_extraction_lowers_precision_not_recall(self):
        """One right node plus two invented ones: recall stays 1, precision 1/3."""
        case = _StubCase(case_id="noisy", body="ignored", expected_nodes=(("张三", "PERSON"),))
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三"), _node("李四"), _node("王五")],
            ),
        )
        assert report.entity_recall == 1.0
        assert report.entity_precision == pytest.approx(1 / 3)
        assert report.details[0].spurious_nodes == 2
        assert report.details[0].missed_nodes == 0

    def test_pure_under_extraction_lowers_recall_not_precision(self):
        """Two expected nodes, one produced and correct: precision 1, recall 1/2."""
        case = _StubCase(
            case_id="thin",
            body="ignored",
            expected_nodes=(("张三", "PERSON"), ("北极星", "PROJECT")),
        )
        report = evaluate([case], extractor=_stub_extractor(nodes=[_node("张三")]))
        assert report.entity_precision == 1.0
        assert report.entity_recall == 0.5
        assert report.details[0].missed_nodes == 1
        assert report.details[0].spurious_nodes == 0

    def test_wrong_type_counts_as_miss_and_spurious(self):
        """The same text under the wrong entity type is not a match."""
        case = _StubCase(case_id="typed", body="ignored", expected_nodes=(("北极星", "PROJECT"),))
        report = evaluate(
            [case],
            extractor=_stub_extractor(nodes=[_node("北极星", "EVENT")]),
        )
        detail = report.details[0]
        assert detail.matched_nodes == 0
        assert detail.missed_nodes == 1
        assert detail.spurious_nodes == 1
        assert report.entity_precision == 0.0
        assert report.entity_recall == 0.0

    def test_wrong_relation_direction_is_not_a_match(self):
        case = _StubCase(
            case_id="direction",
            body="ignored",
            expected_edges=(("张三", "KNOWS", "李四"),),
        )
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三"), _node("李四")],
                edges=[
                    _edge("李四", "KNOWS", "张三", "PERSON", "PERSON"),
                ],
            ),
        )
        assert report.matched_edges == 0
        assert report.details[0].spurious_edges == 1
        assert report.details[0].missed_edges == 1

    def test_all_wrong_scores_zero_precision(self):
        case = _StubCase(case_id="wrong", body="ignored", expected_nodes=(("张三", "PERSON"),))
        report = evaluate([case], extractor=_stub_extractor(nodes=[_node("李四")]))
        assert report.entity_precision == 0.0
        assert report.entity_recall == 0.0
        assert report.entity_f1 == 0.0

    def test_no_output_reports_none_not_zero(self):
        """'We produced nothing' must not read as 'everything was wrong'."""
        case = _StubCase(case_id="silent", body="ignored", expected_nodes=(("张三", "PERSON"),))
        report = evaluate([case], extractor=_stub_extractor())
        assert report.entity_precision is None
        assert report.entity_recall == 0.0

    def test_false_positive_case_is_counted_separately(self):
        """A must-not-extract case with output is a false positive even if neat."""
        case = _StubCase(
            case_id="must_not",
            body="ignored",
            must_not_extract=True,
        )
        report = evaluate([case], extractor=_stub_extractor(nodes=[_node("张三")]))
        assert report.false_positive_cases == 1
        assert report.false_positive_case_ids() == ("must_not",)

    def test_noise_case_with_no_output_is_not_a_false_positive(self):
        case = _StubCase(case_id="quiet", body="ignored")
        report = evaluate([case], extractor=_stub_extractor())
        assert report.false_positive_cases == 0

    def test_false_positive_count_ignores_extra_edges_alone(self):
        case = _StubCase(case_id="edge_only", body="ignored", must_not_extract=True)
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三"), _node("北极星", "PROJECT")],
                edges=[_edge("张三", "WORKS_ON", "北极星")],
            ),
        )
        assert report.false_positive_cases == 1

    def test_duplicate_entity_rate_counts_same_name_twice(self):
        """Two node objects with one name are a duplicate, whatever the type."""
        case = _StubCase(case_id="dup", body="ignored")
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三", "PERSON"), _node("张三", "PERSON")],
            ),
        )
        assert report.duplicate_entities == 1
        assert report.duplicate_entity_rate == 0.5

    def test_invalid_relation_rate_counts_dangling_endpoints(self):
        case = _StubCase(case_id="dangling", body="ignored")
        report = evaluate(
            [case],
            extractor=_stub_extractor(
                nodes=[_node("张三")],
                edges=[_edge("张三", "KNOWS", "李四", "PERSON", "PERSON")],
            ),
        )
        assert report.invalid_relations == 1
        assert report.invalid_relation_rate == 1.0

    def test_coverage_denominator_is_expectant_cases_only(self):
        """Noise cases cannot dilute coverage; that is why they are excluded."""
        cases = [
            _StubCase(
                case_id="hit",
                body="ignored",
                expected_nodes=(("张三", "PERSON"),),
            ),
            _StubCase(case_id="miss", body="ignored", expected_nodes=(("李四", "PERSON"),)),
            _StubCase(case_id="noise", body="ignored"),
        ]
        report = evaluate([cases[0]], extractor=_stub_extractor(nodes=[_node("张三")]))
        assert report.coverage == 1.0
        assert report.expectant_cases == 1

        report = evaluate(cases, extractor=_stub_extractor(nodes=[_node("张三")]))
        assert report.expectant_cases == 2
        assert report.coverage == 0.5

    def test_governance_rejection_counts_ineligible_as_expected(self):
        case = _StubCase(
            case_id="sensitive",
            body="ignored",
            expected_eligible=False,
            expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
            must_not_extract=True,
        )
        # A second, ordinary case proves the rate's denominator is the
        # must-refuse population, not the whole corpus.
        ordinary = _StubCase(case_id="ordinary", body="ignored")
        report = evaluate(
            [case, ordinary],
            extractor=_stub_extractor(
                eligible=False, reasons=["PERSONAL_SENSITIVE_CATEGORY"]
            ),
        )
        assert report.governance_rejections == 1
        assert report.governance_leaks == 0
        assert report.governance_rejection_rate == 1.0
        assert report.details[0].reason_ok is True

    def test_governance_leak_is_counted_when_gate_lets_it_through(self):
        case = _StubCase(
            case_id="leak",
            body="ignored",
            expected_eligible=False,
            expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        )
        report = evaluate([case], extractor=_stub_extractor(eligible=True))
        assert report.governance_leaks == 1
        assert report.governance_rejections == 0
        assert report.details[0].passed is False

    def test_missing_expected_reason_fails_the_case(self):
        case = _StubCase(
            case_id="wrong-reason",
            body="ignored",
            expected_eligible=False,
            expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        )
        report = evaluate(
            [case],
            extractor=_stub_extractor(eligible=False, reasons=["CREDENTIAL_PRESENT"]),
        )
        assert report.details[0].reason_ok is False
        assert report.details[0].passed is False

    def test_report_never_contains_body_text(self):
        """The privacy contract: case_id and counts, never the memory body."""
        secret = "MKR-EVAL-BODY-DO-NOT-ECHO"
        case = _StubCase(
            case_id="privacy",
            body=f"张三负责北极星计划 {secret}",
            expected_nodes=(("张三", "PERSON"),),
        )
        report = evaluate(
            [case],
            extractor=_stub_extractor(nodes=[_node("张三")]),
        )
        serialised = repr(report.as_dict())
        assert secret not in serialised
        assert case.body not in serialised

    def test_report_records_rule_version(self):
        report = evaluate([], extractor=_stub_extractor())
        assert report.rule_version == RULE_VERSION

    def test_by_category_groups_and_totals_match(self):
        cases = [
            _StubCase(
                case_id="a",
                body="ignored",
                category="one",
                expected_nodes=(("张三", "PERSON"),),
            ),
            _StubCase(
                case_id="b",
                body="ignored",
                category="two",
                expected_nodes=(("李四", "PERSON"),),
            ),
        ]
        report = evaluate(cases, extractor=_stub_extractor(nodes=[_node("张三")]))
        by_category = report.by_category()
        assert set(by_category) == {"one", "two"}
        assert by_category["one"].entity_precision == 1.0
        assert by_category["two"].entity_recall == 0.0
        assert sum(r.cases for r in by_category.values()) == report.cases


# ---------------------------------------------------------------------------
# The evaluation corpus, measured
# ---------------------------------------------------------------------------
class TestEvaluationCorpus:
    """Regression bounds on the real extractor over the labelled corpus.

    The thresholds are *floors* chosen one step below the measured value so an
    improvement passes and a regression fails. They are not equality checks
    against today's output: the extractor is allowed to get better.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        # Class-scoped on purpose: the corpus is evaluated once per class.
        return evaluate(EVAL_CASES)

    # -- the corpus itself -------------------------------------------------
    def test_corpus_size_and_category_coverage(self):
        assert len(EVAL_CASES) >= 100
        assert len(CATEGORIES) == 11
        for category in CATEGORIES:
            # The task asks for 8-15 per category; capture_noise is the newest
            # and is allowed to be slightly smaller.
            assert len(cases_in(category)) >= 8, category

    def test_case_ids_are_unique(self):
        ids = [case.case_id for case in EVAL_CASES]
        assert len(ids) == len(set(ids))

    def test_every_case_has_an_explicit_label(self):
        """No case may leave its expectation implicit."""
        for case in EVAL_CASES:
            assert case.expected_eligible is not None
            if not case.expected_eligible:
                assert case.expected_skip_reason, case.case_id
            if case.must_not_extract:
                # must_not_extract exists to protect precision; a case that
                # expects output cannot also be a false-positive trap.
                assert case.expected_nodes == () and case.expected_edges == ()

    def test_no_production_text_in_corpus(self):
        """Every body must be synthetic: no DB artifact paths, no real ids."""
        for case in EVAL_CASES:
            assert "jarvismind.db" not in case.body
            assert "memory_" not in case.body
            assert len(case.body) < 200

    # -- headline metrics --------------------------------------------------
    def test_entity_precision_floor(self, report):
        assert report.entity_precision is not None
        assert report.entity_precision >= 0.98

    def test_relation_precision_floor(self, report):
        assert report.relation_precision is not None
        assert report.relation_precision >= 0.98

    def test_entity_recall_floor(self, report):
        assert report.entity_recall is not None
        assert report.entity_recall >= 0.95

    def test_relation_recall_floor(self, report):
        assert report.relation_recall is not None
        assert report.relation_recall >= 0.95

    def test_false_positive_cases_is_zero(self, report):
        """The one absolute: no must-not-extract body may yield a candidate."""
        assert report.false_positive_cases == 0
        assert report.false_positive_case_ids() == ()

    def test_no_duplicate_entities(self, report):
        assert report.duplicate_entity_rate == 0.0

    def test_no_invalid_relations(self, report):
        assert report.invalid_relation_rate == 0.0

    def test_no_governance_leaks(self, report):
        assert report.governance_leaks == 0
        # Every must-refuse body is refused, so the rate is 1.0 — not 8%.
        assert report.governance_rejection_rate == 1.0

    def test_all_sensitive_cases_are_refused(self, report):
        sensitive = [d for d in report.details if d.category == "sensitive"]
        assert sensitive
        assert all(not d.eligible for d in sensitive)
        assert all(d.expected_eligible is False for d in sensitive)

    def test_coverage_over_expectant_cases(self, report):
        assert report.coverage is not None
        assert report.coverage >= 0.95

    def test_no_case_fails(self, report):
        """Every labelled case passes: labels are the standard, not the output."""
        assert report.failures() == ()

    def test_rule_version_matches_module(self, report):
        assert report.rule_version == RULE_VERSION

    # -- per-category sanity ----------------------------------------------
    def test_must_not_extract_categories_produce_nothing(self, report):
        guarded = {
            "negation_question",
            "pronoun_subject",
            "sensitive",
        }
        for detail in report.details:
            if detail.category in guarded:
                assert detail.produced_nodes == 0, detail.case_id
                assert detail.produced_edges == 0, detail.case_id

    def test_explicit_relation_category_is_fully_covered(self, report):
        by_category = report.by_category()
        explicit = by_category["explicit_relation"]
        assert explicit.entity_precision == 1.0
        assert explicit.relation_precision == 1.0
        assert explicit.entity_recall == 1.0
        assert explicit.relation_recall == 1.0

    def test_narrative_and_noise_categories_produce_nothing(self, report):
        by_category = report.by_category()
        for category in ("narrative_event", "noise"):
            assert by_category[category].produced_nodes == 0
            assert by_category[category].produced_edges == 0

    def test_aggregate_counts_reconcile(self, report):
        assert report.produced_nodes == sum(
            detail.produced_nodes for detail in report.details
        )
        assert report.expected_edges == sum(
            detail.expected_edges for detail in report.details
        )


# ---------------------------------------------------------------------------
# Assertiveness guards: negation / question / pronoun / non-naming
# ---------------------------------------------------------------------------
class TestAssertivenessGuards:
    @pytest.mark.parametrize(
        "body",
        [
            "张三不负责北极星计划",
            "张三并不参加评审",
            "用户不认识李四",
            "用户不常参加晨会",
            "没有人认识李四",
            "Alice never attends Sprint Review",
            "Alice knows no one",
        ],
    )
    def test_negation_yields_nothing_and_says_why(self, body):
        result = extract_candidates(body, memory_id="neg-guard")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_NEGATED_CLAUSE in {r.reason_code for r in result.rejections}

    @pytest.mark.parametrize(
        "body",
        [
            "张三负责什么？",
            "谁负责北极星计划",
            "李四是否认识王五",
            # English questions must contain the keyword to test the guard;
            # without it there is no clause to reject and nothing to assert.
            "Who works on Northstar",
            "Who knows Carol",
        ],
    )
    def test_question_yields_nothing_and_says_why(self, body):
        result = extract_candidates(body, memory_id="q-guard")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_INTERROGATIVE_CLAUSE in {
            r.reason_code for r in result.rejections
        }

    @pytest.mark.parametrize(
        "body",
        [
            "她负责这个项目",
            "他参加晨会",
            "我们认识张三",
            "他们负责北极星计划",
            "She works on Northstar",
            "It belongs to Northstar",
        ],
    )
    def test_pronoun_subject_yields_nothing_and_says_why(self, body):
        result = extract_candidates(body, memory_id="pron-guard")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_PRONOUN_NAME in {r.reason_code for r in result.rejections}

    @pytest.mark.parametrize(
        "body",
        [
            "这个仓库属于北极星计划",
            "该项目属于年度路线图",
            "This repository belongs to Northstar",
            "JarvisMind 负责的项目进展顺利",
        ],
    )
    def test_non_naming_capture_yields_nothing(self, body):
        result = extract_candidates(body, memory_id="nn-guard")
        assert result.nodes == ()
        assert result.edges == ()
        codes = {r.reason_code for r in result.rejections}
        assert codes & {REASON_NON_NAMING_CAPTURE, REASON_FRAGMENT_CAPTURE}

    def test_conjunct_with_pronoun_is_rejected(self):
        result = extract_candidates("张三和她一起负责北极星计划", memory_id="conj")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_PRONOUN_NAME in {r.reason_code for r in result.rejections}

    def test_self_reference_keeps_node_but_drops_edge(self):
        result = extract_candidates("张三认识张三", memory_id="self")
        assert [(n.name, n.entity_type) for n in result.nodes] == [("张三", "PERSON")]
        assert result.edges == ()
        assert REASON_SELF_REFERENCE in {r.reason_code for r in result.rejections}

    # -- counterexamples: the guards must not eat valid clauses -------------
    @pytest.mark.parametrize(
        "body,subject,target",
        [
            ("张三负责北极星计划", "张三", "北极星计划"),
            ("无双工作室 负责 天命计划", "无双工作室", "天命计划"),
            ("未来项目 属于 北极星计划", "未来项目", "北极星计划"),
            ("Bruno 负责 北极星计划", "Bruno", "北极星计划"),
            ("哪吒 负责 北极星计划", "哪吒", "北极星计划"),
            ("本田 负责 天命计划", "本田", "天命计划"),
        ],
    )
    def test_names_that_resemble_guards_survive(self, body, subject, target):
        """不 inside a name, 未 / 哪 / no inside a name are not negations."""
        result = extract_candidates(body, memory_id="counter")
        names = {node.name for node in result.nodes}
        assert subject in names, body
        assert target in names, body
        assert len(result.edges) == 1, body


# ---------------------------------------------------------------------------
# Capture guards: fragment / classification / adverbial
# ---------------------------------------------------------------------------
class TestCaptureGuards:
    @pytest.mark.parametrize(
        "body",
        [
            "用户负责省区客服质管、仲裁、理赔、升级工单等工作。",
            "用户职业经历：2021-07 至 2022-07 任分拨客服工单岗，负责热线电话。",
            "周一鸣参加周会/评审/站会三种活动",
        ],
    )
    def test_fragment_captures_are_refused(self, body):
        result = extract_candidates(body, memory_id="frag")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_FRAGMENT_CAPTURE in {
            r.reason_code for r in result.rejections
        }

    @pytest.mark.parametrize(
        "body",
        [
            "用户在物流快运行业负责省区客服质管与理赔工作",
            "用户在日常工作流程中负责协调三个小组的排班",
        ],
    )
    def test_adverbial_subject_is_refused(self, body):
        result = extract_candidates(body, memory_id="adv")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_ADVERBIAL_CAPTURE in {
            r.reason_code for r in result.rejections
        }

    def test_classification_phrase_is_refused(self):
        result = extract_candidates("JarvisMind 属于个人项目", memory_id="class")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_CLASSIFICATION_PHRASE in {
            r.reason_code for r in result.rejections
        }

    def test_long_person_capture_is_refused_by_length_branch(self):
        """Regression: the length branch referenced an undefined constant.

        A 30-character PERSON capture containing 在 once raised ``NameError``
        instead of rejecting. The branch only fires past ``_LONG_CAPTURE``, so
        only an implausibly long subject reaches it — which is exactly why no
        ordinary case caught the crash.
        """
        body = "在线" + "很长" * 14 + "负责北极星计划"
        result = extract_candidates(body, memory_id="long-cap")
        assert result.nodes == ()
        assert result.edges == ()
        assert REASON_ADVERBIAL_CAPTURE in {
            r.reason_code for r in result.rejections
        }

    def test_short_person_capture_with_preposition_still_extracted(self):
        """The length cut-off must not turn into "any preposition is bad"."""
        result = extract_candidates("未来 负责北极星计划", memory_id="short-cap")
        assert "未来" in {node.name for node in result.nodes}
        assert len(result.edges) == 1

    def test_extractor_never_raises_on_any_corpus_body(self):
        """Every labelled body must return, not raise.

        The guards added in v2 read module-level constants; a typo in one of
        them is invisible until a body reaches that branch. Running the whole
        corpus through the real extractor is the cheapest way to prove no
        branch is dead-on-arrival.
        """
        for case in EVAL_CASES:
            extract_candidates(case.body, memory_id=case.case_id)

    # -- counterexamples ---------------------------------------------------
    @pytest.mark.parametrize(
        "body,expected",
        [
            # A proper name ending in 项目 is not a classification phrase.
            ("张三负责北极星项目", "北极星项目"),
            # 在 inside a PROJECT name is legitimate.
            ("张三负责在线支付平台", "在线支付平台"),
            # A short PERSON capture containing no preposition is a name.
            ("李雷认识韩梅梅", "韩梅梅"),
        ],
    )
    def test_clean_captures_survive_guards(self, body, expected):
        result = extract_candidates(body, memory_id="clean")
        assert expected in {node.name for node in result.nodes}, body
        assert len(result.edges) == 1, body

    def test_fragment_guard_does_not_fire_on_a_single_name(self):
        result = extract_candidates("张三负责北极星计划", memory_id="single")
        assert not any(
            r.reason_code == REASON_FRAGMENT_CAPTURE for r in result.rejections
        )


# ---------------------------------------------------------------------------
# Rule version discipline
# ---------------------------------------------------------------------------
class TestRuleVersionDiscipline:
    def test_version_is_the_documented_one(self):
        assert RULE_VERSION == "jkg-rules/2"

    def test_initializer_reports_the_extraction_version(self):
        """The preview/init API must never carry a second, stale copy."""
        from jarvismind.jkg.initializer import RULES_VERSION

        assert RULES_VERSION == RULE_VERSION

    def test_guard_reason_codes_have_human_reasons(self):
        """Every guard code must map to a specific human sentence.

        Asserting the *exact* sentence, not merely ``!= code``: the fallback text
        is a non-empty string, so the weaker assertion would pass while the code
        was undocumented.
        """
        from jarvismind.jkg import extraction

        fallback = "The candidate was rejected."
        for code in (
            REASON_NEGATED_CLAUSE,
            REASON_INTERROGATIVE_CLAUSE,
            REASON_PRONOUN_NAME,
            REASON_NON_NAMING_CAPTURE,
            REASON_CLASSIFICATION_PHRASE,
            REASON_FRAGMENT_CAPTURE,
            REASON_ADVERBIAL_CAPTURE,
            REASON_SELF_REFERENCE,
        ):
            assert extraction._human_reason(code) != fallback, code

    def test_reason_table_has_no_duplicate_keys(self):
        """A duplicated literal key silently overwrites, hiding the first text.

        Read from source because a dict literal collapses duplicates at compile
        time, so the runtime object cannot reveal them.
        """
        import ast
        from pathlib import Path

        import jarvismind.jkg.extraction as extraction_module

        source = Path(extraction_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        table = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_human_reason":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Dict):
                    table = inner
                    break
        assert table is not None, "_human_reason reason table not found"
        keys = [
            key.value
            for key in table.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        assert len(keys) == len(set(keys)), sorted(
            key for key in set(keys) if keys.count(key) > 1
        )
