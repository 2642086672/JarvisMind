"""Request and response schemas for automatic memory extraction."""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from ..core.security_gate import validate_client_value
from .common import ListEnvelope
from .memory import MemoryType

MAX_CONVERSATION_LENGTH = 50_000
MAX_CONTEXT_LENGTH = 5_000


class ExtractMode(str, Enum):
    AUTO = "auto"
    LLM = "llm"
    LOCAL = "local"


class CandidateStatus(str, Enum):
    PENDING = "PENDING"
    # 003-B: PII awaits USER_APPROVAL + SENSITIVE_PII; it is NOT a rejection.
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExtractRequest(BaseModel):
    model_config = {"extra": "forbid"}

    conversation: str = Field(..., min_length=1, max_length=MAX_CONVERSATION_LENGTH)
    conversation_id: str = Field(..., min_length=1, max_length=255)
    source_id: Optional[str] = Field(default=None, max_length=128)
    context: Optional[str] = Field(default=None, max_length=MAX_CONTEXT_LENGTH)
    mode: ExtractMode = ExtractMode.AUTO
    # 003-B: bubble correlation + explicitly untrusted client hint.
    message_id: Optional[str] = Field(default=None, max_length=128)
    turn_id: Optional[str] = Field(default=None, max_length=255)
    app_decision_reported: Optional[str] = Field(default=None, max_length=64)

    @field_validator(
        "conversation_id", "source_id", "context",
        "message_id", "turn_id", "app_decision_reported",
    )
    @classmethod
    def _reject_credential_in_client_identifiers(cls, value, info: ValidationInfo):
        """003-B1 P0-2: identifiers are persisted into candidates and event
        tables. Reject a credential BEFORE any INSERT; the blocked value is
        never written into block_events either. ``conversation`` is NOT checked
        here - it is the extraction body and must produce a BLOCK event."""
        validate_client_value(value, field=info.field_name)
        return value


class CandidateResponse(BaseModel):
    id: str
    conversation_id: Optional[str] = None
    source_id: Optional[str] = None
    memory_type: MemoryType
    content: str
    summary: Optional[str] = None
    importance: float
    confidence: float
    status: CandidateStatus
    duplicate_of: Optional[str] = None
    conflicts_with: Optional[str] = None
    extraction_engine: str
    extracted_at: str
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    reject_reason: Optional[str] = None
    sensitive_categories: List[str] = Field(default_factory=list)
    metadata: Optional[Dict] = None
    # 003-B security state (server-authoritative).
    scan_status: Optional[str] = None
    decision_version: int = 1
    content_fingerprint: Optional[str] = None
    ruleset_version: Optional[str] = None
    required_grants: List[str] = Field(default_factory=list)
    app_decision_reported: Optional[str] = None
    server_security_result: Optional[str] = None
    final_result: Optional[str] = None


class OutcomeCounts(BaseModel):
    """Mutually exclusive per-unit counters for ONE /extract call (003-B1 P1-3).

    One *processing unit* is one conversation segment in local mode, or one
    LLM-extracted item in llm mode. Every unit is assigned EXACTLY ONE outcome,
    so the following identity always holds:

        processed_units = blocked + duplicate_rejected + low_value + empty
                        + pending_manual + pending_pii_dual + conflict_pending

    ``extracted`` is the number of CANDIDATE ROWS actually created; it is NOT
    the number of processing units:

        extracted = pending_manual + pending_pii_dual + conflict_pending

    ``ignored`` is a DERIVED view (low_value + empty) and is NOT part of the
    sum. ``duplicate_rejected`` never contributes to ``extracted``: a duplicate
    produces an event, not a candidate.

    Lifecycle counters (auto_saved / manually_approved / user_rejected) are
    always 0 in a single /extract response; they belong to /extraction/stats.
    """

    processed_units: int = 0
    extracted: int = 0
    pending_manual: int = 0
    pending_pii_dual: int = 0
    conflict_pending: int = 0
    duplicate_rejected: int = 0
    low_value: int = 0
    empty: int = 0
    ignored: int = 0
    blocked: int = 0
    # Final results never occur inside /extract; kept explicit and always 0.
    auto_saved: int = 0
    manually_approved: int = 0
    user_rejected: int = 0


