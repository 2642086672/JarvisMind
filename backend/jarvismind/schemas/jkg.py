"""Request/response contracts for the read-only JKG HTTP surface (JKG-INT-03).

Shape rules that matter here
----------------------------
* **No memory body ever crosses this boundary.** A source item carries only
  ``memory_id``, the projection's current status, a validity flag and the
  invalidation metadata. There is deliberately no ``content`` field anywhere in
  this module, so a router cannot accidentally serialise one.
* **Paging totals are nullable.** ``app.schemas.common.PageMeta`` declares
  ``total: int``, which is right for JME's CRUD listings but wrong for JKG: a
  valid-only edge scan is bounded by ``query.MAX_SCAN``, and when the scan stops
  early the true total is *unknown*, not zero. ``JkgPageMeta`` therefore keeps
  ``total`` optional and adds ``truncated`` / ``scanned`` / ``candidate_total``
  so a client can tell "0 results" from "0 results seen so far". Reusing
  ``PageMeta`` here would force a fabricated number.
* **Sensitivity is mandatory on every item.** ``sensitivity`` is filled by
  ``app.jkg.governance`` (the same ``evaluate()`` detector JMR uses), and
  ``name_redacted`` / ``metadata_redacted`` say which fields were withheld.

Everything is ``extra="forbid"``, matching ``app.schemas.jmr``: an unexpected
key from a future service change fails loudly instead of being silently served.
"""
from __future__ import annotations

from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..jkg.normalization import ENTITY_TYPES, RELATION_TYPES

T = TypeVar("T")

# Closed vocabularies, mirrored from the frozen normalisation module so a typo
# in this file cannot drift from what the write path accepts.
JkgEntityType = Literal["PERSON", "PROJECT", "EVENT"]
JkgPredicate = Literal["WORKS_ON", "ATTENDS", "BELONGS_TO", "KNOWS"]
JkgDirection = Literal["outgoing", "incoming", "both"]
JkgStateValue = Literal["disabled", "schema_not_ready", "ready"]
JkgSensitivityValue = Literal["PUBLIC", "SENSITIVE", "CREDENTIAL"]
JkgNodeStatus = Literal["ACTIVE", "INVALID"]

ENTITY_TYPE_VALUES = ENTITY_TYPES
PREDICATE_VALUES = RELATION_TYPES
DIRECTION_VALUES = ("outgoing", "incoming", "both")

# Mirrors ``app.jkg.query.MAX_PAGE_SIZE``. Kept as a literal in the schema so a
# request can be rejected before it reaches the service.
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 200
# Mirror ``app.jkg.query`` traversal caps (kept literal, same reason as above).
MAX_TRAVERSAL_HOPS = 3
MAX_TRAVERSAL_NODES = 100
MAX_TRAVERSAL_EDGES = 1000


class JkgPageMeta(BaseModel):
    """Paging metadata that can honestly say "unknown".

    ``total`` is ``None`` exactly when the underlying scan was capped: the
    service then reports ``truncated=True`` and ``meta.total`` must stay null
    rather than be back-filled with ``len(items)``.
    """

    model_config = ConfigDict(extra="forbid")

    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=MAX_PAGE_SIZE)
    total: Optional[int] = Field(
        default=None,
        ge=0,
        description="Total matching rows, or null when the scan was truncated.",
    )
    total_pages: Optional[int] = Field(default=None, ge=0)
    truncated: bool = Field(
        default=False,
        description="True when the bounded scan stopped before exhausting candidates.",
    )
    scanned: Optional[int] = Field(default=None, ge=0)
    candidate_total: Optional[int] = Field(default=None, ge=0)


class JkgListEnvelope(BaseModel, Generic[T]):
    """List response with a nullable total (unlike ``schemas.common.ListEnvelope``)."""

    model_config = ConfigDict(extra="forbid")

    data: List[T]
    meta: JkgPageMeta


