"""Read-only query facade over JMR derived rows (JMR-02 core, no HTTP).

Reads only the three EXISTING derived tables through ``DerivedStore``. Never
writes ``memory_records`` / ``memory_candidates``. Never calls review,
resolve, confirm or any governance write path (spec §14 #2; JMR-02 §5).

Failure modes are explicit and never degrade to fake success:
  * ``JMRUnavailableError(code="p1_schema_missing")`` when ``p1_tasks`` /
    ``p1_task_events`` are absent (JMR-R-07).
  * ``governance_active`` is reported as a fact, not assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from ..p1_gate import is_active as governance_is_active
from ..governance.schema import P1_TABLES
from ..schemas.jmr import ProposalDetail, ProposalItem
from ..core.timeutils import utc_now_iso
from .config import JMRConfig
from .errors import JMRNotFoundError, JMRUnavailableError
from .provenance import RULES
from .store import DerivedStore


_SNAPSHOT_KEYS = ("before_state_json", "after_state_json")


def _validated(item: Dict[str, Any]) -> Dict[str, Any]:
    """Run the LIST response model over a projected row.

    List items deliberately omit the before/after snapshots (design §7.2: the
    snapshots belong to the detail view). They are removed here and re-attached
    only by ``get_proposal``. A row that violates the contract raises
    ``pydantic.ValidationError``; it is never silently dropped.
    """
    list_view = {k: v for k, v in item.items() if k not in _SNAPSHOT_KEYS}
    return ProposalItem.model_validate(list_view).model_dump()

STATUSES = ("PENDING", "APPROVED", "REJECTED", "VOID")
CONFLICT_CLASSES = ("FACT_CONFLICT", "TIME_UPDATE", "WORDING_DIFF", "INSUFFICIENT_INFO")

#: The shared lifecycle tables use their own terminal words. The public JMR
#: proposal contract has four states; map the JME words onto it without
#: rewriting the row itself.
_RAW_STATUS_TO_PROPOSAL = {
    "CONFIRMED": "APPROVED",
    "RESOLVED": "APPROVED",
}


@dataclass(frozen=True)
class QueryPage:
    items: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class JMRStatus:
    governance_active: bool
    p1_schema_installed: bool
    p1_tables_present: List[str]
    algorithm_version: str
    llm_online: bool = False  # 首期恒 False（架构设计 §6 / JMR-02 schema）
    notes: List[str] = field(default_factory=list)
    expired_pending: int = 0
    breaker_tripped: bool = False
    automatic_execution_authorized: bool = False
    daily_spend_cny: float = 0.0
    monthly_spend_cny: float = 0.0
    daily_budget_cny: float = 0.0
    monthly_budget_cny: float = 0.0


def _as_list(raw: Optional[str]) -> List[str]:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _as_dict(raw: Optional[str]) -> Dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> Optional[str]:
    """Empty-value rule: a missing value is ``None``, never ``""``.

    ``0`` and ``False`` are real values and are preserved (the earlier
    ``value or ""`` pattern silently erased them).
    """
    if value is None:
        return None
    text_value = str(value)
    return text_value if text_value != "" else None


def _float_or_none(value: Any) -> Optional[float]:
    """Strict numeric conversion. A non-numeric value is an explicit error."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("confidence must be numeric, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"confidence must be numeric, got {type(value).__name__}")


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"generation must be an integer, got {type(value).__name__}")
    return value


def _review_snapshot(meta: Dict[str, Any], status: str) -> Dict[str, Any]:
    """Whitelist the stored review state.

    Only the status word survives, and only when it is a known value. Any
    ``actor`` / principal / other key is discarded: storage is not a source of
    approval identity, so no client-written value can become authoritative.
    """
    stored = meta.get("review")
    stored_status = stored.get("status") if isinstance(stored, dict) else None
    chosen = stored_status if stored_status in STATUSES else status
    return {"status": chosen, "authoritative": False, "source": "stored_snapshot"}


