"""Persistence for JKG nodes, edges, and edge sources.

All SQL for the three proposed tables lives here; services never write raw SQL.
This module never touches ``memory_records`` or ``memory_candidates``: source
eligibility and status are read through the JME read-only projection in the
service layer, per Authority Spec §12.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session

from ..jkg.models import JkgEdge, JkgEdgeSource, JkgNode


class JkgRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ nodes
    def get_node(self, node_id: str) -> Optional[JkgNode]:
        return self.session.get(JkgNode, node_id)

    def find_node(self, entity_type: str, normalized_key: str) -> Optional[JkgNode]:
        return self.session.execute(
            select(JkgNode).where(
                JkgNode.entity_type == entity_type,
                JkgNode.normalized_key == normalized_key,
            )
        ).scalar_one_or_none()

    def find_node_by_match_key(
        self, entity_type: str, match_key: str
    ) -> Optional[JkgNode]:
        """Find a node by its secondary ``jkg.match_key`` metadata entry.

        Nodes without that entry (legacy rows, or metadata that is NULL or not a
        JSON object) never match, so callers fall back to the exact
        ``normalized_key`` lookup. Ordered by creation for a stable pick if two
        nodes ever share a key.
        """
        valid_metadata = case(
            (func.json_valid(JkgNode.metadata_json) == 1, JkgNode.metadata_json),
            else_=None,
        )
        return self.session.execute(
            select(JkgNode)
            .where(
                JkgNode.entity_type == entity_type,
                func.json_extract(valid_metadata, "$.jkg.match_key") == match_key,
            )
            .order_by(JkgNode.created_at.asc(), JkgNode.node_id.asc())
            .limit(1)
        ).scalars().first()

    def create_node(self, node: JkgNode) -> JkgNode:
        self.session.add(node)
        self.session.flush()
        return node

    def list_nodes(
        self,
        *,
        entity_type: Optional[str] = None,
        normalized_query: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[JkgNode], int]:
        conditions = []
        if entity_type is not None:
            conditions.append(JkgNode.entity_type == entity_type)
        if status is not None:
            conditions.append(JkgNode.status == status)
        if normalized_query:
            conditions.append(
                JkgNode.normalized_key.contains(normalized_query, autoescape=True)
            )
        total = int(
            self.session.execute(
                select(func.count()).select_from(JkgNode).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(JkgNode)
                .where(*conditions)
                .order_by(JkgNode.created_at.asc(), JkgNode.node_id.asc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def count_nodes(self) -> int:
        return int(
            self.session.execute(select(func.count()).select_from(JkgNode)).scalar()
            or 0
        )

    # ------------------------------------------------------------------ edges
    def get_edge(self, edge_id: str) -> Optional[JkgEdge]:
        return self.session.get(JkgEdge, edge_id)

    def find_edge(
        self, subject_id: str, predicate: str, object_id: str
    ) -> Optional[JkgEdge]:
        return self.session.execute(
            select(JkgEdge).where(
                JkgEdge.subject_id == subject_id,
                JkgEdge.predicate == predicate,
                JkgEdge.object_id == object_id,
            )
        ).scalar_one_or_none()

    def create_edge(self, edge: JkgEdge) -> JkgEdge:
        self.session.add(edge)
        self.session.flush()
        return edge

    def list_edges(
        self,
        *,
        node_id: Optional[str] = None,
        predicate: Optional[str] = None,
        direction: str = "both",
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[JkgEdge], int]:
        conditions = []
        if predicate is not None:
            conditions.append(JkgEdge.predicate == predicate)
        if node_id is not None:
            if direction == "outgoing":
                conditions.append(JkgEdge.subject_id == node_id)
            elif direction == "incoming":
                conditions.append(JkgEdge.object_id == node_id)
            else:
                conditions.append(
                    or_(
                        JkgEdge.subject_id == node_id,
                        JkgEdge.object_id == node_id,
                    )
                )
        total = int(
            self.session.execute(
                select(func.count()).select_from(JkgEdge).where(*conditions)
            ).scalar()
            or 0
        )
        rows = (
            self.session.execute(
                select(JkgEdge)
                .where(*conditions)
                .order_by(JkgEdge.recorded_at.asc(), JkgEdge.edge_id.asc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def count_edges(self) -> int:
        return int(
            self.session.execute(select(func.count()).select_from(JkgEdge)).scalar()
            or 0
        )

    def count_edges_for_node(self, node_id: str) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(JkgEdge)
                .where(
                    or_(
                        JkgEdge.subject_id == node_id,
                        JkgEdge.object_id == node_id,
                    )
                )
            ).scalar()
            or 0
        )

    # ---------------------------------------------------------------- sources
    def list_sources(self, edge_id: str) -> List[JkgEdgeSource]:
        return list(
            self.session.execute(
                select(JkgEdgeSource)
                .where(JkgEdgeSource.edge_id == edge_id)
                .order_by(
                    JkgEdgeSource.created_at.asc(),
                    JkgEdgeSource.source_id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def get_source(self, edge_id: str, memory_id: str) -> Optional[JkgEdgeSource]:
        return self.session.execute(
            select(JkgEdgeSource).where(
                JkgEdgeSource.edge_id == edge_id,
                JkgEdgeSource.memory_id == memory_id,
            )
        ).scalar_one_or_none()

    def create_source(self, source: JkgEdgeSource) -> JkgEdgeSource:
        self.session.add(source)
        self.session.flush()
        return source

    def list_sources_for_memory(self, memory_id: str) -> List[JkgEdgeSource]:
        return list(
            self.session.execute(
                select(JkgEdgeSource)
                .where(JkgEdgeSource.memory_id == memory_id)
                .order_by(
                    JkgEdgeSource.created_at.asc(),
                    JkgEdgeSource.source_id.asc(),
                )
            )
            .scalars()
            .all()
        )

    def count_sources(self, edge_id: str) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(JkgEdgeSource)
                .where(JkgEdgeSource.edge_id == edge_id)
            ).scalar()
            or 0
        )

    def count_all_sources(self) -> int:
        return int(
            self.session.execute(
                select(func.count()).select_from(JkgEdgeSource)
            ).scalar()
            or 0
        )

    def count_invalidated_sources(self) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(JkgEdgeSource)
                .where(JkgEdgeSource.invalidated_at.is_not(None))
            ).scalar()
            or 0
        )

    def distinct_source_memory_ids(self, limit: int = 1000) -> List[str]:
        rows = self.session.execute(
            select(JkgEdgeSource.memory_id)
            .distinct()
            .order_by(JkgEdgeSource.memory_id.asc())
            .limit(limit)
        ).scalars()
        return list(rows)

    def distinct_source_memory_ids_after(
        self, after_memory_id: str = "", limit: int = 200
    ) -> List[str]:
        """Page distinct source memory ids with a keyset cursor (JKG-INT-04).

        ``distinct_source_memory_ids`` (above) is deliberately untouched: it is a
        single bounded scan used by the existing ``JkgService.reconcile`` and its
        semantics must not shift. This is an additional, *pageable* variant for
        the lifecycle consumer, which must be able to resume after a partial run
        without re-walking everything it already synced.

        The cursor is the memory id itself rather than an offset: edge sources
        are written while a reconcile runs, and ``OFFSET`` would then skip rows
        that got inserted behind the cursor.
        """
        query = select(JkgEdgeSource.memory_id).distinct()
        if after_memory_id:
            query = query.where(JkgEdgeSource.memory_id > after_memory_id)
        rows = self.session.execute(
            query.order_by(JkgEdgeSource.memory_id.asc()).limit(limit)
        ).scalars()
        return list(rows)

    # ------------------------------------------------------------- integrity
    def orphan_edges(self, limit: int = 100) -> List[JkgEdge]:
        """Edges with zero source rows.

        Service code refuses to create one; this probe exists for corruption
        detection and for the migration verification checklist.
        """
        source_exists = exists().where(JkgEdgeSource.edge_id == JkgEdge.edge_id)
        return list(
            self.session.execute(
                select(JkgEdge)
                .where(~source_exists)
                .order_by(JkgEdge.edge_id.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def all_edges(self, limit: int = 100, offset: int = 0) -> List[JkgEdge]:
        return list(
            self.session.execute(
                select(JkgEdge)
                .order_by(JkgEdge.recorded_at.asc(), JkgEdge.edge_id.asc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def list_edges_without_sources(self, limit: int = 100) -> List[JkgEdge]:
        return self.orphan_edges(limit=limit)


__all__ = ["JkgRepository"]
