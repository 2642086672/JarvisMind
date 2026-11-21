"""Response contracts for JMR read surfaces (JMR-02 core).

These models describe what the (not yet routed, AP-01) JMR endpoints will
return. They are data-shape only: no router is registered here.

Runtime validation is real: ``query.py`` builds every proposal through
``ProposalItem.model_validate`` / ``ProposalPage.model_validate``, so a
malformed row raises ``pydantic.ValidationError`` instead of being returned as
a successful response.

Semantics are fixed here and must not be widened without owner approval:
  * ``rule`` is the only proposal classification (Authority-Spec / JMR design).
  * ``CORRUPT`` is a diagnostic label for untrusted rows, never a rule.
  * ``review`` is a stored SNAPSHOT, never an authoritative approval.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

RULE_VALUES = ("MERGE", "CONFLICT", "STATUS_CHANGE", "SUMMARY")
STATUS_VALUES = ("PENDING", "APPROVED", "REJECTED", "VOID")
CLAIM_TYPE_VALUES = ("FACT", "INFERENCE", "SPECULATION")
CONFLICT_CLASS_VALUES = ("FACT_CONFLICT", "TIME_UPDATE", "WORDING_DIFF", "INSUFFICIENT_INFO")
SOURCE_TABLE_VALUES = ("memory_summaries", "memory_corrections", "memory_merges")

# Diagnostic label for untrusted rows. It is NOT one of RULE_VALUES and is only
# legal together with a non-empty ``corrupt_reason`` (enforced below).
CORRUPT_LABEL = "CORRUPT"

RuleValue = Literal["MERGE", "CONFLICT", "STATUS_CHANGE", "SUMMARY", "CORRUPT"]
StatusValue = Literal["PENDING", "APPROVED", "REJECTED", "VOID"]
ClaimTypeValue = Literal["FACT", "INFERENCE", "SPECULATION"]
ConflictClassValue = Literal["FACT_CONFLICT", "TIME_UPDATE", "WORDING_DIFF", "INSUFFICIENT_INFO"]
SourceTableValue = Literal["memory_summaries", "memory_corrections", "memory_merges"]


class ReviewSnapshot(BaseModel):
    """Stored review state, presented as a SNAPSHOT only.

    ``authoritative`` is fixed to False: nothing read from ``metadata_json`` can
    establish a real approval. Actor identity is never carried over from
    storage; only the status word survives the whitelist.
    """

    model_config = ConfigDict(extra="forbid")

    status: StatusValue
    authoritative: Literal[False] = False
    source: Literal["stored_snapshot"] = "stored_snapshot"


class ProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., min_length=1)
    rule: RuleValue
    status: StatusValue
    source_table: SourceTableValue
    source_memory_ids: List[str]
    lineage: List[Dict[str, Any]] = Field(default_factory=list)
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    algorithm_version: Optional[str] = None
    projection_digest: Optional[str] = None
    confidence: Optional[float] = None
    claim_type: Optional[ClaimTypeValue] = None
    sensitivity: Optional[str] = None
    conflict_class: Optional[ConflictClassValue] = None
    generation: Optional[int] = Field(default=None, ge=1)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    source_fingerprint: Optional[str] = None
    source_watermark: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    review: ReviewSnapshot
    corrupt_reason: Optional[str] = None

    @model_validator(mode="after")
    def _enforce_rule_consistency(self) -> "ProposalItem":
        # A corrupt row must be labelled as such and must never masquerade as a
        # valid rule; a valid rule must never carry a corruption marker.
        if self.rule == CORRUPT_LABEL:
            if not self.corrupt_reason:
                raise ValueError("CORRUPT rule requires corrupt_reason")
        else:
            if self.corrupt_reason is not None:
                raise ValueError("valid rule must not carry corrupt_reason")
        # conflict_class is meaningful only for CONFLICT proposals.
        if self.conflict_class is not None and self.rule != "CONFLICT":
            raise ValueError("conflict_class is only valid for rule=CONFLICT")
        return self


class ProposalDetail(ProposalItem):
    before_state_json: Optional[str] = None
    after_state_json: Optional[str] = None


class ProposalPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ProposalItem]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)


class JMRStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    governance_active: bool
    p1_schema_installed: bool
    p1_tables_present: List[str]
    algorithm_version: str
    llm_online: Literal[False] = False
    notes: List[str] = Field(default_factory=list)
    expired_pending: int = Field(default=0, ge=0)
    breaker_tripped: bool = False
    automatic_execution_authorized: Literal[False] = False
    daily_spend_cny: float = Field(default=0.0, ge=0.0)
    monthly_spend_cny: float = Field(default=0.0, ge=0.0)
    daily_budget_cny: float = Field(default=0.0, ge=0.0)
    monthly_budget_cny: float = Field(default=0.0, ge=0.0)


class JMRRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1, max_length=128)
    run_id: str = Field(default="", max_length=128)
    memory_type: Optional[str] = Field(default=None, max_length=64)
    resume: bool = False


class JMRRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    status: Literal["SUCCEEDED", "PAUSED", "CANCELLED", "FAILED", "BLOCKED"]
    created: Dict[str, int] = Field(default_factory=dict)
    skipped_idempotent: int = Field(default=0, ge=0)
    memory_rows: int = Field(default=0, ge=0)
    projection_digest: str = ""
    failure_reason: str = ""
    cost_cny: float = Field(default=0.0, ge=0.0)
    cost_status: str = ""
    degradation: str = ""
    retry_count: int = Field(default=0, ge=0)
    retry_eligible: bool = False
    expired_voided: int = Field(default=0, ge=0)
    breaker_tripped: bool = False


class JMRTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    run_id: str = ""
    event_count: int = Field(default=0, ge=0)
    last_event: Optional[str] = None
    failure_reason: str = ""
    retry_count: int = Field(default=0, ge=0)
    retry_eligible: bool = False


class JMRControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class JMRControlResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    noop: bool = False
    event_id: str = ""


class JMRReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    actor: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=2000)
    chosen_memory_id: Optional[str] = Field(default=None, max_length=128)
    corrected_content: Optional[str] = Field(default=None, max_length=20000)


class JMRReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    rule: str
    status: str
    decision: str
    authoritative: Literal[False] = False
    audit_id: str = ""
    review: Dict[str, Any] = Field(default_factory=dict)
