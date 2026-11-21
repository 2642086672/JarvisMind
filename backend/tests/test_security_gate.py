"""003-B: JME server-side memory security gate.

All credential-looking values below are SYNTHETIC test fixtures. No real
token, API key, password or private key is read, used, or logged here.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jarvismind.core import sensitive
from jarvismind.core.errors import DatabaseError, ValidationError
from jarvismind.core.security_gate import (
    RULESET_VERSION,
    assert_writable,
    credential_categories,
)
from jarvismind.database import migrations as migrations_module
from jarvismind.database.connection import DatabaseManager
from jarvismind.database.migrations import Migration, MigrationRunner
from jarvismind.database.models import MemoryCandidate, MemoryRecord
from jarvismind.repositories.security_event_repository import SecurityEventRepository
from jarvismind.schemas.extraction import ExtractMode, ExtractRequest
from jarvismind.services.approval_service import ApprovalService
from jarvismind.services.extraction import ExtractionEngine
from jarvismind.services.memory_service import MemoryService

# Synthetic, obviously-fake fixtures.
FAKE_API_KEY = "sk-FAKE-abcdefghijklmnopqrstuvwxyz0123456789"
FAKE_TOKEN = "ghp_" "FAKEabcdefghijklmnopqrstuvwxyz0123456789"
FAKE_PASSWORD = "password=FAKE-PlainText-123456"
FAKE_PRIVATE_KEY = (
    "-----BEGIN " "PRIVATE KEY-----\nFAKEBASE64BLOCK0123456789\n"
    "-----END " "PRIVATE KEY-----"
)
FAKE_SSH_KEY = (
    "-----BEGIN OPENSSH " "PRIVATE KEY-----\nFAKESSHBLOCK0123456789\n"
    "-----END OPENSSH " "PRIVATE KEY-----"
)
FAKE_DB_URI = "postgresql://fakeuser:FAKEPASS@db.invalid/jme"
FAKE_OTP = "验证码是 482913"


def _engine(manager, settings, provider=None):
    return ExtractionEngine(manager, settings, provider=provider)


def _extract(engine, conversation, conversation_id="conv-1", **kwargs):
    return engine.extract(
        ExtractRequest(
            conversation=conversation,
            conversation_id=conversation_id,
            mode=ExtractMode.LOCAL,
            **kwargs,
        )
    )


def _count(manager, table: str) -> int:
    with manager.session() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def _pending(engine, text_value="今天要把周报做完，顺便处理一下升级工单。", cid="c-pending"):
    resp = _extract(engine, text_value, conversation_id=cid)
    return resp.candidates[0].id


# --------------------------------------------------------------- 1-4 BLOCK rules
@pytest.mark.parametrize(
    "fixture,label",
    [
        (FAKE_API_KEY, "api_key"),
        (FAKE_TOKEN, "token"),
        (FAKE_PASSWORD, "password"),
        (FAKE_PRIVATE_KEY, "private_key"),
        (FAKE_SSH_KEY, "ssh_key"),
        (FAKE_DB_URI, "db_uri"),
        (FAKE_OTP, "verification_code"),
    ],
)
def test_credential_fixture_is_blocked_before_persistence(manager, settings, fixture, label):
    engine = _engine(manager, settings)
    resp = _extract(engine, f"请记住这条内容：{fixture}", conversation_id=f"block-{label}")

    assert resp.outcome_counts.blocked >= 1
    assert resp.counts["APPROVED"] == 0
    assert engine.memory_service.list(page_size=100)[1] == 0
    # No candidate carries the credential; no formal memory either.
    with manager.session() as session:
        rows = session.execute(text("SELECT content FROM memory_candidates")).scalars().all()
        assert all(fixture not in (row or "") for row in rows)
        records = session.execute(text("SELECT content FROM memory_records")).scalars().all()
        assert all(fixture not in (row or "") for row in records)
    assert _count(manager, "block_events") >= 1


def test_block_event_contains_no_value_or_digest(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, f"请记住：{FAKE_API_KEY}", conversation_id="c-block-meta")
    with manager.session() as session:
        rows = session.execute(text("SELECT * FROM block_events")).mappings().all()
    assert rows
    serialized = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    assert FAKE_API_KEY not in serialized
    # No content-derived hash column exists at all.
    assert all("content_hash" not in row for row in rows)
    assert all(row["category"] in sensitive.CREDENTIAL_CATEGORIES for row in rows)


def test_block_never_written_to_extraction_events_or_candidates(manager, settings):
    engine = _engine(manager, settings)
    _extract(engine, f"请记住：{FAKE_TOKEN}", conversation_id="c-block-split")
    with manager.session() as session:
        event_types = session.execute(
            text("SELECT event_type FROM extraction_events")
        ).scalars().all()
    assert "BLOCK" not in event_types
    assert _count(manager, "block_events") >= 1


def test_block_cannot_be_bypassed_by_any_authorization(client, auth_headers, manager, settings):
    """Credentials cannot be saved via extract, grants, actor, or direct API."""
    extracted = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": f"请记住：{FAKE_API_KEY}",
            "conversation_id": "c-bypass",
            "message_id": "m-bypass",
        },
        headers=auth_headers,
    )
    assert extracted.status_code == 201
    assert extracted.json()["counts"]["APPROVED"] == 0
    assert FAKE_API_KEY not in extracted.text

    # Direct create with a credential is rejected by the unified write gate.
    direct = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": FAKE_API_KEY},
        headers=auth_headers,
    )
    assert direct.status_code == 422
    assert direct.json()["error"]["details"]["code"] == "credential_blocked"
    assert FAKE_API_KEY not in direct.text

    # PATCH with a credential also cannot bypass the gate.
    created = client.post(
        "/api/v1/memories",
        json={"memory_type": "SEMANTIC", "content": "普通记忆内容"},
        headers=auth_headers,
    )
    memory_id = created.json()["data"]["id"]
    patched = client.patch(
        f"/api/v1/memories/{memory_id}",
        json={"content": FAKE_API_KEY},
        headers=auth_headers,
    )
    assert patched.status_code == 422
    assert FAKE_API_KEY not in patched.text

    # A credential-bearing row that somehow already exists in the database
    # (raw SQL, outside the ORM write gate) must STILL be refused by the G2
    # re-scan inside the approval path. This simulates historical dirty data
    # without letting the ORM gate block the fixture itself.
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO memory_candidates("
                "id, conversation_id, memory_type, content, status, extraction_engine, "
                "extracted_at, scan_status, decision_version, ruleset_version, "
                "required_grants_json, message_id, turn_id, content_fingerprint"
                ") VALUES ('cred-candidate','c-bypass','SEMANTIC',:content,'PENDING',"
                "'local','2026-01-01T00:00:00Z','SCANNED',1,:ruleset,'[]','','','stale')"
            ),
            {"content": FAKE_PASSWORD, "ruleset": RULESET_VERSION},
        )
    with pytest.raises(Exception):
        ApprovalService(manager).review(
            "cred-candidate", decision="approve", actor_label="admin"
        )
    assert _count(manager, "memory_records") == 1  # only the benign PATCH target


def test_logs_and_errors_never_contain_credentials(manager, settings, client, auth_headers, caplog):
    caplog.set_level("DEBUG")
    resp = client.post(
        "/api/v1/memories/extract",
        json={"conversation": f"请记住：{FAKE_API_KEY}", "conversation_id": "c-log"},
        headers=auth_headers,
    )
    assert FAKE_API_KEY not in resp.text
    assert FAKE_API_KEY not in caplog.text
    with pytest.raises(ValidationError):
        assert_writable(FAKE_API_KEY)


# ------------------------------------------------------------ 5-8 approval state
def test_normal_candidate_defaults_to_pending(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "JarvisMind 项目计划明年支持 NAS 部署。")
    assert resp.candidates[0].status.value == "PENDING"
    assert resp.candidates[0].required_grants == ["USER_APPROVAL"]
    assert resp.counts["APPROVED"] == 0
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_pii_candidate_requires_dual_authorization(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "我的邮箱是 someone@example.com，请保持联系。")
    pii = resp.candidates[0]
    assert pii.status.value == "PENDING_AUTHORIZATION"
    assert pii.required_grants == ["USER_APPROVAL", "SENSITIVE_PII"]
    assert resp.outcome_counts.pending_pii_dual == 1
    assert resp.counts["REJECTED"] == 0
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_normal_approval_cannot_replace_pii_authorization(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "我的邮箱是 someone@example.com，请保持联系。")
    cid = resp.candidates[0].id
    with pytest.raises(ValidationError) as exc:
        ApprovalService(manager).review(cid, decision="approve", actor_label="user")
    assert exc.value.detail["code"] == "pii_dual_authorization_required"
    assert engine.memory_service.list(page_size=100)[1] == 0
    with manager.session() as session:
        candidate = session.get(MemoryCandidate, cid)
        assert candidate.status == "PENDING_AUTHORIZATION"


def test_reject_pii_declined_is_allowed_and_deduplicated(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(engine, "我的邮箱是 someone@example.com，请保持联系。")
    cid = resp.candidates[0].id
    rejected = ApprovalService(manager).review(cid, decision="reject", reason="不保存")
    assert rejected.status.value == "REJECTED"
    assert engine.memory_service.list(page_size=100)[1] == 0


def test_legacy_unscanned_candidate_cannot_be_approved(manager, settings):
    with manager.session() as session:
        session.add(
            MemoryCandidate(
                id="legacy-candidate",
                conversation_id="c-legacy",
                memory_type="SEMANTIC",
                content="历史候选内容，长期事实",
                status="PENDING",
                extraction_engine="local",
                extracted_at="2026-01-01T00:00:00Z",
                scan_status="LEGACY_UNSCANNED",
                decision_version=1,
                ruleset_version=None,
                required_grants_json=None,
                message_id="",
                turn_id="",
            )
        )
    with pytest.raises(ValidationError) as exc:
        ApprovalService(manager).review("legacy-candidate", decision="approve")
    assert exc.value.detail["code"] == "legacy_rescan_required"
    assert engine_records_for_candidate(manager, "legacy-candidate") == 0


def engine_records_for_candidate(manager, candidate_id: str) -> int:
    with manager.session() as session:
        return int(
            session.execute(
                text("SELECT COUNT(*) FROM memory_records WHERE source_candidate_id=:cid"),
                {"cid": candidate_id},
            ).scalar()
            or 0
        )


def test_auto_save_is_disabled_by_default(settings):
    assert settings.auto_save_enabled is False


# --------------------------------------------------- 9-12 idempotency & atomicity
def test_repeated_approval_is_rejected(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    engine.review(cid, decision="approve")
    with pytest.raises(Exception):
        engine.review(cid, decision="approve")
    assert engine.memory_service.list(page_size=100)[1] == 1
    assert engine_records_for_candidate(manager, cid) == 1


def test_concurrent_approval_creates_only_one_memory(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        try:
            barrier.wait(timeout=5)
            ApprovalService(manager).review(cid, decision="approve")
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001 - record outcome only
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert outcomes.count("ok") == 1
    assert engine.memory_service.list(page_size=100)[1] == 1
    assert engine_records_for_candidate(manager, cid) == 1


def test_database_unique_constraint_blocks_duplicate_memory(manager, settings):
    """The final protection is a DB index, not a Python if-check."""
    engine = _engine(manager, settings)
    cid = _pending(engine)
    engine.review(cid, decision="approve")
    with manager.session() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO memory_records("
                    "id, memory_type, content, status, created_at, updated_at, "
                    "vector_status, source_candidate_id"
                    ") VALUES ('dup-id','SEMANTIC','duplicate','ACTIVE',"
                    "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','PENDING',:cid)"
                ),
                {"cid": cid},
            )
            session.flush()


def test_approval_event_is_idempotent(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    service = ApprovalService(manager)
    service.review(cid, decision="approve")
    with manager.session() as session:
        rows = session.execute(
            text("SELECT transaction_result, operation_result FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).all()
    assert rows == [("COMMITTED", "SUCCEEDED")]


def test_commit_success_marks_committed(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    ApprovalService(manager).review(cid, decision="approve")
    with manager.session() as session:
        row = session.execute(
            text("SELECT transaction_result, operation_result, memory_id FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).first()
    assert row[0] == "COMMITTED"
    assert row[1] == "SUCCEEDED"
    assert row[2] is not None


# ------------------------------------------------------ 13-16 UNKNOWN & recovery
def test_explicit_rollback_is_converged_to_rolled_back(manager, settings, monkeypatch):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    from jarvismind.repositories.memory_repository import MemoryRepository

    def fail_create(self, record):  # noqa: ANN001
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(MemoryRepository, "create", fail_create)
    with pytest.raises(DatabaseError):
        ApprovalService(manager).review(cid, decision="approve")
    monkeypatch.undo()

    # Not assumed from the exception: the intent stays UNKNOWN.
    with manager.session() as session:
        row = session.execute(
            text("SELECT transaction_result, operation_result FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).first()
    assert row == ("UNKNOWN", "INDETERMINATE")
    assert engine_records_for_candidate(manager, cid) == 0

    result = ApprovalService(manager).recover_unknown()
    assert result["rolled_back"] == 1
    with manager.session() as session:
        row = session.execute(
            text("SELECT transaction_result, operation_result FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).first()
    assert row == ("ROLLED_BACK", "FAILED")


def test_commit_unknown_after_lost_confirmation_is_recovered(manager, settings, monkeypatch):
    """Simulates a process crash / lost response after a successful commit."""
    engine = _engine(manager, settings)
    cid = _pending(engine)

    def lost_confirmation(self, event_id, memory_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(ApprovalService, "_confirm", lost_confirmation)
    ApprovalService(manager).review(cid, decision="approve")
    monkeypatch.undo()

    assert engine_records_for_candidate(manager, cid) == 1
    with manager.session() as session:
        row = session.execute(
            text("SELECT transaction_result, operation_result FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).first()
    assert row == ("UNKNOWN", "INDETERMINATE")

    result = ApprovalService(manager).recover_unknown()
    assert result["committed"] == 1
    assert engine_records_for_candidate(manager, cid) == 1  # recovery never duplicates
    with manager.session() as session:
        row = session.execute(
            text("SELECT transaction_result, operation_result FROM approval_events WHERE candidate_id=:cid"),
            {"cid": cid},
        ).first()
    assert row == ("COMMITTED", "SUCCEEDED")


def test_process_crash_recovery_from_persisted_intent(manager, settings):
    """A persisted UNKNOWN intent + a committed memory converges to COMMITTED."""
    engine = _engine(manager, settings)
    cid = _pending(engine)
    with manager.session() as session:
        SecurityEventRepository(session).prepare_approval_event(
            candidate_id=cid,
            decision="approve",
            decision_version=1,
            content_fingerprint="",
            ruleset_version=RULESET_VERSION,
            idempotency_key=f"{cid}|1||{RULESET_VERSION}",
            actor_label="",
        )
    # Simulate the main transaction having committed before the crash. Use a
    # REAL verified snapshot (with the HMAC fingerprint) rather than a made-up
    # dict - 003-B1 binds the write to the current candidate content.
    service = ApprovalService(manager)
    snapshot = service._load_and_verify(cid, "approve")
    service._commit_atomic(
        cid, "approve", None, snapshot, "irrelevant-event-id"
    )
    result = ApprovalService(manager).recover_unknown()
    assert result["committed"] == 1
    assert engine_records_for_candidate(manager, cid) == 1


def test_unknown_recovery_never_creates_a_memory(manager, settings):
    engine = _engine(manager, settings)
    cid = _pending(engine)
    with manager.session() as session:
        SecurityEventRepository(session).prepare_approval_event(
            candidate_id=cid,
            decision="approve",
            decision_version=1,
            content_fingerprint="",
            ruleset_version=RULESET_VERSION,
            idempotency_key=f"{cid}|unknown|{RULESET_VERSION}",
            actor_label="",
        )
    before = _count(manager, "memory_records")
    ApprovalService(manager).recover_unknown()
    assert _count(manager, "memory_records") == before


# --------------------------------------------------- 17-20 migration & history
def test_v6_migration_success(manager):
    with manager.session() as session:
        versions = session.execute(
            text("SELECT version FROM schema_migrations ORDER BY version")
        ).scalars().all()
        assert 6 in versions
        tables = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }
        for name in (
            "block_events", "extraction_events", "approval_events", "grants",
            "device_registry", "pairing_codes", "memory_rejections",
            "secret_key_versions",
        ):
            assert name in tables
        indexes = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            ).all()
        }
        assert "uq_memory_records_candidate" in indexes
        assert "uq_approval_success" in indexes
        assert "uq_block_events_turn" in indexes
        columns = {row[1] for row in session.execute(text("PRAGMA table_info(memory_records)")).all()}
        assert "source_candidate_id" in columns


def test_migration_failure_rolls_back_to_v5_and_can_retry(manager, monkeypatch, tmp_path):
    """A failing v6 leaves the v5 schema intact and is safely retryable."""
    runner = MigrationRunner(manager)
    runner.rollback_v6()
    assert 6 not in _versions(manager)

    broken = migrations_module.MIGRATIONS[:-1] + (
        Migration(version=6, name="failing", statements=("THIS IS NOT SQL",)),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", broken)
    with pytest.raises(Exception):
        MigrationRunner(manager).apply_pending()
    monkeypatch.undo()

    assert 6 not in _versions(manager)
    assert _count(manager, "memory_records") == 0
    assert MigrationRunner(manager).apply_pending() >= 1
    assert 6 in _versions(manager)


def _versions(manager) -> list[int]:
    with manager.session() as session:
        return [
            int(row)
            for row in session.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            ).scalars().all()
        ]


def test_historical_memory_is_preserved_and_not_backfilled(manager, settings):
    with manager.session() as session:
        session.add(
            MemoryRecord(
                id="historical-memory",
                memory_type="SEMANTIC",
                content="历史正式记忆内容",
                status="ACTIVE",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                vector_status="PENDING",
                source_candidate_id=None,
            )
        )
    with manager.session() as session:
        row = session.get(MemoryRecord, "historical-memory")
        assert row is not None
        assert row.source_candidate_id is None
        assert row.content == "历史正式记忆内容"


def test_delete_candidate_sets_grant_candidate_id_to_null(manager, settings):
    """grants.candidate_id is nullable so ON DELETE SET NULL is valid."""
    engine = _engine(manager, settings)
    cid = _pending(engine)
    with manager.session() as session:
        session.execute(
            text(
                "INSERT INTO grants(grant_id, grant_type, candidate_id, nonce, "
                "issued_at, expires_at) VALUES ('g-test','USER_APPROVAL',:cid,"
                "'nonce-test','2026-01-01T00:00:00Z','2026-12-31T00:00:00Z')"
            ),
            {"cid": cid},
        )
        session.execute(text("DELETE FROM memory_candidates WHERE id=:cid"), {"cid": cid})
    with manager.session() as session:
        value = session.execute(
            text("SELECT candidate_id FROM grants WHERE grant_id='g-test'")
        ).scalar()
    assert value is None


# --------------------------------------------------------- 21-24 stats/import/API
def test_statistics_are_mutually_exclusive(manager, settings):
    engine = _engine(manager, settings)
    resp = _extract(
        engine,
        "JarvisMind 项目计划明年支持 NAS 部署。\n"
        f"请记住：{FAKE_API_KEY}\n"
        "好的，收到。",
        conversation_id="c-stats",
    )
    oc = resp.outcome_counts
    assert oc.processed_units == (
        oc.blocked + oc.duplicate_rejected + oc.ignored + oc.extracted
    )
    assert oc.extracted == (
        oc.pending_manual + oc.pending_pii_dual + oc.conflict_pending
    )
    assert oc.blocked >= 1
    assert oc.auto_saved == 0
    assert oc.manually_approved == 0
    assert oc.user_rejected == 0
    assert resp.counts["APPROVED"] == 0
    assert resp.counts["REJECTED"] == 0
    assert resp.counts["PENDING"] == oc.extracted


def test_duplicate_not_counted_as_created_candidate(manager, settings):
    engine = _engine(manager, settings)
    statement = "我从事物流快运行业，负责省区客服质管和理赔工作。"
    _extract(engine, statement, conversation_id="c-dup-1")
    second = _extract(engine, statement, conversation_id="c-dup-2")
    assert second.outcome_counts.duplicate_rejected == 1
    assert second.outcome_counts.extracted == 0
    assert second.candidates == []
    assert _count(manager, "memory_candidates") == 1


def test_direct_import_skips_credentials(client, auth_headers, manager):
    document = {
        "format": "jme-export",
        "schema_version": 5,
        "memories": [
            {
                "id": "import-safe",
                "memory_type": "SEMANTIC",
                "content": "普通导入记忆",
                "status": "ACTIVE",
            },
            {
                "id": "import-cred",
                "memory_type": "SEMANTIC",
                "content": f"保存这个密钥 {FAKE_API_KEY}",
                "status": "ACTIVE",
            },
        ],
    }
    resp = client.post(
        "/api/v1/memories/import",
        json={"document": document},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body.get("skipped_credentials", 0) >= 1
    with manager.session() as session:
        rows = session.execute(text("SELECT content FROM memory_records")).scalars().all()
    assert all(FAKE_API_KEY not in (row or "") for row in rows)


def test_old_api_cannot_bypass_gate(client, auth_headers, manager):
    resp = client.post(
        "/api/v1/memories/candidates/whatever/approve",
        json={"actor": "admin", "grant_type": "SENSITIVE_PII", "expires_at": "2099-01-01"},
        headers=auth_headers,
    )
    # Unknown fields are rejected by the strict review schema.
    assert resp.status_code == 422
    assert _count(manager, "memory_records") == 0


def test_block_event_idempotency_for_same_turn(manager, settings):
    engine = _engine(manager, settings)
    first = _extract(
        engine,
        f"请记住：{FAKE_API_KEY}",
        conversation_id="c-idem",
        message_id="m-idem",
        turn_id="t-idem",
    )
    second = _extract(
        engine,
        f"请记住：{FAKE_API_KEY}",
        conversation_id="c-idem",
        message_id="m-idem",
        turn_id="t-idem",
    )
    assert first.block_events[0].block_event_id == second.block_events[0].block_event_id
    assert _count(manager, "block_events") == len(first.block_events)


def test_extraction_stats_endpoint_separates_lifecycle_counts(client, auth_headers):
    extracted = client.post(
        "/api/v1/memories/extract",
        json={
            "conversation": "今天要把周报做完，顺便处理一下升级工单。",
            "conversation_id": "c-lifecycle",
        },
        headers=auth_headers,
    )
    cid = extracted.json()["candidates"][0]["id"]
    client.post(
        f"/api/v1/memories/candidates/{cid}/approve", json={}, headers=auth_headers
    )
    stats = client.get("/api/v1/memories/extraction/stats", headers=auth_headers)
    assert stats.status_code == 200
    assert stats.json()["data"]["final_approved"] >= 1
    # The single /extract response itself never reports a final approval.
    assert extracted.json()["outcome_counts"]["manually_approved"] == 0
