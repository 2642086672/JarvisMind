"""JME-MEMORY-OVERVIEW-01 · server-side AI enrichment tests.

The enricher is optional and must never break the endpoint. These tests use a
fake LLM provider (no network, no key) and cover:

* no provider configured → no enricher at all (deterministic path only);
* a valid model reply is adopted and traceable;
* invented memory ids, invented numbers, over-long or empty summaries are
  dropped while the rest of the payload stays intact;
* model failure / non-JSON / garbage returns the deterministic payload;
* the prompt never contains the live API token.
"""
from __future__ import annotations

import json

import pytest

from jarvismind.services.llm.base import LLMError, LLMProvider, LLMResult
from jarvismind.services.memory_overview_enricher import (
    MAX_SUMMARY_CHARS,
    build_overview_enricher,
    build_prompt,
    has_novel_numbers,
    parse_output,
    validate_row,
)


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, text: str = "", *, raises: Exception | None = None) -> None:
        self.text = text
        self.raises = raises
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, timeout=20.0, max_tokens=800, temperature=0.2):
        self.calls.append(list(messages))
        if self.raises is not None:
            raise self.raises
        return LLMResult(text=self.text, model="fake-model")


def payload() -> dict:
    return {
        "generated_at": "2026-11-18T08:00:00+00:00",
        "source_version": "v1",
        "eligible_count": 2,
        "excluded_count": 0,
        "excluded_reasons": {},
        "recent_days": 30,
        "recent_min_importance": 6.5,
        "ai_enhanced": False,
        "categories": [
            {
                "key": "profile",
                "title": "个人画像",
                "summary": "用户喜欢跑步",
                "item_count": 1,
                "items": [
                    {
                        "memory_id": "m1",
                        "memory_type": "PROFILE",
                        "excerpt": "用户喜欢跑步",
                        "created_at": "",
                        "updated_at": "",
                    }
                ],
                "truncated": False,
                "uncertain": False,
                "conflicts": [],
                "available": True,
            },
            {
                "key": "work",
                "title": "工作与职业",
                "summary": "用户在物流行业工作",
                "item_count": 1,
                "items": [
                    {
                        "memory_id": "m2",
                        "memory_type": "SEMANTIC",
                        "excerpt": "用户在物流行业工作",
                        "created_at": "",
                        "updated_at": "",
                    }
                ],
                "truncated": False,
                "uncertain": False,
                "conflicts": [],
                "available": True,
            },
        ],
    }


# ------------------------------------------------------------------ helpers
def test_no_provider_means_no_enricher(settings):
    enricher = build_overview_enricher(settings, provider=None)
    # The shared test settings configure no LLM, so nothing is attached.
    assert enricher is None


def test_novel_numbers_are_detected():
    assert has_novel_numbers("用户在 2026 年入职", ["用户已入职"]) is True
    assert has_novel_numbers("用户 2026 年入职", ["用户 2026 年入职"]) is False
    # Single digits are not policed: too noisy to be a reliable signal.
    assert has_novel_numbers("用户有 3 只猫", ["用户养猫"]) is False


@pytest.mark.parametrize(
    "text",
    [
        '{"categories":[{"key":"profile","summary":"说明","memory_ids":["m1"]}]}',
        '```json\n{"categories":[{"key":"profile","summary":"说明","memory_ids":["m1"]}]}\n```',
        '结果如下：\n{"categories":[{"key":"profile","summary":"说明","memory_ids":["m1"]}]}\n以上。',
    ],
)
def test_parse_tolerates_fences_and_prose(text):
    rows = parse_output(text)
    assert rows is not None and rows[0]["key"] == "profile"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "不是 JSON", "{}", '{"categories":[]}', '{"categories":"x"}'],
)
def test_parse_rejects_unusable_shapes(text):
    assert parse_output(text) is None


def test_validate_accepts_traceable_summary():
    category = payload()["categories"][0]
    assert (
        validate_row(
            {"key": "profile", "summary": "用户有跑步习惯", "memory_ids": ["m1"]},
            category,
        )
        == "用户有跑步习惯"
    )