def normalise_proposal(row: Dict[str, Any], *, source_table: str) -> Dict[str, Any]:
    """Project a trusted (parseable) derived row onto the public proposal shape.

    Keys come from ``metadata_json`` (JMR-04 contract) and the row itself.
    Missing values are explicit ``None``, never invented data. Corrupt rows are
    handled by ``normalise_corrupt`` and are never passed here.
    """
    meta = _as_dict(row.get("metadata_json"))
    source_ids = meta.get("source_memory_ids") or _as_list(
        row.get("memory_ids_json") or row.get("merged_memory_ids_json")
    )
    status = str(row.get("status") or "")
    proposal_status = _RAW_STATUS_TO_PROPOSAL.get(status, status)
    rule = str(meta.get("rule") or "")
    conflict_class = _text_or_none(meta.get("conflict_class"))
    return {
        "proposal_id": _text_or_none(row.get("id")) or "",
        "rule": rule,
        "status": proposal_status,
        "source_table": source_table,
        "source_memory_ids": [str(item) for item in source_ids],
        "lineage": meta.get("lineage") or [],
        "task_id": _text_or_none(meta.get("task_id")),
        "run_id": _text_or_none(meta.get("run_id")),
        "algorithm_version": _text_or_none(meta.get("algorithm_version")),
        "projection_digest": _text_or_none(meta.get("projection_digest")),
        "confidence": _float_or_none(row.get("confidence")),
        "claim_type": _text_or_none(meta.get("claim_type")),
        "sensitivity": _text_or_none(meta.get("sensitivity")),
        "conflict_class": conflict_class if rule == "CONFLICT" else None,
        "generation": _int_or_none(meta.get("generation")),
        "evidence": meta.get("evidence") if isinstance(meta.get("evidence"), list) else [],
        "source_fingerprint": _text_or_none(meta.get("source_fingerprint")),
        "source_watermark": _text_or_none(meta.get("source_watermark")),
        "before_state_json": _text_or_none(row.get("before_state_json")),
        "after_state_json": _text_or_none(row.get("after_state_json")),
        # Strictly the creation time. ``detected_at`` is a different event and
        # must not be presented as created_at.
        "created_at": _text_or_none(row.get("created_at")),
        "expires_at": _text_or_none(meta.get("expires_at")),
        "review": _review_snapshot(meta, proposal_status),
        "corrupt_reason": None,
    }


def normalise_corrupt(row: Dict[str, Any], *, source_table: str) -> Dict[str, Any]:
    """Visible projection of an untrusted row. It is labelled, never classified.

    The row keeps its id, status and source table so an auditor can locate it.
    ``rule`` is the diagnostic label ``CORRUPT``, which is not a valid rule and
    is rejected by the response model unless ``corrupt_reason`` is present.
    The stored value itself is never repaired or rewritten.
    """
    status = str(row.get("status") or "")
    proposal_status = _RAW_STATUS_TO_PROPOSAL.get(status, status)
    return {
        "proposal_id": _text_or_none(row.get("id")) or "",
        "rule": "CORRUPT",
        "status": proposal_status,
        "source_table": source_table,
        "source_memory_ids": [],
        "lineage": [],
        "task_id": None,
        "run_id": None,
        "algorithm_version": None,
        "projection_digest": None,
        "confidence": None,
        "claim_type": None,
        "sensitivity": None,
        "conflict_class": None,
        "generation": None,
        "evidence": [],
        "source_fingerprint": None,
        "source_watermark": None,
        "before_state_json": None,
        "after_state_json": None,
        "created_at": _text_or_none(row.get("created_at")),
        "expires_at": None,
        "review": {"status": proposal_status, "authoritative": False, "source": "stored_snapshot"},
        "corrupt_reason": str(row.get("_jmr_corrupt") or "unknown"),
    }