# --------------------------------------------------------------- query models
class JkgNodeListQuery(BaseModel):
    """Validated ``GET /nodes`` query string."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    entity_type: Optional[JkgEntityType] = None
    q: Optional[str] = Field(default=None, max_length=MAX_QUERY_LENGTH)
    status: Optional[JkgNodeStatus] = None


class JkgEdgeListQuery(BaseModel):
    """Validated ``GET /edges`` query string."""

    model_config = ConfigDict(extra="forbid")

    node_id: Optional[str] = Field(default=None, max_length=128)
    predicate: Optional[JkgPredicate] = None
    direction: JkgDirection = "both"
    valid_only: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def _direction_requires_node(self) -> "JkgEdgeListQuery":
        # Mirrors ``JkgQueryService.list_edges``: an incoming/outgoing filter is
        # meaningless without a node to be incoming to. Rejecting it here turns a
        # service-level 422 into a request-level 422.
        if self.direction != "both" and not self.node_id:
            raise ValueError("direction requires node_id")
        return self


class JkgTraverseQuery(BaseModel):
    """Validated ``GET /nodes/{node_id}/traverse`` query string."""

    model_config = ConfigDict(extra="forbid")

    hops: int = Field(default=1, ge=1, le=MAX_TRAVERSAL_HOPS)
    direction: JkgDirection = "both"
    predicate: Optional[JkgPredicate] = None
    valid_only: bool = True
    max_nodes: int = Field(default=50, ge=1, le=MAX_TRAVERSAL_NODES)
    max_edges: int = Field(default=200, ge=1, le=MAX_TRAVERSAL_EDGES)


class JkgNeighborQuery(BaseModel):
    """Validated ``GET /nodes/{node_id}/neighbors`` query string."""

    model_config = ConfigDict(extra="forbid")

    direction: JkgDirection = "both"
    predicate: Optional[JkgPredicate] = None
    valid_only: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)


# ------------------------------------------------------------- response items
class JkgSourceItem(BaseModel):
    """One memory→edge provenance row. Carries no memory body text."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    edge_id: str
    memory_id: str
    memory_status: Optional[str] = None
    created_at: Optional[str] = None
    invalidated_at: Optional[str] = None
    invalidation_reason: Optional[str] = None
    is_valid: bool


class JkgNodeItem(BaseModel):
    """One knowledge entity."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    entity_type: str
    name: str
    display_name: str
    canonical_name: str
    normalized_key: str
    source_memory_id: str
    status: str
    generation: int = Field(default=1, ge=1)
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Read-time projection of the establishing memory's state.
    source_memory_status: Optional[str] = None
    source_memory_valid: bool = False
    # Filled by ``app.jkg.governance``.
    sensitivity: JkgSensitivityValue = "PUBLIC"
    name_redacted: bool = False
    metadata_redacted: bool = False


class JkgEdgeItem(BaseModel):
    """One relation. ``sources`` is present only where the service includes it."""

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    predicate: str
    relation_type: str
    subject_id: str
    object_id: str
    status: str
    generation: int = Field(default=1, ge=1)
    confidence: float = 0.5
    metadata: Optional[Dict[str, Any]] = None
    recorded_at: Optional[str] = None
    updated_at: Optional[str] = None
    source_count: int = Field(default=0, ge=0)
    valid_source_count: int = Field(default=0, ge=0)
    is_valid: bool = False
    sensitivity: JkgSensitivityValue = "PUBLIC"
    metadata_redacted: bool = False
    sources: List[JkgSourceItem] = Field(default_factory=list)


class JkgNeighborItem(BaseModel):
    """A one-hop neighbour plus the edge that reaches it and the traversal side."""

    model_config = ConfigDict(extra="forbid")

    node: JkgNodeItem
    edge: JkgEdgeItem
    direction: Literal["outgoing", "incoming"]


class JkgNodeSourceItem(BaseModel):
    """The establishing memory of a node, reduced to non-content fields."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    memory_status: Optional[str] = None
    is_valid: bool


class JkgNodeDetailResponse(BaseModel):
    """A node with both edge directions and its own source memory."""

    model_config = ConfigDict(extra="forbid")

    node: JkgNodeItem
    outgoing_edges: List[JkgEdgeItem] = Field(default_factory=list)
    incoming_edges: List[JkgEdgeItem] = Field(default_factory=list)
    sources: List[JkgNodeSourceItem] = Field(default_factory=list)
    edge_total: Optional[int] = Field(
        default=None,
        ge=0,
        description="Total incident edges, or null when the bounded fetch truncated.",
    )
    edges_truncated: bool = False


