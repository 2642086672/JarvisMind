"""Hand-labelled extraction evaluation set (JKG-EXTRACTION-QUALITY / P1-1).

What this file is
-----------------
An **evaluation corpus**, not a regression snapshot. Every case carries the
answer a human decided is *correct*, written down before the extractor was run
against it. The labels are therefore an independent standard: a change to
``extraction.py`` that makes the extractor disagree with a label is a change
that either fixed a bug or introduced one, and the test suite says which.

Hard rule: **no production data.** Every body here is synthetic. The sentence
*shapes* were chosen to mirror the distribution measured on the real corpus
(see ``docs/03-功能模块/JKG/JKG-EXTRACTION-QUALITY.md`` §5), but not one line is
copied from a real memory. Names, projects and pets are invented.

Label semantics
---------------
``expected_nodes``
    The exact set of ``(name, entity_type)`` pairs a correct extractor returns.
    ``set()`` means "nothing should be extracted", which is the label for the
    must-not-extract categories.

``expected_edges``
    The exact set of ``(subject_name, predicate, object_name)`` triples a
    correct extractor returns. Compared on *canonical names*, not on
    ``normalized_key``: the label is about what was said, not about how it was
    folded.

``expected_eligible``
    ``False`` when the privacy gate must refuse the whole body (credential /
    PII / personal-sensitivity category). An ineligible body must yield zero
    nodes and zero edges regardless of what the rules would have matched.

``expected_skip_reason``
    ``None`` when extraction is expected to run. Otherwise the stable reason
    code the gate must report. Only the category code is labelled, never the
    matched value — a rejection must not echo text.

``must_not_extract``
    The precision-critical flag. ``True`` marks a case where *any* produced
    candidate is a false positive, however plausible it looks. These are the
    cases behind ``false_positive_cases`` in the report: negation, questions,
    pronouns, sensitive categories and noise. This flag is not derived from
    ``expected_nodes``; a case may legitimately expect a node and still be
    precision-critical (none currently are, and the test asserts that).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

PERSON = "PERSON"
PROJECT = "PROJECT"
EVENT = "EVENT"

NodeLabel = Tuple[str, str]
EdgeLabel = Tuple[str, str, str]


@dataclass(frozen=True)
class EvalCase:
    """One labelled body.

    ``category`` groups cases for the per-category breakdown in the report; it
    is documentation and never affects scoring.
    """

    case_id: str
    category: str
    body: str
    expected_nodes: Tuple[NodeLabel, ...] = ()
    expected_edges: Tuple[EdgeLabel, ...] = ()
    expected_eligible: bool = True
    expected_skip_reason: Optional[str] = None
    must_not_extract: bool = False


# ---------------------------------------------------------------------------
# 1 · explicit relation clauses — the rules are supposed to hit these
# ---------------------------------------------------------------------------
_EXPLICIT = (
    EvalCase(
        case_id="zh.works_on.basic",
        category="explicit_relation",
        body="张三负责 JarvisMind 项目",
        expected_nodes=(("张三", PERSON), ("JarvisMind 项目", PROJECT)),
        expected_edges=(("张三", "WORKS_ON", "JarvisMind 项目"),),
    ),
    EvalCase(
        case_id="zh.works_on.no_space",
        category="explicit_relation",
        body="长征七号负责信号中继任务",
        expected_nodes=(("长征七号", PERSON), ("信号中继任务", PROJECT)),
        expected_edges=(("长征七号", "WORKS_ON", "信号中继任务"),),
    ),
    EvalCase(
        case_id="zh.works_on.with_space",
        category="explicit_relation",
        body="林小满 负责 北极星计划",
        expected_nodes=(("林小满", PERSON), ("北极星计划", PROJECT)),
        expected_edges=(("林小满", "WORKS_ON", "北极星计划"),),
    ),
    EvalCase(
        case_id="zh.attends.basic",
        category="explicit_relation",
        body="周一鸣参加晨间站会",
        expected_nodes=(("周一鸣", PERSON), ("晨间站会", EVENT)),
        expected_edges=(("周一鸣", "ATTENDS", "晨间站会"),),
    ),
    EvalCase(
        case_id="zh.attends.with_space",
        category="explicit_relation",
        body="张伟 参加 知识图谱评审",
        expected_nodes=(("张伟", PERSON), ("知识图谱评审", EVENT)),
        expected_edges=(("张伟", "ATTENDS", "知识图谱评审"),),
    ),
    EvalCase(
        case_id="zh.belongs_to.basic",
        category="explicit_relation",
        body="季度复盘会属于北极星计划",
        expected_nodes=(("季度复盘会", EVENT), ("北极星计划", PROJECT)),
        expected_edges=(("季度复盘会", "BELONGS_TO", "北极星计划"),),
    ),
    EvalCase(
        case_id="zh.knows.basic",
        category="explicit_relation",
        body="李雷认识韩梅梅",
        expected_nodes=(("李雷", PERSON), ("韩梅梅", PERSON)),
        expected_edges=(("李雷", "KNOWS", "韩梅梅"),),
    ),
    EvalCase(
        case_id="zh.knows.with_space",
        category="explicit_relation",
        body="李雷 认识 韩梅梅",
        expected_nodes=(("李雷", PERSON), ("韩梅梅", PERSON)),
        expected_edges=(("李雷", "KNOWS", "韩梅梅"),),
    ),
    EvalCase(
        case_id="en.works_on.basic",
        category="explicit_relation",
        body="Alice works on Northstar",
        expected_nodes=(("Alice", PERSON), ("Northstar", PROJECT)),
        expected_edges=(("Alice", "WORKS_ON", "Northstar"),),
    ),
    EvalCase(
        case_id="en.attends.basic",
        category="explicit_relation",
        body="Bob attends Sprint Review",
        expected_nodes=(("Bob", PERSON), ("Sprint Review", EVENT)),
        expected_edges=(("Bob", "ATTENDS", "Sprint Review"),),
    ),
    EvalCase(
        case_id="en.belongs_to.basic",
        category="explicit_relation",
        body="Sprint Review belongs to Northstar",
        expected_nodes=(("Sprint Review", EVENT), ("Northstar", PROJECT)),
        expected_edges=(("Sprint Review", "BELONGS_TO", "Northstar"),),
    ),
    EvalCase(
        case_id="en.knows.basic",
        category="explicit_relation",
        body="Alice knows Bob",
        expected_nodes=(("Alice", PERSON), ("Bob", PERSON)),
        expected_edges=(("Alice", "KNOWS", "Bob"),),
    ),
    EvalCase(
        case_id="zh.multi_clause_two_relations",
        category="explicit_relation",
        body="张三负责北极星计划。李四认识张三",
        expected_nodes=(
            ("张三", PERSON),
            ("北极星计划", PROJECT),
            ("李四", PERSON),
        ),
        expected_edges=(
            ("张三", "WORKS_ON", "北极星计划"),
            ("李四", "KNOWS", "张三"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# 2 · narrative event clauses — the shape 90% of the real corpus has.
#
# The label for most of these is "extract nothing". That is not a statement
# that the sentence is worthless; it is a statement that **these eight clause
# forms cannot honestly turn it into a PERSON/PROJECT/EVENT triple**. The
# alternative — inferring entities from co-occurrence — is exactly what the
# module's docstring forbids. A few cases are labelled with the entity that a
# narrator does name explicitly, so recall is not scored as zero by fiat.
# ---------------------------------------------------------------------------
_NARRATIVE = (
    EvalCase(
        case_id="zh.narrative.dated_trip",
        category="narrative_event",
        body="2025-06-17 用户前往普陀山祈求姻缘",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.dated_meeting",
        category="narrative_event",
        body="2025-03-02 和同事在楼下吃了顿火锅",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.past_state",
        category="narrative_event",
        body="该关系于 2025-11-17 左右结束，期间关系逐渐冷淡",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.deployment",
        category="narrative_event",
        body="用户曾在 Windows 10 上部署过个人助手并调试了很久",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.tool_problem",
        category="narrative_event",
        body="使用中曾遇到插件缺失以及默认端口冲突的问题",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.purchase",
        category="narrative_event",
        body="上周把耳机换成了新的，音质好了不少",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.project_state",
        category="narrative_event",
        body="当前阶段先做好自用版本，暂时不做多用户功能",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.decision_note",
        category="narrative_event",
        body="已定决策：图谱采用最小方案，规模定为三类实体",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.learning",
        category="narrative_event",
        body="用户有持续学习技术、改善生活的意愿",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.narrative.request",
        category="narrative_event",
        body="用户要求以后不要再记录工作地点",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.narrative.past_trip",
        category="narrative_event",
        body="On 2025-06-17 the user travelled to a mountain temple",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.narrative.past_problem",
        category="narrative_event",
        body="Last week the build pipeline broke for a couple of hours",
        expected_nodes=(),
        expected_edges=(),
    ),
)


# ---------------------------------------------------------------------------
# 3 · ownership clauses — "用户有一只狗「胖子」".
#
# v2 has no OWNS relation in the frozen vocabulary (WORKS_ON / ATTENDS /
# BELONGS_TO / KNOWS only), so the honest label is "extract nothing" rather
# than "invent a PET node and a relation with no predicate". These cases exist
# to prove the punctuation-heavy 「」 shape does not accidentally satisfy one of
# the four keywords, and to pin where a future OWNS rule would start.
# ---------------------------------------------------------------------------
_OWNERSHIP = (
    EvalCase(
        case_id="zh.owns.pet_quoted",
        category="ownership",
        body="用户曾有一只狗「胖子」",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.pet_count",
        category="ownership",
        body="用户养有两只猫，一只叫团子一只叫煤球",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.device",
        category="ownership",
        body="用户有一台用于真机测试的手机",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.had_and_lost",
        category="ownership",
        body="早些年家里养过一只金毛，后来送人了",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.book",
        category="ownership",
        body="书架上有一本翻烂了的算法书",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.subscription",
        category="ownership",
        body="用户手上有几个长期不用的订阅账号",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.two_pets_and_insurance",
        category="ownership",
        body="给两只猫配了保险，还列了猫粮和猫砂的支出",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.owns.with_belongs_keyword",
        category="ownership",
        body="这只猫属于用户，名字叫团子",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=False,
    ),
    EvalCase(
        case_id="en.owns.basic",
        category="ownership",
        body="The user has a dog named Biscuit",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.owns.hardware",
        category="ownership",
        body="The user owns an older laptop kept for testing",
        expected_nodes=(),
        expected_edges=(),
    ),
)


# ---------------------------------------------------------------------------
# 4 · preference clauses — "用户偏好中文交流".
# No PREFERS relation exists in the frozen vocabulary, so nothing is extracted.
# ---------------------------------------------------------------------------
_PREFERENCE = (
    EvalCase(
        case_id="zh.prefers.language",
        category="preference",
        body="用户偏好中文交流",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.hobby",
        category="preference",
        body="我平时爱好摄影",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.hobby_second",
        category="preference",
        body="以后我更喜欢喝手冲咖啡",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.place",
        category="preference",
        body="用户喜欢海边，对舟山有较深的情感联系",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.work_habit",
        category="preference",
        body="用户习惯先写测试再改实现",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.emphasis_facts",
        category="preference",
        body="用户重视事实与推测的区分，要求明确标注证据边界",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.tool",
        category="preference",
        body="用户更喜欢用命令行而不是图形界面",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.food",
        category="preference",
        body="用户不太能吃辣，偏好清淡口味",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.prefers.style",
        category="preference",
        body="用户偏好简洁直接的回复风格",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.prefers.language",
        category="preference",
        body="The user prefers to communicate in Chinese",
        expected_nodes=(),
        expected_edges=(),
    ),
)


# ---------------------------------------------------------------------------
# 5 · project-membership clauses.
#
# Two distinct labels here, and the split is the point:
#  * "JarvisMind 属于个人项目" — 「属于」 is a keyword, so the rule fires and
#    produces BELONGS_TO with an EVENT subject, but JarvisMind is a PROJECT, not
#    an EVENT. Type is declared by the rule, not inferred, so the label records
#    the intrinsic-type mismatch as a **wrong** extraction.
#  * "季度复盘会属于北极星计划" — event → project, correct.
# ---------------------------------------------------------------------------
_MEMBERSHIP = (
    EvalCase(
        case_id="zh.belongs.event_to_project",
        category="project_membership",
        body="季度复盘会属于北极星计划",
        expected_nodes=(("季度复盘会", EVENT), ("北极星计划", PROJECT)),
        expected_edges=(("季度复盘会", "BELONGS_TO", "北极星计划"),),
    ),
    EvalCase(
        case_id="zh.belongs.plan_to_umbrella",
        category="project_membership",
        body="北极星计划属于年度路线图",
        expected_nodes=(("北极星计划", EVENT), ("年度路线图", PROJECT)),
        expected_edges=(("北极星计划", "BELONGS_TO", "年度路线图"),),
    ),
    EvalCase(
        case_id="zh.belongs.review_to_project",
        category="project_membership",
        body="知识图谱评审 属于 JarvisMind",
        expected_nodes=(("知识图谱评审", EVENT), ("JarvisMind", PROJECT)),
        expected_edges=(("知识图谱评审", "BELONGS_TO", "JarvisMind"),),
    ),
    EvalCase(
        case_id="zh.belongs.project_is_personal_wrong_type",
        category="project_membership",
        body="JarvisMind 属于个人项目",
        # A correct extractor must not claim JarvisMind is an EVENT.
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.belongs.demonstrative_subject",
        category="project_membership",
        body="这个仓库属于北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.membership.descriptive_no_keyword",
        category="project_membership",
        body="项目名称为北极星，定位为个人自用工具",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.membership.scope_note",
        category="project_membership",
        body="该模块只在夜间批量任务里使用",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.belongs.event_to_project",
        category="project_membership",
        body="Sprint Review belongs to Northstar",
        expected_nodes=(("Sprint Review", EVENT), ("Northstar", PROJECT)),
        expected_edges=(("Sprint Review", "BELONGS_TO", "Northstar"),),
    ),
    EvalCase(
        case_id="en.membership.descriptive",
        category="project_membership",
        body="Northstar is the umbrella name for the whole effort",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.belongs.demonstrative_subject",
        category="project_membership",
        body="This repository belongs to Northstar",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
)


# ---------------------------------------------------------------------------
# 6 · acquaintance clauses — "用户认识张三".
#
# 「用户」 is a role noun, not a pronoun, so it is a legitimate PERSON label:
# in a personal-memory corpus the narrator is a stable entity, and collapsing
# it is what the corpus wants. That is why these are labelled as extractions.
# ---------------------------------------------------------------------------
_ACQUAINTANCE = (
    EvalCase(
        case_id="zh.knows.user_meets_person",
        category="acquaintance",
        body="用户认识张三",
        expected_nodes=(("用户", PERSON), ("张三", PERSON)),
        expected_edges=(("用户", "KNOWS", "张三"),),
    ),
    EvalCase(
        case_id="zh.knows.user_meets_person_named",
        category="acquaintance",
        body="用户认识一位做后端的同事",
        expected_nodes=(("用户", PERSON), ("一位做后端的同事", PERSON)),
        expected_edges=(("用户", "KNOWS", "一位做后端的同事"),),
    ),
    EvalCase(
        case_id="zh.knows.two_named_people",
        category="acquaintance",
        body="林小满认识周时雨",
        expected_nodes=(("林小满", PERSON), ("周时雨", PERSON)),
        expected_edges=(("林小满", "KNOWS", "周时雨"),),
    ),
    EvalCase(
        case_id="zh.knows.across_clauses",
        category="acquaintance",
        body="张三认识李四。李四认识王五",
        expected_nodes=(("张三", PERSON), ("李四", PERSON), ("王五", PERSON)),
        expected_edges=(
            ("张三", "KNOWS", "李四"),
            ("李四", "KNOWS", "王五"),
        ),
    ),
    EvalCase(
        case_id="zh.knows.with_wrapping_quotes",
        category="acquaintance",
        body="「张三」认识（李四）",
        expected_nodes=(("张三", PERSON), ("李四", PERSON)),
        expected_edges=(("张三", "KNOWS", "李四"),),
    ),
    EvalCase(
        case_id="zh.knows.self_reference",
        category="acquaintance",
        body="张三认识张三",
        # 张三 is an unambiguous named person, so the node is correct. Only the
        # edge is nonsense: KNOWS is directed and a self-loop carries no fact.
        expected_nodes=(("张三", PERSON),),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.knows.basic_named",
        category="acquaintance",
        body="Alice knows Carol",
        expected_nodes=(("Alice", PERSON), ("Carol", PERSON)),
        expected_edges=(("Alice", "KNOWS", "Carol"),),
    ),
    EvalCase(
        case_id="en.knows.three_people",
        category="acquaintance",
        body="Alice knows Bob. Bob knows Carol",
        expected_nodes=(("Alice", PERSON), ("Bob", PERSON), ("Carol", PERSON)),
        expected_edges=(
            ("Alice", "KNOWS", "Bob"),
            ("Bob", "KNOWS", "Carol"),
        ),
    ),
    EvalCase(
        case_id="zh.knows.no_keyword",
        category="acquaintance",
        body="张三和李四是大学同学",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.knows.keyword_in_longer_word",
        category="acquaintance",
        body="系统的识别能力还不够稳定",
        expected_nodes=(),
        expected_edges=(),
    ),
)


# ---------------------------------------------------------------------------
# 7 · negation and questions — MUST NOT extract.
#
# These are the highest-value cases in the file. Before the v2 guards, every
# Chinese negation produced a person with the particle glued on: "张三不".
# ---------------------------------------------------------------------------
_NEGATION_QUESTION = (
    EvalCase(
        case_id="zh.negation.not_works_on",
        category="negation_question",
        body="张三不负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.negation.never_attends",
        category="negation_question",
        body="张三并不参加知识图谱评审",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.negation.not_knows",
        category="negation_question",
        body="用户不认识李四",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.negation.nobody_knows",
        category="negation_question",
        body="没有人认识李四",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.negation.seldom_attends",
        category="negation_question",
        body="用户不常参加晨间站会",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.negation.not_belongs",
        category="negation_question",
        body="季度复盘会不属于北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.question.what_does_x_do",
        category="negation_question",
        body="张三负责什么？",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.question.who_does_it",
        category="negation_question",
        body="谁负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.question.which_event",
        category="negation_question",
        body="张三参加哪个评审",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.question.whether",
        category="negation_question",
        body="李四是否认识王五",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.question.what_does_x_do",
        category="negation_question",
        body="Who works on Northstar",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.question.who_knows",
        category="negation_question",
        body="Who knows Carol",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.negation.never_attends",
        category="negation_question",
        body="Alice never attends Sprint Review",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.negation.knows_no_one",
        category="negation_question",
        body="Alice knows no one",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.negation.does_not_work_on",
        category="negation_question",
        body="Alice does not work on Northstar",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.negation.not_works_on_inflected",
        category="negation_question",
        body="Alice does not works on Northstar",
        # Ungrammatical, but the keyword *is* present: this case exercises the
        # negation guard rather than passing merely because no rule matched.
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
)


# ---------------------------------------------------------------------------
# 8 · pronoun subjects — MUST NOT extract.
#
# A pronoun is not an entity. Emitting the *object* alone would create a node
# for a statement whose subject is unknown: a fact with no author.
# ---------------------------------------------------------------------------
_PRONOUN = (
    EvalCase(
        case_id="zh.pronoun.she_works_on",
        category="pronoun_subject",
        body="她负责这个项目",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.pronoun.he_attends",
        category="pronoun_subject",
        body="他参加晨间站会",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.pronoun.we_know",
        category="pronoun_subject",
        body="我们认识张三",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.pronoun.they_work_on",
        category="pronoun_subject",
        body="他们负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.pronoun.conjunction_with_name",
        category="pronoun_subject",
        body="张三和她一起负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.pronoun.subject_only_explicit",
        category="pronoun_subject",
        body="她 认识 韩梅梅",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.pronoun.she_works_on",
        category="pronoun_subject",
        body="She works on Northstar",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.pronoun.they_know",
        category="pronoun_subject",
        body="They know Carol",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.pronoun.we_attend",
        category="pronoun_subject",
        body="We attend Sprint Review",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.pronoun.it_belongs",
        category="pronoun_subject",
        body="It belongs to Northstar",
        expected_nodes=(),
        expected_edges=(),
        must_not_extract=True,
    ),
)


# ---------------------------------------------------------------------------
# 9 · sensitive categories — MUST NOT extract.
#
# Nothing here is a credential; these exercise the *personal-sensitivity*
# category gate (D12), which excludes the whole body rather than flagging it.
# Bodies are synthetic; no real address, income or diagnosis is used.
# ---------------------------------------------------------------------------
_SENSITIVE = (
    EvalCase(
        case_id="zh.sensitive.health",
        category="sensitive",
        body="张三负责北极星计划，最近在医院做治疗",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.home_address",
        category="sensitive",
        body="用户家住某个小区三号楼，平时负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.finance",
        category="sensitive",
        body="用户月薪两万，负责北极星计划",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.religion",
        category="sensitive",
        body="用户有宗教信仰，平时参加晨间站会",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.sexual_orientation",
        category="sensitive",
        body="用户曾讨论过性取向的问题，也认识张三",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.politics",
        category="sensitive",
        body="用户的党员身份和投票记录，李四知道这件事",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.credential",
        category="sensitive",
        body="张三负责北极星计划，" + "密码 " + "hunter2",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="CREDENTIAL_PRESENT",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="zh.sensitive.phone_number",
        category="sensitive",
        body="张三负责北极星计划，联系电话 13800138000",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="SENSITIVE_DUAL_AUTH_REQUIRED",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.sensitive.health",
        category="sensitive",
        body="Alice works on Northstar but is in hospital for therapy",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
    EvalCase(
        case_id="en.sensitive.finance",
        category="sensitive",
        body="Bob attends Sprint Review and his annual income is private",
        expected_nodes=(),
        expected_edges=(),
        expected_eligible=False,
        expected_skip_reason="PERSONAL_SENSITIVE_CATEGORY",
        must_not_extract=True,
    ),
)


# ---------------------------------------------------------------------------
# 10 · pure noise — nothing to extract, and nothing is wrong with that.
#
# These are labelled "expect nothing", not "must not extract": a noise
# sentence contains no candidate to be wrong about, so it must not inflate the
# false-positive count. It exists to keep the coverage denominator honest.
# ---------------------------------------------------------------------------
_NOISE = (
    EvalCase(
        case_id="zh.noise.weather",
        category="noise",
        body="今天天气不错",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.acknowledgement",
        category="noise",
        body="好的，收到，明天再说",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.small_talk",
        category="noise",
        body="最近有点忙，回头再聊",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.coordination",
        category="noise",
        body="会议改到下午三点了",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.filler",
        category="noise",
        body="嗯嗯，就这样吧",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.mixed_ascii",
        category="noise",
        body="debug 日志已经贴到 issue 里了",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.short",
        category="noise",
        body="稍等",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise.empty_ish",
        category="noise",
        body="……",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.noise.weather",
        category="noise",
        body="The weather is fine today",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="en.noise.acknowledgement",
        category="noise",
        body="Sounds good, talk tomorrow",
        expected_nodes=(),
        expected_edges=(),
    ),
)


_CAPTURE_NOISE = (
    EvalCase(
        case_id="zh.noise_shape.list_job_duties",
        category="capture_noise",
        body="用户负责省区客服质管、仲裁、理赔、虚假问题件治理、升级工单等工作。",
        # The right-hand capture is a job-description list, not a project name.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.labelled_experience",
        category="capture_noise",
        body="用户职业经历：2021-07 至 2022-07 任分拨客服工单岗，负责热线电话与投诉工单。",
        # Left capture is a field label; right capture is a list of two duties.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.team_roster",
        category="capture_noise",
        body="团队分工：甲负责渠道、乙负责品效、丙负责客服",
        # Every capture spans a roster. Nothing here is one project.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.adverbial_domain",
        category="capture_noise",
        body="用户在物流快运行业负责省区客服质管与理赔工作",
        # "用户在物流快运行业" is a domain adverbial, not a person's name.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.adverbial_domain_long",
        category="capture_noise",
        body="用户在日常工作流程中负责协调三个小组的排班",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.background_relation",
        category="capture_noise",
        body="双方在总部工作或线上认识背景下发展，用户在省区、对方在总部",
        # "认识" appears inside a narrative description: the captures are clauses.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.mixed_separators",
        category="capture_noise",
        body="周一鸣参加周会/评审/站会三种活动",
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.field_then_relation",
        category="capture_noise",
        body="备注：张三负责北极星计划",
        # A labelled field is a fragment; the honest label is "nothing", because
        # the capture would carry the label into the entity name.
        expected_nodes=(),
        expected_edges=(),
    ),
    EvalCase(
        case_id="zh.noise_shape.short_valid_name_survives",
        category="capture_noise",
        body="张三负责北极星计划",
        # Control case: the guards must not touch a clean short clause.
        expected_nodes=(("张三", PERSON), ("北极星计划", PROJECT)),
        expected_edges=(("张三", "WORKS_ON", "北极星计划"),),
    ),
    EvalCase(
        case_id="zh.noise_shape.project_name_with_zai",
        category="capture_noise",
        body="张三负责在线支付平台",
        # Control: 在 inside a PROJECT name is legitimate and must survive.
        expected_nodes=(("张三", PERSON), ("在线支付平台", PROJECT)),
        expected_edges=(("张三", "WORKS_ON", "在线支付平台"),),
    ),
)


#: The whole corpus. 121 cases across 11 categories.
EVAL_CASES: Tuple[EvalCase, ...] = (
    _EXPLICIT
    + _NARRATIVE
    + _OWNERSHIP
    + _PREFERENCE
    + _MEMBERSHIP
    + _ACQUAINTANCE
    + _NEGATION_QUESTION
    + _PRONOUN
    + _SENSITIVE
    + _NOISE
    + _CAPTURE_NOISE
)

#: Category order used by the report and the per-category test.
CATEGORIES: Tuple[str, ...] = (
    "explicit_relation",
    "narrative_event",
    "ownership",
    "preference",
    "project_membership",
    "acquaintance",
    "negation_question",
    "pronoun_subject",
    "sensitive",
    "noise",
    "capture_noise",
)


def cases_in(category: str) -> Tuple[EvalCase, ...]:
    """Return the cases of one category, in corpus order."""
    return tuple(case for case in EVAL_CASES if case.category == category)


__all__ = [
    "PERSON",
    "PROJECT",
    "EVENT",
    "NodeLabel",
    "EdgeLabel",
    "EvalCase",
    "EVAL_CASES",
    "CATEGORIES",
    "cases_in",
]
