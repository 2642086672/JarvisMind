"""Extraction governance additions: exact tokens and personal-sensitivity gates (A1).

Pure tests of ``extract_candidates`` / ``preview_memory``. No database, no
network, no model call. The default path (``exact_tokens=()``) must behave as it
did before these parameters existed.
"""
from __future__ import annotations

from jarvismind.jkg import extraction
from jarvismind.jkg.extraction import (
    REASON_PERSONAL_SENSITIVE_CATEGORY,
    RULE_VERSION,
    extract_candidates,
    preview_memory,
)
from jarvismind.jmr.sensitive import DECISION_ALLOW, DECISION_DENY, DECISION_HOLD, LEVEL_SENSITIVE

_LIVE_TOKEN = "A1-LIVE-TOKEN-7731"


class _FakeModuleProjection:
    def __init__(self, row):
        self._row = row

    def read_memory(self, memory_id):
        return self._row


def _reason_codes(result):
    return [item.reason_code for item in result.rejections]


def test_default_path_is_unchanged_when_no_exact_tokens_are_given():
    body = "张三 负责 JarvisMind。李四 参加 Launch。"
    implicit = extract_candidates(body, memory_id="m1")
    explicit = extract_candidates(body, memory_id="m1", exact_tokens=())
    assert implicit.to_dict() == explicit.to_dict()
    assert implicit.eligible is True
    assert implicit.rule_version == RULE_VERSION == "jkg-rules/2"


def test_exact_token_in_body_is_denied_and_extracts_nothing():
    body = f"张三 负责 JarvisMind。服务密钥是 {_LIVE_TOKEN}。"
    result = extract_candidates(body, memory_id="m1", exact_tokens=(_LIVE_TOKEN,))
    assert result.decision.decision == DECISION_DENY
    assert result.eligible is False
    assert result.nodes == () and result.edges == ()


def test_exact_token_is_not_detected_without_being_supplied():
    body = f"张三 负责 JarvisMind。服务密钥是 {_LIVE_TOKEN}。"
    result = extract_candidates(body, memory_id="m1")
    assert result.decision.decision == DECISION_ALLOW
    assert result.eligible is True


def test_exact_token_rejection_leaks_no_token_text():
    body = f"张三 负责 JarvisMind。{_LIVE_TOKEN}"
    result = extract_candidates(body, memory_id="m1", exact_tokens=(_LIVE_TOKEN,))
    assert _LIVE_TOKEN not in repr(result.to_dict())


def test_personal_category_in_body_skips_the_whole_memory():
    body = "张三 负责 JarvisMind，他的银行卡余额不多。"
    result = extract_candidates(body, memory_id="m1")
    assert result.eligible is False
    assert result.nodes == () and result.edges == ()
    assert result.decision.decision == DECISION_HOLD
    assert result.decision.level == LEVEL_SENSITIVE
    assert _reason_codes(result) == [REASON_PERSONAL_SENSITIVE_CATEGORY]


def test_personal_category_is_caught_through_zero_width_splitting():
    body = "张三 负责 JarvisMind，他的银\u200b行卡余额不多。"
    result = extract_candidates(body, memory_id="m1")
    assert result.eligible is False
    assert result.decision.decision == DECISION_HOLD
    assert REASON_PERSONAL_SENSITIVE_CATEGORY in _reason_codes(result)


def test_personal_category_rejection_carries_no_category_text():
    body = "张三 负责 JarvisMind，他的银行卡余额不多。"
    result = extract_candidates(body, memory_id="m1")
    serialised = repr(result.to_dict())
    assert "银行卡" not in serialised
    assert "余额" not in serialised


def test_name_level_personal_category_drops_only_that_candidate(monkeypatch):
    # The body gate already covers any name that sits in the body, so a name
    # that fails only the per-name gate cannot be built from ordinary text. The
    # classifier is patched to flag exactly the label "Bob", which exercises the
    # per-name drop path while every other candidate survives.
    original = extraction._personal_categories

    def flag_bob_label_only(value):
        if value == "Bob":
            return ("health",)
        return original(value)

    monkeypatch.setattr(extraction, "_personal_categories", flag_bob_label_only)
    body = "Alice 负责 JarvisMind。Bob 认识 Alice。"
    result = extract_candidates(body, memory_id="m1")
    names = {node.name for node in result.nodes}
    assert "Alice" in names and "JarvisMind" in names
    assert "Bob" not in names
    assert [edge.predicate for edge in result.edges] == ["WORKS_ON"]
    assert REASON_PERSONAL_SENSITIVE_CATEGORY in _reason_codes(result)


def test_personal_gate_does_not_fire_on_ordinary_project_text():
    body = "张三 负责 JarvisMind。"
    result = extract_candidates(body, memory_id="m1")
    assert result.eligible is True
    assert result.decision.decision == DECISION_ALLOW
    assert len(result.edges) == 1


def test_preview_memory_passes_exact_tokens_through():
    row = {
        "id": "m1",
        "content": f"张三 负责 JarvisMind。{_LIVE_TOKEN}",
        "metadata_json": None,
        "status": "ACTIVE",
    }
    default = preview_memory(_FakeModuleProjection(row), "m1")
    guarded = preview_memory(_FakeModuleProjection(row), "m1", exact_tokens=(_LIVE_TOKEN,))
    assert default.decision.decision != DECISION_DENY
    assert guarded.decision.decision == DECISION_DENY
    assert guarded.eligible is False