class BlockEventResponse(BaseModel):
    block_event_id: str
    conversation_id: str
    message_id: str
    turn_id: str
    category: str
    hit_length: int
    ruleset_version: str


class ExtractionEventResponse(BaseModel):
    event_id: str
    conversation_id: str
    message_id: str
    turn_id: str
    event_type: str
    ignore_reason_enum: str


class ExtractResponse(BaseModel):
    conversation_id: str
    engine: str
    candidates: List[CandidateResponse]
    # Legacy 3-key view: APPROVED is ALWAYS 0 during extraction.
    counts: Dict[str, int]
    sensitive_categories: List[str] = Field(default_factory=list)
    outcome_counts: OutcomeCounts = Field(default_factory=OutcomeCounts)
    block_events: List[BlockEventResponse] = Field(default_factory=list)
    extraction_events: List[ExtractionEventResponse] = Field(default_factory=list)


class CandidateListResponse(ListEnvelope[CandidateResponse]):
    pass


class AutoRememberRequest(BaseModel):
    """JEA-01: one explicit natural-language memory request."""

    model_config = {"extra": "forbid"}

    text: str = Field(..., min_length=1, max_length=20_000)
    conversation_id: str = Field(..., min_length=1, max_length=255)
    message_id: Optional[str] = Field(default=None, max_length=128)
    turn_id: Optional[str] = Field(default=None, max_length=255)
    source_id: Optional[str] = Field(default=None, max_length=128)

    @field_validator("conversation_id", "message_id", "turn_id", "source_id")
    @classmethod
    def _identifiers_are_safe(cls, value, info: ValidationInfo):
        validate_client_value(value, field=info.field_name)
        return value


class AutoRememberResponse(BaseModel):
    """Receipt for JEA-01 / JME-AUTO-GOVERNANCE-01.

    ``SAVED`` / ``UPDATED`` / ``MERGED`` are returned only after a real
    authoritative row change was committed.
    """

    status: str
    code: str = ""
    message: str = ""
    candidate_id: Optional[str] = None
    memory_id: Optional[str] = None
    content: Optional[str] = None
    memory_type: Optional[MemoryType] = None
    sensitive_categories: List[str] = Field(default_factory=list)
    merged_ids: List[str] = Field(default_factory=list)
    error_type: Optional[str] = None


class CandidateReviewRequest(BaseModel):
    """003-B: strict body; unknown fields are rejected (no grant tampering)."""

    model_config = {"extra": "forbid"}

    # Optional for wire compatibility: the URL already fixes the decision.
    decision: Optional[str] = Field(default=None, pattern="^(approve|reject)$")
    reason: Optional[str] = Field(default=None, max_length=1000)
    # Retained for wire compatibility only. NEVER used as authorization proof;
    # the server writes decided_by itself (003-B P1-1).
    actor: Optional[str] = Field(default=None, max_length=128)
    # Reserved for 003-C signed grants. Accepted but explicitly NOT used as an
    # authorization fact in this phase; client-side values cannot extend or
    # replace any authorization. ``extra: forbid`` already rejects any attempt
    # to inject a server-side authorization result field.
    grants: List[Dict] = Field(default_factory=list)

    @field_validator("reason", "actor", "grants")
    @classmethod
    def _reject_credential_in_review_fields(cls, value, info: ValidationInfo):
        # ``reason`` lands in memory_candidates.reject_reason; ``actor`` is only
        # an unverified claim, but it must never become a credential carrier.
        validate_client_value(value, field=info.field_name)
        return value
