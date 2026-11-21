"""003-B1: security-defect regression suite.

All credential-looking values below are SYNTHETIC fixtures. No real token,
API key, password or private key is read, logged, or persisted by these tests.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text

from jarvismind.core.errors import DatabaseError, ValidationError
from jarvismind.core.fingerprint import (
    FingerprintKeyError,
    content_fingerprint,
    derive_fingerprint_key,
)
from jarvismind.core.security_gate import (
    AUTH_NOT_VERIFIED,
    RULESET_VERSION,
    assert_safe_text,
)
from jarvismind.core.security_gate import CredentialBlockedError
from jarvismind.database.connection import DatabaseManager
from jarvismind.database.migrations import MigrationRunner, initialize_database
from jarvismind.database.models import MemoryRecord
from jarvismind.main import create_app
from jarvismind.repositories.memory_repository import MemoryRepository
from jarvismind.repositories.security_event_repository import (
    SERVER_IDENTITY_LABEL,
    SecurityEventRepository,
)
from jarvismind.schemas.extraction import ExtractMode, ExtractRequest
from jarvismind.schemas.memory import MemoryCreate, MemoryStatus, MemoryUpdate, MemoryType
from jarvismind.services.approval_service import SERVER_ACTOR, ApprovalService
from jarvismind.services.extraction import ExtractionEngine
from jarvismind.services.memory_service import MemoryService
from jarvismind.services.migration_service import MigrationService

FAKE_API_KEY = "sk-FAKE-abcdefghijklmnopqrstuvwxyz0123456789"
FAKE_TOKEN = "ghp_" "FAKEabcdefghijklmnopqrstuvwxyz0123456789"
FAKE_PASSWORD = "password=FAKE-PlainText-123456"
FAKE_PRIVATE_KEY = (
    "-----BEGIN " "PRIVATE KEY-----\nFAKEBASE64BLOCK0123456789\n"
    "-----END " "PRIVATE KEY-----"
)
FAKE_DB_URI = "postgresql://fakeuser:FAKEPASS@db.invalid/jme"
CREDENTIALS = [
    FAKE_API_KEY, FAKE_TOKEN, FAKE_PASSWORD, FAKE_PRIVATE_KEY, FAKE_DB_URI,
]
VALID_SENTENCE = "JarvisMind 项目计划明年支持 NAS 部署。"


def _engine(manager, settings):
    return ExtractionEngine(manager, settings)


def _extract(engine, conversation, conversation_id="conv-b1", **kwargs):
    return engine.extract(
        ExtractRequest(
            conversation=conversation,
            conversation_id=conversation_id,
            mode=ExtractMode.LOCAL,
            **kwargs,
        )
    )


def _pending(engine, cid="c-b1-pending"):
    resp = _extract(engine, VALID_SENTENCE, conversation_id=cid)
    assert resp.candidates, "fixture must produce a candidate"
    return resp.candidates[0].id


def _rows(manager, sql, **params):
    with manager.session() as session:
        return session.execute(text(sql), params).fetchall()


def _scalar(manager, sql, **params):
    rows = _rows(manager, sql, **params)
    return rows[0][0] if rows else None


def _db_has(manager, needle: str) -> bool:
    """Read-only check across every table that could persist client text."""
    for table, column in (
        ("memory_records", "content"),
        ("memory_records", "metadata_json"),
        ("memory_candidates", "content"),
        ("memory_candidates", "metadata_json"),
        ("block_events", "turn_id"),
        ("block_events", "message_id"),
        ("extraction_events", "turn_id"),
        ("extraction_events", "message_id"),
        ("approval_events", "client_actor_claim"),
        ("approval_events", "actor_label"),
    ):
        for row in _rows(manager, f"SELECT {column} FROM {table}"):
            if needle in (row[0] or ""):
                return True
    return False


def _new_manager(tmp_path: Path, settings) -> DatabaseManager:
    data_dir = tmp_path / "b1data"
    db_path = data_dir / "jarvismind.db"
    st = replace(
        settings,
        data_dir=data_dir,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
    )
    mgr = DatabaseManager(st)
    initialize_database(mgr)
    return mgr


# ------------------------------------------------------- P0-1 metadata scanning
def _construct_create(metadata) -> MemoryCreate:
    """Bypass schema validation on purpose, to prove the SERVICE/ORM gate
    still blocks the write even when a caller skips pydantic entirely."""
    return MemoryCreate.model_construct(
        memory_type=MemoryType.SEMANTIC,
        content="普通记忆内容",
        summary=None,
        importance=5.0,
        confidence=0.5,
        status=MemoryStatus.ACTIVE,
        source_id=None,
        expires_at=None,
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"note": FAKE_API_KEY},                                   # plain string
        {"outer": {"inner": FAKE_TOKEN}},                         # nested dict
        {"items": [FAKE_PASSWORD, "harmless"]},                   # nested list
        {"a": {"b": {"c": {"d": [FAKE_PRIVATE_KEY]}}}},           # deep
        {"uri": FAKE_DB_URI},                                     # DB credential
        {"k": ["x", {"y": [{"z": FAKE_API_KEY}]}]},               # mixed
    ],
    ids=["string", "nested-dict", "nested-list", "deep", "db-uri", "mixed"],
)
def test_metadata_shapes_are_blocked_on_create(manager, settings, metadata):
    # Layer 1: schema rejects the request before any persistence.
    with pytest.raises(PydanticValidationError):
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="普通记忆内容",
            metadata=metadata,
        )
    # Layer 2: the service + ORM gate rejects it independently.
    service = MemoryService(manager)
    with pytest.raises(CredentialBlockedError):
        service.create(_construct_create(metadata))
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 0


def test_metadata_replace_via_update_is_blocked(manager, settings):
    service = MemoryService(manager)
    record = service.create(
        MemoryCreate(memory_type=MemoryType.SEMANTIC, content="普通记忆内容")
    )
    with pytest.raises(PydanticValidationError):
        MemoryUpdate(metadata={"a": {"b": FAKE_API_KEY}})
    bypass = MemoryUpdate.model_construct(
        content=None, summary=None, importance=None, confidence=None,
        status=None, source_id=None, expires_at=None,
        metadata={"a": {"b": FAKE_API_KEY}},
        _fields_set={"metadata"},
    )
    with pytest.raises(CredentialBlockedError):
        service.update(record.id, bypass)
    assert _scalar(
        manager, "SELECT COUNT(*) FROM memory_records WHERE metadata_json LIKE '%FAKE%'"
    ) == 0


def test_unscannable_metadata_fails_closed(manager, settings):
    # An unknown object type is rejected by the schema layer...
    with pytest.raises(PydanticValidationError):
        MemoryCreate(
            memory_type=MemoryType.SEMANTIC,
            content="普通记忆内容",
            metadata={"opaque": object()},
        )
    # ...and JSON-serializable but excessively nested metadata is refused by
    # the recursive walker (fail-closed), not written unchecked.
    deep: object = "x"
    for _ in range(12):
        deep = {"n": deep}
    with pytest.raises(ValidationError) as exc:
        MemoryService(manager).create(_construct_create(deep))
    assert exc.value.detail["code"] == "unsafe_metadata"
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 0


def test_orm_write_gate_blocks_direct_repository_insert(manager, settings):
    """Even a caller that skips MemoryService cannot bypass the ORM gate."""
    from jarvismind.core.timeutils import utc_now_iso

    now = utc_now_iso()
    with pytest.raises(CredentialBlockedError):
        with manager.session() as session:
            MemoryRepository(session).create(
                MemoryRecord(
                    id="direct-1",
                    memory_type="SEMANTIC",
                    content=FAKE_API_KEY,
                    created_at=now,
                    updated_at=now,
                    vector_status="PENDING",
                )
            )
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 0


# ------------------------------------------------------------- HTTP behaviour
def test_http_create_update_import_with_metadata_credentials(
    client, auth_headers, manager, settings
):
    create = client.post(
        "/api/v1/memories",
        json={
            "memory_type": "SEMANTIC",
            "content": "普通记忆内容",
            "metadata": {"nested": {"k": FAKE_API_KEY}},
        },
        headers=auth_headers,
    )
    assert create.status_code == 422
    assert FAKE_API_KEY not in create.text

    benign = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "普通记忆内容"},
        headers=auth_headers,
    )
    assert benign.status_code == 201
    memory_id = benign.json()["data"]["id"]
    update = client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"metadata": {"deep": [[FAKE_TOKEN]]}},
        headers=auth_headers,
    )
    assert update.status_code == 422
    assert FAKE_TOKEN not in update.text

    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "import-meta-1",
                "memory_type": "SEMANTIC",
                "content": "导入普通内容",
                "status": "ACTIVE",
                "metadata": {"nested": {"list": [FAKE_DB_URI]}},
            }
        ],
        "sources": [],
    }
    imported = client.post(
        "/api/v1/memories/import", json={"document": document}, headers=auth_headers
    )
    assert imported.status_code == 200
    assert imported.json()["data"]["skipped_credentials"] >= 1
    assert not _db_has(manager, FAKE_API_KEY)
    assert not _db_has(manager, FAKE_TOKEN)
    assert not _db_has(manager, FAKE_DB_URI)


@pytest.mark.parametrize("field", ["message_id", "turn_id"])
def test_http_identifier_credentials_are_refused_before_persistence(
    client, auth_headers, manager, settings, field
):
    resp = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": VALID_SENTENCE,
            "conversation_id": "c-ident",
            "mode": "local",
            field: FAKE_API_KEY,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert FAKE_API_KEY not in resp.text
    assert not _db_has(manager, FAKE_API_KEY)
    assert _scalar(manager, "SELECT COUNT(*) FROM block_events") == 0
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_candidates") == 0


def test_http_forged_actor_and_authorization_result(client, auth_headers, manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    forged = client.post(
        f"/api/v1/memories/candidates/{cid}/approve",
        json={
            "actor": "attacker-claim",
            "grants": [{"grant_id": "fake", "authorization_verification_result": "PASSED"}],
        },
        headers=auth_headers,
    )
    assert forged.status_code == 200
    row = _rows(
        manager,
        "SELECT actor_label, client_actor_claim, authentication_level, "
        "authorization_verification_result, device_id, interaction_id "
        "FROM approval_events WHERE candidate_id=:cid",
        cid=cid,
    )[0]
    assert row[0] == SERVER_IDENTITY_LABEL
    assert row[1] == "attacker-claim"
    assert row[2] == "API_TOKEN_SHARED"
    assert row[3] == AUTH_NOT_VERIFIED
    assert row[4] is None and row[5] is None
    assert _scalar(
        manager, "SELECT decided_by FROM memory_candidates WHERE id=:cid", cid=cid
    ) == SERVER_ACTOR

    injected = client.post(
        f"/api/v1/memories/candidates/{cid}/approve",
        json={"authorization_verification_result": "PASSED"},
        headers=auth_headers,
    )
    assert injected.status_code == 422  # extra: forbid
    assert _scalar(
        manager,
        "SELECT COUNT(*) FROM approval_events WHERE authorization_verification_result='PASSED'",
    ) == 0


def test_authorization_result_cannot_be_written_as_passed(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    service = ApprovalService(manager)
    service.review(cid, decision="approve")
    event_id = _scalar(
        manager, "SELECT event_id FROM approval_events WHERE candidate_id=:cid", cid=cid
    )
    with manager.session() as session:
        with pytest.raises(ValidationError):
            SecurityEventRepository(session).set_approval_result(
                event_id,
                transaction_result="COMMITTED",
                operation_result="SUCCEEDED",
                authorization_verification_result="PASSED",
            )
    assert _scalar(
        manager,
        "SELECT COUNT(*) FROM approval_events WHERE authorization_verification_result='PASSED'",
    ) == 0


def test_recover_endpoint_requires_token(client, auth_headers):
    anonymous = client.post("/api/v1/memories/approval/recover")
    assert anonymous.status_code == 401
    authorized = client.post(
        "/api/v1/memories/approval/recover", headers=auth_headers
    )
    assert authorized.status_code == 200


# -------------------------------------------------- P1-2 HMAC & TOCTOU
def test_fingerprint_is_keyed_and_stable(settings):
    fp = content_fingerprint(settings, content="同一内容", summary=None)
    assert len(fp) == 64
    assert fp == content_fingerprint(settings, content="同一内容", summary=None)
    assert fp != content_fingerprint(settings, content="改变后的内容", summary=None)
    assert fp != content_fingerprint(settings, content="同一内容", metadata_json="{}")
    plain = hashlib.sha256("同一内容".encode("utf-8")).hexdigest()
    assert fp != plain
    assert fp != "".join(["0"] * 64)


def test_missing_server_secret_fails_closed():
    with pytest.raises(FingerprintKeyError):
        derive_fingerprint_key(SimpleNamespace(api_token=""))
    with pytest.raises(FingerprintKeyError):
        content_fingerprint(SimpleNamespace(api_token=""), content="x")


def test_candidates_carry_real_hmac_fingerprint(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, VALID_SENTENCE)
    assert resp.candidates[0].content_fingerprint is not None
    assert len(resp.candidates[0].content_fingerprint) == 64
    stored = _scalar(
        manager,
        "SELECT content_fingerprint FROM memory_candidates WHERE id=:cid",
        cid=resp.candidates[0].id,
    )
    assert stored == resp.candidates[0].content_fingerprint


def test_stale_content_fingerprint_blocks_approval(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    # Simulate an out-of-band content edit (raw SQL bypasses the ORM gate).
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_candidates SET content='被外部修改的内容' WHERE id=:cid"),
            {"cid": cid},
        )
    with pytest.raises(ValidationError) as exc:
        ApprovalService(manager).review(cid, decision="approve")
    assert exc.value.detail["code"] == "stale_content_fingerprint"
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 0


def test_commit_rechecks_content_after_verification(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    service = ApprovalService(manager)
    snapshot = service._load_and_verify(cid, "approve")
    # Change the row between the read-only verification and the write txn.
    with manager.session() as session:
        session.execute(
            text("UPDATE memory_candidates SET content='并发写入的新内容' WHERE id=:cid"),
            {"cid": cid},
        )
    event_id = service._prepare_intent(cid, "approve", snapshot, "")
    with pytest.raises(DatabaseError):
        service._commit_atomic(cid, "approve", None, snapshot, event_id)
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 0


def test_concurrent_approval_stays_idempotent_and_not_verified(manager, settings):
    import threading

    engine = _engine(manager, settings)
    cid = _pending(engine)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        try:
            barrier.wait(timeout=5)
            ApprovalService(manager).review(cid, decision="approve")
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert outcomes.count("ok") == 1
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 1
    assert _scalar(
        manager,
        "SELECT COUNT(*) FROM approval_events WHERE candidate_id=:cid "
        "AND authorization_verification_result<>:passed",
        cid=cid,
        passed="PASSED",
    ) == 1


def test_unknown_recovery_keeps_authorization_not_verified(manager, settings, monkeypatch):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    service = ApprovalService(manager)

    def lost_confirmation(self, event_id, memory_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(ApprovalService, "_confirm", lost_confirmation)
    service.review(cid, decision="approve")
    monkeypatch.undo()
    assert _scalar(
        manager,
        "SELECT transaction_result FROM approval_events WHERE candidate_id=:cid",
        cid=cid,
    ) == "UNKNOWN"
    result = service.recover_unknown()
    assert result["committed"] == 1
    assert _scalar(manager, "SELECT COUNT(*) FROM memory_records") == 1
    assert _scalar(
        manager,
        "SELECT authorization_verification_result FROM approval_events WHERE candidate_id=:cid",
        cid=cid,
    ) == AUTH_NOT_VERIFIED


# ------------------------------------------------------------ P1-3 statistics
def _assert_exclusive(counts):
    total = (
        counts.blocked
        + counts.duplicate_rejected
        + counts.low_value
        + counts.empty
        + counts.pending_manual
        + counts.pending_pii_dual
        + counts.conflict_pending
    )
    assert counts.processed_units == total, counts.model_dump()
    assert counts.extracted == (
        counts.pending_manual + counts.pending_pii_dual + counts.conflict_pending
    )
    assert counts.ignored == counts.low_value + counts.empty
    assert counts.auto_saved == 0 and counts.manually_approved == 0
    assert counts.user_rejected == 0


def test_all_low_value_segments_are_accounted(manager, settings):
    engine = _engine(manager, settings)
    text_value = "你好。嗯嗯。今天天气不错，随便聊聊吧。"
    from jarvismind.services.extraction.local import segment_conversation

    expected = len(segment_conversation(text_value))
    resp = _extract(engine, text_value, conversation_id="c-low")
    assert resp.outcome_counts.processed_units == expected
    assert resp.outcome_counts.low_value == expected
    assert resp.outcome_counts.ignored == expected
    assert resp.outcome_counts.extracted == 0
    _assert_exclusive(resp.outcome_counts)
    assert _scalar(
        manager,
        "SELECT COUNT(*) FROM extraction_events WHERE event_type='LOW_VALUE'",
    ) == expected
    assert len(resp.extraction_events) == expected


def test_all_credential_input_accounted(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, f"请记住：{FAKE_API_KEY}", conversation_id="c-cred")
    assert resp.outcome_counts.blocked >= 1
    assert resp.outcome_counts.extracted == 0
    assert resp.counts["APPROVED"] == 0
    _assert_exclusive(resp.outcome_counts)
    assert _scalar(manager, "SELECT COUNT(*) FROM block_events") >= 1


def test_all_duplicate_input_accounted(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, VALID_SENTENCE, conversation_id="c-first")
    resp = _extract(engine, VALID_SENTENCE, conversation_id="c-second")
    assert resp.outcome_counts.duplicate_rejected >= 1
    assert resp.outcome_counts.extracted == 0
    _assert_exclusive(resp.outcome_counts)
    assert _scalar(
        manager, "SELECT COUNT(*) FROM extraction_events WHERE event_type='DUPLICATE'"
    ) >= 1


def test_all_empty_input_accounted(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "   ", conversation_id="c-empty")
    assert resp.outcome_counts.processed_units == 1
    assert resp.outcome_counts.empty == 1
    assert resp.outcome_counts.ignored == 1
    _assert_exclusive(resp.outcome_counts)
    assert _scalar(
        manager, "SELECT COUNT(*) FROM extraction_events WHERE event_type='EMPTY'"
    ) == 1


def test_conflict_units_are_candidates_not_duplicates(manager, settings, monkeypatch):
    import jarvismind.services.extraction.engine as engine_module

    engine = _engine(manager, settings)
    monkeypatch.setattr(
        engine_module,
        "find_matches",
        lambda content, existing: ([], ["existing-memory-id"]),
    )
    resp = _extract(engine, VALID_SENTENCE, conversation_id="c-conflict")
    assert resp.outcome_counts.conflict_pending >= 1
    assert resp.outcome_counts.extracted >= 1
    assert resp.outcome_counts.duplicate_rejected == 0
    _assert_exclusive(resp.outcome_counts)
    assert _scalar(
        manager,
        "SELECT COUNT(*) FROM memory_candidates WHERE conflicts_with IS NOT NULL",
    ) >= 1


def test_normal_outcome_is_exclusive(manager, settings):
    engine = _engine(manager, settings)
    normal = _extract(engine, VALID_SENTENCE, conversation_id="c-normal")
    assert normal.outcome_counts.pending_manual == 1
    assert normal.outcome_counts.extracted == 1
    _assert_exclusive(normal.outcome_counts)


def test_mixed_outcomes_are_exclusive(manager, settings):
    engine = _engine(manager, settings)
    mixed = _extract(
        engine,
        f"你好。{VALID_SENTENCE}请记住：{FAKE_API_KEY}。今天天气不错随便聊聊吧。",
        conversation_id="c-mixed",
    )
    _assert_exclusive(mixed.outcome_counts)
    assert mixed.outcome_counts.processed_units >= 3
    assert mixed.outcome_counts.blocked >= 1
    assert mixed.outcome_counts.low_value >= 1
    assert mixed.outcome_counts.pending_manual >= 1


def test_repeated_request_is_idempotent_and_accounted(manager, settings):
    engine = _engine(manager, settings)
    payload = dict(conversation=VALID_SENTENCE, conversation_id="c-repeat", mode=ExtractMode.LOCAL)
    first = engine.extract(ExtractRequest(**payload))
    events_after_first = _scalar(manager, "SELECT COUNT(*) FROM extraction_events")
    second = engine.extract(ExtractRequest(**payload))
    assert second.outcome_counts.duplicate_rejected >= 1
    assert second.outcome_counts.extracted == 0
    _assert_exclusive(second.outcome_counts)
    assert _scalar(manager, "SELECT COUNT(*) FROM extraction_events") == events_after_first + 1
    assert first.outcome_counts.extracted == 1


def test_stats_endpoint_uses_the_same_vocabulary(client, auth_headers, manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, "你好。", conversation_id="c-stats")
    resp = client.get("/api/v1/memories/extraction/stats", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    for key in (
        "low_value_total", "empty_total", "duplicate_rejected_total",
        "conflict_total", "pending_manual", "pending_pii_dual",
        "blocked_total", "manually_approved", "user_rejected", "auto_saved",
    ):
        assert key in data, key
    assert data["low_value_total"] >= 1
    assert data["auto_saved"] == 0


# ---------------------------------------------- P1-4 partial v6 migration
@pytest.mark.parametrize(
    "scenario",
    ["leftover-objects", "broken-table", "leftover-columns", "leftover-index"],
)
def test_partial_v6_migration_recovers(tmp_path, settings, scenario):
    mgr = _new_manager(tmp_path / scenario, settings)
    with mgr.session() as session:
        session.execute(text("DELETE FROM schema_migrations WHERE version=6"))
        if scenario == "broken-table":
            # A partially created table with the wrong shape.
            session.execute(text("DROP TABLE IF EXISTS block_events"))
            session.execute(
                text("CREATE TABLE block_events (block_event_id TEXT PRIMARY KEY)")
            )
        elif scenario == "leftover-index":
            # Indexes remain while the version marker is gone.
            pass
        elif scenario == "leftover-columns":
            pass
    runner = MigrationRunner(mgr)
    assert runner.apply_pending() >= 1
    assert 6 in [
        int(row[0])
        for row in _rows(mgr, "SELECT version FROM schema_migrations")
    ]
    columns = {
        row[1] for row in _rows(mgr, "PRAGMA table_info(block_events)")
    }
    assert "hit_length" in columns and "category" in columns
    indexes = {
        row[0] for row in _rows(mgr, "SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "uq_block_events_turn" in indexes
    assert "uq_memory_records_candidate" in indexes
    mgr.dispose()


def test_rollback_then_reapply_preserves_history(tmp_path, settings):
    mgr = _new_manager(tmp_path / "rollback", settings)
    from jarvismind.core.timeutils import utc_now_iso

    now = utc_now_iso()
    with mgr.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_records("
                "id, memory_type, content, status, created_at, updated_at, vector_status"
                ") VALUES ('hist-1','SEMANTIC','历史正式记忆','ACTIVE',:n,:n,'PENDING')"
            ),
            {"n": now},
        )
        session.execute(
            text(
                "INSERT INTO memory_candidates("
                "id, conversation_id, memory_type, content, status, "
                "extraction_engine, extracted_at, scan_status, decision_version, "
                "message_id, turn_id"
                ") VALUES ('hist-c','c','SEMANTIC','历史候选','PENDING',"
                "'local',:n,'LEGACY_UNSCANNED',1,'','')"
            ),
            {"n": now},
        )
    runner = MigrationRunner(mgr)
    runner.rollback_v6()
    assert 6 not in [
        int(row[0]) for row in _rows(mgr, "SELECT version FROM schema_migrations")
    ]
    assert runner.apply_pending() >= 1
    assert _scalar(mgr, "SELECT COUNT(*) FROM memory_records") == 1
    assert _scalar(mgr, "SELECT COUNT(*) FROM memory_candidates") == 1
    assert _scalar(
        mgr, "SELECT source_candidate_id FROM memory_records WHERE id='hist-1'"
    ) is None
    assert _scalar(
        mgr, "SELECT scan_status FROM memory_candidates WHERE id='hist-c'"
    ) == "LEGACY_UNSCANNED"
    mgr.dispose()


# ------------------------------------------------------------------ P2 extras
def test_live_api_token_is_blocked_not_just_redacted(manager, settings):
    engine = _engine(manager, settings)
    live_token = settings.api_token
    assert len(live_token) >= 4
    resp = _extract(
        engine, f"请把这个值记牢：{live_token}", conversation_id="c-live"
    )
    assert resp.outcome_counts.blocked >= 1
    assert resp.outcome_counts.extracted == 0
    assert any(event.category == "api_token" for event in resp.block_events)
    assert not _db_has(manager, live_token)


def test_import_row_scans_nested_metadata(manager, settings):
    service = MigrationService(manager)
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "imp-1",
                "memory_type": "SEMANTIC",
                "content": "普通导入内容",
                "status": "ACTIVE",
                "metadata": {"level": {"deep": [FAKE_API_KEY]}},
            }
        ],
        "sources": [],
    }
    result = service.import_document(document)
    assert result["skipped_credentials"] >= 1
    assert result["imported_memories"] == 0
    assert not _db_has(manager, FAKE_API_KEY)


def test_logs_and_errors_never_carry_values(manager, settings, caplog):
    caplog.set_level("DEBUG")
    service = MemoryService(manager)
    with pytest.raises(CredentialBlockedError):
        service.create(_construct_create({"x": FAKE_API_KEY}))
    assert FAKE_API_KEY not in caplog.text
    with pytest.raises(CredentialBlockedError) as exc:
        assert_safe_text(FAKE_API_KEY, field="message_id")
    assert FAKE_API_KEY not in str(exc.value.detail)
    assert FAKE_API_KEY not in str(exc.value)
