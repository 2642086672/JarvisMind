"""Unit tests for the MCE reconstructor using an in-memory fake projection."""
from __future__ import annotations

from typing import Any, Dict, List

from jarvismind.mce.config import MceConfig
from jarvismind.mce.governance import MceGovernance
from jarvismind.mce.models import ContextKind
from jarvismind.mce.reconstructor import MceReconstructor


class FakeProjection:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: List[Dict[str, Any]] = []

    def scan_memories(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("after_id"):
            return ()
        return tuple(self.rows)


def _row(
    memory_id: str,
    content: str,
    metadata: Dict[str, Any],
    *,
    status: str = "ACTIVE",
    expires_at: str | None = None,
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> Dict[str, Any]:
    return {
        "id": memory_id,
        "memory_type": "PROJECT",
        "content": content,
        "summary": None,
        "importance": 5.0,
        "confidence": 0.8,
        "status": status,
        "created_at": updated_at,
        "updated_at": updated_at,
        "expires_at": expires_at,
        "metadata_json": metadata,
        "forgotten_at": None,
    }


def _reconstructor(rows: List[Dict[str, Any]], **config) -> MceReconstructor:
    return MceReconstructor(
        projection=FakeProjection(rows),
        governance=MceGovernance(),
        config=MceConfig(**config),
    )


def test_no_project_scope_returns_empty_without_scanning():
    reconstructor = _reconstructor(
        [_row("m1", "项目内容", {"project_id": "jarvismind"})]
    )
    result = reconstructor.resolve(project_id=None)
    assert result.rendered_context == ""
    assert result.empty_reason == "no_project_scope"
    assert result.snapshot.sources == []


def test_project_scope_is_exact_and_case_insensitive():
    rows = [
        _row("p1", "[project:jarvismind] 当前阶段：A", {"context_kind": "stage"}),
        _row("p2", "[project:other] 当前阶段：B", {"context_kind": "stage"}),
    ]
    result = _reconstructor(rows).resolve(project_id="JarvisMind")
    assert result.snapshot.stage.memory_id == "p1"
    assert result.snapshot.excluded.other_project == 1
    assert "B" not in result.rendered_context


def test_untagged_rows_are_not_attributed_to_a_project():
    rows = [_row("m1", "用户喜欢咖啡", {"context_kind": "note"})]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.rendered_context == ""
    assert result.snapshot.excluded.no_project == 1


def test_expired_and_non_active_rows_are_excluded():
    rows = [
        _row(
            "expired",
            "决策：过期",
            {"project_id": "jarvismind", "context_kind": "decision"},
            expires_at="2020-01-01T00:00:00+00:00",
        ),
        _row(
            "deleted",
            "决策：已删除",
            {"project_id": "jarvismind", "context_kind": "decision"},
            status="DELETED",
        ),
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.excluded.expired == 1
    assert "过期" not in result.rendered_context
    assert "已删除" not in result.rendered_context


def test_content_prefix_classification_for_historical_rows():
    rows = [
        _row("stage", "当前阶段：MCE-01A", {"project_id": "jarvismind"}),
        _row("done", "已完成：JKG 上线", {"project_id": "jarvismind"}),
        _row("decision", "决策：保持只读", {"project_id": "jarvismind"}),
        _row("risk", "风险：窗口不足", {"project_id": "jarvismind"}),
        _row("next", "下一步：联调", {"project_id": "jarvismind"}),
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.stage.kind is ContextKind.STAGE
    assert result.snapshot.completed_items[0].kind is ContextKind.COMPLETED
    assert result.snapshot.confirmed_decisions[0].kind is ContextKind.DECISION
    assert result.snapshot.open_risks[0].kind is ContextKind.RISK
    assert result.snapshot.next_steps[0].kind is ContextKind.NEXT_STEP


def test_sensitive_rows_are_counted_and_withheld():
    rows = [
        _row(
            "secret",
            "服务器 api key: sk-abcdefghijklmnopqrstuvwx",
            {"project_id": "jarvismind", "context_kind": "decision"},
        )
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.excluded.sensitive == 1
    assert "sk-" not in result.rendered_context


def test_duplicate_content_is_collapsed():
    rows = [
        _row("a", "决策：同一个结论", {"project_id": "jarvismind", "context_kind": "decision"}),
        _row("b", "决策：同一个结论", {"project_id": "jarvismind", "context_kind": "decision"}),
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.excluded.duplicate == 1
    assert len(result.snapshot.confirmed_decisions) == 1


def test_item_cap_prefers_decisions_over_background():
    rows = [
        _row(
            f"d{i}",
            f"决策：结论 {i}",
            {"project_id": "jarvismind", "context_kind": "decision", "decision_key": f"k{i}"},
        )
        for i in range(6)
    ] + [
        _row(
            f"n{i}",
            f"背景说明 {i}",
            {"project_id": "jarvismind", "context_kind": "note"},
        )
        for i in range(6)
    ]
    result = _reconstructor(rows, max_items=3).resolve(
        project_id="jarvismind", query="结论"
    )
    total = len(result.snapshot.confirmed_decisions) + len(
        result.snapshot.background_items
    )
    assert len(result.snapshot.confirmed_decisions) >= 1
    assert total <= 3


def test_jkg_not_requested_reports_skipped():
    rows = [_row("d", "决策：只读", {"project_id": "jarvismind", "context_kind": "decision"})]
    result = _reconstructor(rows).resolve(project_id="jarvismind", include_jkg=False)
    assert result.snapshot.jkg_state == "skipped"
    assert result.snapshot.jkg_reason == "disabled_by_config"


def test_empty_reason_when_all_rows_are_filtered():
    rows = [_row("x", "无关内容", {"context_kind": "note"})]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.empty_reason == "no_eligible_memories"


def test_scan_requests_newest_first():
    projection = FakeProjection([
        _row("m1", "当前阶段：A", {"project_id": "jarvismind"})
    ])
    reconstructor = MceReconstructor(
        projection=projection,
        governance=MceGovernance(),
        config=MceConfig(),
    )
    reconstructor.resolve(project_id="jarvismind")
    assert projection.calls
    assert projection.calls[0]["newest_first"] is True


def test_generic_key_is_not_a_decision_key():
    from jarvismind.mce.models import metadata_decision_key

    assert metadata_decision_key({"key": "cache-key"}) is None
    assert metadata_decision_key({"decision_key": "real-key"}) == "real-key"


def test_superseded_note_is_routed_to_conflicts_not_background():
    rows = [
        _row(
            "note-old",
            "[project:jarvismind] 旧结论",
            {
                "project_id": "jarvismind",
                "context_kind": "note",
                "decision_status": "SUPERSEDED",
            },
        )
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.background_items == []
    assert [item.memory_id for item in result.snapshot.superseded_decisions] == [
        "note-old"
    ]
    assert "旧结论" not in result.rendered_context


def test_stage_selection_uses_recency_not_query_relevance():
    rows = [
        _row(
            "old-stage",
            "[project:jarvismind] 当前阶段：旧阶段 MCE",
            {"project_id": "jarvismind", "context_kind": "stage"},
            updated_at="2026-01-01T00:00:00+00:00",
        ),
        _row(
            "new-stage",
            "[project:jarvismind] 当前阶段：新阶段 JKG",
            {"project_id": "jarvismind", "context_kind": "stage"},
            updated_at="2026-02-01T00:00:00+00:00",
        ),
    ]
    result = _reconstructor(rows).resolve(
        project_id="jarvismind", query="MCE 旧阶段"
    )
    assert result.snapshot.stage.memory_id == "new-stage"


def test_unknown_project_status_is_not_fabricated():
    rows = [
        _row(
            "d",
            "[project:jarvismind] 决策：只读",
            {"project_id": "jarvismind", "context_kind": "decision"},
        )
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.project_status is None
    assert "状态：ACTIVE" not in result.rendered_context


def test_generic_title_is_not_used_as_project_name():
    rows = [
        _row(
            "d",
            "[project:jarvismind] 决策：只读",
            {
                "project_id": "jarvismind",
                "context_kind": "decision",
                "title": "SQLite notes",
            },
        )
    ]
    result = _reconstructor(rows).resolve(project_id="jarvismind")
    assert result.snapshot.project_name is None
    assert "SQLite notes" not in result.rendered_context