@pytest.mark.parametrize(
    "row",
    [
        {"summary": "", "memory_ids": ["m1"]},
        {"summary": "x" * (MAX_SUMMARY_CHARS + 1), "memory_ids": ["m1"]},
        {"summary": "看起来不错", "memory_ids": []},
        {"summary": "用户有跑步习惯", "memory_ids": ["invented"]},
        {"summary": "用户 2019 年开始跑步", "memory_ids": ["m1"]},
    ],
)
def test_validate_rejects_untrustworthy_rows(row):
    category = payload()["categories"][0]
    assert validate_row(row, category) is None


def test_prompt_carries_ids_and_excerpts():
    prompt = build_prompt(payload())
    assert "m1" in prompt and "m2" in prompt
    assert "用户喜欢跑步" in prompt


# ------------------------------------------------------------------ enrich
def test_valid_reply_is_adopted(settings):
    reply = json.dumps(
        {
            "categories": [
                {"key": "profile", "summary": "用户有跑步习惯", "memory_ids": ["m1"]},
                {"key": "work", "summary": "用户从事物流工作", "memory_ids": ["m2"]},
            ]
        },
        ensure_ascii=False,
    )
    enricher = build_overview_enricher(settings, provider=FakeProvider(reply))
    assert enricher is not None

    result = enricher(payload())
    assert result["ai_enhanced"] is True
    assert result["ai_model"] == "fake-model"
    profile = result["categories"][0]
    assert profile["ai_summary"] == "用户有跑步习惯"
    # The deterministic excerpt is preserved for fallback.
    assert profile["summary"] == "用户喜欢跑步"
    # Item ids are untouched.
    assert profile["items"][0]["memory_id"] == "m1"


def test_partial_adoption_keeps_rejected_categories(settings):
    reply = json.dumps(
        {
            "categories": [
                {"key": "profile", "summary": "用户有跑步习惯", "memory_ids": ["m1"]},
                {"key": "work", "summary": "用户 2019 年入职", "memory_ids": ["m2"]},
            ]
        },
        ensure_ascii=False,
    )
    enricher = build_overview_enricher(settings, provider=FakeProvider(reply))
    result = enricher(payload())

    assert result["categories"][0]["ai_summary"] == "用户有跑步习惯"
    assert "ai_summary" not in result["categories"][1]


def test_all_rejected_means_no_enhancement(settings):
    reply = json.dumps(
        {
            "categories": [
                {"key": "profile", "summary": "用户 2019 年开始跑步", "memory_ids": ["m1"]}
            ]
        },
        ensure_ascii=False,
    )
    enricher = build_overview_enricher(settings, provider=FakeProvider(reply))
    result = enricher(payload())
    assert result["ai_enhanced"] is False
    assert all("ai_summary" not in item for item in result["categories"])


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider(raises=LLMError("boom")),
        FakeProvider(raises=RuntimeError("unexpected")),
        FakeProvider("not json at all"),
        FakeProvider(""),
    ],
)
def test_failures_return_the_deterministic_payload(settings, provider):
    enricher = build_overview_enricher(settings, provider=provider)
    result = enricher(payload())
    assert result["ai_enhanced"] is False
    assert result["categories"][0]["summary"] == "用户喜欢跑步"
    assert "ai_summary" not in result["categories"][0]


def test_live_token_never_reaches_the_provider(settings):
    tokened = settings
    provider = FakeProvider('{"categories":[]}')
    enricher = build_overview_enricher(tokened, provider=provider)
    enricher(payload())

    sent = provider.calls[0]
    joined = " ".join(message["content"] for message in sent)
    assert tokened.api_token not in joined


def test_empty_payload_does_not_call_the_model(settings):
    provider = FakeProvider("{}")
    enricher = build_overview_enricher(settings, provider=provider)
    result = enricher({**payload(), "categories": []})
    assert provider.calls == []
    assert result["categories"] == []
