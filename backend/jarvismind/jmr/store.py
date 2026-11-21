"""Persistence of JMR derived proposals.

Only the three EXISTING derived tables are written:
``memory_summaries`` / ``memory_corrections`` / ``memory_merges``.
``memory_records`` and ``memory_candidates`` are never touched (spec §14 #2).

All writes use ORM objects or parameter-bound SQL. ``metadata_json`` contains
the literal ``"table": "memory_records"`` as lineage text; binding it as a
PARAMETER keeps that literal out of the SQL statement text so the connection
gate's table-name classifier cannot mistake a derived-table INSERT for a write
to a protected table.

JMR-04 additions (no schema change):
  * TTL is evaluated from ``metadata_json.expires_at``;
  * ``generation`` increments only when a newer source watermark replaces an
    existing PENDING proposal;
  * a stale in-flight run (older source watermark) can never overwrite a newer
    proposal;
  * reviewed (APPROVED/CONFIRMED/REJECTED/RESOLVED) rows are never overwritten.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..database.models import MemoryCorrection, MemoryMerge, MemorySummary
from .provenance import RULES, rule_of

PENDING = "PENDING"
VOID = "VOID"

_TABLES: Tuple[Tuple[str, str], ...] = (
    ("summaries", "memory_summaries"),
    ("corrections", "memory_corrections"),
    ("merges", "memory_merges"),
)


def _corruption_reason(metadata_json: Any) -> str:
    """Diagnostic label only. Never repairs or rewrites the stored value."""
    if metadata_json is None or str(metadata_json).strip() == "":
        return "metadata_missing"
    try:
        payload = json.loads(metadata_json)
    except (ValueError, TypeError):
        return "metadata_json_unparseable"
    if not isinstance(payload, dict):
        return "metadata_not_object"
    return "rule_missing_or_unknown"


def _meta_dict(metadata_json: Any) -> Dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        payload = json.loads(metadata_json)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _with_generation(metadata_json: str, generation: int) -> str:
    payload = _meta_dict(metadata_json)
    payload["generation"] = int(generation)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _generation_of(metadata_json: Any) -> int:
    raw = _meta_dict(metadata_json).get("generation", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return 1
    return raw


def _watermark_key(value: str):
    """Sortable watermark: parsed UTC datetime when possible, else raw text."""
    parsed = _parse_iso(value)
    if parsed is not None:
        return (1, parsed.timestamp())
    return (0, value)


def _watermark_of(metadata_json: Any) -> str:
    return str(_meta_dict(metadata_json).get("source_watermark") or "")


def _fingerprint_of(metadata_json: Any) -> str:
    return str(_meta_dict(metadata_json).get("source_fingerprint") or "")


def _accepts_replacement(existing_meta: Any, incoming_meta: str) -> Tuple[bool, int]:
    """Decide whether an incoming proposal may replace an existing PENDING row.

    Policy:
      * newer source watermark  -> replace, generation + 1;
      * same watermark + higher generation -> replace (explicit rebuild);
      * same watermark + different fingerprint -> replace, generation + 1;
      * same watermark + same/lower generation -> skip (idempotent);
      * older watermark -> skip (a stale in-flight run must not win).
    """
    existing_watermark = _watermark_of(existing_meta)
    incoming_watermark = _watermark_of(incoming_meta)
    existing_generation = _generation_of(existing_meta)
    incoming_generation = _generation_of(incoming_meta)
    if incoming_watermark and existing_watermark:
        incoming_key = _watermark_key(incoming_watermark)
        existing_key = _watermark_key(existing_watermark)
        if incoming_key < existing_key:
            return False, existing_generation
        if incoming_key > existing_key:
            return True, existing_generation + 1
    if (
        _fingerprint_of(existing_meta)
        and _fingerprint_of(incoming_meta)
        and _fingerprint_of(existing_meta) != _fingerprint_of(incoming_meta)
        and incoming_generation >= existing_generation
    ):
        return True, existing_generation + 1
    if incoming_generation > existing_generation:
        return True, incoming_generation
    return False, existing_generation


class DerivedStore:
    def __init__(self, manager) -> None:
        self.manager = manager

    # ------------------------------------------------------------------ upsert
    def upsert_summary(
        self,
        *,
        proposal_id: str,
        title: str,
        content: str,
        summary_type: str,
        memory_type: str | None,
        source_memory_ids: Sequence[str],
        source_ids: Sequence[str],
        metadata_json: str,
        now: str,
    ) -> bool:
        try:
            with self.manager.session() as session:
                row = session.get(MemorySummary, proposal_id)
                if row is None:
                    session.add(
                        MemorySummary(
                            id=proposal_id,
                            title=title,
                            content=content,
                            summary_type=summary_type,
                            memory_type=memory_type,
                            memory_ids_json=json.dumps(
                                list(source_memory_ids), ensure_ascii=False
                            ),
                            source_ids_json=json.dumps(
                                list(source_ids), ensure_ascii=False
                            ),
                            status=PENDING,
                            confidence=0.6,
                            engine="local",
                            created_at=now,
                            updated_at=now,
                            metadata_json=metadata_json,
                        )
                    )
                    return True
                if row.status != PENDING and row.status != VOID:
                    return False
                if row.status == PENDING:
                    allowed, generation = _accepts_replacement(
                        row.metadata_json, metadata_json
                    )
                    if not allowed:
                        return False
                    metadata_json = _with_generation(metadata_json, generation)
                else:
                    metadata_json = _with_generation(
                        metadata_json, _generation_of(row.metadata_json) + 1
                    )
                row.status = PENDING
                row.title = title
                row.content = content
                row.summary_type = summary_type
                row.memory_type = memory_type
                row.memory_ids_json = json.dumps(
                    list(source_memory_ids), ensure_ascii=False
                )
                row.source_ids_json = json.dumps(list(source_ids), ensure_ascii=False)
                row.metadata_json = metadata_json
                row.updated_at = now
                row.confirmed_at = None
                row.rejected_at = None
                return True
        except IntegrityError:
            # Concurrent insert of the same deterministic id: the other writer
            # won. This is an idempotent skip, not a data error.
            return False

    def upsert_correction(
        self,
        *,
        proposal_id: str,
        memory_id_a: str,
        memory_id_b: str,
        reason: str,
        before_state_json: str,
        after_state_json: str,
        metadata_json: str,
        now: str,
    ) -> bool:
        try:
            with self.manager.session() as session:
                row = session.get(MemoryCorrection, proposal_id)
                if row is None:
                    session.add(
                        MemoryCorrection(
                            id=proposal_id,
                            memory_id_a=memory_id_a,
                            memory_id_b=memory_id_b,
                            detected_at=now,
                            status=PENDING,
                            reason=reason,
                            source="JMR",
                            created_at=now,
                            before_state_json=before_state_json,
                            after_state_json=after_state_json,
                            metadata_json=metadata_json,
                        )
                    )
                    return True
                if row.status != PENDING and row.status != VOID:
                    return False
                if row.status == PENDING:
                    allowed, generation = _accepts_replacement(
                        row.metadata_json, metadata_json
                    )
                    if not allowed:
                        return False
                    metadata_json = _with_generation(metadata_json, generation)
                else:
                    metadata_json = _with_generation(
                        metadata_json, _generation_of(row.metadata_json) + 1
                    )
                row.status = PENDING
                row.memory_id_a = memory_id_a
                row.memory_id_b = memory_id_b
                row.reason = reason
                row.before_state_json = before_state_json
                row.after_state_json = after_state_json
                row.metadata_json = metadata_json
                row.detected_at = now
                row.resolved_at = None
                row.chosen_memory_id = None
                row.corrected_content = None
                return True
        except IntegrityError:
            return False

    def upsert_merge(
        self,
        *,
        proposal_id: str,
        source_memory_ids: Sequence[str],
        new_content: str,
        new_summary: str,
        reason: str,
        metadata_json: str,
        now: str,
    ) -> bool:
        try:
            with self.manager.session() as session:
                row = session.get(MemoryMerge, proposal_id)
                merged_json = json.dumps(list(source_memory_ids), ensure_ascii=False)
                if row is None:
                    session.add(
                        MemoryMerge(
                            id=proposal_id,
                            status=PENDING,
                            merged_memory_ids_json=merged_json,
                            new_content=new_content,
                            new_summary=new_summary,
                            reason=reason,
                            created_at=now,
                            updated_at=now,
                            metadata_json=metadata_json,
                        )
                    )
                    return True
                if row.status != PENDING and row.status != VOID:
                    return False
                if row.status == PENDING:
                    allowed, generation = _accepts_replacement(
                        row.metadata_json, metadata_json
                    )
                    if not allowed:
                        return False
                    metadata_json = _with_generation(metadata_json, generation)
                else:
                    metadata_json = _with_generation(
                        metadata_json, _generation_of(row.metadata_json) + 1
                    )
                row.status = PENDING
                row.merged_memory_ids_json = merged_json
                row.new_content = new_content
                row.new_summary = new_summary
                row.reason = reason
                row.metadata_json = metadata_json
                row.updated_at = now
                row.resolved_at = None
                row.target_memory_id = None
                return True
        except IntegrityError:
            return False

    # ------------------------------------------------------------------ query
    def list_derived(self, *, rule: str = "") -> Dict[str, List[Dict[str, Any]]]:
        """Return this module's derived rows, grouped by physical table.

        Rows whose ``metadata_json`` cannot be parsed, or carries no known
        rule, are NEVER dropped and NEVER classified as a valid rule. They are
        collected under the separate ``corrupt`` key, tagged with
        ``_jmr_corrupt`` and the reason, so audit queries can see them.

        The three original keys keep their exact previous semantics: they hold
        only rows whose rule matches the filter. ``corrupt`` is returned for
        every call and is not affected by ``rule`` (it is never a valid rule).
        """
        out: Dict[str, List[Dict[str, Any]]] = {
            "summaries": [],
            "corrections": [],
            "merges": [],
            "corrupt": [],
        }
        allowed = (rule,) if rule else RULES
        with self.manager.session() as session:
            for key, table in _TABLES:
                rows = session.execute(
                    text(f"SELECT * FROM {table}")
                ).mappings().all()
                for mapped in rows:
                    item = dict(mapped)
                    found = rule_of(item.get("metadata_json"))
                    if found in allowed:
                        out[key].append(item)
                        continue
                    if not found:
                        # Unparseable JSON or missing rule: keep the row visible
                        # as corrupt. Never coerce it into a valid rule bucket.
                        flagged = dict(item)
                        flagged["_jmr_corrupt"] = _corruption_reason(
                            item.get("metadata_json")
                        )
                        flagged["_jmr_source_table"] = table
                        out["corrupt"].append(flagged)
        return out

    def get_derived(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one derived row regardless of physical table."""
        with self.manager.session() as session:
            for _key, table in _TABLES:
                row = session.execute(
                    text(f"SELECT * FROM {table} WHERE id=:id"), {"id": proposal_id}
                ).mappings().first()
                if row is not None:
                    item = dict(row)
                    item["_jmr_source_table"] = table
                    return item
        return None

    def list_expired(self, *, now_iso: str) -> List[Dict[str, Any]]:
        """PENDING proposals whose TTL has elapsed. Never returns approved rows."""
        now = _parse_iso(now_iso)
        if now is None:
            raise ValueError("now_iso must be an ISO-8601 timestamp")
        out: List[Dict[str, Any]] = []
        with self.manager.session() as session:
            for _key, table in _TABLES:
                rows = session.execute(
                    text(
                        f"SELECT id, status, metadata_json, created_at FROM {table} "
                        "WHERE status=:s"
                    ),
                    {"s": PENDING},
                ).mappings().all()
                for row in rows:
                    meta = _meta_dict(row["metadata_json"])
                    expires = _parse_iso(meta.get("expires_at"))
                    if expires is None or expires > now:
                        continue
                    item = dict(row)
                    item["_jmr_source_table"] = table
                    out.append(item)
        return out

    def void_expired(self, *, now_iso: str) -> int:
        """Logically invalidate expired PENDING proposals. Never DELETE."""
        expired = self.list_expired(now_iso=now_iso)
        changed = 0
        with self.manager.session() as session:
            for item in expired:
                table = item["_jmr_source_table"]
                meta = _meta_dict(item.get("metadata_json"))
                meta["review"] = {
                    "status": "VOID",
                    "authoritative": False,
                    "source": "ttl_expired",
                    "reviewed_at": None,
                }
                meta["invalidated_reason"] = "ttl_expired"
                session.execute(
                    text(
                        f"UPDATE {table} SET status=:s, metadata_json=:m WHERE id=:i"
                    ),
                    {
                        "s": VOID,
                        "m": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                        "i": item["id"],
                    },
                )
                changed += 1
        return changed

    def mark_void(self, proposal_id: str, *, reason: str) -> bool:
        row = self.get_derived(proposal_id)
        if row is None:
            return False
        table = row["_jmr_source_table"]
        meta = _meta_dict(row.get("metadata_json"))
        meta["invalidated_reason"] = reason
        meta["review"] = {
            "status": "VOID",
            "authoritative": False,
            "source": "server_invalidation",
            "reviewed_at": None,
        }
        with self.manager.session() as session:
            session.execute(
                text(f"UPDATE {table} SET status=:s, metadata_json=:m WHERE id=:i"),
                {
                    "s": VOID,
                    "m": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    "i": proposal_id,
                },
            )
        return True

    def set_review(
        self, proposal_id: str, review: Dict[str, Any], *, status: str = ""
    ) -> bool:
        """Attach the review snapshot. Does not create or rewrite authoritative data."""
        row = self.get_derived(proposal_id)
        if row is None:
            return False
        table = row["_jmr_source_table"]
        meta = _meta_dict(row.get("metadata_json"))
        meta["review"] = dict(review)
        assignments = ["metadata_json=:m"]
        params: Dict[str, Any] = {
            "m": json.dumps(meta, ensure_ascii=False, sort_keys=True),
            "i": proposal_id,
        }
        if status:
            assignments.insert(0, "status=:s")
            params["s"] = status
        with self.manager.session() as session:
            session.execute(
                text(f"UPDATE {table} SET {', '.join(assignments)} WHERE id=:i"),
                params,
            )
        return True

    def void_by_source_memory(self, memory_id: str, *, session=None) -> int:
        """Logically invalidate derived rows that reference ``memory_id``.

        JMR-08 audit P1-8 / re-audit N-C1: a forgotten source must not leave a
        usable derived copy behind. Never DELETEs; status becomes VOID with an
        explicit reason.

        ``session`` must be the caller's open transaction when called from the
        forget approval: opening a second session there deadlocks SQLite and
        makes forget impossible.
        """
        if not isinstance(memory_id, str) or not memory_id:
            return 0
        if session is not None:
            return self._void_by_source_memory_in(session, memory_id)
        with self.manager.session() as own_session:
            return self._void_by_source_memory_in(own_session, memory_id)

    @staticmethod
    def _void_by_source_memory_in(session, memory_id: str) -> int:
        changed = 0
        if True:  # keep the indentation of the original loop body
            for _key, table in _TABLES:
                rows = session.execute(
                    text(f"SELECT id, metadata_json FROM {table}")
                ).all()
                for row_id, metadata_json in rows:
                    meta = _meta_dict(metadata_json)
                    sources = meta.get("source_memory_ids") or []
                    if memory_id not in sources:
                        continue
                    meta["void_reason"] = "source_forgotten"
                    session.execute(
                        text(
                            f"UPDATE {table} SET status=:s, metadata_json=:m "
                            "WHERE id=:i"
                        ),
                        {
                            "s": VOID,
                            "m": json.dumps(meta, ensure_ascii=False, sort_keys=True),
                            "i": row_id,
                        },
                    )
                    changed += 1
        return changed

    def void_derived(self, *, rule: str = "") -> int:
        """Logical invalidation only. Never DELETE, never DROP (spec §2:54)."""
        allowed = (rule,) if rule else RULES
        changed = 0
        with self.manager.session() as session:
            for _key, table in _TABLES:
                rows = session.execute(
                    text(f"SELECT id, metadata_json FROM {table}")
                ).all()
                for row_id, metadata_json in rows:
                    if rule_of(metadata_json) not in allowed:
                        continue
                    session.execute(
                        text(f"UPDATE {table} SET status=:s WHERE id=:i"),
                        {"s": VOID, "i": row_id},
                    )
                    changed += 1
        return changed
