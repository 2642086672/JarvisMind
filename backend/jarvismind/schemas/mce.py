"""Pydantic contracts for the read-only MCE HTTP surface (MCE-01A).

``POST /api/v1/mce/resolve`` is additive. It does not replace or change
``POST /api/v1/memories/context``: that endpoint keeps its exact request and
response shape. Clients that do not know about MCE continue to work.

Field names are snake_case on purpose; the TypeScript client maps them once in
``src/services/jme/jmeClient.ts``. ``extra="forbid"`` applies to both directions
so a typo in a request cannot silently widen a budget or a project scope.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..mce.models import (
    ContextItem,
    ContextResolutionResult,
    ProjectContextSnapshot,
)

MAX_QUERY_LENGTH = 500
MAX_PROJECT_ID_LENGTH = 64
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
MIN_MAX_TOKENS = 128
MAX_MAX_TOKENS = 8000
DEFAULT_MAX_TOKENS = 1200


class MceContextKind(str, Enum):
    STAGE = "STAGE"
    COMPLETED = "COMPLETED"
    DECISION = "DECISION"
    TENTATIVE = "TENTATIVE"
    RISK = "RISK"
    NEXT_STEP = "NEXT_STEP"
    NOTE = "NOTE"


class MceDecisionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    TENTATIVE = "TENTATIVE"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


class MceResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Empty is valid: the caller may ask "restore this project" without a
    #: question. An unrelated question with no project returns an empty block.
    query: str = Field(default="", max_length=MAX_QUERY_LENGTH)
    project_id: Optional[str] = Field(
        default=None, max_length=MAX_PROJECT_ID_LENGTH
    )
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    max_tokens: int = Field(
        default=DEFAULT_MAX_TOKENS, ge=MIN_MAX_TOKENS, le=MAX_MAX_TOKENS
    )
    include_jkg: bool = True
    include_superseded: bool = False

    @field_validator("project_id")
    @classmethod
    def _clean_project_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        # Control characters are never a legitimate project id and could be
        # used to confuse a log or a UI label.
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
            raise ValueError("project_id contains control characters")
        return text


class MceContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    kind: MceContextKind
    content: str
    summary: Optional[str] = None
    importance: float
    confidence: float
    created_at: str = ""
    updated_at: str = ""
    relevance: float = 0.0
    decision_key: Optional[str] = None
    decision_status: Optional[MceDecisionStatus] = None
    superseded_by: Optional[str] = None
    decision_reasons: List[str] = Field(default_factory=list)
    injected: bool = True
    truncated: bool = False


class MceKnowledgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    entity_type: str
    name: str
    predicate: str
    direction: str
    neighbor_id: str
    neighbor_name: str
    neighbor_entity_type: str
    confidence: float
    valid_source_count: int
    source_memory_ids: List[str] = Field(default_factory=list)


class MceSourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    kind: MceContextKind
    updated_at: str = ""
    injected: bool = True


class MceExcludedCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanned: int = 0
    other_project: int = 0
    no_project: int = 0
    expired: int = 0
    sensitive: int = 0
    filtered: int = 0
    irrelevant: int = 0
    duplicate: int = 0
    superseded: int = 0
    budget_dropped: int = 0
    cap_dropped: int = 0
    jkg_nodes_scanned: int = 0
    jkg_edges_rejected: int = 0


class MceProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Optional[str] = None
    project_name: Optional[str] = None
    project_status: Optional[str] = None
    stage: Optional[MceContextItem] = None
    completed_items: List[MceContextItem] = Field(default_factory=list)
    confirmed_decisions: List[MceContextItem] = Field(default_factory=list)
    tentative_items: List[MceContextItem] = Field(default_factory=list)
    superseded_decisions: List[MceContextItem] = Field(default_factory=list)
    open_risks: List[MceContextItem] = Field(default_factory=list)
    next_steps: List[MceContextItem] = Field(default_factory=list)
    background_items: List[MceContextItem] = Field(default_factory=list)
    related_knowledge: List[MceKnowledgeItem] = Field(default_factory=list)
    sources: List[MceSourceItem] = Field(default_factory=list)
    excluded: MceExcludedCounts = Field(default_factory=MceExcludedCounts)
    jkg_state: str = "disabled"
    jkg_enriched: bool = False
    jkg_reason: str = "not_requested"
    generated_at: str = ""
    truncated: bool = False
    scan_truncated: bool = False


class MceResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_context: MceProjectContext
    rendered_context: str = ""
    resolution_version: str = "mce-v1"
    token_estimate: int = 0
    budget_max_tokens: int = 0
    shell_tokens: int = 0
    dropped_by_budget: int = 0
    truncated: bool = False
    empty_reason: Optional[str] = None
    read_only: bool = True


class MceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: str
    resolution_version: str
    read_only: bool
    database_writes: bool
    schema_changes: bool
    projection_module: str
    config: Dict[str, Any]
    jkg: Dict[str, Any]


def _item_payload(item: ContextItem) -> Dict[str, Any]:
    return {
        "memory_id": item.memory_id,
        "kind": item.kind.value,
        "content": item.content,
        "summary": item.summary,
        "importance": item.importance,
        "confidence": item.confidence,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "relevance": item.relevance,
        "decision_key": item.decision_key,
        "decision_status": (
            item.decision_status.value if item.decision_status else None
        ),
        "superseded_by": item.superseded_by,
        "decision_reasons": list(item.decision_reasons),
        "injected": item.injected,
        "truncated": item.truncated,
    }


def _snapshot_payload(snapshot: ProjectContextSnapshot) -> Dict[str, Any]:
    return {
        "project_id": snapshot.project_id,
        "project_name": snapshot.project_name,
        "project_status": snapshot.project_status,
        "stage": _item_payload(snapshot.stage) if snapshot.stage else None,
        "completed_items": [_item_payload(item) for item in snapshot.completed_items],
        "confirmed_decisions": [
            _item_payload(item) for item in snapshot.confirmed_decisions
        ],
        "tentative_items": [_item_payload(item) for item in snapshot.tentative_items],
        "superseded_decisions": [
            _item_payload(item) for item in snapshot.superseded_decisions
        ],
        "open_risks": [_item_payload(item) for item in snapshot.open_risks],
        "next_steps": [_item_payload(item) for item in snapshot.next_steps],
        "background_items": [
            _item_payload(item) for item in snapshot.background_items
        ],
        "related_knowledge": [
            {
                "node_id": item.node_id,
                "entity_type": item.entity_type,
                "name": item.name,
                "predicate": item.predicate,
                "direction": item.direction,
                "neighbor_id": item.neighbor_id,
                "neighbor_name": item.neighbor_name,
                "neighbor_entity_type": item.neighbor_entity_type,
                "confidence": item.confidence,
                "valid_source_count": item.valid_source_count,
                "source_memory_ids": list(item.source_memory_ids),
            }
            for item in snapshot.related_knowledge
        ],
        "sources": [
            {
                "memory_id": item.memory_id,
                "kind": item.kind.value,
                "updated_at": item.updated_at,
                "injected": item.injected,
            }
            for item in snapshot.sources
        ],
        "excluded": {
            "scanned": snapshot.excluded.scanned,
            "other_project": snapshot.excluded.other_project,
            "no_project": snapshot.excluded.no_project,
            "expired": snapshot.excluded.expired,
            "sensitive": snapshot.excluded.sensitive,
            "filtered": snapshot.excluded.filtered,
            "irrelevant": snapshot.excluded.irrelevant,
            "duplicate": snapshot.excluded.duplicate,
            "superseded": snapshot.excluded.superseded,
            "budget_dropped": snapshot.excluded.budget_dropped,
            "cap_dropped": snapshot.excluded.cap_dropped,
            "jkg_nodes_scanned": snapshot.excluded.jkg_nodes_scanned,
            "jkg_edges_rejected": snapshot.excluded.jkg_edges_rejected,
        },
        "jkg_state": snapshot.jkg_state,
        "jkg_enriched": snapshot.jkg_enriched,
        "jkg_reason": snapshot.jkg_reason,
        "generated_at": snapshot.generated_at,
        "truncated": snapshot.truncated,
        "scan_truncated": snapshot.scan_truncated,
    }


def resolution_payload(result: ContextResolutionResult) -> Dict[str, Any]:
    return {
        "project_context": _snapshot_payload(result.snapshot),
        "rendered_context": result.rendered_context,
        "resolution_version": result.resolution_version,
        "token_estimate": result.token_estimate,
        "budget_max_tokens": result.budget_max_tokens,
        "shell_tokens": result.shell_tokens,
        "dropped_by_budget": result.dropped_by_budget,
        "truncated": result.truncated,
        "empty_reason": result.empty_reason,
        "read_only": True,
    }


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_TOKENS",
    "MAX_LIMIT",
    "MAX_PROJECT_ID_LENGTH",
    "MAX_QUERY_LENGTH",
    "MAX_MAX_TOKENS",
    "MIN_MAX_TOKENS",
    "MceContextItem",
    "MceContextKind",
    "MceDecisionStatus",
    "MceExcludedCounts",
    "MceKnowledgeItem",
    "MceProjectContext",
    "MceResolveRequest",
    "MceResolveResponse",
    "MceSourceItem",
    "MceStatusResponse",
    "resolution_payload",
]
