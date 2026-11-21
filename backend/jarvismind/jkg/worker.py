"""Pull-based JKG worker: lease, one lifecycle pass, optional reconcile (P0-1/P0-3).

There is no daemon here
-----------------------
``JkgWorker`` holds no thread, no timer, no asyncio task and no sleep. Like
``JmrSchedulerEngine`` and ``JkgLifecycleSync`` before it, it is a *pull* object:
pushing it is somebody else's job (a cron entry, the admin route, a developer).
``run_once`` performs exactly one pass and returns. Nothing keeps running after
it returns, which is what makes the whole pipeline auditable — a silent worker
failure and a silent "nothing to do" look different in ``status()`` instead of
both looking like a quiet log.

What one run does
-----------------
1. Resolve the worker switch, then the graph switch. Off / schema absent ⇒
   report and return **without writing anything**, not even bookkeeping: a
   disabled worker must leave no trace that could be mistaken for a run.
2. Take the lease (``jkg.worker.lease``). Refused ⇒ ``skipped`` with a reason.
   That is a *normal* outcome (another run is in progress), not an error, so
   ``ok`` stays true and ``errors`` stays empty.
3. Record ``jkg.worker.last_run`` as ``running`` **before** the work, so a crash
   leaves evidence. If the lease we took had expired under a previous owner,
   that dead run is marked ``abandoned`` here — the takeover path and the
   standalone ``recover_stale_runs`` share one implementation.
4. Run one ``JkgLifecycleSync.tick``.
5. Heartbeat, then (only when due) one reconcile page. A failed heartbeat means
   the lease is no longer ours, so the reconcile — the expensive, optional half
   — is skipped rather than run without an admission ticket.
6. Overwrite ``last_run`` with the final state and write ``last_success`` or
   ``last_failed``.
7. Release the lease in ``finally``, whatever happened above.

A ``dry_run`` is deliberately **not** a run: it takes no lease and writes
nothing at all (not the cursor, not the graph, not the worker bookkeeping). It
is read-only by construction, so it cannot contend with a real pass, and taking
the lease would only block real work for a whole TTL. The test suite pins this
by comparing the entire ``system_settings`` table before and after.

Never raises
------------
Every entry point is wrapped. ``run_once`` returns a dict with ``ok=false`` and
an error record instead of propagating, because its callers are an HTTP route
(which must degrade, not 500) and a scheduler (which must keep its slot alive).
The exception *text* is never recorded — only its type and, for a ``JmeError``,
its stable code. A message can quote the value that caused it, and that value
can be a memory body.

Zero DDL
--------
State lives in the existing ``system_settings`` key/value table under
``jkg.worker.*``. No new table, no new column, no schema version bump — the core
line stays frozen at v6.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import text

from ..core.timeutils import utc_now_iso
from ..governance.projection import ReadOnlyProjection
from ..repositories.system_repository import SystemRepository
from .lease import DEFAULT_TTL_SECONDS, JkgLeaseStore
from .lifecycle import _log_payload

logger = logging.getLogger(__name__)

#: ``system_settings`` keys owned by the worker. ``jkg.sync.*`` belongs to the
#: lifecycle and ``jkg.worker.lease`` to :mod:`app.jkg.lease`.
LAST_RUN_KEY = "jkg.worker.last_run"
LAST_SUCCESS_KEY = "jkg.worker.last_success"
LAST_FAILED_KEY = "jkg.worker.last_failed"

#: Mirror of ``JkgLifecycleSync``'s per-tick ceiling. Duplicated as a plain
#: number instead of imported so this module does not depend on a private name;
#: the lifecycle clamps again on its own side, so the two can only ever agree or
#: this one be stricter.
MAX_RUN_LIMIT = 500

#: How many audit rows ``status()`` is willing to page through to answer "how
#: much is pending". A status probe must stay cheap: the answer is a floor, and
#: ``pending_truncated`` says so instead of pretending to be exact.
PENDING_SCAN_LIMIT = 500

#: Skip reasons. These are outcomes, not failures — each one still returns
#: ``ok=True``.
SKIP_DISABLED = "WORKER_DISABLED"
SKIP_SCHEMA = "SCHEMA_NOT_READY"
SKIP_LEASE_HELD = "LEASE_HELD"
SKIP_LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"

#: ``last_run`` states.
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_ABANDONED = "abandoned"

#: Why an abandoned run was abandoned.
REASON_LEASE_EXPIRED = "LEASE_EXPIRED"

_READ_SETTING = "SELECT value FROM system_settings WHERE key = :k"


def _encode(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _decode(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode a stored JSON object; ``None`` when it is absent or unusable."""
    if raw is None or raw == "":
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a counter without ever raising on a weird payload."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_record(phase: str, exc: BaseException) -> Dict[str, str]:
    """Describe a failure without quoting it.

    Same contract as ``lifecycle._error_record``: type name plus stable code,
    never the message. A phase label is added because a worker has several
    failure points (tick, heartbeat, reconcile, bookkeeping) and an operator
    needs to know *which* one broke before reading any code.
    """
    error_type = type(exc).__name__
    code = getattr(exc, "code", None)
    return {
        "phase": str(phase),
        "error_type": error_type,
        "error_code": str(code) if code else error_type,
    }


