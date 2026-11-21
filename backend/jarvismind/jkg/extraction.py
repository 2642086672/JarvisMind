"""Deterministic rule-based candidate extraction for the derived graph (INT-04).

What this module is
-------------------
A pure function: text in, immutable candidate set out. It performs no I/O, no
database access, no schema change and no model call. Its whole job is to turn an
approved memory body into entity/relation *candidates* that a later, explicitly
authorised stage may write. Nothing here writes the graph — see the C3 boundary
section below.

Rule shape (deliberately narrow)
--------------------------------
Only the frozen vocabularies are produced:

* entities ``PERSON`` / ``PROJECT`` / ``EVENT``
* relations ``WORKS_ON`` / ``ATTENDS`` / ``BELONGS_TO`` / ``KNOWS``

Eight explicit clause forms are recognised (four Chinese, four English). A
clause that does not match one of them yields nothing: this stage never guesses
a relation from co-occurrence, never infers a type from a name's spelling, and
never chains two clauses. Missing an extraction is acceptable; inventing one is
not.

A clause is a span of the body delimited by sentence punctuation. Each rule is a
single keyword (``负责`` / ``works on`` / ...). A clause is used only when that
keyword occurs **exactly once** in it — a second occurrence means the split is
ambiguous, so the clause is rejected rather than resolved by guessing which side
is which.

Assertiveness guards (``jkg-rules/2``)
--------------------------------------
A keyword inside a clause is not yet a fact. Four shapes assert nothing even
though the keyword is present, and each is rejected as a whole clause:

* **negated** — ``张三不负责 JarvisMind``, ``Alice never attends …``, ``没有人认识李四``.
  Without this guard the negation particle is glued onto the subject and the
extractor emits a person called ``张三不``.
* **interrogative** — ``张三负责什么？``, ``谁负责 JarvisMind``. A question asks for
  the value; it does not assert it.
* **pronoun endpoint** — ``她负责这个项目``. A pronoun cannot be an entity, and
  once one endpoint is unattributable the surviving endpoint is a dangling node
  for a relation nobody stated.
* **non-naming capture** — a capture that starts with a demonstrative
  (``这个项目``) or with the structural particle ``的`` (``负责的项目``) is not a
  name: the keyword sits inside a noun phrase, not in a predicate.

Every guard is a *rejection*. None of them adds a candidate, so they can only
raise precision; each carries its own stable reason code so a report can say
which shape was skipped.

Name handling
-------------
Names are trimmed of surrounding whitespace and wrapping punctuation, length
bounded to 1..64, and folded with ``normalization.canonicalize_entity_name`` /
``normalize_entity_name``. This module deliberately contains no case-folding or
whitespace logic of its own: two implementations of "same entity" would drift.

Privacy gate (three layers)
---------------------------
1. The **whole body** is graded with ``evaluate()`` *before* any rule runs. A
   credential or an injection signal stops extraction entirely: a body that
   holds a secret is not a body we mine for entities.
2. A ``SENSITIVE`` grade (personal data) also stops extraction, per decision
   D12: PII is excluded from the graph by default, not merely flagged.
3. Every accepted candidate **name** is graded again on its own. A name that
   grades badly is dropped and the rest of the memory survives, because one bad
   label must not destroy the useful part of an approved memory.

Rejections carry a rule label, a machine reason code and a human sentence. They
never carry the matched text: a rejection is part of an API response, and the
text it describes is exactly what must not travel.

C3 boundary
-----------
This module imports nothing that can write. It does not import ``JkgService``,
it opens no connection, and it holds no candidate table: a preview lives in
memory and is returned, never persisted. Callers decide whether an operator
approves anything, and the write itself belongs to an explicitly invoked path.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core import personal_sensitivity
from ..core.errors import NotFoundError, ValidationError
from ..jmr.sensitive import (
    DECISION_ALLOW,
    DECISION_HOLD,
    LEVEL_SENSITIVE,
    PrivacyDecision,
    combine,
    evaluate,
)
from .normalization import (
    VALID_SOURCE_STATUSES,
    canonicalize_entity_name,
    deserialize_metadata,
    normalize_entity_name,
    validate_entity_type,
    validate_relation_type,
)

#: Bumped whenever the rule set or the acceptance criteria change. A stored
#: candidate set is only comparable with one produced by the same version.
#:
#: ``jkg-rules/2`` adds the assertiveness guards (negation / interrogative /
#: pronoun subject / non-naming capture) and adds no rule. Every stored metadata
#: row carrying ``jkg-rules/1`` therefore means "same eight clause forms, without
#: the guards": its node set is a superset of what v2 would produce from the same
#: text, never a different set.
RULE_VERSION = "jkg-rules/2"

#: Reason code for a body or name that touches one of the personal-sensitivity
#: categories (health, home address, finance, sexual orientation, religion,
#: politics). A stable code only: the matched category is never echoed.
REASON_PERSONAL_SENSITIVE_CATEGORY = "PERSONAL_SENSITIVE_CATEGORY"

#: Inclusive name bounds enforced here. ``normalization`` allows longer labels
#: for hand-entered entities; a rule-derived name is held to a tighter bound
#: because it was cut out of a running sentence.
MIN_NAME_LENGTH = 1
MAX_NAME_LENGTH = 64

# Characters trimmed from both ends of a captured name. Wrapping punctuation is
# never part of an entity: "「Alice」" and "(Alice)" name the same person.
_WRAPPER_CHARS = frozenset(
    " \t\r\n\"'`“”‘’「」『』《》〈〉()（）[]【】{}<>:：,，、.。!！?？;；-—~·"
)

# Characters that may not survive *inside* a name: they mean the clause split
# went wrong, so the capture spans two sentences and is not a name.
_INNER_DELIMITERS = frozenset("。；;！!？?\n\r")

# Pronouns are not entities. Producing "她" or "they" as a PERSON node would
# create a node that merges every pronoun in the corpus into one vertex.
_PRONOUNS = frozenset(
    {
        "我", "你", "您", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
        "i", "you", "he", "she", "it", "we", "they",
    }
)

# A clause ends at CJK sentence punctuation, a newline, or an ASCII period that
# is followed by whitespace. The lookahead keeps "JarvisMind 2.0" in one clause
# while still splitting "… JarvisMind. Bob knows Alice." into two.
_CLAUSE_SPLIT = re.compile(r"[。；;！!？?\n\r]+|\.(?=\s)")

#: Reason codes for a clause that carries the keyword but asserts no relation.
REASON_NEGATED_CLAUSE = "NEGATED_CLAUSE"
REASON_INTERROGATIVE_CLAUSE = "INTERROGATIVE_CLAUSE"
REASON_NON_NAMING_CAPTURE = "NON_NAMING_CAPTURE"

# A negation particle attaches to the *end* of the subject capture: the text
# before a predicate keyword is where "张三不 / 张三并不 / Alice never" lands.
# Matching the tail only (not the whole clause) keeps a name that happens to
# contain 不 elsewhere ("不凡工作室") usable. Bare 未 / 无 / 非 / 别 are
# deliberately absent: "未来项目" and "无双工作室" are names, not negations.
_NEGATION_TAIL_CJK = (
    "并不", "从不", "从未", "绝不", "永不", "未曾", "不曾", "不常", "没有", "不", "没",
)
# ASCII needs a word boundary: "Bruno".endswith("no") is true but is not a
# negation. The lookbehind rejects a match glued to a preceding letter.
_NEGATION_TAIL_ASCII = re.compile(
    r"(?<![0-9A-Za-z])(?:never|not|no|none|nobody|neither|nor|cannot|can't|"
    r"can’t|don't|don’t|doesn't|doesn’t|didn't|didn’t|isn't|isn’t|wasn't|"
    r"wasn’t|won't|won’t|hardly|seldom|rarely|without|no\s+longer|"
    r"fail(?:ed|s)?\s+to)\s*$",
    re.IGNORECASE,
)
# A negation *opening* the subject capture names nobody: "没有人认识李四".
_NEGATION_OPENER_CJK = ("没有人", "没人", "无人", "谁也没", "谁都不")
# A negation right after the keyword negates the predicate itself
# ("负责不了", "参加不了").
_NEGATION_HEAD_CJK = ("不", "没")
_NEGATION_HEAD_ASCII = re.compile(
    r"^\s*(?:not|never|no|n't|n’t|cannot|can't|can’t)\b", re.IGNORECASE
)

# A question asks for a value; it does not assert one. "？" is already consumed
# by ``_CLAUSE_SPLIT``, so the interrogative *words* carry the signal.
#
# 哪 is listed in its determiner/adverbial forms only. Bare 哪 would also reject
# a person called 哪吒, and a missed extraction is cheaper than a wrong guard.
_INTERROGATIVE_MARKERS = (
    "什么", "谁", "哪里", "哪儿", "哪个", "哪些", "哪位", "哪种", "哪家",
    "哪一", "怎么", "为什么", "如何", "是否", "多少", "何时", "何地",
    "何人", "吗", "呢",
)
# English interrogatives. Matched as whole words so "Knowsley" is not a
# question and "what" inside "somewhat" is not one either.
_INTERROGATIVE_ASCII = re.compile(
    r"(?<![0-9A-Za-z])(?:who|what|which|when|where|why|how|whom|whose)\b",
    re.IGNORECASE,
)
# A demonstrative determiner opens a noun phrase, not a name: "这个项目" is a
# reference to something already mentioned, not a stable entity label.
#
# 这/那 require a measure word; 该/此/上述 are determiners on their own. Bare 本
# is deliberately absent: it is also an ordinary name morpheme ("本田", "本人").
_DEMONSTRATIVE_OPENER = re.compile(
    r"^\s*(?:这|那)(?:个|些|只|位|件|条|种|款|台|项|名|次|份|套)"
    r"|^\s*(?:该|此|上述)(?:个|些|只|位|件|条|种|款|台|项|名|次|份|套|项目|等)?"
    r"|^\s*(?:this|that|these|those|the|its|his|her|their|our|my|your)\b",
    re.IGNORECASE,
)
# The structural particle 的 turns the keyword into a noun-phrase modifier
# ("JarvisMind 负责的项目"), so the capture is a description, not a name.
_STRUCTURAL_PARTICLE = "的"

# A classification phrase is a modifier plus the ordinary head noun of the
# declared entity type: "个人项目" is a *kind* of project, not the name of one.
# Such a phrase classifies rather than names, so binding a relation to it would
# attach a fact to a category.
#
# Both halves are closed sets on purpose. A proper name ("北极星项目") carries a
# modifier outside the set and is therefore still extracted, so this guard costs
# no recall on real names — it only refuses to invent an entity from a
# description.
_TYPE_HEAD_NOUNS = {
    "PROJECT": ("项目", "工程", "计划", "工具"),
    "EVENT": ("事件", "会议", "活动"),
    "PERSON": ("人员", "人士"),
}
_CLASSIFICATION_MODIFIERS = (
    "个人", "私人", "内部", "外部", "公共", "商业", "开源", "自研",
    "临时", "长期", "独立", "联合",
)


def _is_classification_phrase(value: str, entity_type: str) -> bool:
    """True when ``value`` is a modifier + type head noun, i.e. a category."""
    heads = _TYPE_HEAD_NOUNS.get(entity_type, ())
    if not any(value.endswith(head) for head in heads):
        return False
    return any(value.startswith(modifier) for modifier in _CLASSIFICATION_MODIFIERS)


# Separators that mean the capture spans more than one item: a list is not a
# name, whatever it enumerates. The ASCII forms are included because a real body
# mixes full-width and half-width freely.
_LIST_SEPARATORS = (
    "、", "，", ",", "；", ";", "／", "/", "｜", "|", "\u00b7",
)
# A colon means the capture began inside a labelled field ("用户职业经历：…",
# "分工：…"). A name never contains a field label, so the capture is a fragment.
_FIELD_MARKERS = (":", "：")
# A locative or temporal preposition inside a PERSON capture means the capture is
# a leading adverbial phrase: "用户在物流快运行业负责…" captured
# "用户在物流快运行业" — the prepositional phrase qualifies *where* the person
# works, it does not name them.
#
# The preposition must have at least two characters before it: a name may begin
# with 在 ("在线支付平台", if it were ever a PERSON) but a clause subject that
# *contains* one at position >=2 is "X 在 Y", i.e. a phrase. Only applied to
# PERSON: a PROJECT legitimately contains 在 and 于 ("在线支付平台").
_ADVERBIAL_PATTERN = re.compile(r"^.{2,}[在于]")
# The remaining prepositions are only suspicious in an already-implausibly-long
# PERSON capture. Testing them on short captures would reject real names.
_ADVERBIAL_MARKERS = ("在", "于", "从", "对", "向", "给", "为", "自")
_LONG_CAPTURE = 24


def _capture_reason(value: str, entity_type: str) -> Optional[str]:
    """Return the reason this capture names no entity, or ``None``.

    Measured motivation: on the real 172-memory corpus every capture containing
    a list separator, a colon or an embedded locative was garbage —
    "用户职业经历:2021-07 至 …", "省区客服质管、仲裁、理赔…",
    "用户在物流快运行业". None of the honest names in the corpus contain any of
    these shapes, so the guard trades no recall for a large precision gain.
    """
    if _is_classification_phrase(value, entity_type):
        return REASON_CLASSIFICATION_PHRASE
    if any(separator in value for separator in _LIST_SEPARATORS):
        return REASON_FRAGMENT_CAPTURE
    if any(marker in value for marker in _FIELD_MARKERS):
        return REASON_FRAGMENT_CAPTURE
    if entity_type == "PERSON":
        if _ADVERBIAL_PATTERN.match(value):
            return REASON_ADVERBIAL_CAPTURE
        if len(value) > _LONG_CAPTURE:
            if any(marker in value for marker in _ADVERBIAL_MARKERS):
                return REASON_ADVERBIAL_CAPTURE
    return None


#: Reason code reused for a clause whose subject is a pronoun. A pronoun cannot
#: be an entity, and once the subject is unattributable the relation is not a
#: fact — emitting the object as a standalone node would invent a dangling
#: vertex for a statement nobody made.
REASON_PRONOUN_NAME = "PRONOUN_NAME"

#: A relation whose two endpoints are the same name states nothing a directed
#: edge can hold. The clause still names a real entity, so the node is kept and
#: only the edge is dropped.
REASON_SELF_REFERENCE = "SELF_REFERENCE"

#: A capture that is a modifier plus the ordinary head noun of its declared type
#: ("个人项目", "内部工具") names a category, not an entity.
REASON_CLASSIFICATION_PHRASE = "CLASSIFICATION_PHRASE"

#: A capture that spans a list or a metadata field is a clause fragment, not a
#: name. Measured on the real corpus: 7/172 bodies put a keyword inside such a
#: capture, and every one of them produced a garbage entity.
REASON_FRAGMENT_CAPTURE = "FRAGMENT_CAPTURE"

#: A PERSON-typed capture containing a locative preposition is an adverbial
#: phrase, not a person. "用户在物流快运行业" is the sentence's domain, not a name.
REASON_ADVERBIAL_CAPTURE = "ADVERBIAL_CAPTURE"

# Separators that join two conjuncts. A capture is split on these before the
# pronoun test so "Alice 和 她" is rejected without a substring test on 她 (which
# would also fire on "其他" and "她们家的" — neither is a bare pronoun).
_CONJUNCT_SPLIT = re.compile(
    r"[和与及跟\u3001,，/]|\band\b|&|一起|共同|都|也|还|再|又", re.IGNORECASE
)


def _has_pronoun_conjunct(value: str) -> bool:
    """True when any conjunct of ``value`` is a bare pronoun.

    Whole-conjunct comparison only: a substring test would reject "其他项目"
    (他 inside 其他) and "她们家的猫", neither of which is a bare pronoun.
    ``Alice 和 她`` is rejected because one conjunct is unattributable: the
    subject of the relation is then not fully identified.
    """
    parts = [part.strip() for part in _CONJUNCT_SPLIT.split(value)]
    return any(part.casefold() in _PRONOUNS for part in parts if part)


def _assertiveness_reason(left_raw: str, right_raw: str) -> Optional[str]:
    """Return a reason code when the clause asserts no relation, else ``None``.

    Pure text inspection of the two captures. It never inspects the rule table
    and never adds a candidate: the only outcomes are "the clause is usable" or
    "the clause is rejected with a stable code".

    Check order is deliberate — a question about a pronoun ("她负责什么？") is
    reported as a question, because that is the reason the clause cannot be
    mined, not the pronoun.
    """
    left = left_raw.strip()
    right = right_raw.strip()

    for marker in _INTERROGATIVE_MARKERS:
        if marker in left or marker in right:
            return REASON_INTERROGATIVE_CLAUSE
    if _INTERROGATIVE_ASCII.search(left) or _INTERROGATIVE_ASCII.search(right):
        return REASON_INTERROGATIVE_CLAUSE

    if _has_pronoun_conjunct(left) or _has_pronoun_conjunct(right):
        return REASON_PRONOUN_NAME

    if left:
        if any(left.endswith(marker) for marker in _NEGATION_TAIL_CJK):
            return REASON_NEGATED_CLAUSE
        if _NEGATION_TAIL_ASCII.search(left):
            return REASON_NEGATED_CLAUSE
        if any(left.startswith(marker) for marker in _NEGATION_OPENER_CJK):
            return REASON_NEGATED_CLAUSE
    if right:
        if any(right.startswith(marker) for marker in _NEGATION_HEAD_CJK):
            return REASON_NEGATED_CLAUSE
        if _NEGATION_HEAD_ASCII.match(right):
            return REASON_NEGATED_CLAUSE

    for value in (left, right):
        if not value:
            continue
        if _DEMONSTRATIVE_OPENER.match(value):
            return REASON_NON_NAMING_CAPTURE
        if value.endswith(_STRUCTURAL_PARTICLE) or value.startswith(_STRUCTURAL_PARTICLE):
            return REASON_NON_NAMING_CAPTURE
    return None


@dataclass(frozen=True)
class _Rule:
    """One explicit clause form.

    ``subject_type`` / ``object_type`` are declared by the rule, not inferred
    from the captured text. A rule that wants ``WORKS_ON`` must already know its
    left side is a person and its right side a project; that knowledge lives in
    the pattern, where it can be reviewed.
    """

    rule_id: str
    keyword: str
    predicate: str
    subject_type: str
    object_type: str
    confidence: float
    ascii_word: bool


_RULES: Tuple[_Rule, ...] = (
    _Rule("zh.works_on", "负责", "WORKS_ON", "PERSON", "PROJECT", 0.60, False),
    _Rule("zh.attends", "参加", "ATTENDS", "PERSON", "EVENT", 0.60, False),
    _Rule("zh.belongs_to", "属于", "BELONGS_TO", "EVENT", "PROJECT", 0.55, False),
    _Rule("zh.knows", "认识", "KNOWS", "PERSON", "PERSON", 0.60, False),
    _Rule("en.works_on", "works on", "WORKS_ON", "PERSON", "PROJECT", 0.60, True),
    _Rule("en.attends", "attends", "ATTENDS", "PERSON", "EVENT", 0.60, True),
    _Rule("en.belongs_to", "belongs to", "BELONGS_TO", "EVENT", "PROJECT", 0.55, True),
    _Rule("en.knows", "knows", "KNOWS", "PERSON", "PERSON", 0.60, True),
)


def _keyword_pattern(keyword: str, *, ascii_word: bool) -> "re.Pattern[str]":
    """Compile one rule keyword.

    An ASCII keyword is matched on word boundaries so ``knows`` does not fire
    inside ``unknowns``. A CJK keyword needs no boundary: it is matched
    literally, because CJK text has no inter-word spacing to anchor on.
    """
    escaped = re.escape(keyword)
    if ascii_word:
        return re.compile(rf"(?<![0-9A-Za-z_]){escaped}(?![0-9A-Za-z_])", re.IGNORECASE)
    return re.compile(escaped)


_KEYWORD_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    rule.rule_id: _keyword_pattern(rule.keyword, ascii_word=rule.ascii_word)
    for rule in _RULES
}


def _strip_format_characters(value: str) -> str:
    """Remove format/control characters (zero-width joiners, BOMs).

    The detector's credential patterns cannot see through a character inserted
    into the middle of a keyword, so the body is graded twice: once as written
    and once with those characters removed. A hit in either pass stops
    extraction. This is a second evaluation of the *same* detector — it adds no
    rule of its own and can only make the outcome stricter.
    """
    return "".join(
        char for char in value if not unicodedata.category(char).startswith("C")
    )


def _personal_categories(text: str) -> Tuple[str, ...]:
    return tuple(personal_sensitivity.categories(text))


def _body_decision(
    text: Optional[str], *, exact_tokens: Tuple[str, ...] = ()
) -> PrivacyDecision:
    """Grade the body as written, and again with format characters stripped.

    A personal-sensitivity hit on either pass downgrades an ALLOW to a HOLD with
    ``REASON_PERSONAL_SENSITIVE_CATEGORY``. A body already at HOLD or DENY keeps
    its stronger reason.
    """
    raw = evaluate(text, exact_tokens=exact_tokens)
    plain = _strip_format_characters(text or "")
    if plain == (text or ""):
        decision = raw
        personal = _personal_categories(text or "")
    else:
        decision = combine(raw, evaluate(plain, exact_tokens=exact_tokens))
        personal = tuple(sorted(set(_personal_categories(text or "")) | set(_personal_categories(plain))))
    if personal and decision.decision == DECISION_ALLOW:
        return PrivacyDecision(
            level=LEVEL_SENSITIVE,
            decision=DECISION_HOLD,
            reason_code=REASON_PERSONAL_SENSITIVE_CATEGORY,
            pii_categories=personal,
            requires_dual_authorization=True,
        )
    return decision


def _human_reason(reason_code: str) -> str:
    """Fixed, value-free explanation for a machine reason code."""
    return {
        "EMPTY_NAME": "The name was empty once surrounding punctuation was removed.",
        "NAME_TOO_LONG": f"The name exceeds {MAX_NAME_LENGTH} characters.",
        "NAME_CONTAINS_DELIMITER": "The capture spans a sentence boundary.",
        "NAME_NOT_NORMALISABLE": "The name could not be standardised.",
        "PRONOUN_NAME": "A pronoun is not an entity.",
        "NEGATED_CLAUSE": "The clause negates the relation, so it asserts nothing.",
        "INTERROGATIVE_CLAUSE": "The clause asks a question; a question asserts no fact.",
        "NON_NAMING_CAPTURE": "The keyword sits inside a noun phrase, not in a predicate.",
        "SELF_REFERENCE": "A relation cannot relate a name to itself.",
        "CLASSIFICATION_PHRASE": "The capture names a category, not an entity.",
        "FRAGMENT_CAPTURE": "The capture spans a list or metadata field, not a name.",
        "ADVERBIAL_CAPTURE": "The capture is an adverbial phrase, not an entity name.",
        "AMBIGUOUS_CLAUSE": "The clause contains the keyword more than once.",
        "INVALID_TYPE": "The rule declared a type outside the frozen vocabulary.",
        "ENDPOINT_DROPPED": "One endpoint was dropped, so the relation has no origin.",
        "SOURCE_NOT_EXTRACTABLE": "The source memory is not in an extractable state.",
        "CREDENTIAL_PRESENT": "A credential was detected; nothing is extracted.",
        "SENSITIVE_DUAL_AUTH_REQUIRED": "Personal data needs a second approval; nothing enters the graph.",
        "PERSONAL_SENSITIVE_CATEGORY": "A personal-sensitivity category was detected; nothing enters the graph.",
        "PROMPT_INJECTION_SUSPECTED": "An injection signal was detected; nothing is extracted.",
        "DETECTION_FAILED": "The privacy detector failed closed; nothing is extracted.",
    }.get(reason_code, "The candidate was rejected.")


@dataclass(frozen=True)
class Rejection:
    """Why one candidate (or one whole memory) produced nothing.

    Holds a rule label, a code and a sentence. It never holds the captured text:
    the whole point of a rejection is that the surrounding value is not echoed.
    """

    rule: str
    reason_code: str
    human_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "reason_code": self.reason_code,
            "human_reason": self.human_reason,
        }


@dataclass(frozen=True)
class Evidence:
    """Where a candidate came from: the rule that fired and a character span.

    The span indexes the source body. It is arithmetic, not a copy: a caller can
    slice the body itself, which keeps a preview free of body text.
    """

    rule: str
    start: int
    end: int

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class NodeCandidate:
    entity_type: str
    name: str
    normalized_key: str
    confidence: float
    evidence: Evidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "name": self.name,
            "normalized_key": self.normalized_key,
            "confidence": self.confidence,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True)
class EdgeCandidate:
    subject_key: str
    predicate: str
    object_key: str
    subject_type: str
    object_type: str
    confidence: float
    evidence: Evidence
    source_memory_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject_key": self.subject_key,
            "predicate": self.predicate,
            "object_key": self.object_key,
            "subject_type": self.subject_type,
            "object_type": self.object_type,
            "confidence": self.confidence,
            "evidence": self.evidence.to_dict(),
            "source_memory_id": self.source_memory_id,
        }


@dataclass(frozen=True)
class ExtractionResult:
    """Immutable outcome of one extraction. Safe to serialise and to log."""

    memory_id: str
    rule_version: str
    decision: PrivacyDecision
    eligible: bool
    nodes: Tuple[NodeCandidate, ...]
    edges: Tuple[EdgeCandidate, ...]
    rejections: Tuple[Rejection, ...]
    duplicates_removed: int
    source_memory_status: Optional[str] = None
    metadata_supplied: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready preview (C3: returned, never persisted).

        Contains the accepted labels — they are the point of the preview — and
        the privacy *codes*. It contains no sentence from the body, no
        ``detail_json`` and no matched value, so the payload can be shown to an
        operator and written to a log without another redaction pass.
        """
        return {
            "memory_id": self.memory_id,
            "rule_version": self.rule_version,
            "eligible": self.eligible,
            "source_memory_status": self.source_memory_status,
            "metadata_supplied": self.metadata_supplied,
            "privacy": self.decision.to_dict(),
            "counts": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "rejections": len(self.rejections),
                "duplicates_removed": self.duplicates_removed,
            },
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "rejections": [item.to_dict() for item in self.rejections],
        }


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------
def _trim(raw: str) -> Tuple[str, int, int]:
    """Trim wrapping characters and report the surviving span.

    Returns ``(value, start, end)`` where the offsets are relative to ``raw``, so
    the caller can turn them into absolute body offsets without re-searching for
    the text (a second search could land on a different occurrence).
    """
    left = 0
    right = len(raw)
    while left < right and raw[left] in _WRAPPER_CHARS:
        left += 1
    while right > left and raw[right - 1] in _WRAPPER_CHARS:
        right -= 1
    return raw[left:right], left, right


