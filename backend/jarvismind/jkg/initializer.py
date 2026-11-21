"""Historical-memory initializer for the JKG derived graph (JKG-INT-05 / A2a).

Every eligible memory is scanned once in ``(created_at, id)`` order under a
snapshot bound, and each memory is fed through ``JkgIngestor``. Progress lives in
a ``JKG_INIT`` row of the P1 task registry (``p1_tasks``), so a run can stop at
any batch boundary and resume from the stored cursor.

Failure model
-------------
* A memory the ingestor reports as ``failed`` is isolated: the run continues,
  the id is recorded and the task still ends COMPLETED.
* Any unexpected exception ends the run as FAILED with the stable code
  ``jkg_init_failed`` (or the code of ``JkgInitFinalizeError``). Only that code
  is logged, never the exception text or memory bodies.

Concurrency
-----------
One process-wide lock serialises ``run_task``. A second JKG_INIT task cannot run
while another one is RUNNING. Pause, resume, cancel and retry do not take the
lock; they only change state, and the running loop observes the change before
each memory.

Finalize (before the task is marked SUCCEEDED, full mode only)
-----------------------------------------------------------
With a ``lifecycle``, the run replays the audit window written during the scan:
it reconciles every source memory, rewinds the lifecycle cursor to the snapshot
and drains the feed, so nothing written meanwhile is lost. Without one, the
``finalizer`` callback is called instead. Either way the step must be idempotent:
a failed finalize leaves the task FAILED and a retry runs it again.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text

from ..core.errors import ValidationError
from ..core.timeutils import utc_now_iso
from ..governance.errors import TaskStateError
from ..governance.projection import ReadOnlyProjection
from ..governance.schema import P1_TABLES
from ..governance.task_registry import (
    CANCELLED,
    CREATED,
    FAILED,
    PAUSED,
    RUNNING,
    SUCCEEDED,
    TaskRegistry,
)
from .adapter import JkgCompositionAdapter, JkgDisabledError
from .extraction import RULE_VERSION
from .errors import (
    JkgInitConflictError,
    JkgInitFinalizeError,
    JkgInitP1MissingError,
    JkgInitStateError,
    JkgInitTaskNotFoundError,
    JkgSchemaNotReadyError,
)
from .ingestor import IngestOutcome, JkgIngestor, is_governance_reason
from .normalization import ENTITY_TYPES, RELATION_TYPES
from .repository import JkgRepository

logger = logging.getLogger(__name__)

TASK_TYPE = "JKG_INIT"
# Preview/init report the version produced by ``extraction.RULE_VERSION``. Kept
# as a module attribute for API stability, but imported rather than duplicated so
# the two can never disagree.
RULES_VERSION = RULE_VERSION
UNEXPECTED_ERROR_CODE = "jkg_init_failed"

_STATUSES = ("ACTIVE", "ARCHIVED")
_MODES = ("full", "retry")
_BATCH_MIN = 1
_BATCH_MAX = 100
_BATCHES_MIN = 1
_BATCHES_MAX = 20
_LIST_MAX = 100
_RETRY_MAX = 1000
_FAILURE_LIST_MAX = 100
_FAILURE_ID_MAX = 1000
_LIFECYCLE_PAGE = 200
_LIFECYCLE_MAX_ROUNDS = 100

_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

_TO_EXTERNAL = {
    CREATED: "PENDING",
    RUNNING: "RUNNING",
    PAUSED: "PAUSED",
    SUCCEEDED: "COMPLETED",
    FAILED: "FAILED",
    CANCELLED: "CANCELLED",
}
_TO_INTERNAL = {external: internal for internal, external in _TO_EXTERNAL.items()}

_RUN_LOCK = threading.Lock()


def _clamp(value: Any, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _count(meta: Dict[str, Any], key: str) -> int:
    return int(meta.get(key) or 0)


def _bump(meta: Dict[str, Any], key: str, amount: int = 1) -> None:
    meta[key] = _count(meta, key) + int(amount)


def _safe_reason(value: Any) -> str:
    candidate = str(value or "")
    return candidate if _SAFE_CODE.match(candidate) else "JKG_INGEST_FAILED"


def _dedupe(ids: Any) -> List[str]:
    cleaned = []
    for raw in ids or []:
        value = str(raw or "").strip()
        if value:
            cleaned.append(value)
    return list(dict.fromkeys(cleaned))


def _record_failure(meta: Dict[str, Any], memory_id: str, code: str) -> bool:
    """Record one failed memory. Returns False when it is already recorded.

    A memory can fail during the scan and again during the finalize replay; it is
    counted and listed once, so ``retry_failed_task`` never repeats it twice.
    """
    failure_ids = meta.setdefault("failure_memory_ids", [])
    if memory_id in failure_ids:
        return False
    _bump(meta, "failed_total")
    failures = meta.setdefault("failures", [])
    if len(failures) < _FAILURE_LIST_MAX:
        failures.append(
            {
                "memory_id": memory_id,
                "error_type": "ingest_failed",
                "error_code": code,
            }
        )
    if len(failure_ids) < _FAILURE_ID_MAX:
        failure_ids.append(memory_id)
    return True


def _task_from_row(row) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata_json") or "{}")
    except (ValueError, TypeError):
        item["metadata"] = {}
    return item


def _percent(status: str, total: int, scanned: int) -> int:
    if total <= 0:
        return 100 if status == "COMPLETED" else 0
    return min(100, round(scanned * 100 / total))


def _state_error(message: str, status: str) -> JkgInitStateError:
    return JkgInitStateError(message, detail={"status": _TO_EXTERNAL.get(status, status)})


def _initial_metadata(
    *,
    mode: str,
    batch_size: int,
    snapshot_at: str,
    total: int,
    retry_memory_ids: List[str],
    source_task_id: str,
) -> Dict[str, Any]:
    return {
        "init_version": 1,
        "mode": mode,
        "batch_size": batch_size,
        "snapshot_at": snapshot_at,
        "started_at": None,
        "finished_at": None,
        "cursor": {"created_at": "", "id": ""},
        "total": total,
        "scanned": 0,
        "processed": 0,
        "skipped": 0,
        "governance_rejected": 0,
        "failed": 0,
        "nodes_added": 0,
        "edges_added": 0,
        "sources_added": 0,
        "nodes_matched": 0,
        "failures": [],
        "failed_total": 0,
        "failure_memory_ids": [],
        "retry_memory_ids": list(retry_memory_ids),
        "retry_index": 0,
        "retry_count": 0,
        "source_task_id": source_task_id,
        "phase": "retrying" if mode == "retry" else "scanning",
        "error_code": "",
    }


class JkgInitializer:
    """Drive the one-shot, resumable backfill of the JKG graph from history."""

    def __init__(
        self,
        manager,
        *,
        adapter: JkgCompositionAdapter,
        ingestor: JkgIngestor,
        registry: Optional[TaskRegistry] = None,
        finalizer: Optional[Callable[[str], None]] = None,
        lifecycle: Optional[Any] = None,
    ) -> None:
        self.manager = manager
        self._adapter = adapter
        self._ingestor = ingestor
        self._registry = registry or TaskRegistry(manager)
        self._finalizer = finalizer
        self._lifecycle = lifecycle
        self._source = ReadOnlyProjection(manager).for_module("JKG")

    # ------------------------------------------------------------------ guards
    def _require_ready(self) -> None:
        cfg = self._adapter.config()
        if not cfg.enabled:
            raise JkgDisabledError(
                detail={"state": "disabled", "enabled_source": cfg.enabled_source}
            )
        schema = self._adapter.schema_status()
        if not schema["schema_ready"]:
            raise JkgSchemaNotReadyError(
                "The JKG schema is not installed",
                detail={
                    "code": "jkg_schema_not_ready",
                    "missing": list(schema["missing_tables"]),
                },
            )

    def _missing_p1_tables(self) -> List[str]:
        with self.manager.session() as session:
            present = {
                row[0]
                for row in session.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                ).all()
            }
        return [table for table in P1_TABLES if table not in present]

    def _require_p1(self) -> None:
        missing = self._missing_p1_tables()
        if missing:
            raise JkgInitP1MissingError(
                "The P1 task schema is not installed",
                detail={"code": "p1_schema_missing", "missing": missing},
            )

    @staticmethod
    def _transition(operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except TaskStateError:
            raise JkgInitStateError(
                "The task state changed; reload the task and retry"
            ) from None

    # ----------------------------------------------------------------- queries
    def _query_tasks(
        self, *, status: Optional[str] = None, limit: int, offset: int = 0
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT task_id, task_type, status, owner, created_at, updated_at, "
            "pause_reason, cancel_reason, failure_reason, metadata_json "
            "FROM p1_tasks WHERE task_type = :type"
        )
        params: Dict[str, Any] = {"type": TASK_TYPE, "n": int(limit), "o": int(offset)}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at DESC, task_id DESC LIMIT :n OFFSET :o"
        with self.manager.session() as session:
            rows = session.execute(text(sql), params).mappings().all()
        return [_task_from_row(row) for row in rows]

    def _count_tasks(self, *, status: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM p1_tasks WHERE task_type = :type"
        params: Dict[str, Any] = {"type": TASK_TYPE}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        with self.manager.session() as session:
            return int(session.execute(text(sql), params).scalar() or 0)

    def _other_running(self, task_id: str) -> bool:
        return any(
            row["task_id"] != task_id for row in self._query_tasks(status=RUNNING, limit=2)
        )

    def _load(self, task_id: str) -> Dict[str, Any]:
        self._require_p1()
        task = self._registry.get(task_id)
        if task is None or task.get("task_type") != TASK_TYPE:
            raise JkgInitTaskNotFoundError()
        return task

    def _payload(self, task: Dict[str, Any]) -> Dict[str, Any]:
        meta = task.get("metadata") or {}
        status = _TO_EXTERNAL.get(task["status"], task["status"])
        total = _count(meta, "total")
        scanned = _count(meta, "scanned")
        return {
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "status": status,
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "mode": meta.get("mode"),
            "batch_size": meta.get("batch_size"),
            "progress": {
                "total": total,
                "scanned": scanned,
                "processed": _count(meta, "processed"),
                "skipped": _count(meta, "skipped"),
                "governance_rejected": _count(meta, "governance_rejected"),
                "failed": _count(meta, "failed"),
                "nodes_added": _count(meta, "nodes_added"),
                "edges_added": _count(meta, "edges_added"),
                "sources_added": _count(meta, "sources_added"),
                "nodes_matched": _count(meta, "nodes_matched"),
                "percent": _percent(status, total, scanned),
                "phase": meta.get("phase"),
            },
            "failures": list(meta.get("failures") or []),
            "failed_total": _count(meta, "failed_total"),
            "retry_count": _count(meta, "retry_count"),
            "source_task_id": meta.get("source_task_id") or "",
            "error_code": meta.get("error_code") or "",
        }

    # ----------------------------------------------------------------- preview
    def preview(self) -> Dict[str, Any]:
        self._require_ready()
        snapshot_at = utc_now_iso()
        eligible = self._source.count_memories(
            created_at_lte=snapshot_at, statuses=_STATUSES
        )
        with self.manager.session() as session:
            repo = JkgRepository(session)
            graph = {
                "nodes": repo.count_nodes(),
                "edges": repo.count_edges(),
                "sources": repo.count_all_sources(),
            }
        missing = self._missing_p1_tables()
        notes = [
            "Preview writes nothing.",
            "Only the four frozen relations are extracted; attributes are not supported.",
        ]
        if missing:
            notes.append("P1 task tables are missing; create and run are unavailable.")
        return {
            "eligible_memories": eligible,
            "snapshot_at": snapshot_at,
            "graph": graph,
            "rules_version": RULES_VERSION,
            "supported_entity_types": list(ENTITY_TYPES),
            "supported_relations": list(RELATION_TYPES),
            "attributes_supported": False,
            "p1_available": not missing,
            "notes": notes,
        }

    # ------------------------------------------------------------------ create
    def create_task(
        self,
        *,
        batch_size: int = 20,
        mode: str = "full",
        retry_memory_ids: Optional[List[str]] = None,
        source_task_id: str = "",
    ) -> Dict[str, Any]:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not _BATCH_MIN <= batch_size <= _BATCH_MAX
        ):
            raise ValidationError(
                "batch_size must be an integer from 1 to 100",
                detail={"field": "batch_size"},
            )
        if mode not in _MODES:
            raise ValidationError("mode must be 'full' or 'retry'", detail={"field": "mode"})
        source = str(source_task_id or "").strip()
        if mode == "full":
            if retry_memory_ids:
                raise ValidationError(
                    "retry_memory_ids is only allowed in retry mode",
                    detail={"field": "retry_memory_ids"},
                )
            ids: List[str] = []
            snapshot_at = utc_now_iso()
        else:
            ids = _dedupe(retry_memory_ids)
            if not ids:
                raise ValidationError(
                    "retry mode requires retry_memory_ids",
                    detail={"field": "retry_memory_ids"},
                )
            if len(ids) > _RETRY_MAX:
                raise ValidationError(
                    f"retry_memory_ids holds at most {_RETRY_MAX} ids",
                    detail={"field": "retry_memory_ids"},
                )
            if not source:
                raise ValidationError(
                    "retry mode requires source_task_id",
                    detail={"field": "source_task_id"},
                )
            snapshot_at = ""
        self._require_p1()
        if mode == "full":
            total = self._source.count_memories(
                created_at_lte=snapshot_at, statuses=_STATUSES
            )
        else:
            total = len(ids)
        task_id = f"jkg-init-{uuid.uuid4().hex}"
        metadata = _initial_metadata(
            mode=mode,
            batch_size=batch_size,
            snapshot_at=snapshot_at,
            total=total,
            retry_memory_ids=ids,
            source_task_id=source,
        )
        record = self._registry.create(task_id, TASK_TYPE, metadata=metadata)
        return self._payload(record)

    def list_tasks(
        self, *, status: Optional[str] = None, limit: int = 20, offset: int = 0
    ) -> Dict[str, Any]:
        self._require_p1()
        internal = None
        if status is not None:
            internal = _TO_INTERNAL.get(str(status).strip().upper())
            if internal is None:
                raise ValidationError("unknown task status", detail={"field": "status"})
        size = _clamp(limit, 1, _LIST_MAX)
        skip = max(0, int(offset))
        rows = self._query_tasks(status=internal, limit=size, offset=skip)
        return {
            "data": [self._payload(row) for row in rows],
            "total": self._count_tasks(status=internal),
            "limit": size,
            "offset": skip,
        }

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._payload(self._load(task_id))

    def failures(self, task_id: str) -> Dict[str, Any]:
        meta = self._load(task_id)["metadata"]
        return {
            "task_id": task_id,
            "failed_total": _count(meta, "failed_total"),
            "failures": list(meta.get("failures") or []),
        }

    # --------------------------------------------------------------------- run
    def run_task(self, task_id: str, *, max_batches: int = 1) -> Dict[str, Any]:
        limit = _clamp(max_batches, _BATCHES_MIN, _BATCHES_MAX)
        self._require_ready()
        self._require_p1()
        if not _RUN_LOCK.acquire(blocking=False):
            raise JkgInitConflictError()
        try:
            task = self._load(task_id)
            if self._other_running(task_id):
                raise JkgInitConflictError(
                    "Another JKG initialization task is running"
                )
            status = task["status"]
            if status == SUCCEEDED:
                return self._payload(task)
            if status in (PAUSED, FAILED, CANCELLED):
                raise _state_error(f"a {_TO_EXTERNAL[status]} task cannot be run", status)
            try:
                return self._drive(task_id, limit)
            except JkgInitFinalizeError as exc:
                logger.error("jkg init run failed task_id=%s code=%s", task_id, exc.code)
                return self._mark_failed(task_id, exc.code)
            except Exception:  # noqa: BLE001 - only the stable code is kept
                logger.error(
                    "jkg init run failed task_id=%s code=%s",
                    task_id,
                    UNEXPECTED_ERROR_CODE,
                )
                return self._mark_failed(task_id, UNEXPECTED_ERROR_CODE)
        finally:
            _RUN_LOCK.release()

    def _drive(self, task_id: str, limit: int) -> Dict[str, Any]:
        task = self._load(task_id)
        meta = dict(task["metadata"])
        if task["status"] == CREATED:
            self._transition(lambda: self._registry.start(task_id))
            meta["started_at"] = meta.get("started_at") or utc_now_iso()
            self._registry.update_metadata(task_id, {"started_at": meta["started_at"]})
        for _ in range(limit):
            items = self._next_items(meta)
            if not items:
                return self._complete(task_id, meta)
            for memory_id, created_at in items:
                stop = self._stop_status(task_id)
                if stop is not None:
                    self._flush(task_id, meta)
                    return self._stopped(task_id, stop)
                outcome = self._ingestor.ingest_memory(memory_id, init_task_id=task_id)
                self._apply(meta, memory_id, outcome)
                if meta["mode"] == "full":
                    meta["cursor"] = {"created_at": created_at, "id": memory_id}
                else:
                    meta["retry_index"] = _count(meta, "retry_index") + 1
            self._flush(task_id, meta)
            if len(items) < _count(meta, "batch_size"):
                return self._complete(task_id, meta)
        return self.get_task(task_id)

    def _next_items(self, meta: Dict[str, Any]) -> List[Tuple[str, str]]:
        size = _count(meta, "batch_size")
        if meta["mode"] == "full":
            cursor = meta.get("cursor") or {}
            rows = self._source.scan_memories(
                after_created_at=cursor.get("created_at") or "",
                after_id=cursor.get("id") or "",
                created_at_lte=meta["snapshot_at"],
                statuses=_STATUSES,
                limit=size,
            )
            return [(str(row["id"]), str(row["created_at"])) for row in rows]
        start = _count(meta, "retry_index")
        ids = list(meta.get("retry_memory_ids") or [])[start : start + size]
        return [(memory_id, "") for memory_id in ids]

    def _stop_status(self, task_id: str) -> Optional[str]:
        current = self._registry.get(task_id)
        status = current["status"] if current is not None else CANCELLED
        return None if status == RUNNING else status

    def _apply(self, meta: Dict[str, Any], memory_id: str, outcome: IngestOutcome) -> None:
        _bump(meta, "scanned")
        status = outcome.status
        if status == "processed":
            _bump(meta, "processed")
        elif status == "skipped":
            _bump(meta, "skipped")
        else:
            _bump(meta, "failed")
        reason = _safe_reason(outcome.reason)
        if is_governance_reason(reason):
            _bump(meta, "governance_rejected")
        if status not in ("processed", "skipped"):
            _record_failure(meta, memory_id, reason)
        _bump(meta, "nodes_added", outcome.nodes_added)
        _bump(meta, "edges_added", outcome.edges_added)
        _bump(meta, "sources_added", outcome.sources_added)
        _bump(meta, "nodes_matched", outcome.nodes_matched)

    def _flush(self, task_id: str, meta: Dict[str, Any]) -> None:
        # finished_at belongs to cancel / complete / fail. Leaving it out keeps a
        # concurrent cancel from being overwritten by a stale in-memory copy.
        patch = {key: value for key, value in meta.items() if key != "finished_at"}
        self._registry.update_metadata(task_id, patch)

    def _complete(self, task_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        if self._registry.get(task_id)["status"] != RUNNING:
            return self.get_task(task_id)
        if meta["mode"] == "full":
            meta["phase"] = "finalizing"
            self._flush(task_id, meta)
            self._run_finalizer(task_id, meta)
        self._transition(lambda: self._registry.succeed(task_id))
        now = utc_now_iso()
        self._registry.update_metadata(
            task_id, {"phase": "completed", "finished_at": now, "error_code": ""}
        )
        return self.get_task(task_id)

    def _run_finalizer(self, task_id: str, meta: Dict[str, Any]) -> None:
        if self._lifecycle is not None:
            self._hand_over(task_id, meta)
            return
        if self._finalizer is None:
            return
        try:
            self._finalizer(task_id)
        except Exception:  # noqa: BLE001 - never keep the callback's text
            raise JkgInitFinalizeError() from None

    def _hand_over(self, task_id: str, meta: Dict[str, Any]) -> None:
        """Replay the audit window written during the scan. Any failure is fatal.

        Three steps, each checked against the graph's state: reconcile every source
        memory, rewind the lifecycle cursor to the snapshot, and tick until the feed
        is drained. Errors raised by the lifecycle end the task as FAILED; a retry
        runs all three again, which is idempotent. A memory that fails on its own is
        not fatal: it is recorded as a failed memory, so ``retry_failed_task`` can
        pick it up.
        """
        lifecycle = self._lifecycle
        try:
            after = ""
            for _ in range(_LIFECYCLE_MAX_ROUNDS):
                page = lifecycle.reconcile(
                    limit=_LIFECYCLE_PAGE, after_memory_id=after
                )
                self._require_lifecycle_ready(page.get("state"))
                self._absorb_lifecycle_errors(meta, page.get("errors"))
                if not page.get("has_more"):
                    break
                after = str(page.get("next_after_memory_id") or "")
            else:
                raise JkgInitFinalizeError()

            anchor = str(meta.get("snapshot_at") or meta.get("started_at") or "")
            if not anchor:
                anchor = str(self._load(task_id)["created_at"])
            rewound = lifecycle.reset_cursor_for_init(anchor)
            self._require_lifecycle_ready(rewound.get("state"))
            if not rewound.get("updated"):
                raise JkgInitFinalizeError()

            for _ in range(_LIFECYCLE_MAX_ROUNDS):
                page = lifecycle.tick(limit=_LIFECYCLE_PAGE)
                self._require_lifecycle_ready(page.get("state"))
                self._absorb_lifecycle_errors(meta, page.get("errors"))
                if int(page.get("entries_read") or 0) < _LIFECYCLE_PAGE:
                    break
                # A-1: after a burst drain the overlap re-read can fill
                # entries_read back up to the page size with already-consumed
                # rows. ``truncated=False`` is the lifecycle's proof that the
                # feed is drained, so it ends the loop too. A fake/older page
                # without the key keeps the entries_read criterion unchanged.
                if page.get("truncated") is False:
                    break
            else:
                raise JkgInitFinalizeError()
        except JkgInitFinalizeError:
            raise
        except Exception:  # noqa: BLE001 - only the stable code is kept
            raise JkgInitFinalizeError() from None
        self._flush(task_id, meta)

    @staticmethod
    def _require_lifecycle_ready(state: Any) -> None:
        if state != "ready":
            raise JkgInitFinalizeError()

    @staticmethod
    def _absorb_lifecycle_errors(meta: Dict[str, Any], errors: Any) -> None:
        for record in errors or []:
            memory_id = str(record.get("memory_id") or "")
            if not memory_id:
                continue
            if _record_failure(meta, memory_id, _safe_reason(record.get("error_code"))):
                _bump(meta, "failed")

    def _stopped(self, task_id: str, status: str) -> Dict[str, Any]:
        if status == CANCELLED:
            current = self._load(task_id)["metadata"]
            self._registry.update_metadata(
                task_id,
                {"finished_at": current.get("finished_at") or utc_now_iso()},
            )
        return self.get_task(task_id)

    def _mark_failed(self, task_id: str, code: str) -> Dict[str, Any]:
        try:
            self._registry.fail(task_id, reason=code)
        except TaskStateError:
            return self.get_task(task_id)
        self._registry.update_metadata(
            task_id, {"error_code": code, "finished_at": utc_now_iso()}
        )
        return self.get_task(task_id)

    # ------------------------------------------------------- pause/resume/cancel
    def pause_task(self, task_id: str) -> Dict[str, Any]:
        task = self._load(task_id)
        if task["status"] == PAUSED:
            return self._payload(task)
        if task["status"] != RUNNING:
            raise _state_error("only a RUNNING task can be paused", task["status"])
        self._transition(lambda: self._registry.pause(task_id, reason="operator_pause"))
        return self.get_task(task_id)

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        task = self._load(task_id)
        if task["status"] != PAUSED:
            raise _state_error("only a PAUSED task can be resumed", task["status"])
        if self._other_running(task_id):
            raise JkgInitConflictError("Another JKG initialization task is running")
        self._transition(lambda: self._registry.resume(task_id))
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        task = self._load(task_id)
        if task["status"] == CANCELLED:
            return self._payload(task)
        if task["status"] not in (CREATED, RUNNING, PAUSED):
            raise _state_error("a finished task cannot be cancelled", task["status"])
        self._transition(lambda: self._registry.cancel(task_id, reason="operator_cancel"))
        self._registry.update_metadata(task_id, {"finished_at": utc_now_iso()})
        return self.get_task(task_id)

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        task = self._load(task_id)
        if task["status"] != FAILED:
            raise _state_error("only a FAILED task can be retried", task["status"])
        self._transition(lambda: self._registry.recover(task_id))
        meta = task["metadata"]
        self._registry.update_metadata(
            task_id,
            {
                "error_code": "",
                "finished_at": None,
                "retry_count": _count(meta, "retry_count") + 1,
                "phase": "retrying" if meta.get("mode") == "retry" else "scanning",
            },
        )
        return self.get_task(task_id)

    def retry_failed_task(self, task_id: str) -> Dict[str, Any]:
        task = self._load(task_id)
        meta = task["metadata"]
        failure_ids = list(meta.get("failure_memory_ids") or [])
        if task["status"] != SUCCEEDED or _count(meta, "failed_total") <= 0 or not failure_ids:
            raise _state_error(
                "only a completed task with failures can spawn a retry", task["status"]
            )
        return self.create_task(
            batch_size=_count(meta, "batch_size") or 20,
            mode="retry",
            retry_memory_ids=failure_ids,
            source_task_id=task_id,
        )


__all__ = ["JkgInitializer", "TASK_TYPE", "RULES_VERSION"]
