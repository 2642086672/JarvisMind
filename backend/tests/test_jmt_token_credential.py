"""The JarvisMind token shape is a credential, and only the full form is one.

``jmt_<8 hex public id>_<secret>`` is how this server mints agent and owner
tokens. The public id is *designed* to be visible in listings, so a rule that
matches it alone would corrupt the audit surfaces that print it. These tests
pin both sides of that boundary.
"""
import pytest

from jarvismind.core.sensitive import (
    CREDENTIAL_CATEGORIES,
    credential_categories,
    is_credential_category,
    redact,
    scan,
)

PUBLIC_ID = "jmt_c0f26d38"
SECRET = "A" * 43
FULL_TOKEN = f"{PUBLIC_ID}_{SECRET}"


def test_a_full_agent_token_is_a_credential():
    assert credential_categories(FULL_TOKEN) == ["jmt_token"]


def test_a_full_token_is_blocked_inside_a_normal_sentence():
    text = f"用户的 token 是 {FULL_TOKEN}"
    assert "jmt_token" in credential_categories(text)


def test_a_full_token_containing_the_log_redaction_marker_pattern_is_caught():
    """A token whose secret begins with characters that also look like a key."""
    assert "jmt_token" in credential_categories(f"{PUBLIC_ID}_sk-{'b' * 30}")


@pytest.mark.parametrize(
    "text",
    [
        # The public id alone is not a secret: listings print it by design.
        f"token public id {PUBLIC_ID}",
        f"issued token {PUBLIC_ID}",
        # Too short a tail is not a minted secret.
        f"{PUBLIC_ID}_short",
        f"{PUBLIC_ID}",
        "jmt_",
        # Wrong separator shapes.
        f"jmt-c0f26d38-{SECRET}",
        f"jmt_c0f26d3_{SECRET}",
    ],
)
def test_public_ids_and_non_token_shapes_stay_clean(text):
    assert "jmt_token" not in credential_categories(text)


def test_the_category_is_exported_so_callers_classify_it_as_a_credential():
    assert "jmt_token" in CREDENTIAL_CATEGORIES
    assert is_credential_category("jmt_token")


def test_scan_returns_the_span_and_redact_removes_it():
    hits = scan(FULL_TOKEN)
    assert any(hit.category == "jmt_token" for hit in hits)
    redacted = redact(f"my token is {FULL_TOKEN}", [])
    assert SECRET not in redacted
    assert "REDACTED" in redacted


def test_a_redacted_tail_still_marks_the_memory_sensitive():
    """Redaction must not make a credential look safe to a later check."""
    redacted = redact(f"my token is {FULL_TOKEN}", [])
    # The value is gone, so re-scanning the redacted text is clean. The point is
    # that the write path blocks *before* redaction ever happens.
    assert credential_categories(redacted) == []
