"""P1-01 unified governance acceptance tests.

Every case runs against an isolated tmp database built by the existing
``manager`` fixture (v1..v6 schema). Governance is activated per case and
restored in teardown, so the 27 startup-safety cases and the rest of the JME
regression suite are unaffected.

Coverage of the task book's 12 acceptance points:

    1  unauthorized original write refused
    2  every original write entry point goes through unified governance
    3  a forged client actor gains no privilege
    4  failed signed-grant verification refuses the operation
    5  audit records are complete (9 fields, including denials)
    6  budget exhaustion blocks background calls
    7  task pause / resume / cancel states are correct
    8  repeated triggers are idempotent
    9  derived modules cannot modify original memory
    10 original memory is never auto-overwritten by reflection / graph / proactive
    11 existing JME regression is exercised by the full suite (not this file)
    12 RK-01 stays an independent, explicitly recorded state (report + command)
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from jarvismind.database.models import MemoryCandidate, MemoryRecord
from jarvismind.governance import build_governance, deactivate_governance
from jarvismind.governance.audit_bus import STATUS_DENIED
from jarvismind.governance.config import GovernanceConfig
from jarvismind.governance.cost_governor import CostGovernor
from jarvismind.governance.errors import (
    CostBlockedError,
    GrantVerificationError,
    UnauthorizedWriteError,
    UntrustedIdentityError,
)
from jarvismind.governance.identity import (
    CAP_MEMORY_READ,
    CAP_MEMORY_WRITE,
    CAP_TASK_MANAGE,
    TrustedPrincipal,
)
from jarvismind.governance.schema import (
    P1_SCHEMA_VERSION,
    inspect as inspect_p1_schema,
    install_p1_schema,
    validate_statements,
)
from jarvismind.governance.task_registry import CANCELLED, CREATED, FAILED, PAUSED, RUNNING, SUCCEEDED
from jarvismind.governance.write_governor import OP_APPROVE, OP_CREATE, OP_DELETE, OP_UPDATE
from jarvismind.p1_gate import is_active
from jarvismind.schemas.memory import MemoryCreate, MemoryUpdate
from jarvismind.services.memory_service import MemoryService

TEST_SUBJECT = "svc:p1-governance-test"


def _cfg(**overrides) -> GovernanceConfig:
    base = dict(
        grant_ttl_seconds=300,
        grant_key_version=1,
        task_budget_cny=100.0,
        task_token_limit=100000,
        task_call_limit=50,
        batch_call_limit=50,
        rate_limit_per_minute=100,
        breaker_threshold=3,
    )
    base.update(overrides)
    return GovernanceConfig(**base)


@pytest.fixture()
def governed(manager, settings):
    gov = build_governance(manager, settings, config=_cfg(), enforce=True)
    principal = TrustedPrincipal.issue(
        settings,
        subject_id=TEST_SUBJECT,
        principal_type="SERVICE",
        grants=(CAP_MEMORY_READ, CAP_MEMORY_WRITE, CAP_TASK_MANAGE),
    )
    try:
        yield gov, principal
    finally:
        # Exact restoration: the ORM gate is process-global.
        deactivate_governance()


def _grant(gov, principal, operation=OP_CREATE, object_ref="NEW", **kwargs):
    return gov.grant_authority.issue(
        principal=principal,
        operation=operation,
        object_ref=object_ref,
        grants=(CAP_MEMORY_WRITE,),
        **kwargs,
    )


def _payload(content="governed memory", **extra):
    return MemoryCreate(memory_type="SEMANTIC", content=content, **extra)


def _memory_count(manager) -> int:
    with manager.session() as session:
        return int(
            session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar() or 0
        )


# ---------------------------------------------------------------- 1 / 2 / 5
def test_unauthorized_original_write_is_refused(governed, manager, settings):
    """P1 application integration changed WHERE an unauthorised write can occur.

    MemoryService is now wrapped by authorized_operation(): a service call is
    AUTO-authorised by the SERVER (its own principal + one-shot signed grant),
    never by a client claim. The write that must be refused is one that BYPASSES
    the service and goes straight to the connection - that is the P0-2 boundary.
    Assertions are stronger than before, not weaker: the auto-authorised path is
    additionally checked for a server principal, a signed grant and an audit row.
    """
    gov, principal = governed
    before = _memory_count(manager)
    with pytest.raises(UnauthorizedWriteError) as caught:
        with manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO memory_records(id, memory_type, content, importance, "
                    "confidence, status, created_at, updated_at) VALUES "
                    "('p1-bypass-1','SEMANTIC','bypass attempt',5.0,0.5,'ACTIVE',"
                    "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                )
            )
    assert caught.value.code == "unauthorized_memory_write"
    assert _memory_count(manager) == before
    # The denial itself is audited, not just the exception.
    denials = [e for e in gov.audit_bus.p1_entries() if e["detail"].get("status") == STATUS_DENIED]
    assert any(
        e["detail"]["failure_reason"] == "write_without_authorized_context" for e in denials
    )
    # A service-level write is authorised by the SERVER, not by a client actor.
    record = MemoryService(manager).create(_payload("server-authorised write"))
    assert record.id
    assert _memory_count(manager) == before + 1
    succeeded = [
        e for e in gov.audit_bus.p1_entries()
        if e["detail"].get("status") == "SUCCEEDED"
        and e["detail"].get("operation") == "MEMORY_CREATE"
    ]
    assert any(e["actor"] == "svc:jme-governed-write" for e in succeeded)
    assert gov.grant_authority.row_count() >= 1


def test_all_original_write_entry_points_are_covered_by_orm_gate(governed, manager):
    gov, principal = governed
    assert gov.write_governor.assert_orm_gate_active() == {
        "active": True,
        "enforce": True,
        "governed_tables": ("memory_records", "memory_candidates"),
    }
    before = _memory_count(manager)

    # (a) service-level creation is wrapped by authorized_operation(): the
    # SERVER mints its own principal + one-shot signed grant, so it succeeds.
    # Its audit actor must be the server subject, never a client label.
    service_record = MemoryService(manager).create(
        _payload("service path under governance")
    )
    assert service_record.id
    assert _memory_count(manager) == before + 1
    service_audit = [
        e for e in gov.audit_bus.p1_entries()
        if e["detail"].get("status") == "SUCCEEDED"
        and e["detail"].get("operation") == "MEMORY_CREATE"
    ]
    assert any(e["actor"] == "svc:jme-governed-write" for e in service_audit)

    # (b) raw SQL that bypasses the service is refused by the connection gate.
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO memory_records(id, memory_type, content, importance, "
                    "confidence, status, created_at, updated_at) VALUES "
                    "('p1-bypass-2','SEMANTIC','raw bypass',5.0,0.5,'ACTIVE',"
                    "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                )
            )

    # (c) raw ORM insert of a formal memory (approval / import style).
    # pytest.raises must wrap the session context, otherwise the session's
    # exit tries to commit a rolled-back transaction and raises a second,
    # unrelated PendingRollbackError.
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.add(
                MemoryRecord(
                    id="p1-raw-orm-1",
                    memory_type="SEMANTIC",
                    content="raw orm bypass",
                    importance=5.0,
                    confidence=0.5,
                    status="ACTIVE",
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            )
            session.flush()

    # (d) raw ORM insert of a candidate (approval-submission style)
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.add(
                MemoryCandidate(
                    id="p1-raw-candidate-1",
                    memory_type="SEMANTIC",
                    content="raw candidate bypass",
                    importance=5.0,
                    confidence=0.5,
                    status="PENDING",
                    extraction_engine="local",
                    extracted_at="2026-01-01T00:00:00+00:00",
                )
            )
            session.flush()

    # Only the server-authorised service write may add a row.
    assert _memory_count(manager) == before + 1


def test_approval_submission_also_goes_through_the_door(governed, manager, settings):
    gov, principal = governed
    forged = replace(principal, subject_id="client-supplied-root", mac="00" * 32)
    grant = _grant(gov, principal, operation=OP_APPROVE, object_ref="candidate-x")
    with pytest.raises(UntrustedIdentityError):
        gov.gateway.approve_candidate(
            "candidate-x", decision="approve", principal=forged, grant=grant
        )


def test_governed_write_succeeds_with_verified_identity_and_grant(governed, manager):
    gov, principal = governed
    before = _memory_count(manager)
    grant = _grant(gov, principal, task_id="T-CREATE", run_id="R-CREATE")
    record = gov.gateway.create_memory(
        _payload("authorised memory"),
        principal=principal,
        grant=grant,
        task_id="T-CREATE",
        run_id="R-CREATE",
    )
    assert record.id
    assert _memory_count(manager) == before + 1
    with manager.session() as session:
        actor = session.execute(
            text(
                "SELECT actor FROM memory_audit_logs "
                "WHERE action='P1:MEMORY_CREATE:SUCCEEDED' ORDER BY created_at DESC LIMIT 1"
            )
        ).scalar()
    assert actor == TEST_SUBJECT


# ------------------------------------------------------------------ 3
def test_client_forged_actor_cannot_gain_privilege(governed, manager, settings):
    gov, principal = governed
    forged = replace(principal, subject_id="client-claimed-admin", mac="ab" * 32)
    grant = _grant(gov, principal, task_id="T-FORGE")
    with pytest.raises(UntrustedIdentityError):
        gov.gateway.create_memory(
            _payload("forged identity"), principal=forged, grant=grant
        )

    # Client-controlled labels in the payload must never become the identity.
    real_grant = _grant(gov, principal, task_id="T-LABELS")
    record = gov.gateway.create_memory(
        _payload(
            "labels are data, not identity",
            metadata={
                "actor": "root",
                "actor_label": "administrator",
                "SERVER_ACTOR": "server:root",
                "required_grants_json": '["SENSITIVE_PII"]',
            },
        ),
        principal=principal,
        grant=real_grant,
        task_id="T-LABELS",
    )
    with manager.session() as session:
        actor = session.execute(
            text(
                "SELECT actor FROM memory_audit_logs WHERE memory_id=:m "
                "AND action='P1:MEMORY_CREATE:SUCCEEDED'"
            ),
            {"m": record.id},
        ).scalar()
    assert actor == TEST_SUBJECT
    assert actor != "root"


def test_principal_mac_cannot_be_forged_by_copying_fields(governed, settings):
    gov, principal = governed
    copy = TrustedPrincipal(
        subject_id="attacker",
        principal_type="SERVICE",
        grants=(CAP_MEMORY_WRITE,),
        issued_at=principal.issued_at,
        key_version=principal.key_version,
        mac=principal.mac,  # reuse a valid MAC for a DIFFERENT subject
    )
    assert copy.verify(settings) is False


# ------------------------------------------------------------------ 4
def test_invalid_grant_signature_is_refused(governed, manager):
    gov, principal = governed
    grant = _grant(gov, principal, task_id="T-BADSIG")
    before = _memory_count(manager)
    with pytest.raises(GrantVerificationError) as caught:
        gov.gateway.create_memory(
            _payload("bad signature"),
            principal=principal,
            grant=replace(grant, signature="00" * 32),
        )
    assert caught.value.detail["reason"] == "signature_mismatch"
    assert _memory_count(manager) == before


def test_grant_replay_expiry_and_operation_binding_are_refused(governed, manager):
    gov, principal = governed
    grant = _grant(gov, principal, task_id="T-REPLAY")
    gov.gateway.create_memory(_payload("one shot"), principal=principal, grant=grant)
    assert gov.grant_authority.verify(grant).reason == "grant_status_CONSUMED"
    with pytest.raises(GrantVerificationError):
        gov.gateway.create_memory(_payload("replay"), principal=principal, grant=grant)

    expired = _grant(gov, principal, ttl_seconds=1, task_id="T-EXPIRE")
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with pytest.raises(GrantVerificationError):
        gov.gateway.create_memory(
            _payload("expired"),
            principal=principal,
            grant=replace(expired, issued_at=past, expires_at=past),
        )

    wrong_op = _grant(gov, principal, operation=OP_DELETE, object_ref="memory-x")
    with pytest.raises(GrantVerificationError):
        gov.gateway.create_memory(
            _payload("wrong operation"), principal=principal, grant=wrong_op
        )
    assert any(
        "operation_mismatch" in str(e["detail"].get("failure_reason", ""))
        for e in gov.audit_bus.p1_entries()
    )


def test_unsigned_claim_cannot_replace_a_signed_grant(governed, manager):
    gov, principal = governed
    before = _memory_count(manager)
    with pytest.raises(GrantVerificationError):
        gov.gateway.create_memory(
            _payload("no grant"),
            principal=principal,
            grant={"actor": "root", "required_grants_json": '["MEMORY_WRITE"]'},
        )
    assert _memory_count(manager) == before


# ------------------------------------------------------------------ 5
def test_audit_records_carry_all_nine_fields(governed, manager):
    gov, principal = governed
    grant = _grant(gov, principal, task_id="T-AUDIT", run_id="R-AUDIT")
    gov.gateway.create_memory(
        _payload("audited write"),
        principal=principal,
        grant=grant,
        task_id="T-AUDIT",
        run_id="R-AUDIT",
    )
    entries = gov.audit_bus.p1_entries()
    assert len(entries) >= 2
    required = (
        "principal_id",
        "operation",
        "object_ref",
        "trigger",
        "executed_at",
        "status",
        "failure_reason",
        "task_id",
        "run_id",
    )
    for entry in entries:
        detail = entry["detail"]
        assert all(key in detail for key in required), detail
        assert detail["p1_audit_version"] == 1
        assert entry["action"].startswith("P1:")
    succeeded = [e for e in entries if e["detail"]["status"] == "SUCCEEDED"][-1]
    assert succeeded["detail"]["task_id"] == "T-AUDIT"
    assert succeeded["detail"]["run_id"] == "R-AUDIT"
    assert succeeded["actor"] == TEST_SUBJECT


# ------------------------------------------------------------------ 6
def test_task_budget_exhaustion_blocks_background_call(governed, manager, settings):
    gov, principal = governed
    tight = CostGovernor(manager, settings, config=_cfg(task_call_limit=1))
    assert tight.enforce(task_id="T-TIGHT").allowed is True
    tight.record(task_id="T-TIGHT", cost_cny=0.5, tokens=100, model_calls=1)
    blocked = tight.check(task_id="T-TIGHT")
    assert blocked.allowed is False
    assert blocked.reason == "task_call_limit_exceeded"
    with pytest.raises(CostBlockedError):
        tight.enforce(task_id="T-TIGHT")


def test_global_llm_budget_exhaustion_blocks(governed, manager, settings):
    gov, principal = governed
    from jarvismind.services.llm.budget import BudgetGuard

    guard = BudgetGuard(manager, limit_cny=1.0)
    guard.record(2.0)  # writes llm.spent_cny in the single shared ledger
    # CostGovernor must be built with the SAME limit; its default comes from
    # settings.llm_budget_cny (0 = unlimited), so inject the capped settings.
    capped = CostGovernor(manager, replace(settings, llm_budget_cny=1.0), config=_cfg())
    result = capped.check(task_id="T-GLOBAL", expected_cost_cny=0.01)
    assert result.allowed is False
    assert result.reason.startswith("global_budget:")


def test_anomaly_trips_the_circuit_breaker(governed, manager, settings):
    gov, principal = governed
    cg = CostGovernor(manager, settings, config=_cfg(breaker_threshold=2))
    cg.anomaly("first timeout")
    assert cg.check(task_id="T-BREAKER").allowed is True
    cg.anomaly("second timeout")
    tripped = cg.check(task_id="T-BREAKER")
    assert tripped.allowed is False
    assert tripped.reason == "circuit_breaker_tripped"
    with pytest.raises(CostBlockedError):
        cg.enforce(task_id="T-BREAKER")
    cg.reset_breaker()
    assert cg.check(task_id="T-BREAKER").allowed is True


def test_task_token_and_batch_caps(governed, manager, settings):
    gov, principal = governed
    cg = CostGovernor(
        manager, settings, config=_cfg(task_token_limit=100, batch_call_limit=1)
    )
    assert cg.enforce(task_id="T-TOK", run_id="B-1", expected_tokens=60).allowed
    cg.record(task_id="T-TOK", run_id="B-1", tokens=60, model_calls=1)
    assert cg.check(task_id="T-TOK", run_id="B-1", expected_tokens=50).reason == "task_token_limit_exceeded"
    assert cg.check(task_id="T-TOK2", run_id="B-1").reason == "batch_limit_exceeded"


# ------------------------------------------------------------------ 7 / 8
def test_task_pause_resume_cancel_and_failure_recovery(governed):
    gov, principal = governed
    reg = gov.task_registry
    assert reg.create("T-1", "REFLECTION", owner=TEST_SUBJECT)["status"] == CREATED
    assert reg.create("T-1", "REFLECTION")["noop"] is True  # idempotent create
    assert reg.start("T-1").to_status == RUNNING
    assert reg.pause("T-1", reason="user request").to_status == PAUSED
    assert reg.pause("T-1", reason="user request").noop is True  # idempotent pause
    assert reg.resume("T-1").to_status == RUNNING
    assert reg.fail("T-1", reason="model timeout").to_status == FAILED
    assert reg.status("T-1")["failure_reason"] == "model timeout"
    assert reg.recover("T-1").to_status == CREATED
    recovered = [e for e in reg.events("T-1") if e["event_type"] == "RECOVER"][-1]
    assert recovered["detail"]["replay_side_effects"] is False
    assert reg.start("T-1").to_status == RUNNING
    assert reg.cancel("T-1", reason="user cancel").to_status == CANCELLED
    assert reg.cancel("T-1", reason="again").noop is True
    with pytest.raises(Exception):
        reg.start("T-1")  # terminal state
    assert reg.status("T-1")["status"] == CANCELLED
    assert len(reg.events("T-1")) == 8


def test_repeated_gateway_trigger_is_idempotent(governed, manager):
    gov, principal = governed
    before = _memory_count(manager)
    g1 = _grant(gov, principal, task_id="T-IDEM", run_id="R-IDEM")
    first = gov.gateway.create_memory(
        _payload("idempotent"),
        principal=principal,
        grant=g1,
        task_id="T-IDEM",
        run_id="R-IDEM",
        idempotency_key="idem-key-1",
    )
    second = gov.gateway.create_memory(
        _payload("should not be written"),
        principal=principal,
        grant=g1,  # already consumed; a real replay would be refused
        task_id="T-IDEM",
        run_id="R-IDEM",
        idempotency_key="idem-key-1",
    )
    assert second.id == first.id
    assert _memory_count(manager) == before + 1
    with manager.session() as session:
        rows = int(
            session.execute(
                text("SELECT COUNT(*) FROM memory_records WHERE content='should not be written'")
            ).scalar()
            or 0
        )
    assert rows == 0


# ------------------------------------------------------------------ 9 / 10
def test_derived_modules_cannot_modify_original_memory(governed, manager):
    gov, principal = governed
    grant = _grant(gov, principal, task_id="T-PROJ")
    gov.gateway.create_memory(
        _payload("original stays original"), principal=principal, grant=grant
    )
    before = gov.projection.content_digest()
    for module in ("JMR", "JKG", "JPA"):
        view = gov.projection.for_module(module)
        assert view.assert_read_only()["read_only"] is True
        page = view.read_memories(page_size=10)
        assert page.authority == "JME"
        for item in page.items:
            assert item["lineage"]["authority"] == "JME"
            with pytest.raises(TypeError):
                item["content"] = "tampered"
    assert gov.projection.content_digest() == before


def test_original_memory_never_overwritten_by_derived_flow(governed, manager):
    gov, principal = governed
    grant = _grant(gov, principal, task_id="T-DERIVED")
    record = gov.gateway.create_memory(
        _payload("source of truth"), principal=principal, grant=grant
    )
    before_digest = gov.projection.content_digest()
    before_rows = _memory_count(manager)

    # Simulated JMR / JKG / JPA cycle: read projection -> build derived
    # structures -> submit a proposal. Proposals are data here; the ONLY way to
    # change original memory is a new authorized write, which never happens.
    derived = []
    for module in ("JMR", "JKG", "JPA"):
        page = gov.projection.for_module(module).read_memories(page_size=100)
        derived.append(
            {
                "module": module,
                "node_count": len(page.items),
                "proposal": "review",
                "source_memory_id": record.id,
            }
        )
    assert len(derived) == 3
    assert gov.projection.get_memory(record.id)["content"] == "source of truth"
    assert gov.projection.content_digest() == before_digest
    assert _memory_count(manager) == before_rows


def test_update_and_delete_also_require_a_signed_grant(governed, manager):
    gov, principal = governed
    create_grant = _grant(gov, principal, task_id="T-MUT")
    record = gov.gateway.create_memory(
        _payload("mutable original"), principal=principal, grant=create_grant
    )
    update_grant = _grant(
        gov, principal, operation=OP_UPDATE, object_ref=record.id, task_id="T-MUT"
    )
    updated = gov.gateway.update_memory(
        record.id,
        MemoryUpdate(content="updated through the door"),
        principal=principal,
        grant=update_grant,
    )
    assert updated.content == "updated through the door"
    delete_grant = _grant(
        gov, principal, operation=OP_DELETE, object_ref=record.id, task_id="T-MUT"
    )
    deleted = gov.gateway.delete_memory(
        record.id, principal=principal, grant=delete_grant
    )
    assert deleted.status == "DELETED"


# ------------------------------------------------------------------ schema / 12
def test_p1_schema_is_append_only_idempotent_and_compatible(governed, manager):
    gov, principal = governed
    statements = validate_statements()
    assert len(statements) == 4
    for statement in statements:
        upper = statement.upper()
        assert upper.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS"))
        assert "DROP" not in upper and "DELETE" not in upper
    before = inspect_p1_schema(manager)
    assert before["p1_object_count"] == before["p1_object_total"]
    assert before["p1_schema_version"] == str(P1_SCHEMA_VERSION)
    result = install_p1_schema(manager)  # re-run must be a no-op
    assert result["status"] == "INSTALLED"
    assert result["idempotent"] is True
    assert result["data_compatible"] is True
    after = inspect_p1_schema(manager)
    assert before["memory_records_rows"] == after["memory_records_rows"]
    assert before["p1_row_counts"] == after["p1_row_counts"]


@pytest.mark.real_migration_gate
def test_p0_migration_block_is_unchanged_by_p1(governed):
    """P1 must NOT weaken or re-enable the P0-03A migration block.

    The marker makes the autouse sandbox restore the REAL runner instead of
    redirecting it to the test schema factory.
    """
    from jarvismind.database.migrations import MIGRATIONS, MigrationBlocked, MigrationRunner

    assert MIGRATIONS[-1].version == 6
    assert len(MIGRATIONS) == 6
    with pytest.raises(MigrationBlocked):
        MigrationRunner(governed[0].manager).apply_pending()
    # RK-01 (--noconftest full bypass) stays OPEN. Its status and the real
    # command evidence live in the consolidated report, not in a claim here.
    assert is_active() is True