class JkgTraversalNodeItem(BaseModel):
    """One node reached by a traversal, with the hop it was first reached at."""

    model_config = ConfigDict(extra="forbid")

    node: JkgNodeItem
    hop: int = Field(..., ge=0)


class JkgTraversalLimits(BaseModel):
    """The caps the traversal ran under, echoed so a caller sees what was applied."""

    model_config = ConfigDict(extra="forbid")

    hops: int = Field(..., ge=1, le=MAX_TRAVERSAL_HOPS)
    max_nodes: int = Field(..., ge=1, le=MAX_TRAVERSAL_NODES)
    max_edges: int = Field(..., ge=1, le=MAX_TRAVERSAL_EDGES)


class JkgTraversalResponse(BaseModel):
    """Bounded multi-hop read. ``truncated`` is true when a hard cap stopped the walk.

    Edges carry no source detail (``include_sources`` is off); each source is
    available from ``GET /edges/{edge_id}``. No memory content appears here.
    """

    model_config = ConfigDict(extra="forbid")

    root_id: str
    hops: int = Field(..., ge=1, le=MAX_TRAVERSAL_HOPS)
    direction: JkgDirection
    nodes: List[JkgTraversalNodeItem] = Field(default_factory=list)
    edges: List[JkgEdgeItem] = Field(default_factory=list)
    truncated: bool = False
    visited_nodes: int = Field(default=0, ge=0)
    visited_edges: int = Field(default=0, ge=0)
    limits: JkgTraversalLimits


class JkgOverviewCounts(BaseModel):
    """Exact ``COUNT(*)`` values. These are real totals, not scan samples."""

    model_config = ConfigDict(extra="forbid")

    nodes: int = Field(default=0, ge=0)
    edges: int = Field(default=0, ge=0)
    sources: int = Field(default=0, ge=0)
    invalidated_sources: int = Field(default=0, ge=0)


class JkgOrphanEdgeItem(BaseModel):
    """An edge with no source rows at all — a corruption signal."""

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    predicate: str
    subject_id: str
    object_id: str
    source_count: int = Field(default=0, ge=0)


class JkgOverviewResponse(BaseModel):
    """Graph summary.

    ``valid_edges`` / ``invalid_edges`` are counted **within the scan window**,
    not over the whole table: a validity decision needs every source's current
    status, so it can only be made for edges that were actually read. When
    ``truncated`` is true those two numbers describe ``scanned_edges`` rows and
    must be presented as partial. ``counts`` stays exact because it comes from
    ``COUNT(*)``.
    """

    model_config = ConfigDict(extra="forbid")

    counts: JkgOverviewCounts
    scanned_edges: int = Field(default=0, ge=0)
    valid_edges: int = Field(default=0, ge=0)
    invalid_edges: int = Field(default=0, ge=0)
    truncated: bool = False
    orphan_edges: List[JkgOrphanEdgeItem] = Field(default_factory=list)


