"""JMR: memory reflection & organisation (derived module, spec §5).

Public surface is Python-level only. JMR never writes original memories; it
reads the approved JME projection and emits PENDING proposals into the three
existing derived tables.
"""
from __future__ import annotations

from typing import Optional

from .config import JMRConfig
from .bridge import JMRReviewBridge
from .detectors import (
    ConflictPair,
    DuplicateCluster,
    RelatedGroup,
    StatusChange,
    all_pairs,
    build_summary_draft,
    cluster_pairs,
    find_conflict_pairs,
    find_duplicate_clusters,
    find_status_changes,
    group_related,
    pair_set,
    status_pairs,
    subject_similarity,
)
from .errors import (
    JMRError,
    JMRProvenanceError,
    JMRTaskCancelled,
    JMRTaskPaused,
    JMRUnsafeTextError,
)
from .provenance import (
    RULES,
    build_metadata,
    deterministic_id,
    rule_of,
    validate_sources,
)
from .query import JMRQueryService
from .records import SourceRecord, digest_of_records
from .service import JMRRunResult, JMRService
from .store import DerivedStore


def build_jmr_service(*, manager, config: Optional[JMRConfig] = None) -> JMRService:
    """Assemble JMR from the existing P1 components.

    Does not install tables, does not activate governance, does not touch the
    production switch (design v1.2 D-13). Callers that need ``p1_tasks`` must
    install the P1 schema explicitly.
    """
    return JMRService.build(manager=manager, config=config)


__all__ = [
    "JMRConfig",
    "JMRService",
    "JMRRunResult",
    "JMRQueryService",
    "JMRReviewBridge",
    "build_jmr_service",
    "SourceRecord",
    "digest_of_records",
    "DerivedStore",
    "RULES",
    "build_metadata",
    "validate_sources",
    "deterministic_id",
    "rule_of",
    "DuplicateCluster",
    "ConflictPair",
    "StatusChange",
    "RelatedGroup",
    "find_duplicate_clusters",
    "find_conflict_pairs",
    "find_status_changes",
    "group_related",
    "build_summary_draft",
    "all_pairs",
    "cluster_pairs",
    "pair_set",
    "status_pairs",
    "JMRError",
    "JMRProvenanceError",
    "JMRTaskPaused",
    "JMRTaskCancelled",
    "JMRUnsafeTextError",
]
