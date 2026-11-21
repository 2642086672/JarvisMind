"""A1 regression: sensitive-content bypasses found by the independent audit.

Every case here was verified to PASS THE OLD FILTER. They must now be excluded
before any model request, and they must never appear in the API payload.

The tests are deliberately hostile: spacing, full-width characters, mixed
scripts and punctuation are all used to try to hide a value.
"""
from __future__ import annotations

import pytest

from jarvismind.core.personal_sensitivity import categories as personal_categories
from jarvismind.core.sensitive import (
    collapse_digit_separators,
    credential_categories,
    normalize_for_scan,
    pii_categories,
    redact,
    tolerant_categories,
)

# (input, expected category family)
AUDIT_BYPASSES = [
    # Exact quote from the audit report.
    ("手机 138 0013 8000", "pii"),
    ("密码 hunter2 用于登录", "credential"),
    ("用户的月薪是三万", "personal"),
    ("user lives at 5 Main St", "personal"),
    ("用户邮箱 a at b dot com", "pii"),
]


def _families(text: str) -> set[str]:
    found: set[str] = set()
    if pii_categories(text):
        found.add("pii")
    if credential_categories(text):
        found.add("credential")
    if personal_categories(text):
        found.add("personal")
    return found


@pytest.mark.parametrize("text,family", AUDIT_BYPASSES)
def test_audit_bypasses_are_now_blocked(text, family):
    assert family in _families(text), f"{text!r} must be flagged as {family}"


# --------------------------------------------------------------- variations
SPACING_AND_WIDTH = [
    "手机 138 0013 8000",
    "手机138-0013-8000",
    "手机 138.0013.8000",
    "手机　138　0013　8000",  # ideographic spaces
    "我的手机是１３８００１３８０００",  # full-width digits
    "tel 138 0013 8000 please",
]


@pytest.mark.parametrize("text", SPACING_AND_WIDTH)
def test_phone_variants_are_flagged(text):
    assert pii_categories(text), f"{text!r} leaked"


OBFUSCATED_EMAIL = [
    "用户邮箱 a at b dot com",
    "邮箱 user(at)example(dot)com",
    "contact me: foo [at] bar [dot] com",
    "邮箱是 foo＠bar.com",
]


@pytest.mark.parametrize("text", OBFUSCATED_EMAIL)
def test_obfuscated_email_is_flagged(text):
    assert pii_categories(text), f"{text!r} leaked"


PASSWORD_VARIANTS = [
    "密码 hunter2 用于登录",
    "密码是 hunter2",
    "密码：hunter2",
    "口令 hunter2",
    "passcode hunter2",
]


@pytest.mark.parametrize("text", PASSWORD_VARIANTS)
def test_password_variants_are_flagged(text):
    assert credential_categories(text), f"{text!r} leaked"


FINANCE_VARIANTS = [
    "用户的月薪是三万",
    "月薪 30000 元",
    "月收入 3 万",
    "年薪二十万",
    "工资 8000 块",
    "monthly pay is 30000",
    "annual income 500000",
]


@pytest.mark.parametrize("text", FINANCE_VARIANTS)
def test_finance_variants_are_flagged(text):
    assert personal_categories(text), f"{text!r} leaked"


ADDRESS_VARIANTS = [
    "user lives at 5 Main St",
    "12 Oak Avenue",
    "221B Baker Street",
    "address is 42 Wallaby Way",
    "他住在朝阳区建国路88号",
]


@pytest.mark.parametrize("text", ADDRESS_VARIANTS)
def test_address_variants_are_flagged(text):
    assert personal_categories(text), f"{text!r} leaked"


# --------------------------------------------------------------- no over-block
ORDINARY_MEMORIES = [
    "用户喜欢跑步",
    "用户在物流行业做理赔，2020 年入职",
    "用户偏好早上喝咖啡",
    "用户正在做一个记账小工具",
    "用户和朋友约好周末爬山",
    "用户养了一只猫",
    "用户希望明年学会游泳",
]


@pytest.mark.parametrize("text", ORDINARY_MEMORIES)
def test_ordinary_memories_are_not_over_filtered(text):
    assert _families(text) == set(), f"{text!r} was over-filtered"


# --------------------------------------------------------------- helpers
def test_normalize_folds_full_width():
    assert normalize_for_scan("１３８００１３８０００") == "13800138000"


def test_collapse_removes_digit_separators():
    assert collapse_digit_separators("138 0013-8000") == "13800138000"


def test_tolerant_categories_is_value_free():
    found = tolerant_categories("手机 138 0013 8000")
    assert found == ["phone_sep"]
    # No digits, no original text anywhere in the result.
    assert all(not any(char.isdigit() for char in item) for item in found)


def test_redaction_removes_the_value():
    text = "我的手机 138 0013 8000，请勿外传"
    cleaned = redact(text)
    # 带空格的手机号由宽松通道脱敏；比对时同时去掉分隔符。
    collapsed = collapse_digit_separators(cleaned)
    assert "13800138000" not in collapsed
    assert "[REDACTED]" in cleaned
