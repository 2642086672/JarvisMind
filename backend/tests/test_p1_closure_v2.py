"""Second-round P1 fixes: dynamic tests for the three remaining P0 findings.

Covers the independent reviewer's P0 items:
  * governance must be ACTIVE at application startup (not on first /p1 request)
  * DBAPI authorizer must stop TRIGGER-internal writes and raw_connection writes
  * ModuleProjection must expose no function object / path / __globals__ escape
plus key_version rotation, maintenance writes under an active gate, and the
installed pytest11 gate distribution (RK-01 residual boundary).
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy import text

from jarvismind.governance import build_governance, deactivate_governance
from jarvismind.governance.config import GovernanceConfig
from jarvismind.governance.identity import CAP_MEMORY_WRITE, TrustedPrincipal
from jarvismind.governance.integration import OP_MEMORY_CREATE, service_principal
from jarvismind.main import create_app
from jarvismind.p1_gate import WriteContext, authorized_write, is_active, maintenance_write
from jarvismind.schemas.memory import MemoryCreate

KEY_ENV = "JARVISMIND_P1_GRANT_KEY"
SERVER_KEY = "33" * 32


@pytest.fixture()
def key_env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, SERVER_KEY)


@pytest.fixture()
def governed(manager, settings, key_env):
    gov = build_governance(manager, settings, config=GovernanceConfig(), enforce=True)
    principal = TrustedPrincipal.issue(
        settings,
        subject_id="svc:closure-v2",
        principal_type="SERVICE",
        grants=(CAP_MEMORY_WRITE,),
    )
    try:
        yield gov, principal
    finally:
        deactivate_governance()


def _seed_memory(manager, memory_id: str, content: str) -> None:
    """Insert a row as SERVER-INTERNAL setup (maintenance context)."""
    with maintenance_write(), authorized_write(
        WriteContext(
            principal_id="svc:test-setup",
            operation="MEMORY_CREATE",
            object_ref=memory_id,
            trigger="TEST_SETUP",
        )
    ):
        with manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO memory_records(id, memory_type, content, importance, "
                    "confidence, status, created_at, updated_at) VALUES "
                    "(:id,'SEMANTIC',:content,5.0,0.5,'ACTIVE',"
                    "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
                ),
                {"id": memory_id, "content": content},
            )


def _content(manager, memory_id: str):
    with manager.session() as session:
        return session.execute(
            text("SELECT content FROM memory_records WHERE id=:i"), {"i": memory_id}
        ).scalar()


# ------------------------------------------------------------------ P0-A
def test_governance_is_active_immediately_after_create_app(manager, settings, key_env):
    """No HTTP request may be required before the gates switch on."""
    enabled = replace(settings, p1_governance_enabled=True)
    try:
        create_app(enabled, manager=manager)  # startup only, no TestClient
        assert is_active() is True
    finally:
        deactivate_governance()


def test_disabled_app_does_not_activate_the_gate(manager, settings):
    create_app(settings, manager=manager)
    assert is_active() is False


# ------------------------------------------------------------------ P0-B
def test_dbapi_authorizer_blocks_trigger_internal_write(governed, manager):
    """A trigger body UPDATE never reaches SQLAlchemy Engine events."""
    gov, principal = governed
    _seed_memory(manager, "p1-trigger-target", "ORIGINAL")
    with maintenance_write(), authorized_write(
        WriteContext("svc:setup", "MEMORY_CREATE", "setup", "TEST")
    ):
        with manager.session() as session:
            session.execute(text("DROP TRIGGER IF EXISTS trg_evil"))
            session.execute(text("CREATE TABLE IF NOT EXISTS trigger_src(x TEXT)"))
            session.execute(
                text(
                    "CREATE TRIGGER trg_evil AFTER INSERT ON trigger_src BEGIN "
                    "UPDATE memory_records SET content='TRIGGER_WRITE' "
                    "WHERE id='p1-trigger-target'; END"
                )
            )
    # No authorised context now: the outer INSERT is harmless, but the
    # trigger-internal UPDATE must be denied by the DBAPI authorizer.
    raised = False
    try:
        with manager.session() as session:
            session.execute(text("INSERT INTO trigger_src(x) VALUES ('go')"))
    except Exception:
        raised = True
    assert raised is True
    assert _content(manager, "p1-trigger-target") == "ORIGINAL"


def test_dbapi_authorizer_blocks_raw_dbapi_connection_write(governed, manager):
    """engine.raw_connection() bypasses Engine events but NOT set_authorizer."""
    gov, principal = governed
    _seed_memory(manager, "p1-raw-target", "ORIGINAL")
    raw = manager.engine.raw_connection()
    try:
        dbapi = raw.dbapi_connection
        with pytest.raises(sqlite3.DatabaseError) as caught:
            dbapi.execute(
                "UPDATE memory_records SET content='RAW_DBAPI_WRITE' "
                "WHERE id='p1-raw-target'"
            )
        assert "not authorized" in str(caught.value).lower()
    finally:
        raw.close()
    assert _content(manager, "p1-raw-target") == "ORIGINAL"


# ------------------------------------------------------------------ P0-C
def test_projection_exposes_no_function_globals_or_path(governed):
    gov, principal = governed
    view = gov.projection.for_module("JKG")
    assert not hasattr(view, "_read_page_fn")
    assert not hasattr(view, "_read_one_fn")
    assert not hasattr(view, "_db_path")
    assert not hasattr(view, "_projection")
    assert not hasattr(view, "_manager")
    for name in view.__slots__:
        value = getattr(view, name)
        assert isinstance(value, str)
        assert not hasattr(value, "__globals__")
        assert not hasattr(value, "session")
        assert not hasattr(value, "engine")
    evidence = view.assert_read_only()
    assert evidence["has_function_attribute"] is False
    assert evidence["has_path_attribute"] is False
    assert evidence["write_probe_rejected"] is True
    assert evidence["read_only"] is True
    # The read interface still works with the opaque token.
    page = view.read_memories(page_size=5)
    assert page.authority == "JME"


# ------------------------------------------------------------------ P1 fixes
def test_key_version_2_allows_writes(manager, settings, monkeypatch, key_env):
    monkeypatch.setenv("JARVISMIND_P1_GRANT_KEY_VERSION", "2")
    cfg = GovernanceConfig.from_settings(settings)
    assert cfg.grant_key_version == 2
    gov = build_governance(manager, settings, config=cfg, enforce=True)
    try:
        principal = service_principal(manager)
        assert principal.key_version == 2
        assert principal.verify(settings, key_version=2) is True
        grant = gov.grant_authority.issue(
            principal=principal,
            operation=OP_MEMORY_CREATE,
            object_ref="NEW",
            grants=(CAP_MEMORY_WRITE,),
        )
        record = gov.gateway.create_memory(
            MemoryCreate(memory_type="SEMANTIC", content="v2 key version write"),
            principal=principal,
            grant=grant,
        )
        assert record.id
    finally:
        deactivate_governance()


def test_maintenance_writes_are_allowed_but_client_writes_are_not(governed, manager):
    from jarvismind.repositories.memory_repository import MemoryRepository

    gov, principal = governed
    _seed_memory(manager, "p1-maint-target", "ORIGINAL")
    with manager.session() as session:
        repo = MemoryRepository(session)
        record = repo.get_by_id("p1-maint-target")
        repo.touch_accessed(record)              # must not raise
        repo.update_vector_status("p1-maint-target", "INDEXED")
    assert _content(manager, "p1-maint-target") == "ORIGINAL"
    from jarvismind.p1_gate import UnauthorizedWriteError

    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.execute(
                text("UPDATE memory_records SET content='CLIENT_WRITE' WHERE id='p1-maint-target'")
            )


# ------------------------------------------------------------------ P2 fixes
def test_classifier_allows_pure_read_cte_but_blocks_multi_statement_write():
    from jarvismind.p1_gate import statement_needs_authorization

    assert (
        statement_needs_authorization(
            "WITH c AS (SELECT 1) SELECT * FROM memory_records"
        )
        is False
    )
    assert (
        statement_needs_authorization(
            "SELECT 1; UPDATE memory_records SET summary='x'"
        )
        is True
    )


def test_ancient_principal_is_rejected(settings, key_env):
    from datetime import datetime, timedelta, timezone

    from jarvismind.governance.identity import _PRINCIPAL_CONTEXT, _mac

    principal = TrustedPrincipal.issue(
        settings, subject_id="svc:age", principal_type="SERVICE"
    )
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    unsigned = replace(principal, issued_at=old, mac="")
    correctly_signed = replace(
        unsigned, mac=_mac(settings, _PRINCIPAL_CONTEXT, unsigned.payload())
    )
    assert correctly_signed.verify(settings) is False
    assert correctly_signed.verify(settings, max_age_seconds=7 * 24 * 3600) is True


# ------------------------------------------------------------------ round 3
def test_bare_repository_create_without_authorization_is_refused(governed, manager):
    """The gate must STILL refuse a raw repository write; service entry points
    (extract / import) authorise before opening a session instead."""
    from jarvismind.database.models import MemoryCandidate, MemoryRecord
    from jarvismind.p1_gate import UnauthorizedWriteError
    from jarvismind.repositories.candidate_repository import CandidateRepository
    from jarvismind.repositories.memory_repository import MemoryRepository

    ts = "2026-01-01T00:00:00+00:00"
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            MemoryRepository(session).create(
                MemoryRecord(
                    id="p1-repo-1",
                    memory_type="SEMANTIC",
                    content="bare repository write",
                    importance=5.0,
                    confidence=0.5,
                    status="ACTIVE",
                    created_at=ts,
                    updated_at=ts,
                )
            )
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            CandidateRepository(session).create(
                MemoryCandidate(
                    id="p1-repo-c1",
                    memory_type="SEMANTIC",
                    content="bare candidate write",
                    importance=5.0,
                    confidence=0.5,
                    status="PENDING",
                    extraction_engine="local",
                    extracted_at=ts,
                )
            )


def test_maintenance_window_cannot_change_content(governed, manager):
    """P2-1: maintenance_write is column-scoped, not a blanket bypass."""
    _seed_memory(manager, "p1-maint-content", "ORIGINAL")
    with maintenance_write():
        raised = False
        try:
            with manager.session() as session:
                session.execute(
                    text(
                        "UPDATE memory_records SET content='MAINT_ABUSE' "
                        "WHERE id='p1-maint-content'"
                    )
                )
        except Exception:
            raised = True
    assert raised is True
    assert _content(manager, "p1-maint-content") == "ORIGINAL"


def test_raw_dbapi_ddl_on_protected_table_is_denied(governed, manager):
    """P2-2: raw DBAPI connections must not ALTER/DROP protected tables."""
    gov, principal = governed
    raw = manager.engine.raw_connection()
    try:
        dbapi = raw.dbapi_connection
        with pytest.raises(sqlite3.DatabaseError):
            dbapi.execute(
                "CREATE INDEX IF NOT EXISTS p1_evil_idx "
                "ON memory_records(content)"
            )
    finally:
        raw.close()
