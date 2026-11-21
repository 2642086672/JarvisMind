"""Read-only project-context reconstruction for MCE-01.

Inputs are authoritative JME memory rows read through
``ReadOnlyProjection.for_module("MCE")`` and, optionally, valid one-hop JKG
relations read through ``JkgCompositionAdapter``. Nothing here writes: no
memory row, audit row, access timestamp, JMR proposal, JKG node or schema.

Lifecycle rules are applied at the source:

* only ``ACTIVE`` rows are scanned (``scan_memories(statuses=("ACTIVE",))``);
* a row with ``expires_at <= now`` is excluded even if the status is stale;
* a row with ``forgotten_at`` set is excluded;
* only a row whose **explicit** metadata/tag/marker names the requested
  project is considered (no name guessing, so one project cannot leak into
  another);
* credential, PII, personally-sensitive and prompt-injection content is
  withheld by ``MceGovernance`` before classification.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.timeutils import parse_iso, utc_now_iso
from .budgeter import ContextBudgeter
from .config import MceConfig
from .conflict_resolver import resolve_conflicts
from .governance import MceGovernance, log_rejection
from .models import (
    ContextItem,
    ContextKind,
    ContextResolutionResult,
    DecisionStatus,
    ExcludedCounts,
    ProjectContextSnapshot,
    RelatedKnowledge,
    SourceRef,
    classify_kind,
    content_preview,
    decision_status_for,
    metadata_decision_key,
    metadata_project_id,
    metadata_project_name,
    metadata_supersedes,
    normalize_project_id,
    parse_metadata,
)

logger = logging.getLogger("jme.mce.reconstructor")

MAX_STAGE_ITEMS = 1
MAX_DECISIONS = 50
MAX_NEXT_STEPS = 5
MAX_RISKS = 5
MAX_TENTATIVE = 4
MAX_COMPLETED = 5
MAX_BACKGROUND = 4
MAX_KNOWLEDGE = 8
MAX_KNOWLEDGE_NODES = 24

_JKG_NODE_PAGE = 100


def _timestamp(item: ContextItem) -> str:
    return item.updated_at or item.created_at or ""


def _newest_first(item: ContextItem) -> tuple:
    return (item.relevance, _timestamp(item), item.importance, item.memory_id)


def _dedupe_rank(item: ContextItem) -> int:
    if item.decision_status is DecisionStatus.CONFIRMED:
        return 0
    if item.decision_status is DecisionStatus.TENTATIVE:
        return 1
    return 2


def _is_expired(expires_at: Any, now: datetime) -> bool:
    if expires_at is None or str(expires_at).strip() == "":
        return False
    try:
        value = parse_iso(str(expires_at))
    except (TypeError, ValueError):
        # Cannot prove it is current; fail closed.
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now


def _content_key(item: ContextItem) -> str:
    return " ".join((item.content or "").casefold().split())


class MceReconstructor:
    """Deterministic reconstruction. One instance is stateless and reusable."""

    def __init__(
        self,
        *,
        projection,
        governance: MceGovernance,
        config: Optional[MceConfig] = None,
        jkg_adapter=None,
    ) -> None:
        self.projection = projection
        self.governance = governance
        self.config = config or MceConfig()
        self.jkg_adapter = jkg_adapter

    # ------------------------------------------------------------------ public
    def resolve(
        self,
        *,
        project_id: Optional[str],
        query: str = "",
        max_tokens: Optional[int] = None,
        max_items: Optional[int] = None,
        include_jkg: bool = True,
        include_superseded: bool = False,
    ) -> ContextResolutionResult:
        normalized = normalize_project_id(project_id)
        snapshot = ProjectContextSnapshot(
            project_id=normalized,
            generated_at=utc_now_iso(),
        )
        if normalized is None:
            return ContextResolutionResult(
                snapshot=snapshot,
                budget_max_tokens=self._budget(max_tokens),
                empty_reason="no_project_scope",
            )

        budget_tokens = self._budget(max_tokens)
        item_cap = self._item_cap(max_items)
        now = datetime.now(timezone.utc)
        rows, scan_truncated = self._scan()
        snapshot.excluded.scanned = len(rows)
        snapshot.scan_truncated = scan_truncated

        items: List[ContextItem] = []
        project_names: List[Tuple[str, str]] = []
        project_statuses: List[Tuple[str, str]] = []

        for row in rows:
            item = self._row_to_item(
                row,
                project_id=normalized,
                now=now,
                query=query,
                excluded=snapshot.excluded,
            )
            if item is None:
                continue
            metadata = parse_metadata(row.get("metadata_json")) or {}
            project_name = metadata_project_name(metadata)
            if project_name:
                project_names.append((_timestamp(item), project_name))
            status = metadata.get("project_status")
            if isinstance(status, str) and status.strip():
                project_statuses.append(
                    (_timestamp(item), content_preview(status, 80)[0])
                )
            items.append(item)

        # Identical content can exist under several lifecycle states. Keep the
        # current confirmation over an older tentative/superseded copy; within
        # the same rank, the newest row wins.
        items.sort(key=lambda item: (_timestamp(item), item.memory_id), reverse=True)
        items.sort(key=_dedupe_rank)
        deduped: List[ContextItem] = []
        seen_content: set[str] = set()
        for item in items:
            key = _content_key(item)
            if key and key in seen_content:
                snapshot.excluded.duplicate += 1
                continue
            seen_content.add(key)
            deduped.append(item)
        items = deduped

        if project_names:
            snapshot.project_name = max(project_names)[1]
        if project_statuses:
            snapshot.project_status = max(project_statuses)[1]

        decisions = self._bucket(snapshot, items)
        self._resolve_decisions(
            snapshot, decisions, include_superseded=include_superseded
        )
        cap_dropped = self._apply_item_cap(snapshot, item_cap)
        snapshot.excluded.cap_dropped = cap_dropped
        snapshot.truncated = snapshot.truncated or cap_dropped > 0
        self._enrich_with_jkg(
            snapshot,
            project_id=normalized,
            include_jkg=include_jkg,
            selected_items=self._current_items(snapshot),
        )
        budget_result = ContextBudgeter(
            max_tokens=budget_tokens,
            max_item_chars=self.config.max_item_chars,
        ).render(snapshot)
        snapshot.excluded.budget_dropped = budget_result.dropped
        snapshot.truncated = snapshot.truncated or budget_result.truncated
        self._build_sources(snapshot)

        empty_reason = None
        if not budget_result.rendered_context:
            if not items:
                empty_reason = "no_eligible_memories"
            else:
                empty_reason = "budget_empty"
        return ContextResolutionResult(
            snapshot=snapshot,
            rendered_context=budget_result.rendered_context,
            token_estimate=budget_result.token_estimate,
            budget_max_tokens=budget_tokens,
            shell_tokens=budget_result.shell_tokens,
            dropped_by_budget=budget_result.dropped,
            truncated=budget_result.truncated,
            empty_reason=empty_reason,
        )

    # ------------------------------------------------------------------- scan
    def _scan(self) -> Tuple[List[Mapping[str, Any]], bool]:
        rows: List[Mapping[str, Any]] = []
        after_created_at = ""
        after_id = ""
        page_size = self.config.page_size
        snapshot_bound = utc_now_iso()
        truncated = False
        while len(rows) < self.config.scan_limit:
            remaining = min(page_size, self.config.scan_limit - len(rows))
            page = self.projection.scan_memories(
                statuses=("ACTIVE",),
                limit=remaining,
                after_created_at=after_created_at,
                after_id=after_id,
                created_at_lte=snapshot_bound,
                newest_first=True,
            )
            if not page:
                break
            rows.extend(page)
            last = page[-1]
            after_created_at = str(last.get("created_at", ""))
            after_id = str(last.get("id", ""))
            if len(page) < remaining:
                break
            if len(rows) >= self.config.scan_limit:
                truncated = True
        return rows, truncated

    def _row_to_item(
        self,
        row: Mapping[str, Any],
        *,
        project_id: str,
        now: datetime,
        query: str,
        excluded: ExcludedCounts,
    ) -> Optional[ContextItem]:
        memory_id = str(row.get("id", "") or "")
        if not memory_id:
            excluded.filtered += 1
            return None
        if str(row.get("status", "")) != "ACTIVE":
            excluded.filtered += 1
            return None
        if row.get("forgotten_at"):
            excluded.filtered += 1
            return None
        if _is_expired(row.get("expires_at"), now):
            excluded.expired += 1
            return None

        content = str(row.get("content", "") or "")
        summary = row.get("summary")
        summary_text = str(summary) if summary is not None else None
        if not content.strip():
            excluded.filtered += 1
            return None

        raw_metadata = row.get("metadata_json")
        metadata = parse_metadata(raw_metadata)
        if metadata is None:
            excluded.filtered += 1
            return None
        row_project = metadata_project_id(metadata, content)
        if row_project is None:
            excluded.no_project += 1
            return None
        if row_project != project_id:
            excluded.other_project += 1
            return None

        decision = self.governance.inspect_memory(
            content=content, summary=summary_text, metadata=metadata
        )
        if not decision.allowed:
            excluded.sensitive += 1
            log_rejection(memory_id, decision)
            return None

        memory_type = str(row.get("memory_type", "SEMANTIC") or "SEMANTIC")
        kind = classify_kind(
            metadata=metadata, memory_type=memory_type, content=content
        )
        status = decision_status_for(metadata=metadata, kind=kind, content=content)
        has_decision_meta = (
            metadata.get("decision_key") is not None
            or metadata.get("decisionStatus") is not None
            or metadata.get("decision_status") is not None
        )
        relevance = self._relevance(query, content, summary_text)
        if (
            kind is ContextKind.NOTE
            and not has_decision_meta
            and query.strip()
            and relevance < 0.2
        ):
            excluded.irrelevant += 1
            return None

        clipped, truncated = content_preview(content, self.config.max_item_chars)
        summary_clipped = (
            content_preview(summary_text, 300)[0] if summary_text else None
        )
        return ContextItem(
            memory_id=memory_id,
            kind=kind,
            content=clipped,
            summary=summary_clipped,
            importance=self._float(row.get("importance"), 5.0),
            confidence=self._float(row.get("confidence"), 0.5),
            created_at=str(row.get("created_at", "") or ""),
            updated_at=str(row.get("updated_at", "") or ""),
            relevance=relevance,
            decision_key=metadata_decision_key(metadata),
            decision_status=(
                status
                if kind in {ContextKind.DECISION, ContextKind.TENTATIVE}
                or has_decision_meta
                else None
            ),
            supersedes=metadata_supersedes(metadata),
            truncated=truncated,
        )

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _relevance(query: str, content: str, summary: Optional[str]) -> float:
        if not (query or "").strip():
            return 0.0
        try:
            from ..services.memory_context_service import lexical_match

            return round(float(lexical_match(query, content, summary)), 4)
        except Exception:  # noqa: BLE001 - relevance is an ordering hint only
            return 0.0

    # ---------------------------------------------------------------- bucketing
    def _bucket(
        self, snapshot: ProjectContextSnapshot, items: Sequence[ContextItem]
    ) -> List[ContextItem]:
        stages: List[ContextItem] = []
        completed: List[ContextItem] = []
        decisions: List[ContextItem] = []
        tentative: List[ContextItem] = []
        risks: List[ContextItem] = []
        next_steps: List[ContextItem] = []
        background: List[ContextItem] = []

        for item in items:
            # Any row carrying decision metadata is resolved as a decision,
            # even if its context_kind says NOTE/TENTATIVE. Otherwise a row
            # explicitly marked SUPERSEDED could be rendered as a note.
            if item.decision_key is not None or item.decision_status is not None:
                decisions.append(item)
            elif item.kind is ContextKind.STAGE:
                stages.append(item)
            elif item.kind is ContextKind.COMPLETED:
                completed.append(item)
            elif item.kind is ContextKind.DECISION:
                decisions.append(item)
            elif item.kind is ContextKind.TENTATIVE:
                tentative.append(item)
            elif item.kind is ContextKind.RISK:
                risks.append(item)
            elif item.kind is ContextKind.NEXT_STEP:
                next_steps.append(item)
            else:
                background.append(item)

        # The current stage is a recency fact, not a relevance ranking: a query
        # that happens to match an old stage must not promote it.
        stages.sort(
            key=lambda item: (
                _timestamp(item),
                item.importance,
                item.memory_id,
            ),
            reverse=True,
        )
        completed.sort(key=_newest_first, reverse=True)
        risks.sort(key=_newest_first, reverse=True)
        next_steps.sort(key=_newest_first, reverse=True)
        background.sort(key=_newest_first, reverse=True)

        if stages:
            snapshot.stage = stages[0]
            background.extend(stages[1:])
        snapshot.completed_items = completed[:MAX_COMPLETED]
        background.extend(completed[MAX_COMPLETED:])
        snapshot.open_risks = risks[:MAX_RISKS]
        background.extend(risks[MAX_RISKS:])
        snapshot.next_steps = next_steps[:MAX_NEXT_STEPS]
        background.extend(next_steps[MAX_NEXT_STEPS:])
        tentative.sort(key=_newest_first, reverse=True)
        snapshot.tentative_items = tentative[:MAX_TENTATIVE]
        background.extend(tentative[MAX_TENTATIVE:])
        background.sort(key=_newest_first, reverse=True)
        snapshot.background_items = background[:MAX_BACKGROUND]

        # Decisions are resolved as one set, so the same-key rule sees every
        # candidate even if the per-kind cap later trims the confirmed list.
        return decisions

    def _resolve_decisions(
        self,
        snapshot: ProjectContextSnapshot,
        decisions: Sequence[ContextItem],
        *,
        include_superseded: bool,
    ) -> None:
        resolution = resolve_conflicts(decisions)
        snapshot.confirmed_decisions = list(resolution.confirmed[:MAX_DECISIONS])
        snapshot.tentative_items.extend(resolution.tentative[:MAX_TENTATIVE])
        snapshot.tentative_items.sort(key=_newest_first, reverse=True)
        snapshot.tentative_items = snapshot.tentative_items[:MAX_TENTATIVE]
        superseded = list(resolution.superseded)
        snapshot.excluded.superseded = len(superseded)
        if not include_superseded:
            superseded = [
                ContextItem(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    content="",
                    summary=None,
                    importance=item.importance,
                    confidence=item.confidence,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    relevance=item.relevance,
                    decision_key=item.decision_key,
                    decision_status=item.decision_status,
                    supersedes=(),
                    superseded_by=item.superseded_by,
                    decision_reasons=item.decision_reasons,
                    injected=False,
                    truncated=item.truncated,
                )
                for item in superseded
            ]
        snapshot.superseded_decisions = superseded

    # ---------------------------------------------------------------- JKG link
    def _enrich_with_jkg(
        self,
        snapshot: ProjectContextSnapshot,
        *,
        project_id: str,
        include_jkg: bool,
        selected_items: Sequence[ContextItem],
    ) -> None:
        if not include_jkg or not self.config.jkg_enabled:
            snapshot.jkg_state = "skipped"
            snapshot.jkg_reason = "disabled_by_config"
            return
        if self.jkg_adapter is None:
            snapshot.jkg_state = "unavailable"
            snapshot.jkg_reason = "no_adapter"
            return
        try:
            state = self.jkg_adapter.state()
            state_value = getattr(state, "value", str(state))
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            snapshot.jkg_state = "error"
            snapshot.jkg_reason = f"state_error:{type(exc).__name__}"
            return
        snapshot.jkg_state = state_value
        if state_value != "ready":
            snapshot.jkg_reason = state_value
            return
        try:
            query_service = self.jkg_adapter.query_service()
        except Exception as exc:  # noqa: BLE001
            snapshot.jkg_state = "error"
            snapshot.jkg_reason = f"query_unavailable:{type(exc).__name__}"
            return

        selected_ids = {item.memory_id for item in selected_items}
        if not selected_ids:
            snapshot.jkg_reason = "no_selected_memories"
            return
        try:
            nodes = self._scan_nodes(query_service, selected_ids, snapshot.excluded)
            knowledge = self._knowledge_from_nodes(
                query_service,
                nodes,
                project_id=project_id,
                selected_ids=selected_ids,
                excluded=snapshot.excluded,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the resolve
            snapshot.jkg_reason = f"read_error:{type(exc).__name__}"
            return
        snapshot.related_knowledge = knowledge[:MAX_KNOWLEDGE]
        snapshot.jkg_enriched = bool(snapshot.related_knowledge)
        snapshot.jkg_reason = "ready" if snapshot.jkg_enriched else "ready_empty"

    def _scan_nodes(
        self,
        query_service,
        selected_ids: set[str],
        excluded: ExcludedCounts,
    ) -> List[Dict[str, Any]]:
        matched: List[Dict[str, Any]] = []
        offset = 0
        scanned = 0
        while scanned < self.config.jkg_max_nodes:
            page_size = min(
                _JKG_NODE_PAGE, self.config.jkg_max_nodes - scanned
            )
            page = query_service.list_nodes(limit=page_size, offset=offset)
            items = page.get("items") or []
            for node in items:
                if (
                    node.get("source_memory_id") in selected_ids
                    and node.get("source_memory_valid") is True
                    and node.get("status") == "ACTIVE"
                ):
                    matched.append(node)
            scanned += len(items)
            if len(items) < page_size:
                break
            offset += len(items)
        excluded.jkg_nodes_scanned = scanned
        return matched

    def _knowledge_from_nodes(
        self,
        query_service,
        nodes: Sequence[Mapping[str, Any]],
        *,
        project_id: str,
        selected_ids: set[str],
        excluded: ExcludedCounts,
    ) -> List[RelatedKnowledge]:
        knowledge: List[RelatedKnowledge] = []
        seen: set[str] = set()
        for node in list(nodes)[:MAX_KNOWLEDGE_NODES]:
            node_id = str(node.get("node_id", "") or "")
            if not node_id:
                continue
            try:
                page = query_service.neighbors(
                    node_id=node_id,
                    direction="both",
                    valid_only=True,
                    limit=self.config.jkg_neighbors_per_node,
                )
            except Exception:  # noqa: BLE001 - skip one bad node
                excluded.jkg_edges_rejected += 1
                continue
            for entry in page.get("items") or []:
                edge = entry.get("edge") or {}
                neighbor = entry.get("node") or {}
                if not edge.get("is_valid"):
                    excluded.jkg_edges_rejected += 1
                    continue
                edge_id = str(edge.get("edge_id", "") or "")
                dedupe_key = edge_id or "|".join(
                    (
                        node_id,
                        str(edge.get("predicate", "")),
                        str(neighbor.get("node_id", "")),
                    )
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                if neighbor.get("status") != "ACTIVE":
                    excluded.jkg_edges_rejected += 1
                    continue
                if neighbor.get("source_memory_valid") is not True:
                    excluded.jkg_edges_rejected += 1
                    continue
                if not self._neighbor_in_project(
                    neighbor, project_id=project_id, selected_ids=selected_ids
                ):
                    excluded.jkg_edges_rejected += 1
                    continue
                safe_node = self._safe_label(node)
                safe_neighbor = self._safe_label(neighbor)
                if safe_node is None or safe_neighbor is None:
                    excluded.jkg_edges_rejected += 1
                    continue
                sources = edge.get("sources") or []
                if not sources and edge_id:
                    # ``neighbors(valid_only=True)`` omits the source list for
                    # payload size. Fetch one edge to keep provenance real.
                    try:
                        full_edge = query_service.get_edge(
                            edge_id, valid_only=True
                        )
                        sources = full_edge.get("sources") or []
                    except Exception:  # noqa: BLE001
                        sources = []
                source_ids = tuple(
                    sorted(
                        {
                            str(source.get("memory_id", ""))
                            for source in sources
                            if source.get("memory_id")
                            and source.get("is_valid") is True
                        }
                    )
                )
                knowledge.append(
                    RelatedKnowledge(
                        node_id=node_id,
                        entity_type=str(node.get("entity_type", "")),
                        name=safe_node,
                        predicate=str(edge.get("predicate", "")),
                        direction=str(entry.get("direction", "outgoing")),
                        neighbor_id=str(neighbor.get("node_id", "")),
                        neighbor_name=safe_neighbor,
                        neighbor_entity_type=str(neighbor.get("entity_type", "")),
                        confidence=self._float(edge.get("confidence"), 0.0),
                        valid_source_count=int(edge.get("valid_source_count") or 0),
                        source_memory_ids=source_ids,
                    )
                )
                if len(knowledge) >= MAX_KNOWLEDGE:
                    return knowledge
        return knowledge

    def _neighbor_in_project(
        self,
        neighbor: Mapping[str, Any],
        *,
        project_id: str,
        selected_ids: set[str],
    ) -> bool:
        source_id = str(neighbor.get("source_memory_id", "") or "")
        if source_id and source_id in selected_ids:
            return True
        metadata = neighbor.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        # No entity-name marker here: a node label is derived text, not an
        # explicit memory-level project declaration.
        tagged = metadata_project_id(metadata, "")
        if tagged != project_id:
            return False
        return self._source_is_current(source_id)

    def _source_is_current(self, memory_id: str) -> bool:
        if not memory_id:
            return False
        read_memory = getattr(self.projection, "read_memory", None)
        if read_memory is None:
            return False
        try:
            row = read_memory(memory_id)
        except Exception:  # noqa: BLE001 - a failed re-read is not proof of validity
            return False
        if not row:
            return False
        if str(row.get("status", "")) != "ACTIVE":
            return False
        if row.get("forgotten_at"):
            return False
        return not _is_expired(row.get("expires_at"), datetime.now(timezone.utc))

    def _safe_label(self, node: Mapping[str, Any]) -> Optional[str]:
        """Use JKG's own read-time sanitizer, then MCE's stronger policy."""
        name = str(node.get("name", "") or "")
        if not name.strip():
            return None
        try:
            from ..jkg.governance import sanitize_node_payload

            sanitized = sanitize_node_payload(dict(node))
        except Exception:  # noqa: BLE001 - fail closed
            return None
        if sanitized.get("sensitivity") == "CREDENTIAL":
            return None
        safe = str(sanitized.get("name", "") or "")
        decision = self.governance.inspect_text(safe)
        if not decision.allowed:
            return None
        return content_preview(safe, 120)[0]

    # ------------------------------------------------------------------- caps
    def _item_cap(self, requested: Optional[int]) -> int:
        if requested is None:
            return self.config.max_items
        try:
            value = int(requested)
        except (TypeError, ValueError):
            return self.config.max_items
        return max(1, min(self.config.max_items, value))

    def _budget(self, requested: Optional[int]) -> int:
        if requested is None:
            return self.config.max_tokens
        try:
            value = int(requested)
        except (TypeError, ValueError):
            return self.config.max_tokens
        return max(128, min(self.config.max_tokens, value))

    def _apply_item_cap(
        self, snapshot: ProjectContextSnapshot, cap: int
    ) -> int:
        total = sum(
            len(value)
            for value in (
                [snapshot.stage] if snapshot.stage else [],
                snapshot.confirmed_decisions,
                snapshot.next_steps,
                snapshot.open_risks,
                snapshot.tentative_items,
                snapshot.completed_items,
                snapshot.background_items,
            )
        )
        dropped = 0
        # Trim from the least important bucket first. At least one confirmed
        # decision is preserved; if the numeric cap is still exceeded because
        # of stage + decision, the stage line goes before that decision.
        for attr in (
            "background_items",
            "completed_items",
            "tentative_items",
            "open_risks",
            "next_steps",
            "confirmed_decisions",
        ):
            if total <= cap:
                break
            values = getattr(snapshot, attr)
            minimum = 1 if attr == "confirmed_decisions" else 0
            while len(values) > minimum and total > cap:
                values.pop()
                total -= 1
                dropped += 1
        if total > cap and snapshot.stage is not None:
            snapshot.stage = None
            total -= 1
            dropped += 1
        return dropped

    @staticmethod
    def _current_items(snapshot: ProjectContextSnapshot) -> List[ContextItem]:
        items: List[ContextItem] = []
        if snapshot.stage is not None:
            items.append(snapshot.stage)
        items.extend(snapshot.confirmed_decisions)
        items.extend(snapshot.next_steps)
        items.extend(snapshot.open_risks)
        items.extend(snapshot.tentative_items)
        items.extend(snapshot.completed_items)
        items.extend(snapshot.background_items)
        return items

    def _build_sources(self, snapshot: ProjectContextSnapshot) -> None:
        sources: List[SourceRef] = []
        seen: set[str] = set()
        ordered: List[ContextItem] = []
        if snapshot.stage is not None and snapshot.stage.injected:
            ordered.append(snapshot.stage)
        ordered.extend(snapshot.confirmed_decisions)
        ordered.extend(snapshot.next_steps)
        ordered.extend(snapshot.open_risks)
        ordered.extend(snapshot.tentative_items)
        ordered.extend(snapshot.completed_items)
        ordered.extend(snapshot.background_items)
        for item in ordered:
            if not item.injected or item.memory_id in seen:
                continue
            seen.add(item.memory_id)
            sources.append(
                SourceRef(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    updated_at=item.updated_at,
                    injected=True,
                )
            )
        snapshot.sources = sources


__all__ = ["MceReconstructor"]
