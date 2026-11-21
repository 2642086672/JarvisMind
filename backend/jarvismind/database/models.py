"""SQLAlchemy ORM models mapped to the SQLite schema.

The schema itself is created by ``app.database.migrations``; these classes only
describe the tables to SQLAlchemy so repositories can use the ORM API.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, String, Text, event, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..core.security_gate import assert_writable
from ..p1_gate import (
    UnauthorizedWriteError,
    current_write_context,
    get_denial_hook,
    is_active as p1_governance_is_active,
    maintenance_active,
)


class Base(DeclarativeBase):
    pass


def _scan_persisted_text(target, *, only_changed: bool) -> None:
    """003-B1 last-line content gate + P1-01 authorization gate at the ORM boundary.

    Repository/service checks can be bypassed by a new caller; an ORM flush
    cannot. On UPDATE only actually-changed fields are re-scanned so routine
    bookkeeping (accessed_at, vector_status, status) never rewrites or blocks
    historical rows.

    P1-01: while governance is active, NO INSERT/UPDATE of memory_records or
    memory_candidates is permitted unless the caller is inside an
    ``authorized_write`` context produced by WriteGovernor. This is what makes
    "every original-memory write goes through unified governance" a runtime
    invariant rather than a convention: direct creation, batch import, approval
    submission, lifecycle writes and raw ORM calls all land here.
    """
    if (
        p1_governance_is_active()
        and current_write_context() is None
        and not maintenance_active()
    ):
        table = str(getattr(target, "__tablename__", "unknown"))
        # A denial must still be audited. The hook is optional and is wrapped so
        # an audit failure can never turn a refused write into an allowed one.
        hook = get_denial_hook()
        if hook is not None:
            try:
                hook(table=table, operation="ORM_WRITE")
            except Exception:
                pass
        raise UnauthorizedWriteError(
            "Original memory write has no P1 authorization context",
            detail={"code": "unauthorized_memory_write", "table": table},
        )
    state = inspect(target)

    def current(name: str):
        if only_changed and not state.attrs[name].history.has_changes():
            return None
        return getattr(target, name, None)

    assert_writable(
        current("content"),
        summary=current("summary"),
        metadata_text=current("metadata_json"),
    )


def _register_write_gate(model) -> None:
    @event.listens_for(model, "before_insert")
    def _before_insert(mapper, connection, target) -> None:
        _scan_persisted_text(target, only_changed=False)

    @event.listens_for(model, "before_update")
    def _before_update(mapper, connection, target) -> None:
        _scan_persisted_text(target, only_changed=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class MemorySource(Base):
    __tablename__ = "memory_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    memory_type: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    source_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("memory_sources.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    accessed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Segmented text (CJK bigrams/unigrams + ASCII words) used by the FTS index.
    search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Vector indexing tracking (JME-4)
    vector_status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    vector_updated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Lifecycle bookkeeping (JME-5)
    forgotten_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    forget_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    merged_into: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 003-B: one formal memory per approved candidate. NULL for all historical
    # rows (their authorization info is never backfilled automatically).
    source_candidate_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class VectorIndexMeta(Base):
    __tablename__ = "vector_index_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("memory_sources.id", ondelete="SET NULL"), nullable=True
    )
    memory_type: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    duplicate_of: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    conflicts_with: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extraction_engine: Mapped[str] = mapped_column(String, nullable=False)
    extracted_at: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 003-B security state. Historical rows keep 'LEGACY_UNSCANNED' and must
    # never be approvable without an explicit re-scan.
    scan_status: Mapped[str] = mapped_column(String, nullable=False, default="LEGACY_UNSCANNED")
    decision_version: Mapped[int] = mapped_column(nullable=False, default=1)
    content_fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ruleset_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    required_grants_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    turn_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    app_decision_reported: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    server_security_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    final_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class MemoryAuditLog(Base):
    __tablename__ = "memory_audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    detail_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class MemorySummary(Base):
    __tablename__ = "memory_summaries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary_type: Mapped[str] = mapped_column(String, nullable=False)
    memory_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    memory_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    engine: Mapped[str] = mapped_column(String, nullable=False, default="local")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rejected_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rejected_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MemoryCorrection(Base):
    __tablename__ = "memory_corrections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    memory_id_a: Mapped[str] = mapped_column(String, nullable=False)
    memory_id_b: Mapped[str] = mapped_column(String, nullable=False)
    detected_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    chosen_memory_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    corrected_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    resolved_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    before_state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after_state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MemoryMerge(Base):
    __tablename__ = "memory_merges"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    target_memory_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    merged_memory_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    new_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    resolved_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MemoryArchiveRecord(Base):
    __tablename__ = "memory_archive_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    previous_status: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[str] = mapped_column(String, nullable=False)
    restored_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    restored_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class MemoryLifecycleReview(Base):
    __tablename__ = "memory_lifecycle_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String, nullable=False)
    review_type: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    previous_expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    new_expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


# Both tables that can carry user-controlled memory text get the same final
# gate. Raw-SQL INSERTs (deliberately used by a few tests) are outside ORM
# events; those paths are covered by the service/repository checks instead.
_register_write_gate(MemoryRecord)
_register_write_gate(MemoryCandidate)
