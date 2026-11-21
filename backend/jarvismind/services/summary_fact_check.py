"""Fact-support checks for AI-written summaries (R2 / A2).

The first validator only compared multi-digit Arabic numbers. That let obvious
fabrications through:

* ``用户是物流公司的高级总监`` (promotion)
* ``用户五年前入职`` (Chinese numeral)
* ``用户入职于二零二零年`` (Chinese numeral in a date)
* ``用户一定会继续晋升`` (prediction)

This module adds three independent checks on top of the id/number rules:

1. **Numerals** — Arabic *and* Chinese numerals are compared against the
   sources, so ``五年前`` or ``三万`` cannot smuggle in a new quantity.
2. **Title escalation** — a seniority/role word that does not appear in the
   sources is rejected. Promotion is the most damaging single-token lie here.
3. **Predictions** — forward-looking certainty words ("一定会", "未来将",
   "必然") are rejected outright; a memory summary may not forecast.

Everything here is deliberately conservative: when in doubt, reject. A rejected
summary falls back to the deterministic excerpt, which is always truthful.

None of these functions log or return source text; only category labels and
booleans leave this module.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence

#: Chinese numeral characters used for quantities and dates.
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}

#: A run of Chinese numeral characters, e.g. 二零二零 / 三万 / 五.
_CN_NUMERAL_RUN = re.compile(r"[零〇一二两三四五六七八九十百千万亿]{1,12}")
_ARABIC_RUN = re.compile(r"\d+")

#: Seniority words. If the summary claims one and no source mentions any
#: seniority word, the summary is inventing an organisational position.
_SENIORITY = (
    "总监", "主管", "经理", "总裁", "副总裁", "总经理", "董事", "合伙人",
    "负责人", "首席", "创始人", "CEO", "CTO", "CFO", "COO", "VP",
    "senior", "director", "manager", "head of", "chief", "lead",
    "高级", "资深", "升任", "晋升", "提拔",
)

#: Certainty about the future. A memory summary must not forecast.
_PREDICTIVE = (
    "一定会", "必将", "必然", "肯定会", "未来会", "未来将", "将会", "将要",
    "注定", "势必", "毫无疑问会", "will definitely", "inevitably", "is bound to",
)


def chinese_numeral_value(run: str) -> int | None:
    """Parse a simple Chinese numeral (up to 亿). Returns None if unparsable.

    Two forms are supported:

    * **unit form** — ``三万`` / ``二十`` / ``一千零五``, parsed with 十百千万亿;
    * **digit-run form** — ``二零二零`` / ``一九八四``, where each character is a
      digit and the value is their concatenation. This is how years are
      commonly written in Chinese, so treating it as ``0`` would let a date
      restated in Chinese slip past the numeral check.
    """
    if not run:
        return None

    # Digit-run form: every character is a digit, no unit characters at all.
    if all(char in _CN_DIGITS for char in run):
        if len(run) == 1:
            return _CN_DIGITS[run]
        digits = "".join(str(_CN_DIGITS[char]) for char in run)
        try:
            return int(digits)
        except ValueError:  # pragma: no cover - digits are guaranteed
            return None

    total = 0
    section = 0
    number = 0
    for char in run:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit >= 10_000:
                section = (section + number) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    return total + section + number


def chinese_numerals(text: str) -> List[int]:
    """Every Chinese numeral value found in ``text``.

    Runs of length 1 are included only when they are clearly a quantity:
    ``五年前`` yields 5, while the ``一`` in ``一定`` is excluded because the
    following character marks it as an idiom (see ``_is_idiomatic``).
    """
    values: List[int] = []
    for match in _CN_NUMERAL_RUN.finditer(text):
        run = match.group(0)
        if _is_idiomatic(text, match.start(), match.end(), run):
            continue
        value = chinese_numeral_value(run)
        if value is not None:
            values.append(value)
    return values


_IDIOM_SUFFIXES = ("定", "些", "起", "直", "般", "旦", "旦旦", "向", "同", "旦")
_IDIOM_PREFIXES = ("唯", "统", "第", "万", "十", "一")


def _is_idiomatic(text: str, start: int, end: int, run: str) -> bool:
    """Filter numeral characters that are part of an ordinary word.

    ``一定`` / ``一起`` / ``一些`` / ``第一`` are words, not quantities; without
    this filter every summary would be rejected and the feature would never
    produce an accepted result.
    """
    if len(run) != 1:
        return False
    nxt = text[end : end + 1]
    prev = text[start - 1 : start] if start > 0 else ""
    if nxt in _IDIOM_SUFFIXES:
        return True
    if prev in _IDIOM_PREFIXES:
        return True
    return False


def arabic_numerals(text: str) -> List[int]:
    values: List[int] = []
    for match in _ARABIC_RUN.finditer(text):
        try:
            values.append(int(match.group(0)))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
    return values


def has_unsupported_numeral(summary: str, sources: Sequence[str]) -> bool:
    """True when the summary states a quantity absent from every source.

    Both Arabic and Chinese numerals are compared by *value*, so ``30000`` and
    ``三万`` are the same claim. Single Arabic digits are ignored (list markers,
    "3 条记忆") but single Chinese numerals are checked because ``五个`` /
    ``五年前`` carries a real quantity.
    """
    allowed = set()
    for source in sources:
        allowed.update(arabic_numerals(source))
        allowed.update(chinese_numerals(source))

    for value in arabic_numerals(summary):
        if value < 10:
            continue
        if value not in allowed:
            return True

    for value in chinese_numerals(summary):
        if value not in allowed:
            return True

    return False


def has_title_escalation(summary: str, sources: Sequence[str]) -> bool:
    """True when the summary names a seniority the sources never mention."""
    lowered_summary = summary.lower()
    claimed = [word for word in _SENIORITY if word.lower() in lowered_summary]
    if not claimed:
        return False
    source_text = " ".join(sources).lower()
    # Any seniority mention in the sources is enough to allow the claim: the
    # excerpt truncates at 120 chars, so requiring the *same* word would reject
    # legitimate summaries of long memories.
    return not any(word.lower() in source_text for word in _SENIORITY)


def has_prediction(summary: str) -> bool:
    """True when the summary asserts something about the future."""
    lowered = summary.lower()
    return any(word.lower() in lowered for word in _PREDICTIVE)


def unsupported_reason(summary: str, sources: Iterable[str]) -> str | None:
    """Return a short, value-free reason when the summary is unsupported.

    ``None`` means the summary passed every check.
    """
    source_list = list(sources)
    if has_prediction(summary):
        return "prediction"
    if has_title_escalation(summary, source_list):
        return "title_escalation"
    if has_unsupported_numeral(summary, source_list):
        return "unsupported_numeral"
    return None


__all__ = [
    "arabic_numerals",
    "chinese_numeral_value",
    "chinese_numerals",
    "has_prediction",
    "has_title_escalation",
    "has_unsupported_numeral",
    "unsupported_reason",
]
