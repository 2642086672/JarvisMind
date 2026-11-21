"""SQLAlchemy mappings for the proposed JKG tables.

These classes describe the frozen Authority Spec §6.1 model plus the fields
this task needs (canonical/display names, metadata, updated_at, source
invalidation). They do **not** create tables: the repository only queries them
after ``app.jkg.schema`` reports the tables present. Mapping them on the shared
``Base`` is safe because nothing in this project calls
``Base.metadata.create_all``; it only lets the repositories use the ORM API.

Field names follow the authoritative spec, with the task vocabulary mapped as:

* task ``id``              -> ``node_id`` / ``edge_id`` / ``source_id``
* task ``relation_type``   -> ``predicate``
* task ``source_node_id``  -> ``subject_id``
* task ``target_node_id``  -> ``object_id``
* task ``display_name``    -> ``name``
* task ``normalized_name`` -> ``normalized_key``
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.models import Base


class JkgNode(Base):
    __tablename__ = "jkg_nodes"

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    # User-facing label. Public query results expose it as display_name too.
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Case-preserving, whitespace-collapsed form.
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    # Casefolded dedupe key; unique together with entity_type.
    normalized_key: Mapped[str] = mapped_column(String, nullable=False)
    # The memory that first established this entity. Never empty.
    source_memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    # Administrative state; relation validity is computed from edge sources.
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    # Reserved for derived-data generations; v1 always writes 1.
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class JkgEdge(Base):
    __tablename__ = "jkg_edges"

    edge_id: Mapped[str] = mapped_column(String, primary_key=True)
    predicate: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("jkg_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[str] = mapped_column(
        ForeignKey("jkg_nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    # ACTIVE / INVALID. Lifecycle sync may mark an edge INVALID when every
    # source is gone; queries still re-check source status so a stale flag can
    # never make a forgotten fact visible.
    status: Mapped[str] = mapped_column(String, nullable=False, default="ACTIVE")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class JkgEdgeSource(Base):
    __tablename__ = "jkg_edge_sources"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    edge_id: Mapped[str] = mapped_column(
        ForeignKey("jkg_edges.edge_id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_records.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    # NULL means the source has not been explicitly invalidated. Read-time
    # validity additionally checks the JME memory status through the read-only
    # projection, so an unsynced forget cannot leave a valid-looking edge.
    invalidated_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invalidation_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )


__all__ = ["JkgNode", "JkgEdge", "JkgEdgeSource"]
