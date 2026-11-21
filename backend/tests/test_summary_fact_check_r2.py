"""A2 regression: AI summaries must not invent facts.

The audit proved these outputs were accepted by the old validator, which only
compared 2+ digit Arabic numbers:

* ``用户是物流公司的高级总监``
* ``用户五年前入职``
* ``用户入职于二零二零年，月薪三万``
* ``用户在2020年入职后升任主管``
* ``用户一定会继续晋升``
* ``用户年薪超过30万元``
"""
from __future__ import annotations

import pytest

from jarvismind.services.memory_overview_enricher import validate_row
from jarvismind.services.summary_fact_check import (
    chinese_numerals,
    has_prediction,
    has_title_escalation,
    has_unsupported_numeral,
    unsupported_reason,
)

SOURCE = "用户在物流行业做理赔，2020 年入职"

CATEGORY = {
    "key": "work",
    "title": "工作与职业",
    "items": [
        {
            "memory_id": "m1",
            "memory_type": "SEMANTIC",
            "excerpt": SOURCE,
            "created_at": "",
            "updated_at": "",
        }
    ],
}

AUDIT_FABRICATIONS = [
    "用户是物流公司的高级总监",
    "用户五年前入职",
    "用户入职于二零二零年，月薪三万",
    "用户在2020年入职后升任主管",
    "用户未来一定会继续晋升",
    "用户年薪超过30万元",
]


@pytest.mark.parametrize("summary", AUDIT_FABRICATIONS)
def test_audit_fabrications_are_rejected(summary):
    row = {"key": "work", "summary": summary, "memory_ids": ["m1"]}
    assert validate_row(row, CATEGORY) is None, f"{summary!r} was accepted"


SUPPORTED_SUMMARIES = [
    "用户在物流行业从事理赔工作",
    "用户于 2020 年入职，在物流行业做理赔",
    "用户的工作与物流理赔相关",
]


@pytest.mark.parametrize("summary", SUPPORTED_SUMMARIES)
def test_supported_summaries_are_accepted(summary):
    row = {"key": "work", "summary": summary, "memory_ids": ["m1"]}
    assert validate_row(row, CATEGORY) == summary


def test_rejection_still_requires_a_real_memory_id():
    row = {"key": "work", "summary": "用户从事理赔", "memory_ids": ["fake"]}
    assert validate_row(row, CATEGORY) is None


def test_rejection_still_requires_a_citation():
    row = {"key": "work", "summary": "用户从事理赔", "memory_ids": []}
    assert validate_row(row, CATEGORY) is None


# --------------------------------------------------------------- unit level
def test_chinese_numerals_are_parsed():
    assert 5 in chinese_numerals("五年前")
    assert 30000 in chinese_numerals("三万")
    assert 2020 in chinese_numerals("二零二零")


def test_idiomatic_single_characters_are_ignored():
    # 一定 / 一些 / 一起 are words, not quantities.
    assert chinese_numerals("用户一定会成功") == []
    assert chinese_numerals("用户有一些偏好") == []


@pytest.mark.parametrize(
    "summary",
    ["用户五年前入职", "用户月薪三万", "用户年薪超过30万元"],
)
def test_unsupported_numerals_detected(summary):
    assert has_unsupported_numeral(summary, [SOURCE]) is True


def test_year_restated_in_chinese_is_not_a_fabrication():
    # 二零二零 与 2020 是同一个声明；数字校验按值比较，不应误判。
    assert has_unsupported_numeral("用户入职于二零二零年", [SOURCE]) is False


def test_different_chinese_year_is_a_fabrication():
    assert has_unsupported_numeral("用户入职于二零一九年", [SOURCE]) is True


def test_matching_numeral_is_allowed():
    assert has_unsupported_numeral("用户 2020 年入职", [SOURCE]) is False


def test_equivalent_chinese_numeral_is_allowed():
    # 2020 in the source may be restated as 二零二零.
    assert has_unsupported_numeral("用户二零二零年入职", [SOURCE]) is False


@pytest.mark.parametrize(
    "summary",
    ["用户是高级总监", "用户升任主管", "用户被提拔为经理", "user is a senior director"],
)
def test_title_escalation_detected(summary):
    assert has_title_escalation(summary, [SOURCE]) is True


def test_title_allowed_when_source_mentions_a_role():
    assert has_title_escalation("用户是理赔主管", ["用户在物流行业做理赔主管"]) is False


@pytest.mark.parametrize(
    "summary",
    ["用户一定会晋升", "用户未来将转岗", "用户必将成功", "it will definitely happen"],
)
def test_predictions_detected(summary):
    assert has_prediction(summary) is True


def test_unsupported_reason_is_short_and_value_free():
    reason = unsupported_reason("用户五年前入职", [SOURCE])
    assert reason == "unsupported_numeral"
    assert "五" not in reason and "2020" not in reason