class JkgStatusResponse(BaseModel):
    """Always-200 capability snapshot; the panel's only source of truth.

    ``state`` is one of ``disabled`` / ``schema_not_ready`` / ``ready`` and
    ``usable`` is ``state == "ready"``. A client must consult this instead of
    treating a 503 from a data endpoint as "no data".
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    enabled_source: str = "default"
    state: JkgStateValue
    usable: bool = False
    schema_ready: bool = False
    missing_tables: List[str] = Field(default_factory=list)
    present_tables: List[str] = Field(default_factory=list)
    schema_version_expected: int = Field(default=1, ge=0)
    page_size: int = Field(default=50, ge=1, le=MAX_PAGE_SIZE)
    max_page_size: int = Field(default=MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    max_scan: int = Field(default=1000, ge=1)


# ---------------------------------------------------- lifecycle sync (INT-04)
# Upper bound for one ``POST /sync/tick`` batch. Independent of ``MAX_PAGE_SIZE``
# (which bounds a *response* page): a tick may process several audit pages.
MAX_SYNC_LIMIT = 500


class JkgSyncCursor(BaseModel):
    """Keyset position in ``memory_audit_logs``: timestamp plus row id.

    Both parts are needed. Timestamps are second-resolution strings, so several
    rows can share one, and ``(created_at, id)`` is the only pair that totally
    orders them.
    """

    model_config = ConfigDict(extra="forbid")

    created_at: str = ""
    id: str = ""


class JkgSyncStatusResponse(BaseModel):
    """Read-only lifecycle-sync snapshot. **Always 200**, like ``/status``.

    ``usable`` answers "can a tick do anything"; ``state`` answers "why not".
    ``cursor_recovered`` is true when a stored cursor could not be decoded and
    the next run will start from the beginning — that is a fact the operator
    needs before deciding whether a tick is safe.
    """

    model_config = ConfigDict(extra="forbid")

    state: JkgStateValue
    enabled: bool = False
    usable: bool = False
    schema_ready: bool = False
    missing_tables: List[str] = Field(default_factory=list)
    cursor: JkgSyncCursor = Field(default_factory=JkgSyncCursor)
    cursor_recovered: bool = False
    last_tick: Optional[Dict[str, Any]] = None
    overlap_seconds: int = Field(default=0, ge=0)


class JkgSyncErrorItem(BaseModel):
    """One isolated per-memory failure.

    Carries the memory id, the exception *type* and a redacted message. It never
    carries the memory body, ``detail_json`` or the raw exception text, because
    an error string can quote the value that caused it.
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    error_type: str
    error_code: str


