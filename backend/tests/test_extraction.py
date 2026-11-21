"""Extraction engine tests: parsing, classification, dedup, conflict, review."""
import pytest

from jarvismind.config import Settings
from jarvismind.schemas.extraction import ExtractMode, ExtractRequest
from jarvismind.services.extraction import ExtractionEngine
from jarvismind.services.extraction.prompt import parse_response
from jarvismind.services.llm.base import LLMError, LLMProvider, LLMResult


class FakeProvider(LLMProvider):
    """Test double for the LLM path. Records calls; never contacts a network."""

    name = "fake"

    def __init__(self, *, text="", fail=None, cost_cny=0.002):
        self.text = text
        self.fail = fail
        self.cost_cny = cost_cny
        self.calls = []

    def complete(self, messages, *, timeout=20.0, max_tokens=800, temperature=0.2):
        self.calls.append({"timeout": timeout, "max_tokens": max_tokens, "messages": messages})
        if self.fail:
            raise self.fail
        return LLMResult(text=self.text, model="fake-1", input_tokens=100, output_tokens=50)

    def estimate_cost_cny(self, result):
        return self.cost_cny


def _engine(manager, settings, provider=None):
    return ExtractionEngine(manager, settings, provider=provider)


def _extract(engine, conversation, conversation_id="conv-1", mode=ExtractMode.LOCAL, source_id=None):
    return engine.extract(
        ExtractRequest(
            conversation=conversation,
            conversation_id=conversation_id,
            source_id=source_id,
            mode=mode,
        )
    )


def _settings(tmp_path, **overrides):
    base = dict(
        env="test",
        data_dir=tmp_path / "data",
        api_token="t",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'data' / 'jarvismind.db').as_posix()}",
        llm_budget_cny=100.0,
        llm_price_in=0.01,
        llm_price_out=0.02,
        llm_auto_save_threshold=0.85,
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------- parsing
def test_parse_response_extracts_candidates():
    payload = parse_response(
        '```json\n{"candidates":[{"memory_type":"SEMANTIC",'
        '"content":"用户偏好中文","summary":"语言","importance":8,"confidence":0.9}]}\n```'
    )
    assert len(payload) == 1
    assert payload[0]["memory_type"] == "SEMANTIC"
    assert payload[0]["confidence"] == 0.9


def test_parse_response_rejects_bad_types_and_empty_content():
    payload = parse_response(
        '{"candidates":[{"memory_type":"BOGUS","content":"x"},'
        '{"memory_type":"PROFILE","content":"  "},'
        '{"memory_type":"PROJECT","content":"有效内容"}]}'
    )
    assert [item["memory_type"] for item in payload] == ["PROJECT"]


def test_parse_response_clamps_ranges():
    payload = parse_response(
        '{"candidates":[{"memory_type":"SEMANTIC","content":"x",'
        '"importance":99,"confidence":5}]}'
    )
    assert payload[0]["importance"] == 10.0
    assert payload[0]["confidence"] == 1.0


def test_parse_response_ignores_non_json():
    with pytest.raises(ValueError):
        parse_response("I cannot help with that.")


