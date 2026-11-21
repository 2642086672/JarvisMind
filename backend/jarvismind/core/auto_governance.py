"""Deterministic policy for JME automatic memory governance.

The policy is intentionally explainable and conservative. It answers ONE
question for a new fact and one existing memory: what is the relationship?

    DUPLICATE   same information; do not write a second row
    UPDATE      the new fact clearly supersedes the old value
    CONFLICT    same attribute, different value, but the user did not say the
                old one is replaced -- a human must decide
    INDEPENDENT different attributes; keep both

``UPDATE`` requires an EXPLICIT update marker in the new text (以后/现在/改成/
不再/已完成 ...). A bare value difference is never treated as a replacement:
"我喜欢拿铁" and "我喜欢美式" can both be true, so that stays a conflict.

No model is involved. Every decision is reproducible from the text alone.
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .dedup import _subject_tokens, is_duplicate, normalize, similarity, token_set

# --------------------------------------------------------------------- attributes
#: Attribute vocabularies. A shared attribute is what makes two sentences
#: comparable at all: different attributes can never "conflict".
_ATTRIBUTE_VOCAB: Dict[str, Tuple[str, ...]] = {
    "name": ("名字", "姓名", "叫", "称呼", "昵称"),
    "occupation": (
        "职业", "工作", "从事", "岗位", "任职", "职责", "公司", "单位", "行业",
        "工程师", "经理", "产品", "开发", "运营", "客服", "理赔",
    ),
    "beverage": ("咖啡", "拿铁", "美式", "茶", "饮品", "喝", "吃", "食物", "口味"),
    "device": (
        "使用", "手机", "电脑", "系统", "设备", "windows", "mac", "iphone",
        "android", "linux", "平板",
    ),
    "location": ("住在", "家住", "搬到", "搬家", "定居", "户籍", "籍贯"),
    "project_status": (
        "状态", "进度", "进行中", "已完成", "完成", "暂停", "上线", "延期",
        "取消", "发布",
    ),
    "preference": ("喜欢", "偏好", "爱好", "习惯", "讨厌", "不喜欢"),
    "style": ("回答", "沟通", "风格", "简洁", "直接", "正式", "语气"),
}

#: Explicit signals that the user is replacing an earlier value. Without one of
#: these, a value difference is a conflict, not an update.
_UPDATE_MARKERS: Tuple[str, ...] = (
    "以后", "现在", "目前", "当前", "已经", "已", "改成", "改为", "换成",
    "不再", "更喜欢", "更新为", "更新成", "最新", "离职", "入职", "毕业",
    "停止", "转到", "转到", "改口", "更正", "纠正", "说错", "记错",
)

#: Negation of the OLD value: "我不喜欢拿铁了" retires 喜欢拿铁.
#: These must be NEGATION phrases, not bare characters: "改善岗" contains 改 but
#: states a different job, not a replacement of one.
_REVERSAL_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"不再(?:喜欢|爱|喝|吃|用|是|需要|使用)"),
    re.compile(r"(?:不|没)(?:再)?(?:喜欢|爱喝|爱吃|爱|喝|吃|用|使用|需要)"),
    re.compile(r"(?:停止|取消|放弃)(?:喜欢|使用|喝|吃)"),
    re.compile(r"换成(?:了)?"),
    re.compile(r"改成(?:了)?"),
)

#: Text that must never become a user fact.
_QUESTION_MARKERS: Tuple[str, ...] = (
    "？", "?", "吗", "呢", "是不是", "是否", "多少", "怎么", "为什么",
    "什么", "哪", "谁", "如何", "怎样", "几点", "多久", "几个",
)
_HYPOTHETICAL_MARKERS: Tuple[str, ...] = (
    "如果", "假如", "假设", "要是", "万一", "比如说", "比如", "例如",
    "可能", "也许", "大概", "或许", "可以试试", "要不要",
)
_THIRD_PARTY_MARKERS: Tuple[str, ...] = (
    "他说", "她说", "他们说", "朋友说", "同事说", "听说", "据说", "网上说",
    "别人说", "有人觉得", "我问他", "我问她", "他喜欢", "她喜欢",
)

_NEGATION_REQUEST = re.compile(r"(?:不要|别|不用|无需)(?:再)?(?:记住|保存|记)")

#: Words that carry no factual value. They must be removed BEFORE comparing two
#: sentences, otherwise "我平时喜欢喝美式" and "我喜欢喝美式咖啡" look different
#: merely because one of them says "平时".
_FILLER: Tuple[str, ...] = (
    "平时", "一直", "通常", "一般", "其实", "大概", "比较", "非常", "真的",
    "主要", "总是", "基本", "算是", "应该", "可以", "目前", "现在", "当前",
    "用户", "我的", "我是", "系统", "方面", "情况", "以及", "并且", "然后",
    "就是", "还是", "也是", "都是", "这个", "那个", "一下", "一些", "有点",
    "；", ";", "，", ",", "。", ".", "！", "!", "、", "：", ":", " ",
)

#: Name-value patterns. When both sentences state a name, the name itself is
#: the fact; extra detail (a birth date, an address) must not turn an identical
#: name into a "conflict".
_NAME_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"我叫([^\s，,。；;：:]{1,20})"),
    re.compile(r"(?:我的)?名字(?:是|叫|为)\s*([^\s，,。；;：:]{1,20})"),
    re.compile(r"姓名(?:是|为)\s*([^\s，,。；;：:]{1,20})"),
    re.compile(r"称呼我(?:为|是)?\s*([^\s，,。；;：:]{1,20})"),
)


def _hits(text: str, markers: Iterable[str]) -> List[str]:
    lowered = (text or "").lower()
    return [marker for marker in markers if marker in lowered]


def is_question(text: str) -> bool:
    """True when the text asks something instead of stating a fact."""
    stripped = (text or "").strip()
    return any(marker in stripped for marker in _QUESTION_MARKERS)


def is_hypothetical(text: str) -> bool:
    return bool(_hits(text, _HYPOTHETICAL_MARKERS))


def is_third_party(text: str) -> bool:
    return bool(_hits(text, _THIRD_PARTY_MARKERS))


def is_reversible_request(text: str) -> bool:
    return bool(_NEGATION_REQUEST.search(text or ""))


def is_fact_like(text: str) -> bool:
    """True only when the sentence can safely be read as a user fact."""
    if not text or not text.strip():
        return False
    if is_question(text) or is_hypothetical(text) or is_third_party(text):
        return False
    if is_reversible_request(text):
        return False
    return True


def attributes(text: str) -> FrozenSet[str]:
    lowered = (text or "").lower()
    found: Set[str] = set()
    for name, vocab in _ATTRIBUTE_VOCAB.items():
        if any(word in lowered for word in vocab):
            found.add(name)
    return frozenset(found)


#: Attributes that can hold exactly one current value. Two different values for
#: one of these are a real disagreement, so they must not both be written.
#: Generic ``preference`` is intentionally NOT here: a person can like coffee
#: and hiking at the same time.
_SINGLE_VALUED: FrozenSet[str] = frozenset(
    {"name", "occupation", "location", "project_status", "device", "beverage", "style"}
)


def _revokes_old_value(new_text: str, existing_text: str) -> bool:
    """True when the new sentence negates the value the old one states.

    "我不喜欢拿铁了" revokes "我喜欢喝拿铁". This is a replacement, not a
    coexistence: keeping both would leave the assistant believing something the
    user just denied.
    """
    shared = _value_tokens(new_text) & _value_tokens(existing_text)
    if not shared:
        return False
    return any(pattern.search(new_text or "") for pattern in _REVERSAL_PATTERNS)


def has_update_marker(text: str) -> bool:
    """An explicit marker that the user is replacing the previous value."""
    lowered = (text or "").lower()
    for marker in _UPDATE_MARKERS:
        if marker in lowered:
            return True
    # A negation attached to the same attribute ("我不喜欢拿铁了") is also a
    # replacement signal, but only when the sentence is not a bare negation of
    # a different subject.
    return "了" in lowered and bool(_hits(lowered, ("不", "没")))


def _strip_fillers(text: str) -> str:
    result = (text or "").lower()
    for word in _FILLER:
        result = result.replace(word, "")
    return result.strip()


def extract_name(text: str) -> Optional[str]:
    """Return the stated name, if any. Value-only: never invents a name."""
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text or "")
        if match is not None:
            value = match.group(1).strip(" ，,。；;：:、「」\"'")
            if value:
                return value
    return None


def _value_tokens(text: str) -> Set[str]:
    """Salient tokens minus the shared attribute vocabulary words.

    Filler words are stripped first: "我平时喜欢喝美式" and "我喜欢喝美式咖啡"
    describe the same preference, and the adverb must not create a difference.
    """
    vocab: Set[str] = set()
    for words in _ATTRIBUTE_VOCAB.values():
        vocab.update(word.lower() for word in words)
    cleaned = _strip_fillers(text)
    if not cleaned:
        return set()
    return {
        token
        for token in _subject_tokens(cleaned)
        if token.lower() not in vocab and token not in vocab
    }


#: Function words that carry no value. Removing them lets "我的爱好是摄影"
#: and "我平时爱好摄影" reduce to the same core "摄影".
_PARTICLES: Tuple[str, ...] = (
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "的", "是", "了", "也", "都", "在", "有", "和", "就", "而", "及", "或",
    "很", "更", "最", "还", "又", "常", "会", "能", "要", "想", "让", "给",
    "把", "被", "对", "于", "从", "到", "与", "着", "过", "吧", "呢", "啊",
    "呀", "哦", "嗯", "其实", "平时", "一直", "通常", "一般", "主要", "总是",
    "现在", "目前", "以后", "最近", "已经", "不再", "还是", "就是", "也是",
)


#: Attribute words removed when reducing a sentence to its value. Status VALUES
#: ("已完成", "进行中") are deliberately NOT removed: they are the fact.
_CORE_STRIP: Tuple[str, ...] = (
    "名字", "姓名", "称呼", "昵称",
    "职业", "工作", "从事", "岗位", "任职", "职责",
    "咖啡", "拿铁", "美式", "茶", "饮品", "喝", "吃", "食物", "口味",
    "使用", "手机", "电脑", "系统", "设备",
    "住在", "家住", "搬到", "定居", "户籍", "籍贯",
    "喜欢", "偏好", "爱好", "习惯", "讨厌",
    "回答", "沟通", "风格", "语气",
)


def _value_core(text: str) -> str:
    """Reduce a sentence to the value it states.

    Removes filler, pronouns/particles and attribute vocabulary; what remains is
    the comparable value ("摄影"), which is far more stable than raw bigrams.
    """
    result = (text or "").lower()
    for word in _FILLER:
        result = result.replace(word, "")
    for word in _PARTICLES:
        result = result.replace(word, "")
    for word in sorted(_CORE_STRIP, key=len, reverse=True):
        result = result.replace(word.lower(), "")
    return "".join(result.split())


def _tokens_equivalent(left: Set[str], right: Set[str]) -> bool:
    """True when the token sets carry the same information.

    One set being a subset of the other means no NEW fact was introduced: the
    longer sentence merely adds detail ("Windows 10" vs "Windows 10 系统").
    """
    if not left or not right:
        return False
    if left == right:
        return True
    if left <= right or right <= left:
        return True
    shared = left & right
    smaller = min(len(left), len(right))
    # Allow one extra token of noise in either direction.
    return smaller >= 2 and len(shared) / smaller >= 0.75


def value_differs(left: str, right: str) -> bool:
    """True when the two texts carry genuinely different non-generic values."""
    left_core = _value_core(left)
    right_core = _value_core(right)
    if left_core and right_core:
        if left_core == right_core:
            return False
        if left_core in right_core or right_core in left_core:
            return False

    left_tokens = _value_tokens(left)
    right_tokens = _value_tokens(right)
    if not left_tokens or not right_tokens:
        return False

    left_name = extract_name(left)
    right_name = extract_name(right)
    if left_name is not None and right_name is not None:
        # A stated name is the fact. Extra detail does not contradict it.
        return left_name != right_name

    return not _tokens_equivalent(left_tokens, right_tokens)


class Relation:
    DUPLICATE = "DUPLICATE"
    UPDATE = "UPDATE"
    CONFLICT = "CONFLICT"
    INDEPENDENT = "INDEPENDENT"


def relation(new_text: str, existing_text: str) -> str:
    """Classify how ``new_text`` relates to one ``existing_text``."""
    if not new_text or not existing_text:
        return Relation.INDEPENDENT
    if normalize(new_text) == normalize(existing_text):
        return Relation.DUPLICATE

    sim = similarity(new_text, existing_text)
    if sim >= 0.92:
        return Relation.DUPLICATE

    shared = attributes(new_text) & attributes(existing_text)
    if not shared:
        # No comparable attribute: a loose duplicate is still a duplicate, but
        # nothing else may be inferred.
        if is_duplicate(new_text, existing_text) and sim >= 0.80:
            return Relation.DUPLICATE
        return Relation.INDEPENDENT

    differs = value_differs(new_text, existing_text)
    if not differs:
        # Same attribute, same value tokens: the wording may differ, the fact
        # did not.
        return Relation.DUPLICATE

    if has_update_marker(new_text):
        # An update marker alone is not enough: "我更喜欢喝拿铁" must not
        # overwrite an unrelated memory that merely shares the word "喜欢".
        # The two sentences must be about the same single-valued attribute, or
        # the new one must explicitly revoke the old value.
        if (shared & _SINGLE_VALUED) or _revokes_old_value(new_text, existing_text):
            return Relation.UPDATE
    if _revokes_old_value(new_text, existing_text):
        return Relation.UPDATE
    # Only a SINGLE-VALUED attribute can truly disagree. A person may like
    # coffee and hiking at the same time, so a different generic preference is
    # simply another memory, not a conflict.
    if shared & _SINGLE_VALUED:
        return Relation.CONFLICT
    return Relation.INDEPENDENT


def best_match(
    new_text: str,
    candidates: Sequence[Tuple[str, str]],
) -> Optional[Tuple[str, str, str, float]]:
    """Pick the single most relevant existing memory, if any.

    Returns ``(memory_id, existing_content, relation, score)`` or ``None``.
    The match must be unambiguous: if two different memories are equally
    plausible, the caller must treat the situation as a conflict instead of
    picking one.
    """
    scored = scored_matches(new_text, candidates)
    if not scored:
        return None
    top = scored[0]
    if len(scored) > 1:
        runner_up = scored[1]
        # Two near-equal targets with different relations: refuse to guess.
        if top[3] - runner_up[3] < 0.05 and top[0] != runner_up[0]:
            if {top[2], runner_up[2]} != {Relation.DUPLICATE}:
                return None
    return top


def scored_matches(
    new_text: str,
    candidates: Sequence[Tuple[str, str]],
) -> List[Tuple[str, str, str, float]]:
    """All comparable candidates, best first. Used for ambiguity detection."""
    scored: List[Tuple[str, str, str, float]] = []
    for memory_id, content in candidates:
        rel = relation(new_text, content)
        if rel == Relation.INDEPENDENT:
            continue
        score = similarity(new_text, content)
        if rel == Relation.DUPLICATE:
            score = max(score, 0.95)
        scored.append((memory_id, content, rel, score))
    scored.sort(key=lambda item: item[3], reverse=True)
    return scored


# ------------------------------------------------------------------------ merging
#: Auto-merge is only allowed for genuinely equivalent memories. The threshold
#: is deliberately far above the duplicate threshold used for "do not write a
#: second row": merging rewrites provenance, so it must be near-certain.
AUTO_MERGE_SIMILARITY = 0.90
AUTO_MERGE_MAX_GROUP = 4


def mergeable_group(
    contents: Sequence[str],
) -> bool:
    """True when a duplicate cluster is safe to merge automatically.

    Equivalence is decided by the same policy that decides DUPLICATE: the
    sentences must be about the same attributes and carry the same value core.
    Raw bigram similarity is deliberately NOT the gate: "我的爱好是摄影" and
    "我平时爱好摄影" share almost no bigrams yet state the same fact.
    """
    items = [item for item in contents if item and item.strip()]
    if len(items) < 2 or len(items) > AUTO_MERGE_MAX_GROUP:
        return False
    expected_attrs = attributes(items[0])
    if not expected_attrs:
        return False
    for index, left in enumerate(items):
        if attributes(left) != expected_attrs:
            return False
        for right in items[index + 1 :]:
            if relation(left, right) != Relation.DUPLICATE:
                return False
            if value_differs(left, right):
                return False
    return True


# ------------------------------------------------------------------------ expiry
def is_expired(expires_at: Optional[str], now_iso: str) -> bool:
    """True when a memory's own expiry timestamp has passed.

    A memory without ``expires_at`` is never expired: time passing alone is not
    evidence that a fact stopped being true.
    """
    if not expires_at:
        return False
    return str(expires_at) <= str(now_iso)


__all__ = [
    "AUTO_MERGE_MAX_GROUP",
    "AUTO_MERGE_SIMILARITY",
    "Relation",
    "attributes",
    "best_match",
    "has_update_marker",
    "is_expired",
    "is_fact_like",
    "is_hypothetical",
    "is_question",
    "is_reversible_request",
    "is_third_party",
    "mergeable_group",
    "relation",
    "value_differs",
]
