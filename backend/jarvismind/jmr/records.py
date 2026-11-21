"""Attribute-access adapter over the immutable JME projection.

``ModuleProjection.read_memories()`` yields ``MappingProxyType`` rows, while the
existing reflection helpers use attribute access (``r.id`` / ``r.content``).
This adapter is a pure data conversion: it copies values out of the projection
and never retains the projection object, a manager, a session or a path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class SourceRecord:
    """One ACTIVE original memory as seen through the approved read projection."""

    id: str
    memory_type: str
    content: str
    summary: Optional[str]
    importance: float
    confidence: float
    status: str
    source_id: Optional[str]
    source_candidate_id: Optional[str]
    created_at: str
    updated_at: str
    expires_at: Optional[str]
    metadata_json: Optional[str]
    vector_status: str
    lineage: Mapping[str, Any]

    @classmethod
    def from_projection(cls, row: Mapping[str, Any]) -> "SourceRecord":
        get = row.get
        lineage = get("lineage") or {}
        return cls(
            id=str(get("id") or ""),
            memory_type=str(get("memory_type") or "SEMANTIC"),
            content=str(get("content") or ""),
            summary=get("summary"),
            importance=float(get("importance") or 0.0),
            confidence=float(get("confidence") or 0.0),
            status=str(get("status") or ""),
            source_id=get("source_id"),
            source_candidate_id=get("source_candidate_id"),
            created_at=str(get("created_at") or ""),
            updated_at=str(get("updated_at") or ""),
            expires_at=get("expires_at"),
            metadata_json=get("metadata_json"),
            vector_status=str(get("vector_status") or ""),
            lineage=MappingProxyType(dict(lineage)),
        )


def digest_of_records(rows: Sequence[SourceRecord]) -> str:
    """Digest of the READ snapshot (14 projection columns + lineage + count).

    This is NOT a substitute for re-querying the database. Acceptance checks
    that prove original data was untouched must use
    ``ReadOnlyProjection.content_digest()`` on a fresh connection.
    """
    digest = hashlib.sha256()
    digest.update(str(len(rows)).encode("utf-8"))
    digest.update(b"\x1e")
    for row in rows:
        payload = {
            "id": row.id,
            "memory_type": row.memory_type,
            "content": row.content,
            "summary": row.summary,
            "importance": row.importance,
            "confidence": row.confidence,
            "status": row.status,
            "source_id": row.source_id,
            "source_candidate_id": row.source_candidate_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "expires_at": row.expires_at,
            "metadata_json": row.metadata_json,
            "vector_status": row.vector_status,
            "lineage": dict(row.lineage),
        }
        digest.update(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")
        )
        digest.update(b"\x1e")
    return digest.hexdigest()
