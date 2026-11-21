"""A1 end-to-end: sensitive memories never reach the API payload.

The unit-level bypass tests prove the scanners flag the shapes. This file proves
the *service* applies them before building a category, and that the API response
contains none of the values.

Seeded rows go into the test database only; production data is untouched.
"""
from __future__ import annotations

import json
import uuid

import pytest

from jarvismind.core.timeutils import utc_now_iso
from jarvismind.database.models import MemoryRecord
from jarvismind.services.memory_overview import MemoryOverviewService

OVERVIEW_URL = "/api/v1/memories/overview"


def _insert(manager, *, content: str, memory_type: str = "SEMANTIC") -> str:
    now = utc_now_iso()
    mid = uuid.uuid4().hex
    with manager.session() as session:
        session.add(
            MemoryRecord(
                id=mid,
                memory_type=memory_type,
                content=content,
                summary=None,
                importance=7.0,
                confidence=0.8,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
                expires_at=None,
                metadata_json=None,
            )
        )
        session.commit()
    return mid


#: The exact inputs the independent audit used to prove the bypass.
AUDIT_BYPASSES = [
    "手机 138 0013 8000",
    "密码 hunter2 用于登录",
    "用户的月薪是三万",
    "user lives at 5 Main St",
    "用户邮箱 a at b dot com",
]

#: Extra shapes the task book requires.
EXTRA_BYPASSES = [
    "我的手机是１３８００１３８０００",  # full-width
    "手机 138-0013-8000",
    "月收入 3 万",
    "他住在朝阳区建国路88号",
    "contact me: foo [at] bar [dot] com",
]


def _all_excerpts(payload) -> str:
    parts = []
    for category in payload["categories"]:
        parts.append(category.get("summary") or "")
        parts.append(category.get("ai_summary") or "")
        parts.extend(item.get("excerpt") or "" for item in category["items"])
    return "\n".join(parts)


@pytest.mark.parametrize("content", AUDIT_BYPASSES + EXTRA_BYPASSES)
def test_sensitive_memory_never_reaches_the_overview(manager, content):
    _insert(manager, content=content)

    payload = MemoryOverviewService(manager).build()
    rendered = _all_excerpts(payload)

    # The memory must be excluded, and its text must not appear anywhere.
    assert content not in rendered
    # Digits of the phone number must not survive either.
    collapsed = rendered.replace(" ", "").replace("-", "")
    assert "13800138000" not in collapsed


def test_bypass_rows_are_counted_as_excluded(manager):
    _insert(manager, content="手机 138 0013 8000")
    _insert(manager, content="用户喜欢跑步")

    payload = MemoryOverviewService(manager).build()
    assert payload["excluded_count"] >= 1
    assert payload["eligible_count"] == 1


def test_api_payload_contains_no_bypass_values(client, auth_headers, manager):
    for content in AUDIT_BYPASSES:
        _insert(manager, content=content)

    response = client.get(OVERVIEW_URL, headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.text

    for content in AUDIT_BYPASSES:
        assert content not in body
    collapsed = body.replace(" ", "").replace("-", "")
    assert "13800138000" not in collapsed


def test_ordinary_memories_still_pass(client, auth_headers, manager):
    _insert(manager, content="用户喜欢跑步", memory_type="PROFILE")
    _insert(manager, content="用户在物流行业做理赔", memory_type="SEMANTIC")

    response = client.get(OVERVIEW_URL, headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible_count"] == 2
    assert payload["excluded_count"] == 0


def test_ai_summary_cannot_reintroduce_a_bypass_value(manager):
    """Even if a cache held a leaked value, the memory must be excluded first."""
    _insert(manager, content="手机 138 0013 8000")

    from jarvismind.services.memory_overview_cache import CachedOverview, MemoryOverviewCache

    cache = MemoryOverviewCache()
    first = MemoryOverviewService(manager).build()
    cache.put(
        CachedOverview(
            source_version=first["source_version"],
            generated_at=utc_now_iso(),
            model="fake",
            summaries={"profile": "手机 138 0013 8000"},
        )
    )

    def echoes(payload):
        return {
            **payload,
            "categories": [
                {**category, "ai_summary": "手机 138 0013 8000"}
                for category in payload["categories"]
            ],
            "ai_enhanced": True,
            "ai_model": "fake",
        }

    service = MemoryOverviewService(manager, enricher=echoes, cache=cache)
    payload = service.build(enrich=True)
    assert "138 0013 8000" not in _all_excerpts(payload)


def test_sensitivity_reasons_are_value_free(manager):
    """The exclusion reason must name the category, never the value."""
    _insert(manager, content="手机 138 0013 8000")

    payload = MemoryOverviewService(manager).build()
    reasons = json.dumps(payload["excluded_reasons"], ensure_ascii=False)
    assert "pii" in reasons or "personal_sensitive" in reasons
    assert "138" not in reasons
