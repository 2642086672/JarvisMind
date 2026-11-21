"""JMR orchestration: one reflection run over the approved read projection.

JMR never writes ``memory_records`` / ``memory_candidates``. All proposals go
to the three existing derived tables with ``status=PENDING``; turning one into
an original memory still requires human review + a signed authorised write
through the P1 gateway (outside this batch).

Task state, cost and audit are provided by the EXISTING P1 components. Those
components do not audit/charge themselves, so this orchestrator compensates
explicitly at every stage.

JMR-04..07 integration (no schema change):
  * every proposal carries the full provenance block plus value-free evidence;
  * TTL expiry logically voids PENDING proposals (never DELETE);
  * generation/source-watermark make stale in-flight runs lose to newer data;
  * stage timeouts, manual retry decisions and a persisted breaker use the
    existing ``scheduler.py`` policies;
  * cost uses the existing ``CostGovernor`` plus the pure ``cost_policy.py``
    budget views; no second ledger is introduced;
  * every proposal runs through ``sensitive.evaluate`` before a row is written.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text

from ..core.errors import ValidationError
from ..core.security_gate import assert_safe_text, assert_writable
from ..core.timeutils import utc_now_iso
from ..governance.audit_bus import AuditBus
from ..governance.cost_governor import CostGovernor
from ..governance.errors import CostBlockedError, TaskStateError
from ..governance.projection import ReadOnlyProjection
from ..governance.schema import P1_TABLES
from ..governance.task_registry import TaskRegistry
from ..services.reflection import local
from .config import JMRConfig
from .cost_policy import compute_call_cost, evaluate_anomaly, evaluate_budget, to_money
from .detectors import (
    ConflictPair,
    DuplicateCluster,
    RelatedGroup,
    StatusChange,
    build_summary_draft,
    cluster_pairs,
    find_conflict_pairs,
    find_duplicate_clusters,
    find_status_changes,
    group_related,
    status_pairs,
)
from .errors import (
    JMRConcurrencyError,
    JMRError,
    JMRProvenanceError,
    JMRStageTimeoutError,
    JMRStateConflictError,
    JMRUnavailableError,
)
from .provenance import (
    RULES,
    build_metadata,
    compute_expires_at,
    deterministic_id,
    evidence_for,
    rule_of,
    validate_sources,
)
from .records import SourceRecord, digest_of_records
from .scheduler import (
    TIMEOUT_EXCEEDED,
    BreakerPolicy,
    BreakerState,
    RetryPolicy,
    TimeoutPolicy,
    admit_run,
    decide_retry,
    judge_timeout,
    manual_reset,
    record_outcome,
)
from .sensitive import combine as combine_privacy
from .sensitive import evaluate as evaluate_privacy
from .sensitive import safe_error_message
from .store import DerivedStore

SERVICE_SUBJECT = "svc:jmr"
SERVICE_TYPE = "SERVICE"
TRIGGER = "JMR"
TASK_TYPE = "JMR_REFLECTION"

SUCCEEDED = "SUCCEEDED"
PAUSED = "PAUSED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

BREAKER_KEY = "jmr.breaker"
RETRY_KEY_PREFIX = "jmr.retry."
RESUME_KEY_PREFIX = "jmr.resume."
DAILY_KEY_PREFIX = "p1.cost.daily."
MONTHLY_KEY_PREFIX = "p1.cost.monthly."

#: Only clearly transient infrastructure failures are eligible for a manual
#: retry. Security, validation and cost blocks are never retryable.
RETRYABLE_FAILURE_CODES = frozenset({"RuntimeError", "TimeoutError", "OSError"})


@dataclass(frozen=True)
class JMRRunResult:
    task_id: str
    run_id: str
    status: str
    created: Dict[str, int] = field(default_factory=dict)
    skipped_idempotent: int = 0
    memory_rows: int = 0
    projection_digest: str = ""
    failure_reason: str = ""
    cost_cny: float = 0.0
    cost_status: str = ""
    degradation: str = ""
    retry_count: int = 0
    retry_eligible: bool = False
    expired_voided: int = 0
    breaker_tripped: bool = False


class JMRService:
    def __init__(
        self,
        *,
        manager,
        projection,
        task_registry: TaskRegistry,
        cost_governor: CostGovernor,
        audit_bus: AuditBus,
        config: Optional[JMRConfig] = None,
    ) -> None:
        self.manager = manager
        self.projection = projection
        self.task_registry = task_registry
        self.cost_governor = cost_governor
        self.audit_bus = audit_bus
        self.config = config or JMRConfig()
        self.store = DerivedStore(manager)

    @classmethod
    def build(cls, *, manager, config: Optional[JMRConfig] = None) -> "JMRService":
        """Assemble from existing components.

        Deliberately does NOT call ``install_p1_schema`` (it would also write
        ``p1.governance_enabled=1``), does NOT call ``build_governance`` and
        does NOT activate the write gate. Test fixtures install the P1 tables
        and activate governance explicitly when they need those states.
        """
        return cls(
            manager=manager,
            projection=ReadOnlyProjection(manager).for_module("JMR"),
            task_registry=TaskRegistry(manager),
            cost_governor=CostGovernor(manager, manager.settings),
            audit_bus=AuditBus(manager),
            config=config or JMRConfig(),
        )

    # ---------------------------------------------------------------- helpers
    def _read_setting(self, key: str, default: Any = None) -> Any:
        with self.manager.session() as session:
            raw = session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
            ).scalar()
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def _write_setting(self, key: str, value: Any) -> None:
        with self.manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO system_settings(key, value, updated_at) "
                    "VALUES (:k, :v, :u) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at"
                ),
                {
                    "k": key,
                    "v": json.dumps(value, ensure_ascii=False, sort_keys=True),
                    "u": utc_now_iso(),
                },
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

    def _require_p1_schema(self) -> None:
        present = self._p1_tables_present()
        missing = [name for name in P1_TABLES if name not in present]
        if missing:
            raise JMRUnavailableError(
                "P1 task tables are not installed; JMR runs require them "
                f"(missing: {', '.join(missing)})",
                detail={"code": "p1_schema_missing", "missing": missing},
            )

    def _base_result(self, task_id: str, run_id: str) -> JMRRunResult:
        return JMRRunResult(
            task_id=task_id,
            run_id=run_id,
            status=SUCCEEDED,
            created={rule: 0 for rule in RULES},
            skipped_idempotent=0,
            memory_rows=self._total(),
            projection_digest=ReadOnlyProjection(self.manager).content_digest(),
            retry_count=self._retry_count(task_id),
            breaker_tripped=self._load_breaker().tripped,
        )

    def _total(self) -> int:
        page = self.projection.read_memories(status="ACTIVE", page=1, page_size=1)
        return int(page.total)

    def _read_rows(self, memory_type: Optional[str]) -> List[Mapping[str, Any]]:
        collected: List[Mapping[str, Any]] = []
        page_no = 1
        while len(collected) < self.config.max_memories:
            page = self.projection.read_memories(
                status="ACTIVE",
                memory_type=memory_type,
                page=page_no,
                page_size=self.config.page_size,
            )
            items = list(page.items)
            if not items:
                break
            collected.extend(items)
            if len(items) < page.page_size:
                break
            page_no += 1
        return collected[: self.config.max_memories]

    def _stage_open(self, task_id: str) -> str:
        task = self.task_registry.get(task_id)
        if task is None:
            return "MISSING"
        state = task["status"]
        if state == PAUSED:
            return PAUSED
        if state == CANCELLED:
            return CANCELLED
        return "OK"

    # ----------------------------------------------------------- breaker/retry
    def _load_breaker(self) -> BreakerState:
        raw = self._read_setting(BREAKER_KEY, {})
        if not isinstance(raw, dict):
            return BreakerState()
        return BreakerState(
            consecutive_failures=int(raw.get("consecutive_failures", 0) or 0),
            tripped=bool(raw.get("tripped", False)),
            reason=str(raw.get("reason", "") or ""),
        )

    def _save_breaker(self, state: BreakerState) -> None:
        self._write_setting(
            BREAKER_KEY,
            {
                "consecutive_failures": state.consecutive_failures,
                "tripped": state.tripped,
                "reason": state.reason,
            },
        )

    def _retry_count(self, task_id: str) -> int:
        raw = self._read_setting(RETRY_KEY_PREFIX + task_id, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return 0
        return raw

    def _bump_retry(self, task_id: str) -> int:
        count = self._retry_count(task_id) + 1
        self._write_setting(RETRY_KEY_PREFIX + task_id, count)
        return count

    def _reset_retry(self, task_id: str) -> None:
        self._write_setting(RETRY_KEY_PREFIX + task_id, 0)

    def _retry_decision(self, task_id: str, reason: str) -> Tuple[int, bool]:
        count = self._bump_retry(task_id)
        task = {
            "task_id": task_id,
            "status": FAILED,
            "failure_reason": reason,
            "metadata_json": json.dumps({"retry_count": count}),
        }
        decision = decide_retry(
            task,
            policy=RetryPolicy(
                max_retries=self.config.max_retries,
                retryable_codes=RETRYABLE_FAILURE_CODES,
            ),
        )
        return count, decision.action == "MANUAL_RETRY_ELIGIBLE"

    # ---------------------------------------------------------------- cost
    def _period_key(self, prefix: str) -> str:
        now = utc_now_iso()
        return prefix + (now[:7] if prefix == MONTHLY_KEY_PREFIX else now[:10])

    def _period_spend(self, prefix: str) -> float:
        raw = self._read_setting(self._period_key(prefix), 0.0)
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _add_period_spend(self, prefix: str, amount: float) -> None:
        if amount <= 0:
            return
        key = self._period_key(prefix)
        current = self._period_spend(prefix)
        self._write_setting(key, round(current + amount, 6))

    def _cost(self, task_id: str, run_id: str) -> Dict[str, Any]:
        cfg = self.config
        call_cost = float(cfg.expected_cost_cny)
        if cfg.expected_tokens > 0:
            priced = compute_call_cost(
                input_tokens=0,
                output_tokens=cfg.expected_tokens,
                price_in_per_1k_cny=getattr(
                    self.cost_governor.settings, "llm_price_in", 0.0
                )
                or 0.0,
                price_out_per_1k_cny=getattr(
                    self.cost_governor.settings, "llm_price_out", 0.0
                )
                or 0.0,
            )
            call_cost = max(call_cost, float(priced.total_cost_cny))
        cost_decimal = to_money(call_cost, "jmr_call_cost_cny")

        if cfg.max_single_call_cny > 0:
            anomaly = evaluate_anomaly(
                call_cost_cny=cost_decimal,
                max_single_call_cny=cfg.max_single_call_cny,
                anomaly_count=0,
                anomaly_threshold=cfg.breaker_threshold,
            )
            if anomaly.anomalous_call:
                self.cost_governor.anomaly("jmr_single_call_cost_anomaly")
                raise CostBlockedError(
                    "JMR single-call cost exceeded the configured anomaly limit",
                    detail={
                        "code": "cost_governance_blocked",
                        "reason": anomaly.reason,
                        "anomaly": anomaly.to_dict(),
                    },
                )

        degradation = ""
        for dimension, budget in (
            ("daily", cfg.daily_budget_cny),
            ("monthly", cfg.monthly_budget_cny),
        ):
            if budget <= 0:
                continue
            view = evaluate_budget(
                dimension,
                budget_cny=budget,
                spent_cny=self._period_spend(
                    DAILY_KEY_PREFIX if dimension == "daily" else MONTHLY_KEY_PREFIX
                ),
                call_cost_cny=cost_decimal,
                unconfigured_reason=f"jmr_{dimension}_budget_unconfigured",
            )
            if view.status in {"DENY", "HOLD"}:
                raise CostBlockedError(
                    f"JMR {dimension} budget blocked this stage",
                    detail={
                        "code": "cost_governance_blocked",
                        "reason": view.reason,
                        "dimension": dimension,
                        "budget_view": view.to_dict(),
                    },
                )
            if view.usage_ratio is not None and view.usage_ratio >= to_money(
                "0.8", "threshold"
            ):
                degradation = "reduce_model_calls_and_batch_size"

        # enforce()/record() use DIFFERENT keyword names in CostGovernor.
        # All calls are keyword-only on purpose (v1.2 P2-1).
        self.cost_governor.enforce(
            task_id=task_id,
            run_id=run_id,
            expected_cost_cny=call_cost,
            expected_tokens=cfg.expected_tokens,
            model_calls=cfg.model_calls_per_stage,
        )
        self.cost_governor.record(
            task_id=task_id,
            run_id=run_id,
            cost_cny=call_cost,
            tokens=cfg.expected_tokens,
            model_calls=cfg.model_calls_per_stage,
        )
        self._add_period_spend(DAILY_KEY_PREFIX, call_cost)
        self._add_period_spend(MONTHLY_KEY_PREFIX, call_cost)
        return {
            "cost_cny": call_cost,
            "cost_status": "ALLOW",
            "degradation": degradation,
        }

    # --------------------------------------------------------------- timeout
    @staticmethod
    def _stage_started() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _check_stage_timeout(self, task_id: str, started_at: str) -> None:
        judgement = judge_timeout(
            {
                "task_id": task_id,
                "status": "RUNNING",
                "created_at": started_at,
                "updated_at": started_at,
            },
            now=datetime.now(timezone.utc),
            policy=TimeoutPolicy(self.config.stage_timeout_seconds),
        )
        if judgement.status == TIMEOUT_EXCEEDED:
            raise JMRStageTimeoutError(
                f"stage exceeded {self.config.stage_timeout_seconds}s",
                detail={
                    "code": "jmr_stage_timeout",
                    "task_id": task_id,
                    "elapsed_seconds": judgement.elapsed_seconds,
                },
            )

    # ---------------------------------------------------------------- expire
    def expire_due(self) -> int:
        """Void PENDING proposals whose TTL elapsed. Never physical deletes."""
        changed = self.store.void_expired(now_iso=utc_now_iso())
        if changed:
            self.audit_bus.record(
                principal_id=SERVICE_SUBJECT,
                principal_type=SERVICE_TYPE,
                operation="JMR_EXPIRY",
                object_ref="GLOBAL",
                trigger=TRIGGER,
                status=SUCCEEDED,
                extra={"expired_voided": changed},
            )
        return changed

    def list_expired(self) -> List[Dict[str, Any]]:
        return self.store.list_expired(now_iso=utc_now_iso())

    # ---------------------------------------------------------------- audit
    def _audit_run(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        failure_reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.audit_bus.record(
            principal_id=SERVICE_SUBJECT,
            principal_type=SERVICE_TYPE,
            operation="JMR_REFLECTION",
            object_ref=task_id,
            trigger=TRIGGER,
            status=status,
            failure_reason=failure_reason,
            task_id=task_id,
            run_id=run_id,
            extra=extra,
        )

    def _audit_proposal(
        self,
        *,
        proposal_id: str,
        rule: str,
        source_memory_ids: Sequence[str],
        task_id: str,
        run_id: str,
        status: str,
        failure_reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = {
            "rule": rule,
            "proposal_id": proposal_id,
            "source_memory_ids": list(source_memory_ids),
            "task_id": task_id,
            "run_id": run_id,
            "algorithm_version": self.config.algorithm_version,
        }
        if extra:
            payload.update(extra)
        self.audit_bus.record(
            principal_id=SERVICE_SUBJECT,
            principal_type=SERVICE_TYPE,
            operation="JMR_PROPOSAL" if status == SUCCEEDED else "JMR_REFLECTION",
            object_ref=proposal_id,
            trigger=TRIGGER,
            status=status,
            failure_reason=failure_reason,
            task_id=task_id,
            run_id=run_id,
            extra=payload,
        )

    def _audit_cost(
        self,
        *,
        task_id: str,
        run_id: str,
        cost_cny: float,
        degradation: str,
    ) -> None:
        """One value-free cost record per run that produced real work.

        Idempotent resumes that write nothing intentionally do not append this
        row: JMR-01 standard 8 requires the five-table row count to stay exact.
        """
        self.audit_bus.record(
            principal_id=SERVICE_SUBJECT,
            principal_type=SERVICE_TYPE,
            operation="JMR_COST",
            object_ref=task_id,
            trigger=TRIGGER,
            status=SUCCEEDED,
            task_id=task_id,
            run_id=run_id,
            extra={
                "cost_cny": round(float(cost_cny), 6),
                "cost_status": "ALLOW",
                "degradation": degradation,
                "budget_window": "utc",
            },
        )

    # ------------------------------------------------------------------ detect
    def _detect(
        self, records: Sequence[SourceRecord]
    ) -> Tuple[
        Tuple[DuplicateCluster, ...],
        Tuple[StatusChange, ...],
        Tuple[Any, ...],
        Tuple[RelatedGroup, ...],
        Tuple[str, str, float],
    ]:
        cfg = self.config
        clusters = find_duplicate_clusters(
            records,
            threshold=cfg.duplicate_threshold,
            subsumption=cfg.subsumption_threshold,
        )
        excluded = cluster_pairs(clusters)
        # v1.2: STATUS_CHANGE claims its pairs before CONFLICT, otherwise a
        # timestamped state update is swallowed because it also looks like a
        # low-similarity conflict on the same subject.
        changes = find_status_changes(
            records, min_group=cfg.status_min_group, exclude_pairs=excluded
        )
        conflicts = find_conflict_pairs(
            records,
            max_similarity=cfg.conflict_max_similarity,
            exclude_pairs=excluded | status_pairs(changes),
        )
        related = group_related(records)
        summary = build_summary_draft(records, summary_type="REFLECTION")
        return clusters, changes, conflicts, related, summary

    # ------------------------------------------------------------------- write
    @staticmethod
    def _snapshot(row: SourceRecord, state_token: str = "") -> str:
        return json.dumps(
            {
                "memory_id": row.id,
                "content": row.content,
                "status": row.status,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "state_token": state_token,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _set_meta(metadata_json: str, **updates: Any) -> str:
        payload = json.loads(metadata_json)
        payload.update(updates)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _write(
        self,
        *,
        records: Sequence[SourceRecord],
        clusters: Sequence[DuplicateCluster],
        changes: Sequence[StatusChange],
        conflicts: Sequence[Any],
        related: Sequence[RelatedGroup],
        summary: Tuple[str, str, float],
        task_id: str,
        run_id: str,
    ) -> Tuple[Dict[str, int], int]:
        by_id = {row.id: row for row in records}
        all_evidence = {item["memory_id"]: item for item in evidence_for(records)}
        created = {rule: 0 for rule in RULES}
        skipped = 0
        now = utc_now_iso()
        # JMR-08 audit P1-9: the stage deadline must be checked while proposals
        # are produced, not only after the stage returns.
        stage_started = self._stage_started()
        version = self.config.algorithm_version
        digest = digest_of_records(records)
        expires_at = compute_expires_at(self.config.result_ttl_days)

        def meta(
            ids: Sequence[str],
            rule: str,
            extra=None,
            *,
            confidence: Optional[float] = None,
        ) -> str:
            validate_sources(records, source_memory_ids=list(ids))
            lineage = [dict(by_id[str(mid)].lineage) for mid in ids]
            return build_metadata(
                source_memory_ids=list(ids),
                lineage=lineage,
                task_id=task_id,
                run_id=run_id,
                rule=rule,
                algorithm_version=version,
                projection_digest=digest,
                extra=extra,
                confidence=confidence,
                evidence=[all_evidence[str(mid)] for mid in ids],
                expires_at=expires_at,
                generation=1,
            )

        def emit(
            *,
            rule: str,
            proposal_id: str,
            ids: Sequence[str],
            content: str,
            summary_text: str,
            metadata_json: str,
            write,
            review_texts: Sequence[str] = (),
        ) -> None:
            nonlocal skipped
            self._check_stage_timeout(task_id, stage_started)
            # JMR-07: privacy grading is fail-closed. CREDENTIAL => DENY and
            # PII => HOLD; neither may be written as an automatic proposal.
            privacy = combine_privacy(
                evaluate_privacy(content),
                evaluate_privacy(summary_text),
                *(evaluate_privacy(text) for text in review_texts),
            )
            if not privacy.may_auto_proceed:
                self._audit_proposal(
                    proposal_id=proposal_id,
                    rule=rule,
                    source_memory_ids=ids,
                    task_id=task_id,
                    run_id=run_id,
                    status="DENIED",
                    failure_reason=(
                        f"privacy_{privacy.decision.lower()}:{privacy.reason_code}"
                    ),
                    extra={"privacy": privacy.to_dict()},
                )
                return
            metadata_json = self._set_meta(
                metadata_json,
                sensitivity=privacy.level,
                privacy=privacy.to_dict(),
            )
            try:
                assert_writable(
                    content, summary=summary_text, metadata_text=metadata_json
                )
            except ValidationError as exc:
                # Only credential/PII scan hits are skipped. Never swallow a DB
                # error or an unexpected exception here.
                self._audit_proposal(
                    proposal_id=proposal_id,
                    rule=rule,
                    source_memory_ids=ids,
                    task_id=task_id,
                    run_id=run_id,
                    status="DENIED",
                    failure_reason=f"content_scan_blocked:{type(exc).__name__}",
                )
                return
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id,
                    rule=rule,
                    source_memory_ids=ids,
                    task_id=task_id,
                    run_id=run_id,
                    status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
                return
            if not write():
                skipped += 1
                return
            created[rule] += 1
            try:
                self._audit_proposal(
                    proposal_id=proposal_id,
                    rule=rule,
                    source_memory_ids=ids,
                    task_id=task_id,
                    run_id=run_id,
                    status=SUCCEEDED,
                )
            except Exception:
                # JMR-08 audit P1-2: never leave a written proposal without its
                # audit row. Compensate by voiding it, then fail the run.
                self.store.mark_void(proposal_id, reason="audit_write_failed")
                created[rule] = max(0, created[rule] - 1)
                raise

        # 1) MERGE -------------------------------------------------------------
        for cluster in clusters:
            ids = list(cluster.memory_ids)
            proposal_id = deterministic_id(
                rule="MERGE", source_memory_ids=ids, algorithm_version=version
            )
            try:
                validate_sources(records, source_memory_ids=ids)
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="MERGE", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            members = [by_id[mid] for mid in ids]
            draft = local.build_merge_draft(members, reason=cluster.reason)
            new_content = str(draft.get("new_content") or "")
            new_summary = str(draft.get("new_summary") or "")
            try:
                metadata = meta(
                    ids, "MERGE", {"confidence": cluster.confidence},
                    confidence=cluster.confidence,
                )
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="MERGE", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            emit(
                rule="MERGE", proposal_id=proposal_id, ids=ids,
                content=new_content, summary_text=new_summary,
                metadata_json=metadata,
                review_texts=[member.content for member in members],
                write=lambda pid=proposal_id, i=ids, c=new_content, s=new_summary, m=metadata: (
                    self.store.upsert_merge(
                        proposal_id=pid, source_memory_ids=i, new_content=c,
                        new_summary=s, reason="重复记忆合并建议（待人工审核）",
                        metadata_json=m, now=now,
                    )
                ),
            )

        # 2) STATUS_CHANGE ------------------------------------------------------
        for change in changes:
            ids = [change.earlier_memory_id, change.memory_id]
            proposal_id = deterministic_id(
                rule="STATUS_CHANGE", source_memory_ids=ids, algorithm_version=version
            )
            try:
                validate_sources(records, source_memory_ids=ids)
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="STATUS_CHANGE",
                    source_memory_ids=ids, task_id=task_id, run_id=run_id,
                    status="DENIED", failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            before = self._snapshot(by_id[change.earlier_memory_id], change.before_status)
            after = self._snapshot(by_id[change.memory_id], change.after_status)
            try:
                metadata = meta(
                    ids, "STATUS_CHANGE",
                    {"subject": change.subject, "before_status": change.before_status,
                     "after_status": change.after_status},
                )
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="STATUS_CHANGE",
                    source_memory_ids=ids, task_id=task_id, run_id=run_id,
                    status="DENIED", failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            emit(
                rule="STATUS_CHANGE", proposal_id=proposal_id, ids=ids,
                content=change.reason, summary_text=after, metadata_json=metadata,
                review_texts=[before, after],
                write=lambda pid=proposal_id, a=change.earlier_memory_id,
                              b=change.memory_id, r=change.reason,
                              bs=before, af=after, m=metadata: (
                    self.store.upsert_correction(
                        proposal_id=pid, memory_id_a=a, memory_id_b=b, reason=r,
                        before_state_json=bs, after_state_json=af,
                        metadata_json=m, now=now,
                    )
                ),
            )

        # 3) CONFLICT -----------------------------------------------------------
        for pair in conflicts:
            ids = [pair.memory_id_a, pair.memory_id_b]
            proposal_id = deterministic_id(
                rule="CONFLICT", source_memory_ids=ids, algorithm_version=version
            )
            try:
                validate_sources(records, source_memory_ids=ids)
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="CONFLICT", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            before = self._snapshot(by_id[pair.memory_id_a])
            after = self._snapshot(by_id[pair.memory_id_b])
            # Owner-approved single category for the current detector. A missing
            # or unapproved value fails this pair explicitly instead of being
            # silently promoted to WORDING_DIFF.
            conflict_class = getattr(pair, "conflict_class", None)
            if conflict_class != "WORDING_DIFF":
                self._audit_proposal(
                    proposal_id=proposal_id, rule="CONFLICT", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=(
                        "conflict_class_not_approved"
                        f":{conflict_class!r}"
                    ),
                )
                continue
            try:
                metadata = meta(
                    ids,
                    "CONFLICT",
                    {"confidence": pair.confidence, "conflict_class": conflict_class},
                    confidence=pair.confidence,
                )
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="CONFLICT", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
                continue
            emit(
                rule="CONFLICT", proposal_id=proposal_id, ids=ids,
                content=pair.reason, summary_text="", metadata_json=metadata,
                review_texts=[before, after],
                write=lambda pid=proposal_id, a=pair.memory_id_a,
                              b=pair.memory_id_b, r=pair.reason,
                              bs=before, af=after, m=metadata: (
                    self.store.upsert_correction(
                        proposal_id=pid, memory_id_a=a, memory_id_b=b, reason=r,
                        before_state_json=bs, after_state_json=af,
                        metadata_json=m, now=now,
                    )
                ),
            )

        # 4) SUMMARY (RELATED lives inside its metadata, never a separate row) --
        if records:
            ids = [row.id for row in records]
            proposal_id = deterministic_id(
                rule="SUMMARY", source_memory_ids=ids, algorithm_version=version
            )
            title, content, confidence = summary
            related_ids = sorted({mid for group in related for mid in group.memory_ids})
            extra = {
                "confidence": confidence,
                "related_memory_ids": related_ids,
                "related_groups": [
                    {"subject": group.subject, "memory_ids": list(group.memory_ids)}
                    for group in related
                ],
            }
            try:
                metadata = meta(ids, "SUMMARY", extra, confidence=confidence)
            except JMRProvenanceError as exc:
                self._audit_proposal(
                    proposal_id=proposal_id, rule="SUMMARY", source_memory_ids=ids,
                    task_id=task_id, run_id=run_id, status="DENIED",
                    failure_reason=f"provenance_invalid:{exc}",
                )
            else:
                source_ids = sorted(
                    {row.source_id for row in records if row.source_id}
                )
                emit(
                    rule="SUMMARY", proposal_id=proposal_id, ids=ids,
                    content=content, summary_text=title, metadata_json=metadata,
                    review_texts=[row.content for row in records],
                    write=lambda pid=proposal_id, i=ids, t=title, c=content,
                                  s=source_ids, m=metadata: (
                        self.store.upsert_summary(
                            proposal_id=pid, title=t, content=c,
                            summary_type="REFLECTION", memory_type=None,
                            source_memory_ids=i, source_ids=s,
                            metadata_json=m, now=now,
                        )
                    ),
                )
        return created, skipped

    # --------------------------------------------------------------------- run
    def run(
        self,
        *,
        task_id: str,
        run_id: str = "",
        memory_type: Optional[str] = None,
        resume: bool = False,
    ) -> JMRRunResult:
        self._require_p1_schema()
        breaker = self._load_breaker()
        admission = admit_run(breaker)
        if not admission.allowed:
            return JMRRunResult(
                task_id=task_id,
                run_id=run_id,
                status=BLOCKED,
                failure_reason=f"breaker_tripped:{breaker.reason}",
                retry_count=self._retry_count(task_id),
                breaker_tripped=True,
            )

        expired_voided = self.expire_due()
        tr = self.task_registry
        existing_task = tr.get(task_id)
        if existing_task is not None and existing_task.get("task_type") != TASK_TYPE:
            raise JMRStateConflictError(
                f"task {task_id} belongs to another task type",
                detail={
                    "code": "task_type_conflict",
                    "task_id": task_id,
                    "task_type": existing_task.get("task_type"),
                },
            )
        # JMR-08 audit C-2: memory_type is persisted in p1_tasks.metadata_json
        # and task events. Treat it as untrusted input.
        if memory_type:
            assert_safe_text(memory_type, field="memory_type")
        tr.create(
            task_id,
            TASK_TYPE,
            owner=SERVICE_SUBJECT,
            run_id=run_id,
            metadata={
                "algorithm_version": self.config.algorithm_version,
                "memory_type": memory_type or "",
                "retry_count": self._retry_count(task_id),
            },
        )
        state = tr.get(task_id)["status"]
        base = self._base_result(task_id, run_id)
        if state == CANCELLED:
            return JMRRunResult(
                **{
                    **base.__dict__,
                    "status": CANCELLED,
                    "expired_voided": expired_voided,
                }
            )
        if state == SUCCEEDED and not resume:
            return JMRRunResult(
                **{
                    **base.__dict__,
                    "status": SUCCEEDED,
                    "expired_voided": expired_voided,
                }
            )
        if state == PAUSED and not resume:
            return JMRRunResult(
                **{
                    **base.__dict__,
                    "status": PAUSED,
                    "expired_voided": expired_voided,
                }
            )
        if state == FAILED and not resume:
            task = tr.get(task_id) or {}
            return JMRRunResult(
                **{
                    **base.__dict__,
                    "status": FAILED,
                    "failure_reason": task.get("failure_reason") or "",
                    "retry_eligible": False,
                    "expired_voided": expired_voided,
                }
            )
        if state == "RUNNING":
            # JMR-08 audit P1-6: ``resume`` must not be a blanket bypass. Two
            # concurrent runs of one task would double-charge cost and race the
            # terminal transition. Only a run that was explicitly resumed via
            # JMRService.resume() (control endpoint) may continue a RUNNING
            # task, and the marker is consumed exactly once.
            allowed = resume and self._consume_resume_marker(task_id)
            if not allowed:
                raise JMRConcurrencyError(
                    f"task {task_id} is already running",
                    detail={"code": "jmr_run_conflict", "task_id": task_id},
                )
        if state == FAILED and resume:
            tr.recover(task_id, run_id=run_id)
            state = "CREATED"
        if state == PAUSED and resume:
            tr.resume(task_id, run_id=run_id)
        elif state == "CREATED":
            # fail() is legal only from RUNNING, so the first cost check must
            # happen AFTER start() (v1.2 P2-6).
            tr.start(task_id, run_id=run_id)

        created = {rule: 0 for rule in RULES}
        skipped = 0
        cost_total = 0.0
        cost_status = ""
        degradation = ""
        retry_count = self._retry_count(task_id)
        retry_eligible = False
        breaker_tripped = breaker.tripped
        try:
            # STAGE 1: READ
            stage_at = self._stage_started()
            control = self._stage_open(task_id)
            if control in (PAUSED, CANCELLED):
                return JMRRunResult(**{**base.__dict__, "status": control,
                                       "expired_voided": expired_voided})
            cost = self._cost(task_id, run_id)
            cost_total += cost["cost_cny"]
            cost_status = cost["cost_status"]
            degradation = cost["degradation"] or degradation
            rows = self._read_rows(memory_type)
            records = [SourceRecord.from_projection(row) for row in rows]
            self._check_stage_timeout(task_id, stage_at)

            # STAGE 2: DETECT
            stage_at = self._stage_started()
            control = self._stage_open(task_id)
            if control in (PAUSED, CANCELLED):
                return JMRRunResult(**{**base.__dict__, "status": control,
                                       "expired_voided": expired_voided})
            cost = self._cost(task_id, run_id)
            cost_total += cost["cost_cny"]
            cost_status = cost["cost_status"]
            degradation = cost["degradation"] or degradation
            clusters, changes, conflicts, related, summary = self._detect(records)
            self._check_stage_timeout(task_id, stage_at)

            # STAGE 3: WRITE
            stage_at = self._stage_started()
            control = self._stage_open(task_id)
            if control in (PAUSED, CANCELLED):
                return JMRRunResult(**{**base.__dict__, "status": control,
                                       "expired_voided": expired_voided})
            cost = self._cost(task_id, run_id)
            cost_total += cost["cost_cny"]
            cost_status = cost["cost_status"]
            degradation = cost["degradation"] or degradation
            created, skipped = self._write(
                records=records,
                clusters=clusters,
                changes=changes,
                conflicts=conflicts,
                related=related,
                summary=summary,
                task_id=task_id,
                run_id=run_id,
            )
            self._check_stage_timeout(task_id, stage_at)
            tr.succeed(task_id, run_id=run_id)
            self._reset_retry(task_id)
            retry_count = 0
            new_breaker = record_outcome(
                breaker, SUCCEEDED,
                policy=BreakerPolicy(self.config.breaker_threshold),
            )
            self._save_breaker(new_breaker)
            breaker = new_breaker
            created_total = sum(created.values())
            if created_total > 0:
                self._audit_cost(
                    task_id=task_id,
                    run_id=run_id,
                    cost_cny=cost_total,
                    degradation=degradation,
                )
            # JMR-01 standard 8: a resume that writes nothing must not append a
            # second run-level audit row; the first run's audit already exists.
            if created_total > 0 or skipped == 0:
                self._audit_run(
                    task_id=task_id, run_id=run_id, status=SUCCEEDED,
                    extra={
                        "cost_cny": cost_total,
                        "degradation": degradation,
                        "expired_voided": expired_voided,
                    },
                )
        except CostBlockedError as exc:
            reason = f"cost_governance_blocked:{type(exc).__name__}:{exc}"
            tr.fail(task_id, reason=reason, run_id=run_id)
            retry_count, retry_eligible = self._retry_decision(task_id, reason)
            new_breaker = record_outcome(
                breaker, FAILED,
                policy=BreakerPolicy(self.config.breaker_threshold),
            )
            self._save_breaker(new_breaker)
            self._audit_run(
                task_id=task_id, run_id=run_id, status=FAILED,
                failure_reason=reason,
                extra={"retry_count": retry_count, "retry_eligible": retry_eligible},
            )
            return JMRRunResult(
                **{
                    **self._base_result(task_id, run_id).__dict__,
                    "status": BLOCKED,
                    "created": created,
                    "skipped_idempotent": skipped,
                    "failure_reason": reason,
                    "cost_cny": cost_total,
                    "cost_status": cost_status,
                    "degradation": degradation,
                    "retry_count": retry_count,
                    "retry_eligible": retry_eligible,
                    "expired_voided": expired_voided,
                    "breaker_tripped": new_breaker.tripped,
                }
            )
        except Exception as exc:
            reason = safe_error_message(exc, exact_tokens=self._exact_tokens())
            # Only fail when the task is still RUNNING; an external pause/cancel
            # is not an orchestration failure.
            current = tr.get(task_id)
            if current and current["status"] == "RUNNING":
                tr.fail(task_id, reason=reason, run_id=run_id)
            retry_count, retry_eligible = self._retry_decision(task_id, reason)
            new_breaker = record_outcome(
                breaker, FAILED,
                policy=BreakerPolicy(self.config.breaker_threshold),
            )
            self._save_breaker(new_breaker)
            self._audit_run(
                task_id=task_id, run_id=run_id, status=FAILED,
                failure_reason=reason,
                extra={"retry_count": retry_count, "retry_eligible": retry_eligible},
            )
            return JMRRunResult(
                **{
                    **self._base_result(task_id, run_id).__dict__,
                    "status": FAILED,
                    "created": created,
                    "skipped_idempotent": skipped,
                    "failure_reason": reason,
                    "cost_cny": cost_total,
                    "cost_status": cost_status,
                    "degradation": degradation,
                    "retry_count": retry_count,
                    "retry_eligible": retry_eligible,
                    "expired_voided": expired_voided,
                    "breaker_tripped": new_breaker.tripped,
                }
            )
        return JMRRunResult(
            task_id=task_id,
            run_id=run_id,
            status=SUCCEEDED,
            created=created,
            skipped_idempotent=skipped,
            memory_rows=self._total(),
            projection_digest=ReadOnlyProjection(self.manager).content_digest(),
            cost_cny=cost_total,
            cost_status=cost_status,
            degradation=degradation,
            retry_count=retry_count,
            retry_eligible=retry_eligible,
            expired_voided=expired_voided,
            breaker_tripped=breaker_tripped,
        )

    # ------------------------------------------------------------ operations
    def task_status(self, task_id: str) -> Dict[str, Any]:
        self._require_p1_schema()
        task = self.task_registry.get(task_id)
        if task is None or task.get("task_type") != TASK_TYPE:
            from .errors import JMRNotFoundError

            raise JMRNotFoundError(f"JMR task {task_id!r} not found")
        events = self.task_registry.events(task_id, limit=1000)
        retry_count = self._retry_count(task_id)
        decision = decide_retry(
            {
                "task_id": task_id,
                "status": task.get("status"),
                "failure_reason": task.get("failure_reason") or "",
                "metadata_json": json.dumps({"retry_count": retry_count}),
            },
            policy=RetryPolicy(
                max_retries=self.config.max_retries,
                retryable_codes=RETRYABLE_FAILURE_CODES,
            ),
        )
        return {
            "task_id": task_id,
            "status": task.get("status", ""),
            "run_id": task.get("run_id", ""),
            "event_count": len(events),
            "last_event": events[-1]["event_type"] if events else None,
            "failure_reason": task.get("failure_reason") or "",
            "retry_count": retry_count,
            "retry_eligible": decision.action == "MANUAL_RETRY_ELIGIBLE",
        }

    def pause(self, task_id: str, *, reason: str = "") -> Dict[str, Any]:
        self._require_p1_schema()
        self._require_jmr_task(task_id)
        if reason:
            assert_safe_text(reason, field="reason")
        result = self.task_registry.pause(task_id, reason=reason)
        return {
            "task_id": task_id,
            "status": result.to_status,
            "noop": result.noop,
            "event_id": result.event_id,
        }

    def resume(self, task_id: str) -> Dict[str, Any]:
        self._require_p1_schema()
        self._require_jmr_task(task_id)
        result = self.task_registry.resume(task_id)
        current = self.task_registry.get(task_id) or {}
        if not result.noop or current.get("status") == "RUNNING":
            # One-shot marker: a RUNNING task may be continued by a single
            # resume-triggered run, never by a concurrent run that merely
            # passes resume=true. RUNNING -> RUNNING is a registry no-op, but an
            # explicit operator resume still expresses that intent.
            self._write_setting(RESUME_KEY_PREFIX + task_id, {"requested": True})
        return {
            "task_id": task_id,
            "status": result.to_status,
            "noop": result.noop,
            "event_id": result.event_id,
        }

    def cancel(self, task_id: str, *, reason: str = "") -> Dict[str, Any]:
        self._require_p1_schema()
        self._require_jmr_task(task_id)
        if reason:
            assert_safe_text(reason, field="reason")
        result = self.task_registry.cancel(task_id, reason=reason)
        if result.noop:
            return {
                "task_id": task_id,
                "status": result.to_status,
                "noop": True,
                "event_id": "",
            }
        return {
            "task_id": task_id,
            "status": result.to_status,
            "noop": False,
            "event_id": result.event_id,
        }

    def _consume_resume_marker(self, task_id: str) -> bool:
        """Atomically consume the one-shot resume marker (re-audit N-P2-1).

        A read-then-write pair let two concurrent runs both observe ``true``.
        A single conditional UPDATE makes exactly one caller win.
        """
        requested = json.dumps({"requested": True}, sort_keys=True)
        consumed = json.dumps({"requested": False}, sort_keys=True)
        with self.manager.session() as session:
            result = session.execute(
                text(
                    "UPDATE system_settings SET value=:new, updated_at=:u "
                    "WHERE key=:k AND value=:old"
                ),
                {
                    "new": consumed,
                    "u": utc_now_iso(),
                    "k": RESUME_KEY_PREFIX + task_id,
                    "old": requested,
                },
            )
            return result.rowcount == 1

    def _exact_tokens(self) -> tuple:
        """Live server secrets with no regex shape (used only for redaction)."""
        settings = getattr(self.cost_governor, "settings", None)
        values = []
        for name in ("api_token", "llm_api_key"):
            value = getattr(settings, name, "") if settings is not None else ""
            if isinstance(value, str) and len(value) >= 4:
                values.append(value)
        return tuple(values)

    def _require_jmr_task(self, task_id: str) -> None:
        task = self.task_registry.get(task_id)
        if task is None or task.get("task_type") != TASK_TYPE:
            from .errors import JMRNotFoundError

            raise JMRNotFoundError(f"JMR task {task_id!r} not found")

    def reset_breaker(self, *, operator_confirmed: bool = True) -> Dict[str, Any]:
        """Explicit human reset; the scheduler never calls this automatically."""
        state = manual_reset(
            self._load_breaker(), operator_confirmed=operator_confirmed
        )
        self._save_breaker(state)
        self.audit_bus.record(
            principal_id=SERVICE_SUBJECT,
            principal_type=SERVICE_TYPE,
            operation="JMR_BREAKER_RESET",
            object_ref="GLOBAL",
            trigger=TRIGGER,
            status=SUCCEEDED,
            extra={"operator_confirmed": True},
        )
        return {"tripped": state.tripped, "consecutive_failures": 0}

    def plan_batches(self, task_ids: Sequence[str], *, batch_size: int) -> List[Dict[str, Any]]:
        from .scheduler import plan_batches

        return [
            {"batch_id": batch.batch_id, "task_ids": list(batch.task_ids)}
            for batch in plan_batches(task_ids, batch_size=batch_size)
        ]

    #
    def read_derived(self, *, rule: str = "") -> Dict[str, List[Dict[str, Any]]]:
        """Return derived rows grouped by their frozen ``rule`` key.

        ``DerivedStore.list_derived`` groups by physical table for persistence;
        the public read API is rule-keyed (§20.1), so a caller never needs to
        know which table a proposal landed in.
        """
        by_table = self.store.list_derived(rule=rule)
        out: Dict[str, List[Dict[str, Any]]] = {name: [] for name in RULES}
        for table_key, items in by_table.items():
            # ``corrupt`` holds unparseable rows. They must never be bucketed
            # into a valid rule, so they are excluded from the rule-keyed read.
            if table_key == "corrupt":
                continue
            for item in items:
                key = rule_of(item.get("metadata_json"))
                if key in out:
                    out[key].append(item)
        if rule:
            return {rule: out.get(rule, [])}
        return out

    def void_and_rebuild(self, *, task_id: str, run_id: str) -> JMRRunResult:
        """Logically invalidate this module's derived rows, then regenerate.

        VOID rows are REVIVED by ``DerivedStore.upsert_*`` (v1.2 P1-4 fix), so
        a rebuild restores exactly the same number of PENDING proposals.
        """
        self.store.void_derived()
        rows = self._read_rows(None)
        records = [SourceRecord.from_projection(row) for row in rows]
        clusters, changes, conflicts, related, summary = self._detect(records)
        created, skipped = self._write(
            records=records,
            clusters=clusters,
            changes=changes,
            conflicts=conflicts,
            related=related,
            summary=summary,
            task_id=task_id,
            run_id=run_id,
        )
        self._audit_run(task_id=task_id, run_id=run_id, status=SUCCEEDED)
        return JMRRunResult(
            task_id=task_id,
            run_id=run_id,
            status=SUCCEEDED,
            created=created,
            skipped_idempotent=skipped,
            memory_rows=self._total(),
            projection_digest=ReadOnlyProjection(self.manager).content_digest(),
        )