# ---------------------------------------------------------------- classification
def test_local_extraction_classifies_types(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(
        engine,
        "我从事物流快运行业，负责省区客服质管和理赔工作。\n"
        "JarvisMind 项目的技术栈是 React Native 和 Expo。\n"
        "我养了两只猫，喜欢去舟山海边拍视频。",
    )
    types = {candidate.memory_type for candidate in resp.candidates}
    assert "SEMANTIC" in types
    assert "PROJECT" in types


def test_chitchat_is_not_extracted(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "你好！谢谢，好的，收到。")
    assert resp.candidates == []


def test_short_statement_is_ignored(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "好的")
    assert resp.candidates == []


def test_confidence_and_importance_are_scored(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "我长期从事物流快运行业，工作内容涉及货物破损与理赔。")
    candidate = resp.candidates[0]
    assert 0.0 <= candidate.confidence <= 1.0
    assert 0.0 <= candidate.importance <= 10.0


# -------------------------------------------------------------------- sensitive
def test_sensitive_credentials_are_never_stored(manager, settings):
    engine = _engine(manager, settings)
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    resp = _extract(
        engine,
        f"请记住我的 API key：{secret}，以后一直使用它调用接口。",
    )
    assert "openai_key" in resp.sensitive_categories
    for candidate in resp.candidates:
        assert secret not in candidate.content
    # And nothing was written to the database containing the secret.
    from sqlalchemy import text

    with manager.session() as session:
        rows = session.execute(text("SELECT content FROM memory_records")).all()
    assert all(secret not in (row.content or "") for row in rows)


def test_redaction_happens_before_classification(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "我的邮箱是 someone@example.com，请保持联系。")
    assert "email" in resp.sensitive_categories
    for candidate in resp.candidates:
        assert "someone@example.com" not in candidate.content


# -------------------------------------------------------------------- duplicate
def test_duplicate_candidate_is_not_persisted(manager, settings):
    """003-B: a duplicate is an extraction event, never a new candidate."""
    engine = _engine(manager, settings)
    statement = "我从事物流快运行业，负责省区客服质管和理赔工作。"
    first = _extract(engine, statement, conversation_id="c1")
    assert first.outcome_counts.extracted == 1

    second = _extract(engine, statement, conversation_id="c2")
    assert second.candidates == []
    assert second.outcome_counts.duplicate_rejected == 1
    assert second.outcome_counts.extracted == 0
    assert any(event.event_type == "DUPLICATE" for event in second.extraction_events)
    assert engine.list_candidates(page_size=100)[1] == 1


# --------------------------------------------------------------------- conflict
def test_conflict_is_marked_and_kept_pending(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, "JarvisMind 项目当前使用 SQLite 作为唯一数据源。", conversation_id="c1")
    second = _extract(
        engine, "JarvisMind 项目计划迁移到 NAS 部署，使用新的存储方案。", conversation_id="c2"
    )
    assert any(candidate.conflicts_with for candidate in second.candidates)
    # A conflict is never auto-approved: both memories must survive.
    assert all(
        candidate.status.value != "APPROVED" for candidate in second.candidates
    )


# ------------------------------------------------------------------ auto save
def test_high_confidence_candidate_is_never_auto_saved(manager, settings):
    """003-B P0-2: confidence must never auto-create a formal memory."""
    engine = _engine(manager, settings)
    resp = _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。")
    assert resp.counts["APPROVED"] == 0
    assert resp.counts["PENDING"] == 1
    assert resp.outcome_counts.auto_saved == 0
    assert resp.outcome_counts.manually_approved == 0
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_low_confidence_candidate_stays_pending(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "今天要把周报做完，顺便处理一下升级工单。")
    assert resp.counts["PENDING"] == 1
    assert engine.memory_service.list(page_size=100)[1] == 0


# --------------------------------------------------------------------- review
def test_approve_pending_candidate_creates_memory(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "今天要把周报做完，顺便处理一下升级工单。")
    pending = resp.candidates[0]
    assert pending.status.value == "PENDING"

    approved = engine.review(pending.id, decision="approve", actor="tester")
    assert approved.status.value == "APPROVED"
    # 003-B: client-supplied actor is never an authorization fact.
    assert approved.decided_by == "server:user-approval"
    records, total = engine.memory_service.list(page_size=100)
    assert total == 1
    assert records[0].source_candidate_id == pending.id


def test_reject_pending_candidate(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "今天要把周报做完，顺便处理一下升级工单。")
    pending = resp.candidates[0]

    rejected = engine.review(
        pending.id, decision="reject", reason="短期任务不需保存", actor="tester"
    )
    assert rejected.status.value == "REJECTED"
    assert rejected.reject_reason == "短期任务不需保存"
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_review_requires_pending_status(manager, settings):
    """003-B: a second approval is rejected by the atomic gate."""
    engine = _engine(manager, settings)
    resp = _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。")
    candidate = resp.candidates[0]
    assert candidate.status.value == "PENDING"
    engine.review(candidate.id, decision="approve")
    from jarvismind.core.errors import JmeError

    with pytest.raises(JmeError):
        engine.review(candidate.id, decision="approve")
    assert engine.memory_service.list(page_size=100)[1] == 1


def test_invalid_decision_rejected(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "今天要把周报做完，顺便处理一下升级工单。")
    from jarvismind.core.errors import ValidationError

    with pytest.raises(ValidationError):
        engine.review(resp.candidates[0].id, decision="maybe")


def test_get_unknown_candidate_returns_404(manager, settings):
    engine = _engine(manager, settings)
    from jarvismind.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        engine.get_candidate("nope")


# ------------------------------------------------------------------ source link
def test_extract_validates_source_id(manager, settings):
    engine = _engine(manager, settings)
    from jarvismind.core.errors import ValidationError

    with pytest.raises(ValidationError):
        _extract(engine, "我从事物流快运行业。", source_id="missing")


def test_extract_links_source(manager, settings):
    from jarvismind.services.source_service import SourceService
    from jarvismind.schemas.source import SourceCreate

    source = SourceService(manager).create(
        SourceCreate(source_type="CHAT", source_name="会话 1")
    )
    engine = _engine(manager, settings)
    resp = _extract(
        engine,
        "我从事物流快运行业，负责省区客服质管和理赔工作。",
        source_id=source.id,
    )
    assert all(candidate.source_id == source.id for candidate in resp.candidates)


# ------------------------------------------------------------------ LLM paths
def test_llm_mode_without_provider_fails(manager, settings):
    engine = _engine(manager, settings, provider=None)
    from jarvismind.core.errors import ExtractionError

    with pytest.raises(ExtractionError):
        _extract(engine, "我从事物流快运行业。", mode=ExtractMode.LLM)


def test_llm_provider_result_is_used(manager, settings):
    provider = FakeProvider(
        text='{"candidates":[{"memory_type":"PROJECT","content":"JarvisMind 将接入 NAS 存储",'
        '"summary":"NAS 计划","importance":8.5,"confidence":0.95}]}'
    )
    engine = _engine(manager, settings, provider=provider)
    resp = _extract(engine, "JarvisMind 项目以后要部署到 NAS 上。", mode=ExtractMode.LLM)
    assert resp.engine == "llm"
    assert len(provider.calls) == 1
    assert provider.calls[0]["timeout"] == settings.llm_timeout
    # 003-B: even a 0.95-confidence LLM candidate stays pending.
    assert resp.counts["APPROVED"] == 0
    assert resp.outcome_counts.extracted == 1
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_llm_timeout_falls_back_to_local(manager, settings):
    provider = FakeProvider(fail=LLMError("LLM request timed out after 20s"))
    engine = _engine(manager, settings, provider=provider)
    resp = _extract(engine, "我从事物流快运行业，负责省区客服质管和理赔工作。", mode=ExtractMode.LLM)
    assert resp.engine == "llm"
    assert resp.candidates, "fallback should still produce candidates"


def test_unparseable_llm_reply_falls_back_to_local(manager, settings):
    provider = FakeProvider(text="抱歉，我无法完成这个请求。")
    engine = _engine(manager, settings, provider=provider)
    resp = _extract(engine, "我从事物流快运行业，负责省区客服质管和理赔工作。", mode=ExtractMode.LLM)
    assert resp.candidates


def test_budget_blocks_llm_call(manager, settings):
    limited = _settings(
        settings.data_dir.parent,
        llm_budget_cny=0.0001,
        llm_price_in=10.0,
        llm_price_out=10.0,
    )
    provider = FakeProvider(
        text='{"candidates":[{"memory_type":"SEMANTIC","content":"x","confidence":0.9}]}'
    )
    engine = ExtractionEngine(manager, limited, provider=provider)
    from jarvismind.core.errors import BudgetExceededError

    with pytest.raises(BudgetExceededError):
        _extract(engine, "我从事物流快运行业。", mode=ExtractMode.LLM)
    assert provider.calls == []


def test_budget_is_recorded_after_call(manager, settings):
    priced = _settings(
        settings.data_dir.parent,
        llm_budget_cny=100.0,
        llm_price_in=0.01,
        llm_price_out=0.02,
    )
    provider = FakeProvider(
        text='{"candidates":[{"memory_type":"SEMANTIC","content":"长期事实","confidence":0.5}]}'
    )
    engine = ExtractionEngine(manager, priced, provider=provider)
    before = engine.budget_status()["spent_cny"]
    _extract(engine, "我从事物流快运行业。", mode=ExtractMode.LLM)
    after = engine.budget_status()["spent_cny"]
    assert after > before


# ----------------------------------------------------------------- persistence
def test_candidates_are_persisted_and_searchable(manager, settings):
    """003-B: only an explicit approval makes the memory searchable."""
    engine = _engine(manager, settings)
    resp = _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。")
    candidate_id = resp.candidates[0].id

    rows, total = engine.list_candidates(page_size=100)
    assert total >= 1
    assert any(row.id == candidate_id for row in rows)
    assert engine.memory_service.search(query="NAS")[1] == 0

    engine.review(candidate_id, decision="approve")
    _results, count = engine.memory_service.search(query="NAS")
    assert count == 1


def test_candidate_list_filters(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。")
    _extract(engine, "今天要把周报做完，顺便处理一下升级工单。")

    pending, total_pending = engine.list_candidates(status="PENDING")
    assert total_pending == 2
    assert all(row.status.value == "PENDING" for row in pending)

    approved, total_approved = engine.list_candidates(status="APPROVED")
    assert total_approved == 0
    assert approved == []


def test_conversation_id_filter(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。", conversation_id="keep")
    _extract(engine, "今天要把周报做完，顺便处理一下升级工单。", conversation_id="other")
    rows, total = engine.list_candidates(conversation_id="keep")
    assert total == 1
    assert rows[0].conversation_id == "keep"


def test_budget_status_reports_limit_and_spend(manager, settings):
    funded = _settings(
        settings.data_dir.parent,
        llm_budget_cny=100.0,
        llm_price_in=0.01,
        llm_price_out=0.02,
    )
    engine = ExtractionEngine(manager, funded)
    status = engine.budget_status()
    assert status["limit_cny"] == 100.0
    assert status["remaining_cny"] == 100.0
    assert status["period_days"] == 30