def _standardise(
    value: str, *, rule_id: str, rejections: List[Rejection]
) -> Optional[Tuple[str, str]]:
    """Return ``(canonical_name, normalized_key)`` or record a rejection."""
    if not value:
        rejections.append(Rejection(rule_id, "EMPTY_NAME", _human_reason("EMPTY_NAME")))
        return None
    if len(value) > MAX_NAME_LENGTH:
        rejections.append(
            Rejection(rule_id, "NAME_TOO_LONG", _human_reason("NAME_TOO_LONG"))
        )
        return None
    if any(char in _INNER_DELIMITERS for char in value):
        rejections.append(
            Rejection(
                rule_id, "NAME_CONTAINS_DELIMITER", _human_reason("NAME_CONTAINS_DELIMITER")
            )
        )
        return None
    if value.casefold() in _PRONOUNS:
        rejections.append(
            Rejection(rule_id, "PRONOUN_NAME", _human_reason("PRONOUN_NAME"))
        )
        return None
    try:
        canonical = canonicalize_entity_name(value)
        normalized = normalize_entity_name(canonical)
    except ValidationError:
        rejections.append(
            Rejection(
                rule_id, "NAME_NOT_NORMALISABLE", _human_reason("NAME_NOT_NORMALISABLE")
            )
        )
        return None
    if not MIN_NAME_LENGTH <= len(canonical) <= MAX_NAME_LENGTH:
        rejections.append(
            Rejection(rule_id, "NAME_TOO_LONG", _human_reason("NAME_TOO_LONG"))
        )
        return None
    return canonical, normalized


