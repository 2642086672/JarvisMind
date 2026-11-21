"""Secondary entity match keys (JKG-INT-05 / A1). Pure, no database.

Principle under test: 不确定不合并 (when unsure, do not merge).
"""
from __future__ import annotations

import pytest

from jarvismind.core.errors import ValidationError
from jarvismind.jkg.matching import entity_match_key


@pytest.mark.parametrize(
    "variant",
    ["JarvisMind", "Jarvis Mind", "JarvisMind 项目", "jarvismind", "Jarvis-Mind", "Jarvis_Mind", "ＪａｒｖｉｓＭｉｎｄ", "JARVIS  MIND 项目"],
)
def test_project_variants_share_one_key(variant):
    assert entity_match_key("PROJECT", variant) == "jarvismind"


def test_project_suffix_is_stripped_only_when_trailing():
    assert entity_match_key("PROJECT", "JarvisMind 项目") == "jarvismind"
    assert entity_match_key("PROJECT", "项目管理系统") == "项目管理系统"


def test_project_name_that_is_only_the_suffix_falls_back_to_normalized_name():
    assert entity_match_key("PROJECT", "项目") == "项目"


@pytest.mark.parametrize("name", ["Alice Smith", "Alice-Smith", "Alice_Smith", "alice·smith", "AliceSmith"])
def test_person_names_are_not_separator_folded(name):
    assert entity_match_key("PERSON", name) == name.casefold().replace("  ", " ")


def test_person_alice_smith_and_alice_hyphen_smith_stay_distinct():
    assert entity_match_key("PERSON", "Alice Smith") != entity_match_key("PERSON", "Alice-Smith")


def test_person_case_and_whitespace_still_fold():
    assert entity_match_key("PERSON", "Alice   Smith") == entity_match_key("PERSON", "alice smith")


def test_person_suffix_project_is_not_stripped():
    assert entity_match_key("PERSON", "张三项目") == "张三项目"
    assert entity_match_key("PERSON", "张三项目") != entity_match_key("PERSON", "张三")


def test_different_people_are_never_merged():
    assert entity_match_key("PERSON", "张三") != entity_match_key("PERSON", "李四")


def test_event_keeps_internal_separators():
    assert entity_match_key("EVENT", "Launch Day") != entity_match_key("EVENT", "LaunchDay")
    assert entity_match_key("EVENT", "Launch-Day") != entity_match_key("EVENT", "Launch Day")


def test_event_case_and_whitespace_still_fold():
    assert entity_match_key("EVENT", "Launch  Day") == entity_match_key("EVENT", "launch day")


def test_event_suffix_project_is_not_stripped():
    assert entity_match_key("EVENT", "launch 项目") == "launch 项目"


def test_key_is_deterministic():
    assert entity_match_key("PROJECT", "JarvisMind 项目") == entity_match_key("PROJECT", "JarvisMind 项目")


def test_empty_name_is_rejected():
    with pytest.raises(ValidationError):
        entity_match_key("PROJECT", "   ")
    with pytest.raises(ValidationError):
        entity_match_key("PERSON", "")


def test_name_longer_than_the_entity_bound_is_rejected():
    with pytest.raises(ValidationError):
        entity_match_key("PERSON", "a" * 201)


def test_name_at_the_entity_bound_is_accepted():
    assert len(entity_match_key("PERSON", "a" * 200)) == 200


def test_non_string_name_is_rejected():
    with pytest.raises(ValidationError):
        entity_match_key("PERSON", None)  # type: ignore[arg-type]


def test_unknown_entity_type_is_rejected():
    with pytest.raises(ValidationError):
        entity_match_key("ORGANISATION", "JarvisMind")