class JkgSyncTickResponse(BaseModel):
    """Result of exactly one lifecycle pass.

    ``dry_run`` and ``wrote`` are both present on purpose: a caller must be able
    to tell "nothing was done because I asked for a preview" from "nothing was
    done because there was nothing to do".
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: JkgStateValue
    skipped: bool = False
    dry_run: bool = False
    wrote: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    entries_read: int = Field(default=0, ge=0)
    memories_processed: int = Field(default=0, ge=0)
    processed_memory_ids: List[str] = Field(default_factory=list)
    sources_invalidated: int = Field(default=0, ge=0)
    sources_reactivated: int = Field(default=0, ge=0)
    edges_invalidated: int = Field(default=0, ge=0)
    edges_reactivated: int = Field(default=0, ge=0)
    marked_stale: int = Field(default=0, ge=0)
    errors: List[JkgSyncErrorItem] = Field(default_factory=list)
    unknown_actions: List[str] = Field(default_factory=list)
    cursor_before: JkgSyncCursor = Field(default_factory=JkgSyncCursor)
    cursor_after: JkgSyncCursor = Field(default_factory=JkgSyncCursor)
    cursor_recovered: bool = False
    overlap_seconds: int = Field(default=0, ge=0)
    truncated: bool = False
    ingested_processed: int = Field(default=0, ge=0)
    ingested_skipped: int = Field(default=0, ge=0)
    ingested_failed: int = Field(default=0, ge=0)
    governance_rejected: int = Field(default=0, ge=0)
    ok: bool = True


class JkgWorkerLease(BaseModel):
    """The stored lease document, echoed as-is.

    Every field is machine-generated (two UUIDs and three timestamps), so there
    is nothing here that could carry user text. ``stolen_from`` is the previous
    owner when an expired lease was taken over; it is the one fact that tells a
    takeover apart from a first acquisition.
    """

    model_config = ConfigDict(extra="forbid")

    owner: str = ""
    acquired_at: str = ""
    expires_at: str = ""
    heartbeat_at: str = ""
    ttl_seconds: int = Field(default=0, ge=0)
    stolen_from: Optional[str] = None


class JkgWorkerStatusResponse(BaseModel):
    """Read-only worker snapshot. **Always 200**, like ``/status``.

    ``usable`` answers "would ``run_once`` do work right now" — it folds the
    worker switch and the graph state together, because either one being off
    means the same thing to a scheduler. ``pending`` is a floor, not a total:
    the probe stops at ``pending_scan_limit`` and says so with
    ``pending_truncated`` instead of implying it counted the whole backlog.
    """

    model_config = ConfigDict(extra="forbid")

    state: JkgStateValue
    enabled: bool = False
    jkg_enabled: bool = False
    usable: bool = False
    schema_ready: bool = False
    ingest_wired: bool = False
    lease: Optional[Dict[str, Any]] = None
    lease_holder: str = ""
    lease_expired: bool = False
    last_run: Optional[Dict[str, Any]] = None
    last_success: Optional[Dict[str, Any]] = None
    last_failed: Optional[Dict[str, Any]] = None
    pending: Optional[int] = Field(default=None, ge=0)
    pending_truncated: bool = False
    pending_scan_limit: int = Field(default=0, ge=0)
    batch_limit: int = Field(default=0, ge=0)
    lease_ttl_seconds: int = Field(default=0, ge=0)
    reconcile_every: int = Field(default=0, ge=0)
    runs_this_process: int = Field(default=0, ge=0)
    cursor: JkgSyncCursor = Field(default_factory=JkgSyncCursor)


class JkgWorkerRunResponse(BaseModel):
    """Result of exactly one worker pass.

    ``skipped`` plus ``skip_reason`` distinguish the two ways a pass can decline
    to run: the worker/graph is off (``WORKER_DISABLED`` / ``SCHEMA_NOT_READY``)
    or somebody else holds the lease (``LEASE_HELD`` / ``LEASE_UNAVAILABLE``).
    All four are **normal** outcomes: ``ok`` stays true and ``errors`` stays
    empty, because a refused admission ticket is the lease doing its job.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    skipped: bool = False
    skip_reason: Optional[str] = None
    state: JkgStateValue
    enabled: bool = False
    run_id: str
    owner: str = ""
    dry_run: bool = False
    wrote: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    entries_read: int = Field(default=0, ge=0)
    memories_processed: int = Field(default=0, ge=0)
    sources_invalidated: int = Field(default=0, ge=0)
    sources_reactivated: int = Field(default=0, ge=0)
    edges_invalidated: int = Field(default=0, ge=0)
    edges_reactivated: int = Field(default=0, ge=0)
    marked_stale: int = Field(default=0, ge=0)
    ingested_processed: int = Field(default=0, ge=0)
    ingested_skipped: int = Field(default=0, ge=0)
    ingested_failed: int = Field(default=0, ge=0)
    governance_rejected: int = Field(default=0, ge=0)
    reconcile_checked: int = Field(default=0, ge=0)
    reconcile_sources_invalidated: int = Field(default=0, ge=0)
    reconcile_sources_reactivated: int = Field(default=0, ge=0)
    reconcile_due: bool = False
    lease_lost: bool = False
    truncated: bool = False
    ingest_wired: bool = False
    errors: List[Dict[str, str]] = Field(default_factory=list)


class JkgSyncReconcileResponse(BaseModel):
    """Result of one keyset-cursored reconcile page.

    ``next_after_memory_id`` is the resume point: pass it back as
    ``after_memory_id`` until ``has_more`` is false. It is echoed even when the
    page is empty, so a caller can always store exactly one value.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: JkgStateValue
    skipped: bool = False
    dry_run: bool = False
    wrote: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    memories_checked: int = Field(default=0, ge=0)
    sources_invalidated: int = Field(default=0, ge=0)
    sources_reactivated: int = Field(default=0, ge=0)
    edges_invalidated: List[str] = Field(default_factory=list)
    edges_reactivated: List[str] = Field(default_factory=list)
    errors: List[JkgSyncErrorItem] = Field(default_factory=list)
    after_memory_id: str = ""
    next_after_memory_id: str = ""
    has_more: bool = False
    ok: bool = True


class JkgExtractionEvidence(BaseModel):
    """Provenance of one candidate: the rule that fired and a character span.

    A span, not a copy. Slicing the body is the caller's business; the preview
    itself never carries body text.
    """

    model_config = ConfigDict(extra="forbid")

    rule: str
    start: int = Field(default=0, ge=0)
    end: int = Field(default=0, ge=0)


class JkgExtractionNodeItem(BaseModel):
    """One entity candidate. In-memory only: this is never persisted (C3)."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str
    name: str
    normalized_key: str
    confidence: float = 0.0
    evidence: JkgExtractionEvidence


