"""Idempotent ingestion of one JME memory into the JKG derived graph (JKG-INT-05 / A1).

This module is the write core only. It holds no orchestration (no task table,
no batching, no HTTP). A later task agent calls ``ingest_memory`` per memory.

Pipeline for one memory
-----------------------
1. Read the source through ``ReadOnlyProjection.for_module("JKG")`` (mode=ro).
2. Eligibility: missing -> skipped NOT_FOUND; status outside ACTIVE/ARCHIVED ->
   skipped SOURCE_NOT_ELIGIBLE; ``expires_at`` in the past -> skipped EXPIRED;
   unparseable ``expires_at`` -> skipped INVALID_EXPIRY. Nothing is written.
3. Governance: content, summary and metadata are each graded with
   ``jmr.sensitive.evaluate`` (plus any caller-supplied exact tokens) and the
   verdicts are merged with ``combine``. Any non-ALLOW verdict, or a
   personal-sensitivity category in content or summary, skips the memory.
4. Extraction: the existing ``extract_candidates`` rules, unchanged. Every
   accepted node candidate is written, including one with no edge. Nothing is
   written when there are neither nodes nor edges.
5. Writes go only through ``JkgService`` (``upsert_node`` / ``create_edge``).
   Node reuse checks the exact ``normalized_key`` first, then the secondary
   ``jkg.match_key`` metadata. The write path never touches ``memory_records``,
   ``memory_candidates`` or ``memory_audit_logs``.

Idempotency: re-ingesting the same memory finds every node by its exact key and
every edge by its triple, and adds no source row that already exists, so the
counts of nodes, edges and sources stay unchanged.

Failures: only ``JkgError`` and ``ValidationError`` are caught, and they become
``failed`` with the stable ``code`` of the exception. Other exceptions,
including ``KeyboardInterrupt`` and ``SystemExit``, propagate. No exception text,
body or metadata is returned or logged.

Writes are one transaction per node or edge, not one for the whole memory. A
failure midway can therefore leave earlier nodes or edges in place. A retry
completes the work without duplicating anything, because of the idempotency
rule above.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Literal, Mapping, Optional, Tuple

from ..core import personal_sensitivity
from ..core.errors import ValidationError
from ..governance.projection import ReadOnlyProjection
from ..jmr.sensitive import combine, evaluate
from .adapter import JkgCompositionAdapter
from .errors import JkgError
from .extraction import (
    REASON_PERSONAL_SENSITIVE_CATEGORY,
    RULE_VERSION,
    extract_candidates,
)
from .matching import entity_match_key
from .normalization import VALID_SOURCE_STATUSES, deserialize_metadata
from .repository import JkgRepository
from .service import CONTENT_STALE_REASON

_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class IngestOutcome:
    status: Literal["processed", "skipped", "failed"]
    reason: str
    nodes_added: int
    edges_added: int
    sources_added: int
    nodes_matched: int
    rejections: int


def _safe_code(value: Any, fallback: str) -> str:
    text = str(value or "")
    return text if _SAFE_CODE.match(text) else fallback


def _skipped(reason: str) -> IngestOutcome:
    return IngestOutcome("skipped", reason, 0, 0, 0, 0, 0)


def _failed(code: str) -> IngestOutcome:
    return IngestOutcome("failed", code, 0, 0, 0, 0, 0)


#: Reasons meaning "governance refused this memory", as opposed to a plain skip.
GOVERNANCE_REASONS = frozenset(
    {
        "CREDENTIAL_PRESENT",
        "SENSITIVE_DUAL_AUTH_REQUIRED",
        "PERSONAL_SENSITIVE_CATEGORY",
        "PROMPT_INJECTION_SUSPECTED",
        "DETECTION_FAILED",
        "PRIVACY_NOT_ALLOWED",
    }
)
GOVERNANCE_PREFIXES = ("GOVERNANCE_", "SENSITIVE_")


def is_governance_reason(reason: str) -> bool:
    return reason in GOVERNANCE_REASONS or reason.startswith(GOVERNANCE_PREFIXES)


class JkgIngestor:
    """Extract one memory and write its candidates into the derived graph."""

    def __init__(
        self,
        manager,
        *,
        adapter: JkgCompositionAdapter,
        exact_tokens: Optional[Callable[[], Iterable[str]]] = None,
    ) -> None:
        self._manager = manager
        self._adapter = adapter
        self._exact_tokens = exact_tokens
        self._projection = ReadOnlyProjection(manager).for_module("JKG")

    # ------------------------------------------------------------ eligibility
    @staticmethod
    def _expiry_reason(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        try:
            expires = datetime.fromisoformat(str(value))
        except ValueError:
            return "INVALID_EXPIRY"
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return "EXPIRED"
        return None

    def _tokens(self) -> Tuple[str, ...]:
        if self._exact_tokens is None:
            return ()
        return tuple(str(token) for token in self._exact_tokens() if token)

    # ------------------------------------------------------------- governance
    @staticmethod
    def _governance_reason(row: Mapping[str, Any], tokens: Tuple[str, ...]) -> Optional[str]:
        content = str(row.get("content") or "")
        summary = str(row.get("summary") or "")
        metadata_json = str(row.get("metadata_json") or "")
        decision = combine(
            evaluate(content, exact_tokens=tokens),
            evaluate(summary, exact_tokens=tokens),
            evaluate(metadata_json, exact_tokens=tokens),
        )
        if not decision.may_auto_proceed:
            return _safe_code(decision.reason_code, "PRIVACY_NOT_ALLOWED")
        if personal_sensitivity.categories(content) or personal_sensitivity.categories(summary):
            return REASON_PERSONAL_SENSITIVE_CATEGORY
        return None

    # ------------------------------------------------------------ node lookup
    def _existing_node_id(
        self, entity_type: str, normalized_key: str, match_key: str
    ) -> Optional[str]:
        with self._manager.session() as session:
            repo = JkgRepository(session)
            node = repo.find_node(entity_type, normalized_key)
            if node is None:
                node = repo.find_node_by_match_key(entity_type, match_key)
            return node.node_id if node is not None else None

    # ----------------------------------------------------------------- writes
    def _write(
        self,
        service,
        *,
        memory_id: str,
        observed_at: str,
        result,
        init_task_id: Optional[str],
    ) -> IngestOutcome:
        nodes_added = 0
        nodes_matched = 0
        edges_added = 0
        sources_added = 0

        candidates = {(node.entity_type, node.normalized_key): node for node in result.nodes}

        node_ids: Dict[Tuple[str, str], str] = {}
        for entity_type, normalized_key in sorted(candidates):
            candidate = candidates[(entity_type, normalized_key)]
            match_key = entity_match_key(entity_type, candidate.name)
            existing_id = self._existing_node_id(entity_type, normalized_key, match_key)
            if existing_id is not None:
                node_ids[(entity_type, normalized_key)] = existing_id
                nodes_matched += 1
                continue
            payload = service.upsert_node(
                entity_type=entity_type,
                name=candidate.name,
                source_memory_id=memory_id,
                metadata={
                    "jkg": {
                        "match_key": match_key,
                        "rule_version": RULE_VERSION,
                        "init_task_id": init_task_id,
                    }
                },
            )
            node_ids[(entity_type, normalized_key)] = payload["node_id"]
            if payload.get("created"):
                nodes_added += 1
            else:
                nodes_matched += 1

        for edge in result.edges:
            payload = service.create_edge(
                subject_id=node_ids[(edge.subject_type, edge.subject_key)],
                predicate=edge.predicate,
                object_id=node_ids[(edge.object_type, edge.object_key)],
                source_memory_ids=[memory_id],
                confidence=edge.confidence,
                metadata={
                    "jkg": {
                        "rule_version": RULE_VERSION,
                        "rule": edge.evidence.rule,
                        "observed_at": observed_at,
                        "init_task_id": init_task_id,
                    }
                },
                reactivate_reasons=(CONTENT_STALE_REASON,),
            )
            if payload.get("created"):
                edges_added += 1
            sources_added += len(payload.get("sources_added") or [])

        return IngestOutcome(
            "processed",
            "INGESTED",
            nodes_added,
            edges_added,
            sources_added,
            nodes_matched,
            len(result.rejections),
        )

    # ------------------------------------------------------------------ entry
    def ingest_memory(
        self, memory_id: str, *, init_task_id: Optional[str] = None
    ) -> IngestOutcome:
        if not isinstance(memory_id, str) or not memory_id.strip():
            return _failed("INVALID_MEMORY_ID")
        memory_id = memory_id.strip()

        row = self._projection.read_memory(memory_id)
        if row is None:
            return _skipped("NOT_FOUND")
        status = str(row.get("status") or "")
        if status not in VALID_SOURCE_STATUSES:
            return _skipped("SOURCE_NOT_ELIGIBLE")
        expiry = self._expiry_reason(row.get("expires_at"))
        if expiry is not None:
            return _skipped(expiry)

        try:
            tokens = self._tokens()
        except Exception:  # noqa: BLE001 - fail closed, no exception text kept
            return _failed("EXACT_TOKENS_UNAVAILABLE")

        governance = self._governance_reason(row, tokens)
        if governance is not None:
            return _skipped(governance)

        result = extract_candidates(
            str(row.get("content") or ""),
            memory_id=memory_id,
            metadata=deserialize_metadata(row.get("metadata_json")),
            memory_status=status,
            exact_tokens=tokens,
        )
        if not result.eligible:
            first = result.rejections[0].reason_code if result.rejections else ""
            return _skipped(_safe_code(first, "NOT_ELIGIBLE"))
        if not result.nodes and not result.edges:
            return _skipped("NO_CANDIDATES")

        observed_at = str(row.get("updated_at") or row.get("created_at") or "")
        try:
            service = self._adapter.write_service()
            return self._write(
                service,
                memory_id=memory_id,
                observed_at=observed_at,
                result=result,
                init_task_id=init_task_id,
            )
        except (JkgError, ValidationError) as exc:
            return _failed(_safe_code(getattr(exc, "code", None), "JKG_WRITE_FAILED"))


__all__ = ["IngestOutcome", "JkgIngestor"]