def _name_verdict(
    name: str,
    *,
    rule_id: str,
    rejections: List[Rejection],
    exact_tokens: Tuple[str, ...] = (),
) -> bool:
    """Second privacy layer: may *this label* enter the graph?

    ``evaluate`` is the single detector; this function only decides what a bad
    verdict means for one candidate. Anything that is not ``ALLOW``, and any
    personal-sensitivity category, drops the candidate.
    """
    decision = evaluate(name, exact_tokens=exact_tokens)
    if decision.decision != DECISION_ALLOW:
        rejections.append(
            Rejection(rule_id, decision.reason_code, _human_reason(decision.reason_code))
        )
        return False
    if _personal_categories(name):
        rejections.append(
            Rejection(
                rule_id,
                REASON_PERSONAL_SENSITIVE_CATEGORY,
                _human_reason(REASON_PERSONAL_SENSITIVE_CATEGORY),
            )
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Clause walking
# ---------------------------------------------------------------------------
def _clauses(text: str) -> List[Tuple[int, int, str]]:
    """Split ``text`` into ``(start, end, clause)`` triples of absolute offsets."""
    spans: List[Tuple[int, int, str]] = []
    cursor = 0
    for match in _CLAUSE_SPLIT.finditer(text):
        spans.append((cursor, match.start(), text[cursor : match.start()]))
        cursor = match.end()
    spans.append((cursor, len(text), text[cursor:]))
    return spans


def extract_candidates(
    text: Optional[str],
    *,
    memory_id: str,
    metadata: Any = None,
    memory_status: Optional[str] = None,
    exact_tokens: Iterable[str] = (),
) -> ExtractionResult:
    """Extract entity/relation candidates from one memory body.

    ``memory_status`` is optional: when the caller already knows the source
    row's state it is honoured (a ``DELETED`` / ``MERGED`` memory yields nothing
    and reports ``eligible=False``); when it is ``None`` only the privacy gate
    decides eligibility, which is what a pure text-level caller can honestly
    claim.

    ``metadata`` is accepted for signature stability with the preview caller but
    contributes nothing here: rule extraction runs on the body, and metadata is
    already graded at the HTTP exit by ``jkg.governance``. Grading it in two
    places with two different outcomes would be worse than grading it once.

    ``exact_tokens`` are live secrets with no regex shape. They are passed to
    both the body gate and the per-name gate. The default ``()`` keeps the
    behaviour of the earlier callers unchanged.

    A body or name that touches a personal-sensitivity category is excluded
    (whole body for the body gate, that one candidate for the name gate).
    """
    rejections: List[Rejection] = []
    tokens: Tuple[str, ...] = tuple(exact_tokens)
    decision = _body_decision(text, exact_tokens=tokens)

    if not decision.may_auto_proceed:
        # Layers 1 and 2: the body itself is unsafe. Nothing is mined, and the
        # reason code is the detector's own — never a paraphrase, never the text.
        rejections.append(
            Rejection("privacy_gate", decision.reason_code, _human_reason(decision.reason_code))
        )
        return ExtractionResult(
            memory_id=memory_id,
            rule_version=RULE_VERSION,
            decision=decision,
            eligible=False,
            nodes=(),
            edges=(),
            rejections=tuple(rejections),
            duplicates_removed=0,
            source_memory_status=memory_status,
            metadata_supplied=metadata is not None,
        )

    if memory_status is not None and memory_status not in VALID_SOURCE_STATUSES:
        rejections.append(
            Rejection(
                "source_status",
                "SOURCE_NOT_EXTRACTABLE",
                _human_reason("SOURCE_NOT_EXTRACTABLE"),
            )
        )
        return ExtractionResult(
            memory_id=memory_id,
            rule_version=RULE_VERSION,
            decision=decision,
            eligible=False,
            nodes=(),
            edges=(),
            rejections=tuple(rejections),
            duplicates_removed=0,
            source_memory_status=memory_status,
            metadata_supplied=metadata is not None,
        )

    body = text or ""
    nodes: Dict[Tuple[str, str], NodeCandidate] = {}
    raw_edges: List[Tuple[EdgeCandidate, str]] = []
    duplicates_removed = 0

    for clause_start, _clause_end, clause in _clauses(body):
        if not clause.strip():
            continue
        for rule in _RULES:
            matches = list(_KEYWORD_PATTERNS[rule.rule_id].finditer(clause))
            if not matches:
                continue
            if len(matches) > 1:
                rejections.append(
                    Rejection(rule.rule_id, "AMBIGUOUS_CLAUSE", _human_reason("AMBIGUOUS_CLAUSE"))
                )
                continue

            try:
                predicate = validate_relation_type(rule.predicate)
                subject_type = validate_entity_type(rule.subject_type)
                object_type = validate_entity_type(rule.object_type)
            except ValidationError:
                rejections.append(
                    Rejection(rule.rule_id, "INVALID_TYPE", _human_reason("INVALID_TYPE"))
                )
                continue

            match = matches[0]
            left_raw = clause[: match.start()]
            right_raw = clause[match.end() :]

            # Assertiveness guard: a keyword in a negated, interrogative or
            # pronoun-subject clause asserts nothing. Rejecting the whole
            # clause here is what keeps "张三不负责 X" from producing a person
            # called "张三不" — and it can only remove candidates, never add
            # one, so it cannot lower precision.
            unasserted = _assertiveness_reason(left_raw, right_raw)
            if unasserted is not None:
                rejections.append(
                    Rejection(rule.rule_id, unasserted, _human_reason(unasserted))
                )
                continue

            # (The pronoun test lives inside _assertiveness_reason: it needs the
            # same conjunct handling as the other guards, and duplicating it here
            # would let the two rules drift.)

            # A clause may hold a second relation ("Alice 认识 Bob，Bob 负责
            # JarvisMind"). Splitting it would put half a sentence into an entity
            # name, so a side that contains any rule keyword makes the clause
            # unusable. Rejecting is the narrow choice: a missed extraction is
            # recoverable, a wrong node is not.
            if any(
                other.rule_id != rule.rule_id
                and _KEYWORD_PATTERNS[other.rule_id].search(side)
                for side in (left_raw, right_raw)
                for other in _RULES
            ):
                rejections.append(
                    Rejection(
                        rule.rule_id, "AMBIGUOUS_CLAUSE", _human_reason("AMBIGUOUS_CLAUSE")
                    )
                )
                continue

            endpoints: List[Optional[Tuple[str, str, Evidence]]] = []
            for raw, base, entity_type in (
                (left_raw, clause_start, subject_type),
                (right_raw, clause_start + match.end(), object_type),
            ):
                value, offset_start, offset_end = _trim(raw)
                standardised = _standardise(
                    value, rule_id=rule.rule_id, rejections=rejections
                )
                if standardised is None:
                    endpoints.append(None)
                    continue
                canonical, normalized = standardised
                endpoints.append(
                    (
                        normalized,
                        canonical,
                        Evidence(
                            rule=rule.rule_id,
                            start=base + offset_start,
                            end=base + offset_end,
                        ),
                    )
                )

            subject, target = endpoints

            # A capture that is a modifier + type head noun ("个人项目") classifies
            # rather than names. Dropping it here means a relation is never bound
            # to a category, and the surviving endpoint of a one-sided relation is
            # not emitted as a dangling node either.
            # A capture that classifies ("个人项目") or spans a fragment
            # ("…、…", "经历：…") names no entity. Classification is resolved
            # first so the report names the more specific shape.
            capture_reason = None
            for endpoint, entity_type in (
                (subject, subject_type),
                (target, object_type),
            ):
                if endpoint is None:
                    continue
                reason = _capture_reason(endpoint[1], entity_type)
                if reason == REASON_CLASSIFICATION_PHRASE:
                    capture_reason = reason
                    break
                capture_reason = capture_reason or reason
            if capture_reason is not None:
                rejections.append(
                    Rejection(
                        rule.rule_id,
                        capture_reason,
                        _human_reason(capture_reason),
                    )
                )
                continue

            for endpoint, entity_type in ((subject, subject_type), (target, object_type)):
                if endpoint is None:
                    continue
                normalized, canonical, evidence = endpoint
                if not _name_verdict(
                    canonical,
                    rule_id=rule.rule_id,
                    rejections=rejections,
                    exact_tokens=tokens,
                ):
                    continue
                key = (entity_type, normalized)
                existing = nodes.get(key)
                if existing is not None:
                    duplicates_removed += 1
                    continue
                nodes[key] = NodeCandidate(
                    entity_type=entity_type,
                    name=canonical,
                    normalized_key=normalized,
                    confidence=rule.confidence,
                    evidence=evidence,
                )

            if subject is None or target is None:
                continue
            subject_key = subject[0]
            object_key = target[0]
            if subject_key == object_key:
                rejections.append(
                    Rejection(rule.rule_id, "SELF_REFERENCE", _human_reason("SELF_REFERENCE"))
                )
                continue
            raw_edges.append(
                (
                    EdgeCandidate(
                        subject_key=subject_key,
                        predicate=predicate,
                        object_key=object_key,
                        subject_type=subject_type,
                        object_type=object_type,
                        confidence=rule.confidence,
                        evidence=Evidence(
                            rule=rule.rule_id,
                            start=clause_start,
                            end=clause_start + len(clause),
                        ),
                        source_memory_id=memory_id,
                    ),
                    rule.rule_id,
                )
            )

    edges: Dict[Tuple[str, str, str], EdgeCandidate] = {}
    for candidate, rule_id in raw_edges:
        # An edge may only exist between two accepted nodes. If a candidate name
        # was dropped by the per-name privacy layer, the relation that needed it
        # has lost an endpoint and cannot be formed.
        if (candidate.subject_type, candidate.subject_key) not in nodes or (
            candidate.object_type,
            candidate.object_key,
        ) not in nodes:
            rejections.append(
                Rejection(rule_id, "ENDPOINT_DROPPED", _human_reason("ENDPOINT_DROPPED"))
            )
            continue
        edge_key = (candidate.subject_key, candidate.predicate, candidate.object_key)
        if edge_key in edges:
            duplicates_removed += 1
            continue
        edges[edge_key] = candidate

    return ExtractionResult(
        memory_id=memory_id,
        rule_version=RULE_VERSION,
        decision=decision,
        eligible=True,
        nodes=tuple(nodes.values()),
        edges=tuple(edges.values()),
        rejections=tuple(rejections),
        duplicates_removed=duplicates_removed,
        source_memory_status=memory_status,
        metadata_supplied=metadata is not None,
    )


def preview_memory(
    module_projection, memory_id: str, *, exact_tokens: Iterable[str] = ()
) -> ExtractionResult:
    """Read one memory through the read-only projection, then extract.

    ``module_projection`` is a ``ReadOnlyProjection.for_module(...)`` view. The
    read goes through the token/mode=ro path, so this function cannot be handed
    a writable source even by mistake. A missing row raises the shared
    ``NotFoundError`` (the HTTP layer turns it into 404) instead of returning an
    empty result, because "no such memory" and "nothing extractable" are
    different answers.
    """
    row = module_projection.read_memory(memory_id)
    if row is None:
        raise NotFoundError(f"Memory {memory_id} not found")
    return extract_candidates(
        row.get("content"),
        memory_id=str(row.get("id") or memory_id),
        metadata=deserialize_metadata(row.get("metadata_json")),
        memory_status=str(row.get("status") or ""),
        exact_tokens=exact_tokens,
    )


__all__ = [
    "RULE_VERSION",
    "REASON_PERSONAL_SENSITIVE_CATEGORY",
    "MIN_NAME_LENGTH",
    "MAX_NAME_LENGTH",
    "Evidence",
    "Rejection",
    "NodeCandidate",
    "EdgeCandidate",
    "ExtractionResult",
    "extract_candidates",
    "preview_memory",
]
