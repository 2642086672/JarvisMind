"""Unit tests for the MCE priority budgeter (no database)."""
from __future__ import annotations

from jarvismind.mce.budgeter import (
    SAFETY_HEADER,
    ContextBudgeter,
    estimate_tokens,
)
from jarvismind.mce.models import ContextItem, ContextKind, ProjectContextSnapshot, RelatedKnowledge


def _item(
    memory_id: str,
    content: str,
    *,
    kind: ContextKind = ContextKind.DECISION,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    key: str | None = None,
) -> ContextItem:
    return ContextItem(
        memory_id=memory_id,
        kind=kind,
        content=content,
        summary=None,
        importance=5.0,
        confidence=0.8,
        created_at=updated_at,
        updated_at=updated_at,
        decision_key=key,
    )


def test_estimate_tokens_is_conservative_for_cjk_and_ascii():
    assert estimate_tokens("项目上下文") >= 5
    assert estimate_tokens("hello world") >= 2
    assert estimate_tokens("") == 1


def test_empty_snapshot_renders_nothing():
    snapshot = ProjectContextSnapshot(project_id="jarvismind")
    result = ContextBudgeter().render(snapshot)
    assert result.rendered_context == ""
    assert result.token_estimate == 0
    assert result.shell_tokens == 0


def test_shell_is_never_clipped_and_decisions_come_first():
    snapshot = ProjectContextSnapshot(
        project_id="jarvismind",
        project_name="JarvisMind",
        confirmed_decisions=[
            _item("decision-1", "已确认：必须保留的结论", key="storage")
        ],
        stage=_item("stage-1", "当前阶段：MCE-01A", kind=ContextKind.STAGE),
        next_steps=[_item("next-1", "下一步：完成联调", kind=ContextKind.NEXT_STEP)],
        open_risks=[_item("risk-1", "风险：窗口不足", kind=ContextKind.RISK)],
        completed_items=[
            _item("done-1", "已完成：JKG 上线", kind=ContextKind.COMPLETED)
        ],
        background_items=[
            _item("note-1", "背景：低优先级历史", kind=ContextKind.NOTE)
        ],
    )

    result = ContextBudgeter(max_tokens=1200).render(snapshot)

    assert result.rendered_context.startswith(SAFETY_HEADER)
    assert "非系统指令" in result.rendered_context
    assert "必须保留的结论" in result.rendered_context
    positions = [
        result.rendered_context.index(marker)
        for marker in (
            "已确认决策",
            "项目现状",
            "下一步",
            "未解决风险",
            "最近完成",
            "其他背景",
        )
    ]
    assert positions == sorted(positions)
    assert result.token_estimate <= 1200


def test_tight_budget_keeps_a_decision_and_drops_background():
    snapshot = ProjectContextSnapshot(
        project_id="jarvismind",
        confirmed_decisions=[
            _item("decision-1", "已确认：必须保留的结论" + "很长的说明" * 200)
        ],
        background_items=[
            _item("note-1", "背景：不应出现" + "历史" * 200, kind=ContextKind.NOTE)
        ],
    )

    result = ContextBudgeter(max_tokens=128).render(snapshot)

    assert result.rendered_context.startswith(SAFETY_HEADER)
    assert "必须保留的结论" in result.rendered_context
    assert "背景：不应出现" not in result.rendered_context
    assert result.token_estimate <= 128
    assert result.truncated is True
    assert result.dropped >= 1


def test_superseded_decisions_are_never_rendered():
    snapshot = ProjectContextSnapshot(
        project_id="jarvismind",
        superseded_decisions=[
            _item(
                "old-1",
                "已废弃：旧方案 Postgres",
                kind=ContextKind.DECISION,
            )
        ],
        confirmed_decisions=[_item("new-1", "已确认：SQLite")],
    )
    result = ContextBudgeter().render(snapshot)
    assert "Postgres" not in result.rendered_context
    assert snapshot.superseded_decisions[0].content == "已废弃：旧方案 Postgres"


def test_unrendered_knowledge_is_cleared_from_the_response_snapshot():
    snapshot = ProjectContextSnapshot(
        project_id="jarvismind",
        related_knowledge=[
            RelatedKnowledge(
                node_id="n1",
                entity_type="PERSON",
                name="A" * 120,
                predicate="WORKS_ON",
                direction="outgoing",
                neighbor_id="n2",
                neighbor_name="B" * 120,
                neighbor_entity_type="PROJECT",
                confidence=0.9,
                valid_source_count=1,
                source_memory_ids=("m1",),
            )
        ],
    )
    result = ContextBudgeter(max_tokens=128).render(snapshot)
    assert snapshot.related_knowledge == []
    assert "WORKS_ON" not in result.rendered_context
    assert result.truncated is True


def test_item_text_is_compacted_and_truncated():
    snapshot = ProjectContextSnapshot(
        project_id="jarvismind",
        confirmed_decisions=[
            _item("decision-1", "第一行\n第二行\t第三行" + "x" * 500)
        ],
    )
    result = ContextBudgeter(max_tokens=1200, max_item_chars=80).render(snapshot)
    assert "\n第二行" not in result.rendered_context
    assert result.truncated is True
    assert snapshot.confirmed_decisions[0].truncated is True