class JkgExtractionEdgeItem(BaseModel):
    """One relation candidate. In-memory only: this is never persisted (C3)."""

    model_config = ConfigDict(extra="forbid")

    subject_key: str
    predicate: str
    object_key: str
    subject_type: str
    object_type: str
    confidence: float = 0.0
    evidence: JkgExtractionEvidence
    source_memory_id: str


class JkgExtractionRejectionItem(BaseModel):
    """Why a candidate produced nothing. Holds codes and a sentence, no text."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    reason_code: str
    human_reason: str


class JkgExtractionPrivacy(BaseModel):
    """The ``evaluate()`` verdict on the whole body, reduced to codes.

    Mirrors ``PrivacyDecision.to_dict()`` exactly. Every field there is already
    value-free (categories, codes, principal/trigger labels), so none of it can
    carry body text.
    """

    model_config = ConfigDict(extra="forbid")

    sensitivity: str
    decision: str
    reason_code: str
    credential_categories: List[str] = Field(default_factory=list)
    pii_categories: List[str] = Field(default_factory=list)
    injection_signals: List[str] = Field(default_factory=list)
    required_grants: List[str] = Field(default_factory=list)
    requires_dual_authorization: bool = False
    principal_id: str = ""
    trigger: str = ""
    error_type: Optional[str] = None


class JkgExtractionCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: int = Field(default=0, ge=0)
    edges: int = Field(default=0, ge=0)
    rejections: int = Field(default=0, ge=0)
    duplicates_removed: int = Field(default=0, ge=0)


class JkgExtractionPreviewResponse(BaseModel):
    """Read-only extraction preview (C3). Nothing here was written anywhere.

    ``rule_version`` is part of the payload because a preview is only comparable
    with one produced by the same rule set.
    """

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    rule_version: str
    eligible: bool = False
    source_memory_status: Optional[str] = None
    metadata_supplied: bool = False
    privacy: JkgExtractionPrivacy
    counts: JkgExtractionCounts
    nodes: List[JkgExtractionNodeItem] = Field(default_factory=list)
    edges: List[JkgExtractionEdgeItem] = Field(default_factory=list)
    rejections: List[JkgExtractionRejectionItem] = Field(default_factory=list)
    persisted: bool = False


# ----------------------------------------------- historical init (JKG-INT-05)
JkgInitTaskStatus = Literal["PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"]
JkgInitTaskMode = Literal["full", "retry"]
JkgInitPhase = Literal["scanning", "finalizing", "retrying", "completed"]


class JkgInitGraphCounts(BaseModel):
    """Exact graph totals at preview time."""

    model_config = ConfigDict(extra="forbid")

    nodes: int = Field(default=0, ge=0)
    edges: int = Field(default=0, ge=0)
    sources: int = Field(default=0, ge=0)


class JkgInitPreviewResponse(BaseModel):
    """What a historical backfill would scan. Preview writes nothing."""

    model_config = ConfigDict(extra="forbid")

    eligible_memories: int = Field(default=0, ge=0)
    snapshot_at: str
    graph: JkgInitGraphCounts
    rules_version: str
    supported_entity_types: List[str] = Field(default_factory=list)
    supported_relations: List[str] = Field(default_factory=list)
    attributes_supported: bool = False
    p1_available: bool = False
    notes: List[str] = Field(default_factory=list)


class JkgInitProgress(BaseModel):
    """Counters of one backfill task. ``percent`` is scanned / total, capped at 100."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=0, ge=0)
    scanned: int = Field(default=0, ge=0)
    processed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    governance_rejected: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    nodes_added: int = Field(default=0, ge=0)
    edges_added: int = Field(default=0, ge=0)
    sources_added: int = Field(default=0, ge=0)
    nodes_matched: int = Field(default=0, ge=0)
    percent: int = Field(default=0, ge=0, le=100)
    phase: Optional[JkgInitPhase] = None