class JMRQueryService:
    """Read-only facade. Constructed with the JME manager only."""

    def __init__(self, manager, config: Optional[JMRConfig] = None) -> None:
        self.manager = manager
        self.config = config or JMRConfig()
        self.store = DerivedStore(manager)

    # ----------------------------------------------------------- preconditions
    def _require_p1_schema(self) -> None:
        present = self._p1_tables_present()
        missing = [name for name in P1_TABLES if name not in present]
        if missing:
            raise JMRUnavailableError(
                "P1 task tables are not installed; /jmr endpoints require "
                f"them (missing: {', '.join(missing)})",
                detail={"code": "p1_schema_missing", "missing": missing},
            )

    def _p1_tables_present(self) -> List[str]:
        with self.manager.session() as session:
            names = {
                row[0]
                for row in session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        return [name for name in P1_TABLES if name in names]

    # ------------------------------------------------------------------ status
    def _breaker_tripped(self) -> bool:
        import json

        with self.manager.session() as session:
            raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"),
                {"k": "jmr.breaker"},
            ).scalar()
        if not raw:
            return False
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return False
        return bool(payload.get("tripped")) if isinstance(payload, dict) else False

    def _spend(self, key: str) -> float:
        import json

        with self.manager.session() as session:
            raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
            ).scalar()
        if raw is None or raw == "":
            return 0.0
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return 0.0
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def status(self) -> JMRStatus:
        present = self._p1_tables_present()
        installed = all(name in present for name in P1_TABLES)
        notes: List[str] = []
        if not installed:
            notes.append("p1_schema_missing")
        expired = len(self.store.list_expired(now_iso=utc_now_iso()))
        if expired:
            notes.append(f"expired_pending:{expired}")
        now = utc_now_iso()
        daily_spend = self._spend("p1.cost.daily." + now[:10])
        monthly_spend = self._spend("p1.cost.monthly." + now[:7])
        return JMRStatus(
            governance_active=governance_is_active(),
            p1_schema_installed=installed,
            p1_tables_present=present,
            algorithm_version=self.config.algorithm_version,
            notes=notes,
            expired_pending=expired,
            breaker_tripped=self._breaker_tripped(),
            daily_spend_cny=daily_spend,
            monthly_spend_cny=monthly_spend,
            daily_budget_cny=float(self.config.daily_budget_cny),
            monthly_budget_cny=float(self.config.monthly_budget_cny),
        )

    # ---------------------------------------------------------------- proposals
    def _collect_projected(self, rule: str = "", status: str = "") -> List[Dict[str, Any]]:
        """Projected (unvalidated, snapshot-bearing) items, sorted, filtered.

        Corrupt rows are included when no rule filter is given (visible to
        unfiltered queries and counted in ``total``). They never match a
        specific valid-rule filter.
        """
        if rule and rule not in RULES:
            raise ValueError(f"unknown rule: {rule}")
        if status and status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        rows = self.store.list_derived(rule=rule)
        out: List[Dict[str, Any]] = []
        for key, table in (
            ("summaries", "memory_summaries"),
            ("corrections", "memory_corrections"),
            ("merges", "memory_merges"),
        ):
            for row in rows[key]:
                item = normalise_proposal(row, source_table=table)
                if status and item["status"] != status:
                    continue
                out.append(item)
        if not rule:
            for row in rows.get("corrupt", []):
                item = normalise_corrupt(row, source_table=row.get("_jmr_source_table", ""))
                if status and item["status"] != status:
                    continue
                out.append(item)
        out.sort(key=lambda item: (item["created_at"] or "", item["proposal_id"]))
        return out

    def _collect(self, rule: str = "", status: str = "") -> List[Dict[str, Any]]:
        """List-view items. Every item is validated; a violation raises."""
        return [_validated(item) for item in self._collect_projected(rule=rule, status=status)]

    def list_proposals(
        self, *, rule: str = "", status: str = "", page: int = 1, page_size: int = 20
    ) -> QueryPage:
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("page must be >= 1 and page_size in [1, 100]")
        self._require_p1_schema()
        items = self._collect(rule=rule, status=status)
        start = (page - 1) * page_size
        return QueryPage(
            items=items[start : start + page_size],
            total=len(items),
            page=page,
            page_size=page_size,
        )

    def get_proposal(self, proposal_id: str) -> Dict[str, Any]:
        self._require_p1_schema()
        for item in self._collect_projected():
            if item["proposal_id"] == proposal_id:
                # Detail view keeps the before/after snapshots and is validated
                # against ProposalDetail. Validation failure raises.
                return ProposalDetail.model_validate(item).model_dump()
        raise JMRNotFoundError(f"proposal {proposal_id!r} not found")

    def list_conflicts(
        self, *, conflict_class: str = "", page: int = 1, page_size: int = 20
    ) -> QueryPage:
        if conflict_class and conflict_class not in CONFLICT_CLASSES:
            raise ValueError(f"unknown conflict_class: {conflict_class}")
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("page must be >= 1 and page_size in [1, 100]")
        self._require_p1_schema()
        items = [
            item
            for item in self._collect(rule="CONFLICT", status="PENDING")
            if not conflict_class or item["conflict_class"] == conflict_class
        ]
        start = (page - 1) * page_size
        return QueryPage(
            items=items[start : start + page_size],
            total=len(items),
            page=page,
            page_size=page_size,
        )
