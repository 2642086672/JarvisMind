"""Plain-dict serialisation for JKG rows.

Keeping this separate from the services means the write path and the read path
return exactly the same shape. These functions do not touch the database.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from .models import JkgEdge, JkgEdgeSource, JkgNode
from .normalization import VALID_SOURCE_STATUSES, deserialize_metadata


# Distinguishes "the caller did not look the source up" from "the lookup found
# no memory row". Only the first omits the validity keys; the second reports
# ``source_memory_status=None`` / ``source_memory_valid=False`` so a node whose
# source memory is gone can never read as a normal valid node.
_NOT_LOOKED_UP: Any = object()


def node_payload(
    node: JkgNode, *, source_memory_status: Any = _NOT_LOOKED_UP
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "node_id": node.node_id,
        "entity_type": node.entity_type,
        "name": node.name,
        # Task-facing alias; the frozen schema stores the label in ``name``.
        "display_name": node.name,
        "canonical_name": node.canonical_name,
        "normalized_key": node.normalized_key,
        "source_memory_id": node.source_memory_id,
        "status": node.status,
        "generation": node.generation,
        "metadata": deserialize_metadata(node.metadata_json),
        "created_at": node.created_at,
        "updated_at": node.updated_at,
    }
    if source_memory_status is not _NOT_LOOKED_UP:
        payload["source_memory_status"] = source_memory_status
        payload["source_memory_valid"] = source_memory_status in VALID_SOURCE_STATUSES
    return payload


def source_payload(
    source: JkgEdgeSource, *, memory_status: Optional[str]
) -> Dict[str, Any]:
    valid = (
        source.invalidated_at is None
        and memory_status in VALID_SOURCE_STATUSES
    )
    return {
        "source_id": source.source_id,
        "edge_id": source.edge_id,
        "memory_id": source.memory_id,
        "memory_status": memory_status,
        "created_at": source.created_at,
        "invalidated_at": source.invalidated_at,
        "invalidation_reason": source.invalidation_reason,
        "is_valid": valid,
    }


def edge_payload(
    edge: JkgEdge,
    sources: Iterable[JkgEdgeSource],
    memory_statuses: Mapping[str, Optional[str]],
    *,
    include_sources: bool = True,
) -> Dict[str, Any]:
    items = []
    valid_count = 0
    for source in sources:
        item = source_payload(
            source, memory_status=memory_statuses.get(source.memory_id)
        )
        if item["is_valid"]:
            valid_count += 1
        items.append(item)
    payload: Dict[str, Any] = {
        "edge_id": edge.edge_id,
        "predicate": edge.predicate,
        # Task-facing alias; the frozen schema calls this ``predicate``.
        "relation_type": edge.predicate,
        "subject_id": edge.subject_id,
        "object_id": edge.object_id,
        "status": edge.status,
        "generation": edge.generation,
        "confidence": edge.confidence,
        "metadata": deserialize_metadata(edge.metadata_json),
        "recorded_at": edge.recorded_at,
        "updated_at": edge.updated_at,
        "source_count": len(items),
        "valid_source_count": valid_count,
        "is_valid": edge.status == "ACTIVE" and valid_count > 0,
    }
    if include_sources:
        payload["sources"] = items
    return payload


__all__ = ["node_payload", "source_payload", "edge_payload"]
