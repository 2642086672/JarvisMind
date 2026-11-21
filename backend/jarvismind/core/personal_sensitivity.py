"""Deterministic personal-sensitivity policy for auto-written memories.

The credential gate in ``core.security_gate`` covers secrets and regex-shaped
PII. Ordinary language still contains categories that must never enter the
auto-write path even though no regex can reliably find them (health, exact
home address, finances, sexual orientation, religion, politics).

This module is deliberately conservative: a keyword hit means PROTECTED, not
"probably safe". It returns category names only, never matched values.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

# Category names are intentionally generic and value-free.
_GROUPS = {
    "health": (
        "健康", "疾病", "诊断", "医院", "医生", "药物", "吃药", "手术",
        "住院", "治疗", "抑郁", "焦虑", "癌", "糖尿病", "高血压", "过敏",
        "体检", "病史", "病情", "mental health", "diagnosis",
    ),
    "home_address": (
        "家庭住址", "家住", "详细地址", "门牌", "小区", "户籍", "身份证",
        # Fail-closed for residence statements: the policy cannot reliably tell
        # a city from a street address, so both are held for authorization.
        "住在", "居住", "定居", "搬家到",
    ),
    "finance": (
        "银行账户", "银行卡", "工资", "收入", "存款", "负债", "贷款",
        "信用卡", "余额", "债务", "资产", "税后", "年薪",
        # R2/A1: pay-shaped statements that the original list missed.
        "月薪", "月收入", "年收入", "月工资", "到手工资", "薪水", "薪酬",
        "奖金", "分红", "股票期权", "净资产", "欠款", "花呗", "借呗",
    ),
    "sexual": (
        "性取向", "性生活", "同性恋", "双性恋", "异性恋", "出柜",
    ),
    "religion": (
        "宗教信仰", "信教", "佛教", "基督教", "伊斯兰", "无神论", "天主教",
    ),
    "politics": (
        "政治倾向", "政治立场", "党派", "党员", "投票", "选举",
    ),
}

# A few English terms are included because the user may mix languages.
_ENGLISH = {
    "health": ("diagnosis", "medication", "hospital", "therapy", "disease"),
    "finance": (
        "salary", "bank account", "debt", "loan", "credit card",
        # R2/A1
        "monthly pay", "annual income", "net worth", "paycheck",
    ),
    "sexual": ("sexual orientation", "lgbt"),
    "religion": ("religion", "religious"),
    "politics": ("political", "party affiliation"),
}

# ---------------------------------------------------------------------------
# R2/A1: patterns for categories whose *shape* matters more than a keyword.
#
# These run against NFKC-normalised text (full-width digits/letters folded) and
# a digit-collapsed copy (``138 0013 8000`` -> ``13800138000``). They exist
# because keyword lists alone cannot catch an English street address or a
# Chinese pay statement written without the word "工资".
_SHAPE_RULES = {
    "home_address": (
        # 5 Main St / 12 Oak Avenue / 221B Baker Street
        re.compile(
            r"\b\d{1,5}[A-Za-z]?\s+[A-Za-z][A-Za-z.'-]*"
            r"(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}\s+"
            r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|lane|ln|dr|drive|"
            r"way|court|ct|place|pl|terrace|highway|hwy)\b\.?",
            re.IGNORECASE,
        ),
        # "lives at ..." / "address is ..."
        re.compile(
            r"\b(?:lives?|resides?|address)\b[^.。;；\n]{0,12}"
            r"(?:at|is|:)\s*\S{2,}",
            re.IGNORECASE,
        ),
        # 中文门牌：xx路xx号 / xx街xx号
        re.compile(r"[\u4e00-\u9fa5]{2,8}(?:路|街|大道|巷|弄)\s*\d{1,4}\s*号"),
    ),
    "finance": (
        # 月薪三万 / 月收入 3 万 / 年薪二十万
        re.compile(
            r"(?:月薪|月收入|年薪|年收入|月工资|工资|收入|奖金|分红)"
            r"[^\d\u4e00-\u9fa5]{0,6}"
            r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万亿]{1,8})"
            r"\s*(?:万|千|百|元|块|k|K|w|W|rmb|cny)?",
        ),
        # 30000 元 / 3 万 / 30k / 20k/month
        re.compile(
            r"\b\d{4,7}\s*(?:元|块|rmb|cny)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b\d{1,3}\s*(?:k|w)\b\s*(?:/|per\s*)?(?:month|year|月|年)?", re.IGNORECASE),
    ),
}

_DIGIT_SEPARATORS = re.compile(r"(?<=\d)[ \t\-_.·•—–](?=\d)")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _collapse_digits(text: str) -> str:
    return _DIGIT_SEPARATORS.sub("", text)


def categories(text: Optional[str]) -> List[str]:
    """Return generic sensitive category names found in ``text``.

    Three passes, any hit means PROTECTED:

    1. keyword groups on the lowercased original;
    2. keyword groups on NFKC-normalised text (full-width input);
    3. shape rules on normalised + digit-collapsed text (addresses, pay).

    This is still a policy, not a completeness claim: unknown text is not
    assumed safe solely because nothing matched. Callers that must not leak
    apply the credential/PII gates independently, and ``contains_sensitive``
    combines all of them.
    """
    if not text:
        return []
    found = set()

    variants = {text.lower(), _normalize(text).lower()}
    for variant in variants:
        for category, needles in _GROUPS.items():
            if any(needle in variant for needle in needles):
                found.add(category)
        for category, needles in _ENGLISH.items():
            if any(needle in variant for needle in needles):
                found.add(category)

    collapsed = _collapse_digits(_normalize(text))
    for category, patterns in _SHAPE_RULES.items():
        if any(pattern.search(collapsed) for pattern in patterns):
            found.add(category)

    return sorted(found)


def is_sensitive(text: Optional[str]) -> bool:
    return bool(categories(text))


__all__ = ["categories", "is_sensitive"]