class JkgWorker:
    """One pull-based lifecycle pass per call. Never starts anything."""

    def __init__(
        self,
        manager,
        *,
        adapter,
        lifecycle,
        ingest=None,
        config=None,
    ) -> None:
        # No I/O: constructing the worker opens no connection, reads no setting
        # and installs no table, exactly like the adapter it wraps.
        self.manager = manager
        self._adapter = adapter
        self._lifecycle = lifecycle
        self._ingest = ingest
        self._config = config
        self._leases = JkgLeaseStore(manager)
        self._owner = ""
        self._runs = 0

    # ------------------------------------------------------------------ wiring
    def _setting(self, name: str, default: Any) -> Any:
        """Read one worker setting, falling back to the documented default.

        ``config`` is whatever the container holds (``Settings`` in production).
        A partially-built or absent config must degrade to the safe default —
        off for the switch, and the documented numbers for the rest — rather
        than raise out of a status probe.
        """
        cfg = self._config
        if cfg is None:
            cfg = getattr(self.manager, "settings", None)
        if cfg is None:
            return default
        return getattr(cfg, name, default)

    @property
    def enabled(self) -> bool:
        """The worker switch. OFF by default; the lifecycle switch is separate."""
        value = self._setting("jkg_worker_enabled", False)
        return bool(value) and self._lifecycle is not None

    @property
    def lease_ttl_seconds(self) -> int:
        value = _as_int(
            self._setting("jkg_worker_lease_ttl_seconds", DEFAULT_TTL_SECONDS),
            DEFAULT_TTL_SECONDS,
        )
        return max(1, value)

    @property
    def batch_limit(self) -> int:
        value = _as_int(self._setting("jkg_worker_batch_limit", 200), 200)
        return max(1, min(value, MAX_RUN_LIMIT))

    @property
    def reconcile_every(self) -> int:
        return max(0, _as_int(self._setting("jkg_worker_reconcile_every", 0), 0))

    @property
    def ingest_wired(self) -> bool:
        """Whether extraction is actually wired into the pass.

        Reported, not enforced: a tick without an ingestor still syncs and
        invalidates, it just never extracts. Telling an operator that up front
        is cheaper than reading a run of zero ``ingested_processed`` counters.
        """
        if self._ingest is not None:
            return True
        return bool(getattr(self._lifecycle, "_ingest", None))

    # ------------------------------------------------------------- bookkeeping
    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            with self.manager.session() as session:
                raw = session.execute(text(_READ_SETTING), {"k": key}).scalar()
        except Exception:  # noqa: BLE001 - a status probe must not raise
            return None
        return _decode(raw)

    def _write(self, values: Dict[str, Any]) -> bool:
        """Persist worker records atomically; ``False`` when the write failed."""
        if not values:
            return True
        try:
            with self.manager.session() as session:
                repo = SystemRepository(session)
                for key, value in values.items():
                    repo.set(key, _encode(value))
            return True
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not raise
            logger.warning(
                "jkg.worker.bookkeeping_failed %s",
                _log_payload(
                    str(values.get(LAST_RUN_KEY, {}).get("run_id") or ""),
                    error_type=type(exc).__name__,
                    keys=sorted(values),
                ),
            )
            return False

    # ---------------------------------------------------------------- recovery
    def _mark_abandoned(
        self, owner: str, *, finished_at: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Record that the run holding ``owner``'s lease died without finishing.

        Idempotent: a run already recorded as abandoned/completed/failed is left
        alone. That matters because both the takeover path and the standalone
        recovery call reach this method, and both can run twice.

        Returns the ``last_failed`` record that was written, or ``None`` when
        there was nothing to mark.
        """
        last_run = self._read(LAST_RUN_KEY)
        if not isinstance(last_run, dict):
            return None
        if str(last_run.get("state") or "") != STATE_RUNNING:
            return None
        existing_owner = str(last_run.get("owner") or "")
        if owner and existing_owner and existing_owner != owner:
            # ``last_run`` describes a *different* run than the lease we are
            # recovering. Marking it would blame the wrong run.
            return None
        run_id = str(last_run.get("run_id") or "")
        stamp = finished_at or utc_now_iso()
        failed = dict(last_run)
        failed.update(
            {
                "run_id": run_id,
                "state": STATE_ABANDONED,
                "reason": REASON_LEASE_EXPIRED,
                "finished_at": stamp,
                "ok": False,
                "dry_run": bool(last_run.get("dry_run", False)),
            }
        )
        abandoned = dict(last_run)
        abandoned.update({"state": STATE_ABANDONED, "finished_at": stamp})
        if not self._write({LAST_FAILED_KEY: failed, LAST_RUN_KEY: abandoned}):
            return None
        logger.warning(
            "jkg.worker.run_abandoned %s",
            _log_payload(
                run_id,
                reason=REASON_LEASE_EXPIRED,
                owner=str(last_run.get("owner") or ""),
                started_at=last_run.get("started_at"),
            ),
        )
        return failed

    def recover_stale_runs(self) -> dict:
        """Clear an expired lease and mark the run that owned it. Never raises.

        Two repairs, both keyed on the same invariant — *a live run always holds
        a live lease*:

        * an **expired** ``jkg.worker.lease`` row is deleted, so the next caller
          does not have to reason about a corpse;
        * a ``last_run`` still claiming ``running`` is rewritten as
          ``abandoned`` and mirrored into ``jkg.worker.last_failed`` with
          ``reason="LEASE_EXPIRED"``.

        The second repair also covers ``last_run=running`` with **no lease row
        at all**. That is not an accident: ``run_once`` writes the ``running``
        record only *after* it holds the lease, and releases the lease only
        *after* the final record is written. So a ``running`` record without a
        lease can only be left over from a run that died after the lease was
        cleared — orphaned bookkeeping, which is exactly what a recovery pass is
        for. A live run cannot be caught by this, because it always has its
        lease.

        A **live** lease is never touched, which is what keeps this safe to call
        at any time.

        ``p1_tasks`` is deliberately **not** involved: that table is the
        JMR/JKG-init task state machine with its own transitions and its own
        owner, and a worker pass is not a task — mixing the two would let an
        administrative "cancel" silently kill a lifecycle run.
        """
        result: Dict[str, Any] = {
            "ok": True,
            "recovered": False,
            "lease_cleared": False,
            "abandoned_run_id": "",
            "errors": [],
        }
        try:
            lease = self._leases.read()
            now_iso = self._leases.now()
            expired = bool(lease) and self._leases.is_expired(lease, now_iso=now_iso)
            if not expired and lease is not None:
                # A live holder: nothing is stale. Return before touching any
                # record, so a recovery call can never disturb a running pass.
                return result
            if expired:
                cleared = self._leases.clear_expired()
                if cleared is None:
                    # Someone renewed or took over between the read and the
                    # delete. Do not report a recovery that did not happen.
                    return result
                result["lease_cleared"] = True
                result["recovered"] = True
                stale_owner = str(cleared.get("owner") or "")
                logger.warning(
                    "jkg.worker.lease_reclaimed %s",
                    _log_payload(
                        "",
                        owner=stale_owner,
                        expired_at=cleared.get("expires_at"),
                        heartbeat_at=cleared.get("heartbeat_at"),
                    ),
                )
            else:
                # No lease row at all — see the docstring: a ``running`` record
                # in this state is orphaned bookkeeping, not a live run.
                stale_owner = ""
            failed = self._mark_abandoned(stale_owner)
            if failed is not None:
                result["abandoned_run_id"] = str(failed.get("run_id") or "")
                result["recovered"] = True
        except Exception as exc:  # noqa: BLE001 - recovery must not raise
            result["ok"] = False
            result["errors"].append(_error_record("recover", exc))
        return result

    # ------------------------------------------------------------------ status
    def _lifecycle_status(self) -> Dict[str, Any]:
        """The lifecycle's own snapshot, or the degraded shape. Never raises."""
        if self._lifecycle is None:
            return {
                "state": "disabled",
                "enabled": False,
                "schema_ready": False,
                "usable": False,
                "cursor": {"created_at": "", "id": ""},
            }
        try:
            return dict(self._lifecycle.status())
        except Exception:  # noqa: BLE001 - a status probe must not raise
            return {
                "state": "schema_not_ready",
                "enabled": False,
                "schema_ready": False,
                "usable": False,
                "cursor": {"created_at": "", "id": ""},
            }

    def _pending(self, graph_state: str, cursor: Dict[str, Any]) -> tuple:
        """``(pending, truncated)`` — audit rows not yet consumed.

        Deliberately measures from the *stored* cursor with no overlap window:
        ``tick`` re-reads the last minute for correctness, but the backlog is the
        rows past the cursor, and adding the overlap back would report work that
        has already been done. ``None`` when the graph cannot be read at all.
        """
        if graph_state != "ready":
            return None, False
        try:
            projection = ReadOnlyProjection(self.manager).for_module("JKG")
            rows = projection.read_audit_entries(
                after_created_at=str(cursor.get("created_at") or ""),
                after_id=str(cursor.get("id") or ""),
                limit=PENDING_SCAN_LIMIT,
            )
        except Exception:  # noqa: BLE001 - a status probe must not raise
            return None, False
        return len(rows), len(rows) >= PENDING_SCAN_LIMIT

    def status(self) -> dict:
        """Read-only snapshot: switch, graph state, lease, last runs, backlog.

        Writes nothing, creates nothing and raises nothing. ``usable`` answers
        "would ``run_once`` do any work right now"; that is the question a
        scheduler asks before spending a slot, and it is not the same as
        ``enabled`` (a worker can be enabled while the graph is still disabled).
        """
        try:
            lifecycle_status = self._lifecycle_status()
        except Exception:  # noqa: BLE001 - belt and braces; the helper is safe
            lifecycle_status = {"state": "schema_not_ready"}
        graph_state = str(lifecycle_status.get("state") or "schema_not_ready")
        cursor = lifecycle_status.get("cursor")
        if not isinstance(cursor, dict):
            cursor = {"created_at": "", "id": ""}
        try:
            lease = self._leases.read()
        except Exception:  # noqa: BLE001 - a status probe must not raise
            lease = None
        try:
            lease_expired = bool(lease) and self._leases.is_expired(lease)
        except Exception:  # noqa: BLE001 - same reason
            lease_expired = False
        pending, pending_truncated = self._pending(graph_state, cursor)
        enabled = self.enabled
        return {
            "state": graph_state,
            "enabled": enabled,
            "jkg_enabled": bool(lifecycle_status.get("enabled", False)),
            "usable": bool(enabled and graph_state == "ready"),
            "schema_ready": bool(lifecycle_status.get("schema_ready", False)),
            "ingest_wired": self.ingest_wired,
            "lease": lease,
            "lease_holder": str((lease or {}).get("owner") or ""),
            "lease_expired": lease_expired,
            "last_run": self._read(LAST_RUN_KEY),
            "last_success": self._read(LAST_SUCCESS_KEY),
            "last_failed": self._read(LAST_FAILED_KEY),
            "pending": pending,
            "pending_truncated": pending_truncated,
            "pending_scan_limit": PENDING_SCAN_LIMIT,
            "batch_limit": self.batch_limit,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "reconcile_every": self.reconcile_every,
            "runs_this_process": self._runs,
            "cursor": dict(cursor),
        }

    # ------------------------------------------------------------------- result
    @staticmethod
    def _shell(run_id: str, *, dry_run: bool) -> Dict[str, Any]:
        """A result with every documented key present and zeroed.

        Built once and mutated, so no early-return path can be missing a key —
        the response model forbids extras but a caller still should not have to
        guess whether ``errors`` exists.
        """
        return {
            "ok": True,
            "skipped": False,
            "skip_reason": None,
            "state": "schema_not_ready",
            "enabled": False,
            "run_id": run_id,
            "owner": "",
            "dry_run": bool(dry_run),
            "wrote": False,
            "started_at": None,
            "finished_at": None,
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
            "reconcile_checked": 0,
            "reconcile_sources_invalidated": 0,
            "reconcile_sources_reactivated": 0,
            "reconcile_due": False,
            "lease_lost": False,
            "truncated": False,
            "ingest_wired": False,
            "errors": [],
        }

    def _apply_tick(self, result: Dict[str, Any], ticked: Dict[str, Any]) -> None:
        """Copy the lifecycle counters into the run result."""
        for key in (
            "entries_read",
            "memories_processed",
            "sources_invalidated",
            "sources_reactivated",
            "edges_invalidated",
            "edges_reactivated",
            "marked_stale",
            "ingested_processed",
            "ingested_skipped",
            "ingested_failed",
            "governance_rejected",
        ):
            result[key] = _as_int(ticked.get(key, 0))
        result["truncated"] = bool(ticked.get("truncated", False))
        for item in ticked.get("errors") or []:
            if isinstance(item, dict):
                result["errors"].append(
                    {
                        "phase": "tick",
                        "error_type": str(item.get("error_type") or "Unknown"),
                        "error_code": str(
                            item.get("error_code")
                            or item.get("error_type")
                            or "Unknown"
                        ),
                    }
                )

    def _apply_reconcile(self, result: Dict[str, Any], page: Dict[str, Any]) -> None:
        result["reconcile_checked"] = _as_int(page.get("memories_checked", 0))
        result["reconcile_sources_invalidated"] = _as_int(
            page.get("sources_invalidated", 0)
        )
        result["reconcile_sources_reactivated"] = _as_int(
            page.get("sources_reactivated", 0)
        )
        for item in page.get("errors") or []:
            if isinstance(item, dict):
                result["errors"].append(
                    {
                        "phase": "reconcile",
                        "error_type": str(item.get("error_type") or "Unknown"),
                        "error_code": str(
                            item.get("error_code")
                            or item.get("error_type")
                            or "Unknown"
                        ),
                    }
                )

    # ------------------------------------------------------------------ run once
    def run_once(
        self,
        *,
        limit: Optional[int] = None,
        dry_run: bool = False,
        reconcile_every: Optional[int] = None,
    ) -> dict:
        """Perform exactly one pass. Returns a dict; never raises, ever.

        ``skipped`` is true when the pass did not execute and ``skip_reason``
        says why. A skip is a **normal** outcome — ``ok`` stays true and
        ``errors`` stays empty — because "somebody else is already running" is
        the desired behaviour of an admission ticket, not a failure.
        """
        run_id = uuid.uuid4().hex
        result = self._shell(run_id, dry_run=dry_run)
        owner = ""
        lease_held = False
        started_at: Optional[str] = None
        try:
            if not self.enabled:
                result.update(
                    {"skipped": True, "skip_reason": SKIP_DISABLED, "state": "disabled"}
                )
                logger.debug(
                    "jkg.worker.skipped %s",
                    _log_payload(run_id, reason=SKIP_DISABLED),
                )
                return result

            lifecycle_status = self._lifecycle_status()
            graph_state = str(lifecycle_status.get("state") or "schema_not_ready")
            result["state"] = graph_state
            result["enabled"] = True
            result["ingest_wired"] = self.ingest_wired
            if graph_state != "ready":
                result.update({"skipped": True, "skip_reason": SKIP_SCHEMA})
                logger.debug(
                    "jkg.worker.skipped %s",
                    _log_payload(
                        run_id, reason=SKIP_SCHEMA, state=graph_state
                    ),
                )
                return result

            try:
                batch = int(limit) if limit is not None else self.batch_limit
            except (TypeError, ValueError):
                batch = self.batch_limit
            batch = max(1, min(batch, MAX_RUN_LIMIT))

            if dry_run:
                # A preview is not a run: no lease, no bookkeeping, no writes of
                # any kind. It cannot contend with a real pass, so it must not
                # queue behind one either.
                ticked = self._lifecycle.tick(limit=batch, dry_run=True)
                self._apply_tick(result, ticked)
                result["state"] = str(ticked.get("state") or graph_state)
                result["started_at"] = ticked.get("started_at")
                result["finished_at"] = ticked.get("finished_at")
                result["ok"] = not result["errors"]
                logger.info(
                    "jkg.worker.dry_run %s",
                    _log_payload(
                        run_id,
                        entries_read=result["entries_read"],
                        memories_processed=result["memories_processed"],
                        errors=len(result["errors"]),
                    ),
                )
                return result

            owner = uuid.uuid4().hex
            admission = self._leases.acquire(owner, self.lease_ttl_seconds)
            stolen_from = str(admission.get("stolen_from") or "")
            if not admission.get("acquired"):
                reason = (
                    SKIP_LEASE_HELD
                    if admission.get("reason") == "HELD"
                    else SKIP_LEASE_UNAVAILABLE
                )
                result.update(
                    {
                        "skipped": True,
                        "skip_reason": reason,
                        "owner": owner,
                        "state": graph_state,
                    }
                )
                logger.info(
                    "jkg.worker.skipped %s",
                    _log_payload(
                        run_id,
                        reason=reason,
                        holder=str(admission.get("holder") or ""),
                        lease_reason=str(admission.get("reason") or ""),
                    ),
                )
                return result

            lease_held = True
            self._runs += 1
            result.update({"owner": owner, "state": graph_state})
            if stolen_from:
                # We displaced a dead run. Its bookkeeping is ours to repair now,
                # while we hold the only admission ticket.
                self._mark_abandoned(stolen_from)

            started_at = utc_now_iso()
            result["started_at"] = started_at
            self._write(
                {
                    LAST_RUN_KEY: {
                        "run_id": run_id,
                        "state": STATE_RUNNING,
                        "owner": owner,
                        "started_at": started_at,
                        "dry_run": False,
                        "limit": batch,
                        "reconcile_every": self._reconcile_period(),
                        "stolen_from": stolen_from or None,
                    }
                }
            )

            ticked = self._lifecycle.tick(limit=batch, dry_run=False)
            self._apply_tick(result, ticked)
            result["state"] = str(ticked.get("state") or graph_state)
            result["wrote"] = bool(ticked.get("wrote", False))

            due = self._reconcile_due(reconcile_every)
            result["reconcile_due"] = due
            if due:
                if self._leases.heartbeat(owner):
                    page = self._lifecycle.reconcile(limit=batch)
                    self._apply_reconcile(result, page)
                else:
                    # The lease is no longer ours: another worker may already be
                    # running. Reconcile is the optional half of the pass, so it
                    # is the half that yields.
                    result["lease_lost"] = True
                    logger.warning(
                        "jkg.worker.lease_lost %s",
                        _log_payload(run_id, owner=owner),
                    )

            result["ok"] = not result["errors"]
            return result
        except Exception as exc:  # noqa: BLE001 - run_once must never raise
            result["ok"] = False
            result["errors"].append(_error_record("run", exc))
            logger.error(
                "jkg.worker.failed %s",
                _log_payload(run_id, error_type=type(exc).__name__),
            )
            return result
        finally:
            finished_at = utc_now_iso()
            try:
                self._finish(result, run_id, owner, lease_held, started_at, finished_at)
            except Exception as exc:  # noqa: BLE001 - release must still happen
                result["ok"] = False
                result["errors"].append(_error_record("finalize", exc))
            finally:
                if lease_held:
                    # Unconditional: a lease that outlives the run blocks every
                    # later run for a whole TTL, which is the one failure mode
                    # this whole module exists to prevent.
                    released = self._leases.release(owner)
                    if not released:
                        logger.warning(
                            "jkg.worker.release_failed %s",
                            _log_payload(run_id, owner=owner),
                        )

    # ------------------------------------------------------------- run helpers
    def _reconcile_period(self, override: Optional[int] = None) -> int:
        if override is not None:
            return max(0, _as_int(override, 0))
        return self.reconcile_every

    def _reconcile_due(self, override: Optional[int] = None) -> bool:
        """Whether this run should also perform one reconcile page.

        Period 0 (the default) means never. Otherwise every Nth successful
        admission reconciles, counted from this process's own run counter. That
        is a *hint*, not a schedule: the durable clock is ``runs_this_process``
        plus the run records, and an operator who needs an exact cadence drives
        reconciliation itself. Pretending to a precision the process cannot
        keep across restarts would be worse than saying so.
        """
        period = self._reconcile_period(override)
        if period <= 0:
            return False
        return self._runs % period == 0

    def _finish(
        self,
        result: Dict[str, Any],
        run_id: str,
        owner: str,
        lease_held: bool,
        started_at: Optional[str],
        finished_at: str,
    ) -> None:
        """Persist the run record. Only a real (leased, non-dry) run writes."""
        if result.get("dry_run") or not lease_held:
            return
        result["finished_at"] = result.get("finished_at") or finished_at
        record = {
            "run_id": run_id,
            "state": STATE_COMPLETED if result.get("ok") else STATE_FAILED,
            "owner": owner,
            "started_at": started_at,
            "finished_at": result["finished_at"],
            "dry_run": bool(result.get("dry_run", False)),
            "wrote": bool(result.get("wrote", False)),
            "ok": bool(result.get("ok", False)),
            "entries_read": _as_int(result.get("entries_read", 0)),
            "memories_processed": _as_int(result.get("memories_processed", 0)),
            "errors": len(result.get("errors") or []),
            "reconcile_checked": _as_int(result.get("reconcile_checked", 0)),
            "lease_lost": bool(result.get("lease_lost", False)),
        }
        values: Dict[str, Any] = {LAST_RUN_KEY: record}
        target = LAST_SUCCESS_KEY if result.get("ok") else LAST_FAILED_KEY
        values[target] = record
        self._write(values)
        logger.info(
            "jkg.worker.run %s",
            _log_payload(
                run_id,
                state=record["state"],
                entries_read=record["entries_read"],
                memories_processed=record["memories_processed"],
                errors=record["errors"],
                wrote=record["wrote"],
            ),
        )


__all__ = [
    "JkgWorker",
    "LAST_RUN_KEY",
    "LAST_SUCCESS_KEY",
    "LAST_FAILED_KEY",
    "MAX_RUN_LIMIT",
    "PENDING_SCAN_LIMIT",
    "SKIP_DISABLED",
    "SKIP_SCHEMA",
    "SKIP_LEASE_HELD",
    "SKIP_LEASE_UNAVAILABLE",
    "STATE_RUNNING",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_ABANDONED",
    "REASON_LEASE_EXPIRED",
]
