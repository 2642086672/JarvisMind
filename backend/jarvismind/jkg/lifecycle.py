"""JKG lifecycle consumer: pull JME lifecycle events, reconcile the graph (INT-04).

How it is driven
----------------
There is no daemon here. ``JkgLifecycleSync`` holds no thread, no timer and no
background task: every entry point is an ordinary call, and the only production
callers are the two admin HTTP routes. If nobody ticks, nothing happens — which
is the property that makes the whole design auditable.

Why JME does not notify JKG
---------------------------
JME core may not import JKG (composition boundary), so the dependency points one
way: **this module reads JME, JME never calls it**. The facts arrive through
``memory_audit_logs``, read with the read-only projection (``mode=ro`` over an
opaque token), never through ``manager.session()`` — a writable connection into
the authority database would defeat the read-only boundary that ``projection.py``
exists to enforce.

What a tick does
----------------
1. Resolve the switch and schema. Not ready / disabled ⇒ report and return,
   without reading or writing anything.
2. Read the cursor from ``system_settings`` (``jkg.sync.*`` only).
3. Page the audit feed from ``cursor.created_at - overlap`` so a row committed
   while the previous batch was being processed is not skipped. When a burst
   fills that whole overlap page with rows at or below the stored cursor, the
   tick keeps reading forward from the strict cursor in the same run (A-1),
   so the cursor can never stall on a re-read of already-consumed history.
4. Reduce entries to one action set per ``memory_id``.
5. Per memory: ``sync_memory`` (the idempotent floor), plus a staleness pass for
   content-changing actions. Then, for a live memory (ACTIVE or ARCHIVED) with a
   creation, restore or content-changing action, ``ingest`` extracts it into the
   graph. The order is sync, stale, ingest: an edge the new body still supports is
   re-attested after its stale mark and comes back ACTIVE.
6. Advance the cursor to the last row actually read and report counters.

The stale pass, and why the marker is what it is
-----------------------------------------------
``JkgService.sync_memory`` clears an invalidation only when the stored reason
starts with ``MEMORY_`` — that prefix is the signature of *automatic* status
invalidation. When a memory's body is rewritten, the relations derived from the
old body are stale even though the memory is still perfectly valid. Marking them
with ``CONTENT_UPDATED_STALE`` therefore does two things at once: it invalidates
the old edges now, and it survives every later ``sync_memory`` because the
reason does not carry the ``MEMORY_`` prefix. That is the implementation of D8
("old facts must not remain authoritative after an edit"), and a test asserts it.

Whether a write really moved the derivation is decided from
``memory_audit_logs.detail_json["fields"]``, not from the action name: an archive
is recorded as ``UPDATE`` too (P0-5). Only ``status`` is treated as bookkeeping;
every other field, and every row whose detail cannot be classified, keeps the
pre-existing "assume stale" behaviour.

Archived is not deleted
-----------------------
``ARCHIVED`` is a member of ``VALID_SOURCE_STATUSES`` (decisions D9 / D17): an
archived memory's knowledge stays valid and the graph only *labels* the source
status. ``DELETED`` / ``MERGED`` invalidate. The two paths look similar in the
audit feed and mean opposite things, so each has its own test — and because an
archive is a status-only ``UPDATE``, the P0-5 evidence check is what keeps those
two paths from being confused.

Failure isolation
-----------------
One memory that fails to sync does not stop the batch and does not block the
cursor: the error is recorded as a type name and a code, never as the exception's
text (which could quote a memory body), and the remaining memories are processed.
A consumer that stops at the first bad row is a consumer that silently stops
consuming.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.errors import ValidationError
from ..core.timeutils import utc_now_iso
from ..governance.projection import ReadOnlyProjection
from ..jmr.sensitive import redact_for_log
from ..repositories.system_repository import SystemRepository
from .config import resolve_enabled
from .ingestor import is_governance_reason
from .normalization import VALID_SOURCE_STATUSES
from .schema import JKG_ENABLED_KEY
from .service import CONTENT_STALE_REASON

logger = logging.getLogger(__name__)

#: ``system_settings`` keys written by this module. Exactly three, all under the
#: ``jkg.sync.`` prefix (JMR-05 already owns ``jmr.scheduler.*``).
CURSOR_KEY = "jkg.sync.cursor"
LAST_TICK_KEY = "jkg.sync.last_tick"
VERSION_KEY = "jkg.sync.version"

#: Shape version of the cursor value, not of the graph. A cursor written by an
#: older shape is treated as unreadable and recovered from the beginning.
CURSOR_VERSION = 1

#: Default look-back applied to the cursor's timestamp. Two rows can share a
#: timestamp to the second, and a row can be committed between the slot where a
#: batch stopped reading and the moment the cursor was written. Re-reading a
#: short window is free because every downstream action is idempotent; skipping
#: one row is not free, because nothing would ever come back for it.
DEFAULT_OVERLAP_SECONDS = 60

#: Actions that rewrite a memory body. The derived relations of the *old* body
#: must stop being authoritative, which ``sync_memory`` alone cannot express
#: (the memory is still valid, so nothing is invalidated automatically).
#: ``AUTO_UPDATE`` is included because auto-supersede rewrites the body just as a
#: user edit does; omitting it would leave an automatically corrected fact
#: authoritative after the correction.
#:
#: **The action name alone is not sufficient.** JME records an archive or a
#: restore as ``action="UPDATE"`` exactly as it records a body rewrite, so a
#: membership test here treats a bare status change as a content change. That is
#: the P0-5 defect: every archive marked a still-valid memory's relations
#: ``CONTENT_UPDATED_STALE``, ``sync_memory`` revived the source, and the graph
#: was rewritten twice to end up exactly where it started — with
#: ``marked_stale`` over-counted, and with a window in which a failure between
#: the two writes would have silently dropped knowledge. The set therefore stays
#: as the *candidate* screen (which actions could possibly move a body) and
#: ``_is_content_changing`` decides with the per-row evidence.
CONTENT_CHANGING_ACTIONS = frozenset({"UPDATE", "AUTO_UPDATE"})

#: Prefix matched for correction actions (``CORRECTION_RESOLVE`` rewrites the
#: body; the other ``CORRECTION_*`` actions do not, but the marker is idempotent
#: and a false positive only re-confirms staleness that is already recorded).
CONTENT_CHANGING_PREFIXES = ("CORRECTION",)

#: Actions that extract a memory into the graph even when its body did not change:
#: a new memory (created or imported), and one restored from ARCHIVED. Content-changing
#: actions are extracted too (see ``_should_ingest``).
INGEST_ACTIONS = frozenset({"CREATE", "IMPORT", "RESTORE"})

#: Statuses whose memory may be extracted. Deleted and merged memories never are.
INGEST_STATUSES = frozenset({"ACTIVE", "ARCHIVED"})

#: The invalidation reason for a stale content derivation. Deliberately does NOT
#: start with ``MEMORY_`` — see the module docstring and ``_is_auto_reason``.
STALE_REASON = CONTENT_STALE_REASON

#: Actions this consumer knows by name. Anything else is still synced (the safe
#: idempotent floor) and reported under ``unknown_actions`` so an operator can
#: extend the table instead of guessing whether it was handled.
KNOWN_ACTIONS = frozenset(
    {
        "CREATE",
        "UPDATE",
        "AUTO_UPDATE",
        "IMPORT",
        "MODIFICATION_APPROVED",
        "CORRECTION_DETECT",
        "CORRECTION_RESOLVE",
        "CORRECTION_REJECT",
        "MERGE_PREVIEW",
        "MERGE_CONFIRM",
        "MERGE_SOURCE",
        "AUTO_MERGE_SOURCE",
        "AUTO_MERGE_CONFIRM",
        "ARCHIVE",
        "RESTORE",
        "SOFT_DELETE",
        "AUTO_EXPIRE",
        "FORGET",
        "FORGET_REQUESTED",
        "FORGET_REQUEST_REISSUED",
        "FORGET_APPROVED",
        "FORGET_EXECUTED",
        "FORGET_REJECTED",
        "FORGET_FAILED",
        "SUMMARY_CREATE",
        "SUMMARY_PRODUCT_CREATED",
    }
)

#: Largest batch a single tick may claim, and largest page read from the audit
#: feed. ``limit`` may exceed the page size; the reader then pages until the
#: batch is full or the feed runs out.
_MAX_LIMIT = 500
_MAX_PAGE = 200

#: A-1: how many extra pages one tick may read when the overlap page turned out
#: to be entirely already-consumed rows (see ``_continue_past_cursor``). The cap
#: bounds a single tick under a live writer that keeps appending faster than the
#: drain reads; hitting it reports ``truncated=True`` and the next tick continues
#: from the advanced cursor, so progress is guaranteed either way.
_MAX_CONTINUATION_ROUNDS = 100


_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _safe_code(value: Any, fallback: str) -> str:
    """Keep a code only when it is a plain identifier; otherwise use ``fallback``."""
    text = str(value or "")
    return text if _SAFE_CODE.match(text) else fallback


def _error_record(memory_id: str, exc: BaseException) -> Dict[str, str]:
    """Describe a failure without quoting it.

    Only two value-free facts go into the report: the exception's class name and,
    when the exception is a ``JmeError``, its stable ``code``. The message text is
    deliberately dropped — ``safe_error_message`` redacts *secrets*, but a memory
    body is not a secret, so it would pass straight through and land in an HTTP
    response or a log line. Nothing here is worth that risk: the type plus the
    memory id is enough to reproduce the failure from the audit row.
    """
    error_type = type(exc).__name__
    code = getattr(exc, "code", None)
    return {
        "memory_id": memory_id,
        "error_type": error_type,
        "error_code": str(code) if code else error_type,
    }


def _log_payload(
    run_id: str, /, *, memory_id: Optional[str] = None, **fields: Any
) -> Dict[str, Any]:
    """Build a log/persist payload whose generated identifiers survive verbatim.

    ``redact_for_log`` scrubs user-derived text, and over-redaction is the right
    default for it. An *identifier* is not user text: it is a value this process
    generated, and routing a 32-character hex id through the tolerant phone
    pattern rewrites roughly one id in a hundred — ``app/core/sensitive.py``
    matches ``1[3-9]\\d{9}`` anywhere inside a hex run, with no digit-boundary
    guard. A rewritten id defeats the one thing an id is for: correlating a log
    line, a ``system_settings`` row and an API response.

    Exactly two keys are therefore exempt from the content filter, and both are
    machine-generated identifiers:

    * ``run_id`` — ``uuid.uuid4().hex`` minted by ``tick`` / ``reconcile``;
    * ``memory_id`` — the JME UUID that names the row being processed. It is
      already reported **verbatim** in ``errors[].memory_id``, so filtering it
      in the log did not protect anything; it only made the two views of the
      same failure disagree about the id.

    Everything else (counters, statuses, error codes, timestamps) still goes
    through ``redact_for_log``: those fields are content-adjacent, and a log
    line is not a place to be optimistic. Do not add a key here without the
    same "we generated it, the user did not write it" argument.
    """
    payload: Dict[str, Any] = dict(redact_for_log(dict(fields)))
    # Assigned after the filter, so the identifiers always win: a caller that
    # also puts ``run_id`` in ``fields`` gets the verbatim value, not the
    # filtered one. Losing that silently is exactly the bug this helper exists
    # to prevent.
    payload["run_id"] = str(run_id)
    if memory_id is not None:
        payload["memory_id"] = str(memory_id)
    return payload


def _is_content_changing(
    action: str, derivation_affected: Optional[bool] = None
) -> bool:
    """Whether one audit row means the graph derivation may now be wrong.

    ``derivation_affected`` is the evidence recorded in ``memory_audit_logs``:
    ``True`` (the write may have moved an input the extraction rules read),
    ``False`` (every field it names is proven bookkeeping — ``status`` alone), or
    ``None`` (unclassifiable).

    The three cases are decided differently on purpose, and the asymmetry is the
    whole fix:

    * ``True`` -> stale, whatever the action is called;
    * ``False`` -> **not** stale, even for ``UPDATE``. This is the archive case:
      the row names only ``status``, so the body the graph was derived from is
      still the body on disk, and the source's validity is already decided by
      ``sync_memory`` from the current status. Marking it stale as well made
      every archive rewrite the graph twice (mark stale, then revive the source
      because ``ARCHIVED`` is a valid source status) and inflated
      ``marked_stale``; a failure in between silently dropped the knowledge.
    * ``None`` -> fall back to the action name. An unclassifiable row is a row
      this consumer cannot reason about, and the conservative answer for a
      derived graph is to assume the old derivation may be wrong. Refusing to
      mark stale on missing evidence would be knowledge quietly surviving an
      edit.

    ``None`` is the default so an action-only caller (a test, or a future path
    with no audit detail) keeps the pre-fix meaning instead of silently
    expanding to "never stale".
    """
    if derivation_affected is True:
        return True
    if derivation_affected is False:
        return False
    if action in CONTENT_CHANGING_ACTIONS:
        return True
    return any(action.startswith(prefix) for prefix in CONTENT_CHANGING_PREFIXES)



def _should_ingest(
    actions: Sequence[str],
    memory_status: Optional[str],
    *,
    content_changed: Optional[bool] = None,
) -> bool:
    """Whether the memory's current body should be (re-)extracted into the graph.

    Only a live memory qualifies. A deleted or merged one must stay out of the
    graph, and the ingestor would refuse it anyway. Among live memories, creation,
    restore and any body rewrite need an extraction.

    ``content_changed`` is the per-row evidence decision (see
    ``_is_content_changing``). When it says the derivation is unaffected, a
    status-only ``UPDATE`` does not re-extract: the body the graph was built
    from is still the body on disk, so a re-extraction could only re-derive the
    same relation. ``None`` keeps the action-name behaviour.
    """
    if memory_status not in INGEST_STATUSES:
        return False
    if content_changed is False:
        return any(action in INGEST_ACTIONS for action in actions)
    return any(
        action in INGEST_ACTIONS or _is_content_changing(action) for action in actions
    )


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO8601 timestamp; ``None`` when it cannot be interpreted."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _shift_timestamp(value: str, seconds: int) -> Optional[str]:
    """Move an ISO8601 timestamp back by ``seconds``; ``None`` on a bad input.

    A failed parse disables the overlap for this run rather than failing the
    batch: re-reading is an optimisation, not a correctness requirement, and a
    timestamp format this consumer cannot read must not stop it from consuming.
    """
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    try:
        return (parsed - timedelta(seconds=seconds)).isoformat()
    except (OverflowError, ValueError):
        return None


def _entry_keyset(entry: Dict[str, Any]) -> Tuple[str, str]:
    """The ``(created_at, id)`` ordering key of one audit row.

    String tuple order matches the projection's SQL keyset order
    (``ORDER BY created_at ASC, id ASC`` over ISO timestamps and hex ids), so
    comparing two keysets here answers the same question as the ``WHERE``
    predicate that pages the feed.
    """
    return (str(entry.get("created_at") or ""), str(entry.get("id") or ""))


class JkgLifecycleSync:
    """Pull-based lifecycle consumer for the derived graph."""

    def __init__(
        self,
        manager,
        *,
        adapter=None,
        ingest: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.manager = manager
        self._adapter = adapter
        self._ingest = ingest

    # ------------------------------------------------------------------ wiring
    @property
    def adapter(self):
        """The JKG composition adapter, constructed lazily if not supplied.

        Lazy on purpose: building the sync object must open no connection and
        install nothing, exactly like ``container.jkg_adapter``.
        """
        if self._adapter is None:
            from .adapter import JkgCompositionAdapter

            self._adapter = JkgCompositionAdapter(self.manager)
        return self._adapter

    def _projection(self) -> ReadOnlyProjection:
        """Read-only view of the JME authority tables (``mode=ro``, token-gated)."""
        return ReadOnlyProjection(self.manager).for_module("JKG")

    # ------------------------------------------------------------ precondition
    def _state(self) -> Dict[str, Any]:
        """Resolve switch + schema without raising, and without touching jkg_*.

        ``adapter.status()`` is the adapter's non-throwing surface. The switch is
        re-resolved from the live ``jkg.enabled`` row through the documented
        ``resolve_enabled`` helper on every call, because the adapter caches its
        config at construction time and a switch flipped between two ticks must
        take effect on the next one instead of on the next process restart.
        """
        try:
            status = dict(self.adapter.status())
        except Exception:  # noqa: BLE001 - a health probe must not raise
            return {
                "state": "schema_not_ready",
                "enabled": False,
                "schema_ready": False,
                "missing_tables": [],
            }
        try:
            setting_value = self._read_setting(JKG_ENABLED_KEY)
        except Exception:  # noqa: BLE001 - fall back to the adapter's own view
            setting_value = "1" if status.get("enabled") else "0"
        enabled, source = resolve_enabled(setting_value=setting_value, env_value=None)
        status["enabled"] = enabled
        status["enabled_source"] = source
        if not enabled:
            # A closed switch outranks the schema: report disabled regardless of
            # which tables happen to exist.
            status["state"] = "disabled"
        elif not status.get("schema_ready"):
            status["state"] = "schema_not_ready"
        else:
            status["state"] = "ready"
        return status

    # ------------------------------------------------------------------ cursor
    def _read_setting(self, key: str) -> Optional[str]:
        with self.manager.session() as session:
            return SystemRepository(session).get(key)

    def _write_settings(self, values: Dict[str, str]) -> None:
        with self.manager.session() as session:
            repo = SystemRepository(session)
            for key, value in values.items():
                repo.set(key, value)

    def _load_cursor(self) -> Tuple[Dict[str, str], bool]:
        """Return ``(cursor, recovered)``.

        A missing cursor is the ordinary first-run case (no recovery flag). A
        cursor that exists but cannot be decoded as the expected shape *is*
        recovery: the run restarts from the beginning and says so, because
        silently trusting half a cursor would skip an unknown amount of history.
        """
        raw = self._read_setting(CURSOR_KEY)
        if raw is None or not str(raw).strip():
            return {"created_at": "", "id": ""}, False
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {"created_at": "", "id": ""}, True
        if not isinstance(payload, dict):
            return {"created_at": "", "id": ""}, True
        version = payload.get("version", CURSOR_VERSION)
        if version != CURSOR_VERSION:
            return {"created_at": "", "id": ""}, True
        created_at = payload.get("created_at")
        entry_id = payload.get("id")
        if not isinstance(created_at, str) or not isinstance(entry_id, str):
            return {"created_at": "", "id": ""}, True
        return {"created_at": created_at, "id": entry_id}, False

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        """Read-only snapshot: switch, schema, cursor, last tick.

        Writes nothing, creates nothing and raises nothing. ``usable`` answers
        "can a tick do anything right now", which is the question an operator
        actually asks; ``state`` answers "why not".
        """
        state = self._state()
        try:
            cursor, recovered = self._load_cursor()
        except Exception:  # noqa: BLE001 - a status probe must not raise
            cursor, recovered = {"created_at": "", "id": ""}, True
        try:
            last_tick_raw = self._read_setting(LAST_TICK_KEY)
        except Exception:  # noqa: BLE001 - same reason
            last_tick_raw = None
        last_tick: Optional[Dict[str, Any]] = None
        if last_tick_raw:
            try:
                decoded = json.loads(last_tick_raw)
                last_tick = decoded if isinstance(decoded, dict) else None
            except (TypeError, ValueError):
                last_tick = None
        return {
            "state": state.get("state", "schema_not_ready"),
            "enabled": bool(state.get("enabled", False)),
            "usable": state.get("state") == "ready",
            "schema_ready": bool(state.get("schema_ready", False)),
            "missing_tables": list(state.get("missing_tables", [])),
            "cursor": dict(cursor),
            "cursor_recovered": recovered,
            "last_tick": last_tick,
            "overlap_seconds": DEFAULT_OVERLAP_SECONDS,
        }

    # -------------------------------------------------------------------- tick
    def tick(self, *, limit: int = 200, dry_run: bool = False) -> dict:
        """Run exactly one lifecycle pass. Passive, re-entrant, no daemon.

        ``dry_run`` performs every read and every computation and then throws the
        result away: no cursor write, no graph write. It is the tool an operator
        uses to see what a tick *would* do before letting it run.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = _MAX_PAGE
        limit = max(1, min(int(limit), _MAX_LIMIT))

        run_id = uuid.uuid4().hex
        started_at = utc_now_iso()
        state = self._state()
        current_state = str(state.get("state", "schema_not_ready"))

        if current_state != "ready":
            # Not ready: no read, no write, no table. Report and leave.
            return {
                "run_id": run_id,
                "state": current_state,
                "skipped": True,
                "dry_run": bool(dry_run),
                "wrote": False,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "entries_read": 0,
                "memories_processed": 0,
                "sources_invalidated": 0,
                "sources_reactivated": 0,
                "edges_invalidated": 0,
                "edges_reactivated": 0,
                "marked_stale": 0,
                "ingested_processed": 0,
                "ingested_skipped": 0,
                "ingested_failed": 0,
                "governance_rejected": 0,
                "errors": [],
                "unknown_actions": [],
                "cursor_before": {"created_at": "", "id": ""},
                "cursor_after": {"created_at": "", "id": ""},
                "cursor_recovered": False,
                "overlap_seconds": DEFAULT_OVERLAP_SECONDS,
                "truncated": False,
            }

        cursor_before, recovered = self._load_cursor()
        overlap_seconds = DEFAULT_OVERLAP_SECONDS
        effective_after = cursor_before["created_at"]
        shifted = _shift_timestamp(cursor_before["created_at"], overlap_seconds)
        if shifted is not None:
            effective_after = shifted
        elif cursor_before["created_at"]:
            # Unparseable timestamp: no overlap, but still a strict cursor.
            effective_after = cursor_before["created_at"]

        window_id = cursor_before["id"] if not shifted else ""
        entries = self._read_entries(
            after_created_at=effective_after,
            after_id=window_id,
            limit=limit,
        )
        # P0-5: whether a body actually moved is recorded per audit row, not
        # implied by the action name — an archive is an ``UPDATE`` too. The
        # evidence is read over the identical keyset window so the two reads
        # agree on which rows are in the batch; a row the second read does not
        # classify degrades to ``None``, never to "no content change".
        changes = self._read_derivation_evidence(
            after_created_at=effective_after,
            after_id=window_id,
            limit=limit,
        )
        truncated = len(entries) >= limit and len(entries) > 0

        # A-1: a burst of >= limit rows inside the 60s overlap window fills the
        # whole page with rows at or below the stored cursor. Re-reading that
        # same page on every later tick would leave cursor_after == cursor_before
        # forever while the tail of the burst is never consumed. Continue from
        # the strict cursor keyset within this same tick instead. The overlap
        # re-read itself is kept (late-committed transactions are still picked
        # up and every downstream action is idempotent); only the *continuation*
        # skips the already-consumed history, and rows are deduplicated by audit
        # id so no row is processed twice within one tick.
        if (
            shifted is not None
            and cursor_before["created_at"]
            and entries
            and len(entries) >= limit
            and _entry_keyset(entries[-1])
            <= (cursor_before["created_at"], cursor_before["id"])
        ):
            truncated = self._continue_past_cursor(
                entries,
                changes,
                cursor_before=cursor_before,
                limit=limit,
            )

        entries_read = len(entries)
        actions_by_memory = self._group_entries(entries)
        rows_by_memory = self._group_derivation_evidence(entries, changes)

        unknown_actions = sorted(
            {action for actions in actions_by_memory.values() for action in actions}
            - KNOWN_ACTIONS
        )

        totals: Dict[str, int] = {
            "sources_invalidated": 0,
            "sources_reactivated": 0,
            "edges_invalidated": 0,
            "edges_reactivated": 0,
            "marked_stale": 0,
            "ingested_processed": 0,
            "ingested_skipped": 0,
            "ingested_failed": 0,
            "governance_rejected": 0,
        }
        errors: List[Dict[str, str]] = []
        processed: List[str] = []

        # ``dry_run`` must not touch the graph, so the per-memory work is skipped
        # entirely rather than performed-then-rolled-back: an ORM flush that is
        # later rolled back would still look like a write to anything watching
        # SQLite, and the whole value of dry_run is that it is not a write.
        if not dry_run:
            for memory_id, actions in actions_by_memory.items():
                processed.append(memory_id)
                self._process_memory(
                    memory_id,
                    actions,
                    rows=rows_by_memory.get(memory_id, ()),
                    totals=totals,
                    errors=errors,
                    run_id=run_id,
                )
        else:
            processed = list(actions_by_memory)

        cursor_after = dict(cursor_before)
        if entries:
            last = entries[-1]
            candidate = {
                "created_at": str(last.get("created_at") or ""),
                "id": str(last.get("id") or ""),
            }
            # A-1: the cursor only ever moves forward. An overlap page made
            # entirely of old rows must not drag the stored cursor backwards.
            if (candidate["created_at"], candidate["id"]) >= (
                cursor_before["created_at"],
                cursor_before["id"],
            ):
                cursor_after = candidate

        finished_at = utc_now_iso()
        payload = {
            "run_id": run_id,
            "state": current_state,
            "skipped": False,
            "dry_run": bool(dry_run),
            "wrote": False,
            "started_at": started_at,
            "finished_at": finished_at,
            "entries_read": entries_read,
            "memories_processed": len(processed),
            "processed_memory_ids": processed,
            "sources_invalidated": totals["sources_invalidated"],
            "sources_reactivated": totals["sources_reactivated"],
            "edges_invalidated": totals["edges_invalidated"],
            "edges_reactivated": totals["edges_reactivated"],
            "marked_stale": totals["marked_stale"],
            "ingested_processed": totals["ingested_processed"],
            "ingested_skipped": totals["ingested_skipped"],
            "ingested_failed": totals["ingested_failed"],
            "governance_rejected": totals["governance_rejected"],
            "errors": errors,
            "unknown_actions": unknown_actions,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "cursor_recovered": recovered,
            "overlap_seconds": overlap_seconds,
            "truncated": truncated,
            "ok": not errors,
        }

        if dry_run:
            # Report only. ``wrote`` stays false and nothing was persisted.
            return payload

        self._write_settings(
            {
                CURSOR_KEY: json.dumps(
                    {"version": CURSOR_VERSION, **cursor_after},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                VERSION_KEY: str(CURSOR_VERSION),
                LAST_TICK_KEY: json.dumps(
                    _log_payload(
                        run_id,
                        finished_at=finished_at,
                        entries_read=entries_read,
                        memories_processed=len(processed),
                        marked_stale=totals["marked_stale"],
                        errors=len(errors),
                        ok=not errors,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )
        payload["wrote"] = True
        logger.info(
            "jkg.sync.tick %s",
            _log_payload(
                run_id,
                entries_read=entries_read,
                memories_processed=len(processed),
                sources_invalidated=totals["sources_invalidated"],
                sources_reactivated=totals["sources_reactivated"],
                marked_stale=totals["marked_stale"],
                errors=len(errors),
                unknown_actions=len(unknown_actions),
            ),
        )
        return payload

    # ------------------------------------------------------------ read + group
    def _continue_past_cursor(
        self,
        entries: List[Dict[str, Any]],
        changes: Dict[str, Optional[bool]],
        *,
        cursor_before: Dict[str, str],
        limit: int,
    ) -> bool:
        """A-1: drain the feed past the stored cursor within the same tick.

        Called only when the overlap page was full and ended at or below
        ``cursor_before`` — i.e. the page holds nothing but already-consumed
        history. Reading then continues from the **strict** cursor keyset
        (``created_at > cursor`` or ``= cursor AND id > cursor.id``), which by
        construction contains only rows newer than everything consumed so far.
        Entries are appended in feed order and deduplicated by audit id against
        the overlap page; the derivation evidence is read over each identical
        keyset window so the P0-5 join stays aligned.

        The 60s late-transaction semantics of the overlap page itself are
        untouched: those rows are still re-read and re-processed (idempotently).
        What this method gives up is only the *re-read* of old rows beyond the
        first overlap page during a burst — they were consumed by earlier ticks,
        and re-reading them is exactly what stalled the cursor forever.

        Returns the tick's ``truncated`` flag: ``False`` once a short/empty page
        proves the feed is drained, ``True`` when the round cap was hit and a
        later tick must continue (the cursor has still advanced).
        """
        seen = {str(entry.get("id") or "") for entry in entries}
        after_ts = cursor_before["created_at"]
        after_id = cursor_before["id"]
        for _ in range(_MAX_CONTINUATION_ROUNDS):
            page = self._read_entries(
                after_created_at=after_ts,
                after_id=after_id,
                limit=limit,
            )
            if not page:
                return False
            page_changes = self._read_derivation_evidence(
                after_created_at=after_ts,
                after_id=after_id,
                limit=limit,
            )
            changes.update(page_changes)
            for entry in page:
                entry_id = str(entry.get("id") or "")
                if entry_id not in seen:
                    seen.add(entry_id)
                    entries.append(entry)
            after_ts, after_id = _entry_keyset(page[-1])
            if len(page) < limit:
                return False
        return True

    def _read_entries(
        self, *, after_created_at: str, after_id: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Read the audit feed through the read-only projection, in pages."""
        projection = self._projection()
        page_size = max(1, min(limit, _MAX_PAGE))
        collected: List[Dict[str, Any]] = []
        cursor_at = after_created_at
        cursor_id = after_id
        while len(collected) < limit:
            want = min(page_size, limit - len(collected))
            page = projection.read_audit_entries(
                after_created_at=cursor_at,
                after_id=cursor_id,
                limit=want,
            )
            if not page:
                break
            collected.extend(dict(row) for row in page)
            last = page[-1]
            cursor_at = str(last.get("created_at") or "")
            cursor_id = str(last.get("id") or "")
            if len(page) < want:
                break
        return collected[:limit]

    def _read_derivation_evidence(
        self, *, after_created_at: str, after_id: str, limit: int
    ) -> Dict[str, Optional[bool]]:
        """Map audit id -> whether that write may have changed derivation inputs.

        A failed read is **not** fatal: it degrades every row in the batch to
        ``None``, which ``_is_content_changing`` resolves from the action name.
        Turning "the evidence read failed" into "nothing changed" would be the
        silent-loss failure this consumer exists to avoid.
        """
        try:
            projection = self._projection()
            read = getattr(projection, "read_audit_derivation_changes", None)
            if read is None:  # pragma: no cover - older projection shape
                return {}
            rows = read(
                after_created_at=after_created_at,
                after_id=after_id,
                limit=limit,
            )
        except Exception:  # noqa: BLE001 - the stale pass must still run
            logger.warning("jkg.sync.derivation_evidence_unavailable")
            return {}
        return {str(audit_id): flag for audit_id, flag in rows}

    @staticmethod
    def _group_entries(entries: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Reduce audit rows to ``memory_id -> sorted action set``.

        The *set* matters, not the count: a memory edited twice in one window is
        one memory to process, and its actions must both be considered (an
        ``UPDATE`` is not made unnecessary by a later ``CREATE``-like action).
        Insertion order is preserved so the batch processes memories in the order
        they first appeared in the feed, which keeps logs readable.
        """
        grouped: Dict[str, List[str]] = {}
        for entry in entries:
            memory_id = str(entry.get("memory_id") or "").strip()
            if not memory_id:
                continue
            action = str(entry.get("action") or "").strip() or "UNKNOWN"
            bucket = grouped.setdefault(memory_id, [])
            if action not in bucket:
                bucket.append(action)
        return grouped

    @staticmethod
    def _group_derivation_evidence(
        entries: Iterable[Dict[str, Any]],
        changes: Dict[str, Optional[bool]],
    ) -> Dict[str, List[Tuple[str, Optional[bool]]]]:
        """Reduce audit rows to ``memory_id -> [(action, derivation), ...]``.

        One row per audit entry (not deduplicated like ``_group_entries``): the
        stale decision is per-row, and a window can legitimately hold a
        status-only ``UPDATE`` *and* a body-rewriting one for the same memory.

        ``changes`` is joined by audit id. An id missing from it — because it was
        committed between the two reads, or because the probe could not classify
        it — becomes ``None``, which ``_is_content_changing`` resolves from the
        action name rather than from silence.
        """
        grouped: Dict[str, List[Tuple[str, Optional[bool]]]] = {}
        for entry in entries:
            memory_id = str(entry.get("memory_id") or "").strip()
            if not memory_id:
                continue
            action = str(entry.get("action") or "").strip() or "UNKNOWN"
            audit_id = str(entry.get("id") or "")
            grouped.setdefault(memory_id, []).append(
                (action, changes.get(audit_id))
            )
        return grouped

    # ---------------------------------------------------------------- per item
    def _process_memory(
        self,
        memory_id: str,
        actions: Sequence[str],
        *,
        rows: Sequence[Tuple[str, Optional[bool]]] = (),
        totals: Dict[str, int],
        errors: List[Dict[str, str]],
        run_id: str,
    ) -> None:
        """Sync one memory; stale it too if its content actually changed. Never raises.

        ``rows`` carries one ``(action, derivation_affected)`` pair per audit
        entry in the batch window, which is what tells a body rewrite apart from
        a status-only ``UPDATE`` (see ``_is_content_changing``). It defaults to
        empty so a caller that only knows the action names keeps the
        action-name behaviour.

        ``write_service()`` is *inside* the isolation boundary on purpose: the
        adapter re-runs its enablement/schema check (``_require_ready``) on every
        call, so obtaining the facade is a genuine failure point rather than the
        retrieval of an already-validated object. The switch can be flipped or
        the tables dropped between the moment ``tick`` resolved its state and the
        moment this row is processed; that failure belongs to **this** memory.
        Recording it and returning is what keeps the cursor advancing and the rest
        of the batch alive — the exact case this method's contract promises.

        Nothing is accumulated into ``totals`` before the first success, so an
        early failure leaves the counters untouched; the caller owns their keys
        and has already created them, so no ``KeyError`` is possible either.
        """
        try:
            service = self.adapter.write_service()
            result = service.sync_memory(memory_id)
        except Exception as exc:  # noqa: BLE001 - isolate one bad memory
            errors.append(_error_record(memory_id, exc))
            logger.warning(
                "jkg.sync.memory_failed %s",
                _log_payload(
                    run_id,
                    memory_id=memory_id,
                    error_type=type(exc).__name__,
                ),
            )
            return

        totals["sources_invalidated"] += int(result.get("sources_invalidated", 0))
        totals["sources_reactivated"] += int(result.get("sources_reactivated", 0))
        totals["edges_invalidated"] += len(result.get("edges_invalidated", []) or [])
        totals["edges_reactivated"] += len(result.get("edges_reactivated", []) or [])

        logger.debug(
            "jkg.sync.memory %s",
            _log_payload(
                run_id,
                memory_id=memory_id,
                memory_status=result.get("memory_status"),
                actions=list(actions),
                sources_invalidated=result.get("sources_invalidated"),
                edges_invalidated=len(result.get("edges_invalidated", []) or []),
            ),
        )

        content_changed = self._content_changed(actions, rows)
        if content_changed:
            try:
                marked = self._mark_stale(memory_id)
            except Exception as exc:  # noqa: BLE001 - same isolation as above
                errors.append(_error_record(memory_id, exc))
                logger.warning(
                    "jkg.sync.stale_failed %s",
                    _log_payload(
                        run_id,
                        memory_id=memory_id,
                        error_type=type(exc).__name__,
                    ),
                )
                return
            totals["marked_stale"] += int(marked.get("sources_marked", 0))
            totals["edges_invalidated"] += int(marked.get("edges_invalidated", 0))
            logger.info(
                "jkg.sync.stale %s",
                _log_payload(
                    run_id,
                    memory_id=memory_id,
                    actions=list(actions),
                    sources_marked=marked.get("sources_marked", 0),
                ),
            )

        if self._ingest is not None and _should_ingest(
            actions, result.get("memory_status"), content_changed=content_changed
        ):
            self._ingest_memory(memory_id, totals=totals, errors=errors, run_id=run_id)

    @staticmethod
    def _content_changed(
        actions: Sequence[str],
        rows: Sequence[Tuple[str, Optional[bool]]],
    ) -> bool:
        """Whether any audit row for this memory rewrote its body.

        ``rows`` (one entry per audit row) is authoritative when present; the
        action set is the fallback for a caller that has no per-row evidence,
        which keeps the action-only meaning rather than collapsing to "never
        stale".
        """
        if rows:
            return any(
                _is_content_changing(action, derivation_affected)
                for action, derivation_affected in rows
            )
        return any(_is_content_changing(action) for action in actions)

    def _ingest_memory(
        self,
        memory_id: str,
        *,
        totals: Dict[str, int],
        errors: List[Dict[str, str]],
        run_id: str,
    ) -> None:
        """Extract one memory through the injected ``ingest`` callable. Never raises.

        Only the outcome's status and its safe reason are kept. The candidate names,
        the memory body and any exception text stay out of the report and the log.
        """
        try:
            outcome = self._ingest(memory_id)
        except Exception as exc:  # noqa: BLE001 - isolate one bad memory
            totals["ingested_failed"] += 1
            errors.append(
                {
                    "memory_id": memory_id,
                    "error_type": "ingest_failed",
                    "error_code": _safe_code(
                        getattr(exc, "code", None), "JKG_INGEST_FAILED"
                    ),
                }
            )
            logger.warning(
                "jkg.sync.ingest_failed %s",
                _log_payload(
                    run_id,
                    memory_id=memory_id,
                    error_type=type(exc).__name__,
                ),
            )
            return
        status = str(getattr(outcome, "status", "") or "")
        reason = _safe_code(getattr(outcome, "reason", None), "JKG_INGEST_FAILED")
        if status == "processed":
            totals["ingested_processed"] += 1
        elif status == "skipped":
            totals["ingested_skipped"] += 1
        else:
            totals["ingested_failed"] += 1
            errors.append(
                {
                    "memory_id": memory_id,
                    "error_type": "ingest_failed",
                    "error_code": reason,
                }
            )
        if is_governance_reason(reason):
            totals["governance_rejected"] += 1
        logger.debug(
            "jkg.sync.ingest %s",
            _log_payload(run_id, memory_id=memory_id, status=status, reason=reason),
        )

    # -------------------------------------------------------------- stale pass
    def _mark_stale(self, memory_id: str) -> Dict[str, int]:
        """Invalidate the edge sources derived from a memory whose body changed.

        Runs *after* ``sync_memory`` so it sees the post-status state of the
        graph. Only rows that are still valid are touched, which is what makes
        the pass idempotent: a second tick over the same window finds nothing
        left to mark and reports ``sources_marked = 0``.

        Edge status is recomputed with the same rule ``sync_memory`` uses (an
        edge is valid while at least one of its sources is valid), so the two
        passes cannot disagree about what an edge's stored status means.
        """
        from .repository import JkgRepository

        now = utc_now_iso()
        marked = 0
        edges_invalidated = 0
        with self.manager.session() as session:
            repo = JkgRepository(session)
            sources = repo.list_sources_for_memory(memory_id)
            affected_edges = sorted({source.edge_id for source in sources})
            for source in sources:
                if source.invalidated_at is not None:
                    continue
                source.invalidated_at = now
                source.invalidation_reason = STALE_REASON
                marked += 1

            statuses = self._memory_statuses(
                source.memory_id
                for edge_id in affected_edges
                for source in repo.list_sources(edge_id)
            )
            for edge_id in affected_edges:
                edge = repo.get_edge(edge_id)
                if edge is None:
                    continue
                edge_sources = repo.list_sources(edge_id)
                valid_count = sum(
                    1
                    for source in edge_sources
                    if source.invalidated_at is None
                    and statuses.get(source.memory_id) in VALID_SOURCE_STATUSES
                )
                new_status = "ACTIVE" if valid_count > 0 else "INVALID"
                if edge.status != new_status:
                    edge.status = new_status
                    edge.updated_at = now
                    if new_status == "INVALID":
                        edges_invalidated += 1
        return {"sources_marked": marked, "edges_invalidated": edges_invalidated}

    def _memory_statuses(self, memory_ids: Iterable[str]) -> Dict[str, Optional[str]]:
        """Resolve source statuses through the read-only projection."""
        projection = self._projection()
        statuses: Dict[str, Optional[str]] = {}
        for memory_id in memory_ids:
            key = str(memory_id)
            if key in statuses:
                continue
            row = projection.read_memory(key)
            statuses[key] = None if row is None else str(row.get("status") or "")
        return statuses

    # ----------------------------------------------------------------- rebuild
    def rebuild_cursor(self) -> dict:
        """Reset the cursor to the beginning. An explicit administrative action.

        Kept separate from ``tick`` on purpose: replaying history is a decision,
        not a side effect of a failed read. The next tick re-syncs every memory
        that ever produced an edge source, which is safe (``sync_memory`` is
        idempotent) but not free, so it happens only when asked for.
        """
        before, _ = self._load_cursor()
        self._write_settings(
            {
                CURSOR_KEY: json.dumps(
                    {"version": CURSOR_VERSION, "created_at": "", "id": ""},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                VERSION_KEY: str(CURSOR_VERSION),
            }
        )
        logger.info("jkg.sync.cursor_rebuilt %s", redact_for_log({"run_id": None}))
        return {
            "rebuilt": True,
            "cursor_before": before,
            "cursor_after": {"created_at": "", "id": ""},
        }

    # ---------------------------------------------------------- init handover
    def reset_cursor_for_init(self, created_at: str) -> dict:
        """Rewind the cursor to an initialization's start, so the replay covers it.

        The initialization scan reads only memories created up to its snapshot. Every
        audit row written from that point on is therefore replayed by the following
        ticks. The rewind is unconditional: a cursor the ordinary sync already moved
        past the start would otherwise hide the rows written during the
        initialization. Replaying them again is free, because every action is
        idempotent. The cursor is never set to "now".
        """
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValidationError(
                "created_at must be a non-empty string",
                detail={"field": "created_at"},
            )
        state = str(self._state().get("state", "schema_not_ready"))
        if state != "ready":
            return {"updated": False, "state": state}
        cursor_before, _ = self._load_cursor()
        cursor_after = {"created_at": created_at.strip(), "id": ""}
        self._write_settings(
            {
                CURSOR_KEY: json.dumps(
                    {"version": CURSOR_VERSION, **cursor_after},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                VERSION_KEY: str(CURSOR_VERSION),
            }
        )
        return {
            "updated": True,
            "state": state,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
        }

    # --------------------------------------------------------------- reconcile
    def reconcile(
        self, *, limit: int = 200, after_memory_id: str = ""
    ) -> dict:
        """Walk distinct source memories with a keyset cursor.

        ``JkgService.reconcile`` is **not** reused here: it has no resume point
        and it lets the first exception abort the whole sweep, so a single
        corrupt source would stop every later memory from ever being reconciled
        and the next call would start over from the same broken row. This version
        pages on the memory id, isolates each failure, and returns where it
        stopped, which is what makes "call it again until ``has_more`` is false"
        a correct maintenance loop.

        Each memory goes through ``sync_memory`` only: reconcile repairs the
        derivation the current authority state implies. It deliberately does not
        run the stale pass, because reconcile has no audit window and therefore
        no evidence that a body changed.

        The write facade is acquired **inside** the per-memory ``try``, not once
        before the loop. ``write_service()`` re-runs the enablement/schema check
        (``_require_ready``) on every call, so it can start failing *after* the
        sweep began — the very "schema dropped / switch flipped mid-run" case
        ``tick`` already isolates per memory. Acquiring per memory keeps that
        failure attributable to a memory id, lets ``next_after_memory_id`` advance
        past the failed row (so a retry cannot loop on it forever) and reports it
        as ``ok=False`` plus an ``errors`` entry instead of escaping as a 500.
        The instance is then cached in a local, so a healthy page still probes the
        schema once rather than once per memory; a failed acquisition is simply
        retried on the next memory, which is what makes a transient outage
        recoverable within a single sweep.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = _MAX_PAGE
        limit = max(1, min(int(limit), _MAX_LIMIT))
        after = str(after_memory_id or "")

        state = self._state()
        current_state = str(state.get("state", "schema_not_ready"))
        started_at = utc_now_iso()
        run_id = uuid.uuid4().hex
        if current_state != "ready":
            return {
                "run_id": run_id,
                "state": current_state,
                "skipped": True,
                "dry_run": False,
                "wrote": False,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "memories_checked": 0,
                "sources_invalidated": 0,
                "sources_reactivated": 0,
                "edges_invalidated": [],
                "edges_reactivated": [],
                "errors": [],
                "after_memory_id": after,
                "next_after_memory_id": after,
                "has_more": False,
                "ok": True,
            }

        from .repository import JkgRepository

        with self.manager.session() as session:
            memory_ids = JkgRepository(session).distinct_source_memory_ids_after(
                after, limit
            )

        errors: List[Dict[str, str]] = []
        # ``None`` until a facade is actually obtained: see the loop below.
        service = None
        sources_invalidated = 0
        sources_reactivated = 0
        edges_invalidated: List[str] = []
        edges_reactivated: List[str] = []
        checked = 0
        next_after = after
        for memory_id in memory_ids:
            checked += 1
            next_after = memory_id
            try:
                if service is None:
                    service = self.adapter.write_service()
                result = service.sync_memory(memory_id)
            except Exception as exc:  # noqa: BLE001 - one row must not end the sweep
                errors.append(_error_record(memory_id, exc))
                logger.warning(
                    "jkg.sync.reconcile_failed %s",
                    _log_payload(
                        run_id,
                        memory_id=memory_id,
                        error_type=type(exc).__name__,
                    ),
                )
                continue
            sources_invalidated += int(result.get("sources_invalidated", 0))
            sources_reactivated += int(result.get("sources_reactivated", 0))
            edges_invalidated.extend(result.get("edges_invalidated", []) or [])
            edges_reactivated.extend(result.get("edges_reactivated", []) or [])

        has_more = checked >= limit and checked > 0
        logger.info(
            "jkg.sync.reconcile %s",
            _log_payload(
                run_id,
                memories_checked=checked,
                errors=len(errors),
                has_more=has_more,
            ),
        )
        return {
            "run_id": run_id,
            "state": current_state,
            "skipped": False,
            "dry_run": False,
            "wrote": checked > 0,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "memories_checked": checked,
            "sources_invalidated": sources_invalidated,
            "sources_reactivated": sources_reactivated,
            "edges_invalidated": edges_invalidated,
            "edges_reactivated": edges_reactivated,
            "errors": errors,
            "after_memory_id": after,
            "next_after_memory_id": next_after,
            "has_more": has_more,
            "ok": not errors,
        }


__all__ = [
    "JkgLifecycleSync",
    "INGEST_ACTIONS",
    "INGEST_STATUSES",
    "CURSOR_KEY",
    "LAST_TICK_KEY",
    "VERSION_KEY",
    "CURSOR_VERSION",
    "DEFAULT_OVERLAP_SECONDS",
    "STALE_REASON",
    "KNOWN_ACTIONS",
    "CONTENT_CHANGING_ACTIONS",
    "CONTENT_CHANGING_PREFIXES",
]