class JkgInitFailureItem(BaseModel):
    """One memory that could not be ingested. Carries codes only, never content."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    error_type: str
    error_code: str


class JkgInitTaskResponse(BaseModel):
    """One JKG_INIT task as the API reports it."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_type: str
    status: JkgInitTaskStatus
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    mode: JkgInitTaskMode
    batch_size: int = Field(..., ge=1, le=100)
    progress: JkgInitProgress
    failures: List[JkgInitFailureItem] = Field(default_factory=list)
    failed_total: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    source_task_id: str = ""
    error_code: str = ""


class JkgInitTaskListResponse(BaseModel):
    """A page of tasks. ``total`` counts every task matching the status filter."""

    model_config = ConfigDict(extra="forbid")

    data: List[JkgInitTaskResponse]
    total: int = Field(default=0, ge=0)
    limit: int = Field(..., ge=1, le=100)
    offset: int = Field(..., ge=0)


class JkgInitFailuresResponse(BaseModel):
    """A page of one task's failures. ``failed_total`` is the true count."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    failed_total: int = Field(default=0, ge=0)
    failures: List[JkgInitFailureItem] = Field(default_factory=list)
    limit: int = Field(..., ge=1, le=100)
    offset: int = Field(..., ge=0)


class JkgInitTaskCreateRequest(BaseModel):
    """Body of ``POST /init/tasks``. Retry tasks are spawned by the server only."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=20, ge=1, le=100)
    mode: Literal["full"] = "full"


def build_jkg_page_meta(
    *,
    page: int,
    page_size: int,
    total: Optional[int],
    truncated: bool = False,
    scanned: Optional[int] = None,
    candidate_total: Optional[int] = None,
) -> JkgPageMeta:
    """Compute page meta without inventing a total.

    ``total_pages`` is derived only when ``total`` is known; when the scan was
    truncated both stay ``None`` so nothing downstream can render "page 1 of 1"
    for a result set of unknown size.
    """
    total_pages: Optional[int] = None
    if total is not None:
        total_pages = (total + page_size - 1) // page_size if page_size else 0
    return JkgPageMeta(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        truncated=truncated,
        scanned=scanned,
        candidate_total=candidate_total,
    )


__all__ = [
    "JkgPageMeta",
    "JkgListEnvelope",
    "JkgNodeListQuery",
    "JkgEdgeListQuery",
    "JkgNeighborQuery",
    "JkgTraverseQuery",
    "JkgTraversalNodeItem",
    "JkgTraversalLimits",
    "JkgTraversalResponse",
    "JkgNodeStatus",
    "JkgSourceItem",
    "JkgNodeItem",
    "JkgEdgeItem",
    "JkgNeighborItem",
    "JkgNodeSourceItem",
    "JkgNodeDetailResponse",
    "JkgOverviewCounts",
    "JkgOrphanEdgeItem",
    "JkgOverviewResponse",
    "JkgStatusResponse",
    "MAX_SYNC_LIMIT",
    "JkgSyncCursor",
    "JkgSyncStatusResponse",
    "JkgSyncErrorItem",
    "JkgSyncTickResponse",
    "JkgSyncReconcileResponse",
    "JkgWorkerLease",
    "JkgWorkerStatusResponse",
    "JkgWorkerRunResponse",
    "JkgExtractionEvidence",
    "JkgExtractionNodeItem",
    "JkgExtractionEdgeItem",
    "JkgExtractionRejectionItem",
    "JkgExtractionPrivacy",
    "JkgExtractionCounts",
    "JkgExtractionPreviewResponse",
    "JkgInitTaskStatus",
    "JkgInitTaskMode",
    "JkgInitPhase",
    "JkgInitGraphCounts",
    "JkgInitPreviewResponse",
    "JkgInitProgress",
    "JkgInitFailureItem",
    "JkgInitTaskResponse",
    "JkgInitTaskListResponse",
    "JkgInitFailuresResponse",
    "JkgInitTaskCreateRequest",
    "JkgEntityType",
    "JkgPredicate",
    "JkgDirection",
    "JkgStateValue",
    "JkgSensitivityValue",
    "ENTITY_TYPE_VALUES",
    "PREDICATE_VALUES",
    "DIRECTION_VALUES",
    "MAX_PAGE_SIZE",
    "MAX_QUERY_LENGTH",
    "build_jkg_page_meta",
]
