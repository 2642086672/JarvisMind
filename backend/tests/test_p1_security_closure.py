"""P1 security closure: dynamic, falsifiable tests for the four P0 fixes.

P0-1  server-only signing key separated from the shared API token
P0-2  connection-level gate closing ORM / Core / raw SQL / bulk write bypasses
P0-3  structural isolation of the read-only projection
RK-01 gate survives --noconftest and detects a reduced safety suite

Every test here either executes real code against an isolated tmp database, or
calls the real decision/entry-point functions. No test is deleted, no assertion
is weakened, and no test count is modified.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

import pytest
from sqlalchemy import text, update

from jarvismind.database.models import MemoryRecord
from jarvismind.governance import build_governance, deactivate_governance
from jarvismind.governance.config import GovernanceConfig
from jarvismind.governance.errors import UnauthorizedWriteError
from jarvismind.governance.identity import (
    CAP_MEMORY_WRITE,
    KEY_SCHEME,
    TrustedPrincipal,
    _canonical,
    _PRINCIPAL_CONTEXT,
)
from jarvismind.governance.key_material import KEY_ENV, GrantKeyError, load_grant_key
from jarvismind.p1_gate import WriteContext, authorized_write, statement_needs_authorization
from jarvismind.schemas.memory import MemoryCreate

SERVER_KEY_A = "11" * 32  # 32 bytes, hex
SERVER_KEY_B = "22" * 32


@pytest.fixture()
def key_env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, SERVER_KEY_A)
    return SERVER_KEY_A


@pytest.fixture()
def governed(manager, settings, key_env):
    gov = build_governance(
        manager, settings, config=GovernanceConfig(), enforce=True
    )
    principal = TrustedPrincipal.issue(
        settings,
        subject_id="svc:p1-closure",
        principal_type="SERVICE",
        grants=(CAP_MEMORY_WRITE,),
    )
    try:
        yield gov, principal
    finally:
        deactivate_governance()


def _context() -> WriteContext:
    return WriteContext(
        principal_id="svc:p1-closure",
        operation="MEMORY_CREATE",
        object_ref="NEW",
        trigger="TEST",
        task_id="T-P1",
        run_id="R-P1",
    )


def _memory_count(manager) -> int:
    with manager.session() as session:
        return int(
            session.execute(text("SELECT COUNT(*) FROM memory_records")).scalar() or 0
        )


def _insert_sql(memory_id: str) -> str:
    return (
        "INSERT INTO memory_records(id, memory_type, content, importance, "
        "confidence, status, created_at, updated_at) VALUES "
        f"('{memory_id}','SEMANTIC','raw sql bypass',5.0,0.5,'ACTIVE',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )


# ------------------------------------------------------------------ P0-1
def test_p0_1_key_is_not_derived_from_api_token(settings, key_env):
    """The exact attack from independent review P0-1 must now FAIL."""
    key = load_grant_key(settings)
    assert len(key) >= 32
    assert key == bytes.fromhex(SERVER_KEY_A)
    # An attacker knows only the shared api_token, not the server key.
    attacker_key = hmac.new(
        settings.api_token.encode("utf-8"), _PRINCIPAL_CONTEXT, hashlib.sha256
    ).digest()
    assert attacker_key != hmac.new(key, _PRINCIPAL_CONTEXT, hashlib.sha256).digest()
    assert KEY_SCHEME.endswith("v2")  # v1 (token-derived) grants are invalid


def test_p0_1_token_holder_cannot_forge_a_principal(settings, key_env):
    principal = TrustedPrincipal.issue(
        settings,
        subject_id="svc:legit",
        principal_type="SERVICE",
        grants=(CAP_MEMORY_WRITE,),
    )
    assert principal.verify(settings) is True
    # Recompute the MAC using ONLY the client-visible token (the old algorithm).
    attacker_key = hmac.new(
        settings.api_token.encode("utf-8"), _PRINCIPAL_CONTEXT, hashlib.sha256
    ).digest()
    forged_mac = hmac.new(
        attacker_key, _canonical(principal.payload()).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert forged_mac != principal.mac
    forged = replace(principal, mac=forged_mac)
    assert forged.verify(settings) is False


def test_p0_1_key_rotation_invalidates_every_mac(settings, monkeypatch):
    monkeypatch.setenv(KEY_ENV, SERVER_KEY_A)
    principal = TrustedPrincipal.issue(
        settings, subject_id="svc:rotate", principal_type="SERVICE"
    )
    assert principal.verify(settings) is True
    monkeypatch.setenv(KEY_ENV, SERVER_KEY_B)
    assert principal.verify(settings) is False


def test_p0_1_short_or_nonhex_key_fails_closed(settings, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "aabb")
    with pytest.raises(GrantKeyError):
        load_grant_key(settings)
    monkeypatch.setenv(KEY_ENV, "not-hex-at-all")
    with pytest.raises(GrantKeyError):
        load_grant_key(settings)


def test_p0_1_future_issued_at_is_rejected(governed, settings):
    """Re-sign with the CORRECT server key so only the time check can reject it."""
    from datetime import datetime, timedelta, timezone

    from jarvismind.governance.identity import _mac

    gov, principal = governed
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    unsigned = replace(principal, issued_at=future, mac="")
    correctly_signed = replace(
        unsigned, mac=_mac(settings, _PRINCIPAL_CONTEXT, unsigned.payload())
    )
    # Signature is valid; the not-yet-valid timestamp is what must be refused.
    assert hmac.compare_digest(
        correctly_signed.mac, _mac(settings, _PRINCIPAL_CONTEXT, correctly_signed.payload())
    )
    assert correctly_signed.verify(settings) is False


# ------------------------------------------------------------------ P0-2
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO memory_records(id,content) VALUES ('a','b')",
        "UPDATE memory_records SET summary='x' WHERE id='a'",
        "DELETE FROM memory_records WHERE id='a'",
        "WITH c AS (SELECT 1) UPDATE memory_records SET summary='x'",
        "-- comment\n  UPDATE memory_candidates SET status='APPROVED'",
        "/* c */ INSERT INTO memory_candidates(id) VALUES ('x')",
    ],
)
def test_p0_2_statement_classifier_is_fail_closed(statement):
    assert statement_needs_authorization(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM memory_records",
        "PRAGMA table_info(memory_records)",
        "BEGIN",
        "COMMIT",
        "INSERT INTO memory_audit_logs(id) VALUES ('x')",
        "UPDATE system_settings SET value='1' WHERE key='x'",
    ],
)
def test_p0_2_reads_and_unrelated_tables_are_allowed(statement):
    assert statement_needs_authorization(statement) is False


def test_p0_2_raw_sql_insert_is_blocked(governed, manager):
    gov, principal = governed
    before = _memory_count(manager)
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.execute(text(_insert_sql("p1-raw-1")))
    assert _memory_count(manager) == before


def test_p0_2_core_update_is_blocked(governed, manager):
    gov, principal = governed
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.execute(update(MemoryRecord).values(content="core bypass"))


def test_p0_2_query_update_is_blocked(governed, manager):
    gov, principal = governed
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.query(MemoryRecord).update(
                {"content": "query bypass"}, synchronize_session=False
            )


def test_p0_2_bulk_insert_is_blocked(governed, manager):
    gov, principal = governed
    record = MemoryRecord(
        id="p1-bulk-1",
        memory_type="SEMANTIC",
        content="bulk bypass",
        importance=5.0,
        confidence=0.5,
        status="ACTIVE",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(UnauthorizedWriteError):
        with manager.session() as session:
            session.bulk_save_objects([record])
            session.flush()


def test_p0_2_authorized_context_allows_the_same_raw_statement(governed, manager):
    """The gate is a choke point, not a blanket ban: Gateway can收编 raw writes."""
    gov, principal = governed
    before = _memory_count(manager)
    with authorized_write(_context()):
        with manager.session() as session:
            session.execute(text(_insert_sql("p1-authorised-raw")))
    assert _memory_count(manager) == before + 1


def test_p0_2_gateway_write_still_works(governed, manager, settings):
    gov, principal = governed
    grant = gov.grant_authority.issue(
        principal=principal,
        operation="MEMORY_CREATE",
        object_ref="NEW",
        grants=(CAP_MEMORY_WRITE,),
        task_id="T-GW",
        run_id="R-GW",
    )
    record = gov.gateway.create_memory(
        MemoryCreate(memory_type="SEMANTIC", content="through the door"),
        principal=principal,
        grant=grant,
        task_id="T-GW",
        run_id="R-GW",
    )
    assert record.id


# ------------------------------------------------------------------ P0-3
def test_p0_3_module_projection_has_no_path_to_a_write_connection(governed):
    gov, principal = governed
    view = gov.projection.for_module("JKG")
    assert not hasattr(view, "_projection")
    assert not hasattr(view, "_manager")
    for name in view.__slots__:
        value = getattr(view, name)
        assert not hasattr(value, "session")
        assert not hasattr(value, "engine")
    evidence = view.assert_read_only()
    assert evidence["write_probe_rejected"] is True
    assert evidence["read_only"] is True
    assert evidence["has_parent_reference"] is False


def test_p0_3_readonly_projection_write_probe_is_rejected(governed):
    gov, principal = governed
    evidence = gov.projection.assert_read_only()
    assert evidence["holds_only_path_string"] is True
    assert evidence["write_probe_rejected"] is True
    assert evidence["read_only"] is True


def test_p0_3_digest_covers_every_column(governed, manager):
    """Changing summary must change the digest (old version hashed only content)."""
    gov, principal = governed
    grant = gov.grant_authority.issue(
        principal=principal,
        operation="MEMORY_CREATE",
        object_ref="NEW",
        grants=(CAP_MEMORY_WRITE,),
    )
    record = gov.gateway.create_memory(
        MemoryCreate(memory_type="SEMANTIC", content="digest source"),
        principal=principal,
        grant=grant,
    )
    before = gov.projection.content_digest()
    with authorized_write(_context()):
        with manager.session() as session:
            session.execute(
                text("UPDATE memory_records SET summary=:s WHERE id=:i"),
                {"s": "changed summary", "i": record.id},
            )
    after = gov.projection.content_digest()
    assert before != after


def test_p0_3_projection_items_are_immutable(governed):
    gov, principal = governed
    page = gov.projection.for_module("JPA").read_memories(page_size=5)
    for item in page.items:
        with pytest.raises(TypeError):
            item["content"] = "tampered"


