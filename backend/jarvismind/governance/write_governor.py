"""P1-A: the single authorized door for original-memory writes.

Two layers, both fail-closed:

1. ``WriteGovernor.authorize()`` verifies a server-issued TrustedPrincipal and
   (for every write) a cryptographically signed, one-shot SignedGrant. It also
   runs CostGovernor and records the decision on the AuditBus.
2. ``models.py`` enforces at the ORM boundary that every INSERT/UPDATE of
   ``memory_records`` / ``memory_candidates`` happens inside an
   ``authorized_write`` context while governance is active. This covers direct
   creation, batch import, approval submission, lifecycle writes and raw ORM
   calls alike - no caller can skip the door by picking another service.

Client-supplied identity claims are never trusted: ``actor``, ``actor_label``,
``SERVER_ACTOR`` and ``required_grants_json`` are labels/data, not identities.
The audit ``actor`` is always ``WriteContext.principal_id``, derived on the
server. Complexity: O(1) per authorization, O(1) per write.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import text

from ..core.timeutils import utc_now_iso
from ..p1_gate import WriteContext, authorized_write, is_active
from ..schemas.memory import MemoryCreate, MemoryUpdate
from ..services.memory_service import MemoryService
from .audit_bus import STATUS_ALLOWED, STATUS_DENIED, STATUS_FAILED, STATUS_SUCCEEDED
from .config import GovernanceConfig
from .errors import (
    CostBlockedError,
    GrantVerificationError,
    UnauthorizedWriteError,
    UntrustedIdentityError,
)
from .identity import CAP_MEMORY_WRITE, GrantAuthority, SignedGrant, TrustedPrincipal

OP_CREATE = "MEMORY_CREATE"
OP_UPDATE = "MEMORY_UPDATE"
OP_DELETE = "MEMORY_DELETE"
OP_IMPORT = "MEMORY_IMPORT"
OP_APPROVE = "CANDIDATE_APPROVE"

_IDEM_PREFIX = "p1.write.idem."


@dataclass(frozen=True)
class AuthorizationResult:
    context: WriteContext
    grant_id: str
    cost_reason: str


class WriteGovernor:
    def __init__(
        self,
        manager,
        settings,
        *,
        config: GovernanceConfig,
        grant_authority: GrantAuthority,
        audit_bus,
        cost_governor,
        task_registry,
        enforce: bool = True,
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.config = config
        self.grant_authority = grant_authority
        self.audit_bus = audit_bus
        self.cost_governor = cost_governor
        self.task_registry = task_registry
        self.enforce = enforce

    def _deny(self, reason: str, **fields) -> None:
        self.audit_bus.record(
            principal_id=fields.get("principal_id", "") or "UNTRUSTED",
            principal_type=fields.get("principal_type", ""),
            operation=fields.get("operation", "UNKNOWN"),
            object_ref=fields.get("object_ref", "GLOBAL"),
            trigger=fields.get("trigger", ""),
            status=STATUS_DENIED,
            failure_reason=reason,
            task_id=fields.get("task_id", ""),
            run_id=fields.get("run_id", ""),
            grant_id=fields.get("grant_id", ""),
        )

    def authorize(
        self,
        *,
        principal,
        grant,
        operation: str,
        object_ref: str,
        trigger: str,
        task_id: str = "",
        run_id: str = "",
        expected_cost_cny: float = 0.0,
        expected_tokens: int = 0,
        check_cost: bool = True,
    ) -> AuthorizationResult:
        """Verify identity + signature + budget, audit the decision, return a
        WriteContext that is required by the ORM gate."""
        base = {
            "operation": operation,
            "object_ref": object_ref,
            "trigger": trigger,
            "task_id": task_id,
            "run_id": run_id,
        }

        # 1. Identity: only a server-MAC'd TrustedPrincipal is accepted.
        if not isinstance(principal, TrustedPrincipal):
            self._deny("principal_is_not_trusted", **base)
            raise UntrustedIdentityError("a TrustedPrincipal is required")
        if not principal.verify(self.settings, key_version=self.config.grant_key_version):
            self._deny("principal_mac_invalid", **{**base, "principal_id": principal.subject_id})
            raise UntrustedIdentityError("principal MAC verification failed")
        if not principal.has(CAP_MEMORY_WRITE):
            self._deny(
                "principal_lacks_memory_write",
                **{**base, "principal_id": principal.subject_id},
            )
            raise UnauthorizedWriteError("principal lacks MEMORY_WRITE capability")
        identity = {
            "principal_id": principal.subject_id,
            "principal_type": principal.principal_type,
        }

        # 2. Signed grant: every field is bound to this concrete operation.
        if not isinstance(grant, SignedGrant):
            self._deny("signed_grant_required", **{**base, **identity})
            raise GrantVerificationError("a SignedGrant is required for an original write")
        if grant.subject_id != principal.subject_id:
            self._deny(
                "grant_subject_mismatch",
                **{**base, **identity, "grant_id": grant.grant_id},
            )
            raise GrantVerificationError("grant subject does not match the principal")
        if grant.operation != operation:
            self._deny(
                "grant_operation_mismatch",
                **{**base, **identity, "grant_id": grant.grant_id},
            )
            raise GrantVerificationError("grant is bound to a different operation")
        if grant.object_ref not in (object_ref, "*"):
            self._deny(
                "grant_object_mismatch",
                **{**base, **identity, "grant_id": grant.grant_id},
            )
            raise GrantVerificationError("grant is bound to a different object")
        if CAP_MEMORY_WRITE not in grant.grants:
            self._deny(
                "grant_lacks_memory_write",
                **{**base, **identity, "grant_id": grant.grant_id},
            )
            raise GrantVerificationError("grant does not carry MEMORY_WRITE")
        verification = self.grant_authority.verify(grant)
        if not verification.ok:
            self._deny(
                f"grant_verification_{verification.reason}",
                **{**base, **identity, "grant_id": grant.grant_id},
            )
            raise GrantVerificationError(
                "signed grant failed verification",
                detail={"reason": verification.reason, "grant_id": grant.grant_id},
            )

        # 3. Cost governance (global ledger + task/batch/rate/token/call caps).
        cost_reason = "not_checked"
        if check_cost:
            try:
                check = self.cost_governor.enforce(
                    task_id=task_id or principal.subject_id,
                    run_id=run_id,
                    expected_cost_cny=expected_cost_cny,
                    expected_tokens=expected_tokens,
                )
                cost_reason = check.reason
            except CostBlockedError:
                self._deny(
                    "cost_governance_blocked",
                    **{**base, **identity, "grant_id": grant.grant_id},
                )
                raise

        ctx = WriteContext(
            principal_id=principal.subject_id,
            operation=operation,
            object_ref=object_ref,
            trigger=trigger,
            task_id=task_id,
            run_id=run_id,
            grant_id=grant.grant_id,
        )
        self.audit_bus.record(
            **identity,
            operation=operation,
            object_ref=object_ref,
            trigger=trigger,
            status=STATUS_ALLOWED,
            task_id=task_id,
            run_id=run_id,
            grant_id=grant.grant_id,
            extra={"cost_reason": cost_reason},
        )
        return AuthorizationResult(ctx, grant.grant_id, cost_reason)

    def assert_orm_gate_active(self) -> Dict[str, Any]:
        """Structural evidence that the ORM-level door is switched on."""
        return {
            "active": is_active(),
            "enforce": self.enforce,
            "governed_tables": ("memory_records", "memory_candidates"),
        }


class GovernedMemoryGateway:
    """The only write door for P1 and every future derived module.

    It reuses ``MemoryService`` for the actual business logic (content scan,
    FTS text, audit row, vector hook), but wraps it in an authorized context so
    the ORM gate cannot be bypassed.
    """

    def __init__(self, manager, governor: WriteGovernor) -> None:
        self.manager = manager
        self.governor = governor

    def _service(self) -> MemoryService:
        # No embedding/vector provider: governance writes stay independent of
        # optional model backends and are safe in isolated test databases.
        return MemoryService(self.manager)

    def _idem_get(self, key: str) -> Optional[str]:
        with self.manager.session() as session:
            return session.execute(
                text("SELECT value FROM system_settings WHERE key=:k"),
                {"k": _IDEM_PREFIX + key},
            ).scalar()

    def _idem_set(self, key: str, memory_id: str) -> None:
        with self.manager.session() as session:
            session.execute(
                text(
                    "INSERT INTO system_settings(key, value, updated_at) "
                    "VALUES (:k, :v, :u) ON CONFLICT(key) DO NOTHING"
                ),
                {"k": _IDEM_PREFIX + key, "v": memory_id, "u": utc_now_iso()},
            )

    def _finish(
        self,
        result: AuthorizationResult,
        *,
        operation: str,
        object_ref: str,
        trigger: str,
        principal: TrustedPrincipal,
        cost_cny: float = 0.0,
        tokens: int = 0,
        status: str = STATUS_SUCCEEDED,
        failure_reason: str = "",
    ) -> None:
        self.governor.audit_bus.record(
            principal_id=principal.subject_id,
            principal_type=principal.principal_type,
            operation=operation,
            object_ref=object_ref,
            trigger=trigger,
            status=status,
            failure_reason=failure_reason,
            task_id=result.context.task_id,
            run_id=result.context.run_id,
            grant_id=result.grant_id,
        )
        if status == STATUS_SUCCEEDED:
            self.governor.cost_governor.record(
                task_id=result.context.task_id or principal.subject_id,
                run_id=result.context.run_id,
                cost_cny=cost_cny,
                tokens=tokens,
            )
            # One-shot: a replay of the same grant is rejected afterwards.
            self.governor.grant_authority.consume(result.grant_id)

    def create_memory(
        self,
        payload: MemoryCreate,
        *,
        principal,
        grant,
        trigger: str = "P1_GATEWAY",
        task_id: str = "",
        run_id: str = "",
        object_ref: str = "NEW",
        idempotency_key: str = "",
        expected_cost_cny: float = 0.0,
        expected_tokens: int = 0,
    ):
        if idempotency_key:
            existing_id = self._idem_get(idempotency_key)
            if existing_id:
                return self._service().get(existing_id, touch=False)
        result = self.governor.authorize(
            principal=principal,
            grant=grant,
            operation=OP_CREATE,
            object_ref=object_ref,
            trigger=trigger,
            task_id=task_id,
            run_id=run_id,
            expected_cost_cny=expected_cost_cny,
            expected_tokens=expected_tokens,
        )
        try:
            with authorized_write(result.context):
                record = self._service().create(payload, actor=result.context.principal_id)
        except Exception as exc:
            self._finish(
                result,
                operation=OP_CREATE,
                object_ref=object_ref,
                trigger=trigger,
                principal=principal,
                status=STATUS_FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise
        if idempotency_key:
            self._idem_set(idempotency_key, record.id)
        self._finish(
            result,
            operation=OP_CREATE,
            object_ref=record.id,
            trigger=trigger,
            principal=principal,
            cost_cny=expected_cost_cny,
            tokens=expected_tokens,
        )
        return record

    def update_memory(
        self,
        memory_id: str,
        payload: MemoryUpdate,
        *,
        principal,
        grant,
        trigger: str = "P1_GATEWAY",
        task_id: str = "",
        run_id: str = "",
    ):
        result = self.governor.authorize(
            principal=principal,
            grant=grant,
            operation=OP_UPDATE,
            object_ref=memory_id,
            trigger=trigger,
            task_id=task_id,
            run_id=run_id,
        )
        try:
            with authorized_write(result.context):
                record = self._service().update(
                    memory_id, payload, actor=result.context.principal_id
                )
        except Exception as exc:
            self._finish(
                result,
                operation=OP_UPDATE,
                object_ref=memory_id,
                trigger=trigger,
                principal=principal,
                status=STATUS_FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._finish(
            result,
            operation=OP_UPDATE,
            object_ref=memory_id,
            trigger=trigger,
            principal=principal,
        )
        return record

    def delete_memory(
        self,
        memory_id: str,
        *,
        principal,
        grant,
        trigger: str = "P1_GATEWAY",
        task_id: str = "",
        run_id: str = "",
    ):
        result = self.governor.authorize(
            principal=principal,
            grant=grant,
            operation=OP_DELETE,
            object_ref=memory_id,
            trigger=trigger,
            task_id=task_id,
            run_id=run_id,
        )
        try:
            with authorized_write(result.context):
                record = self._service().delete(
                    memory_id, actor=result.context.principal_id
                )
        except Exception as exc:
            self._finish(
                result,
                operation=OP_DELETE,
                object_ref=memory_id,
                trigger=trigger,
                principal=principal,
                status=STATUS_FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._finish(
            result,
            operation=OP_DELETE,
            object_ref=memory_id,
            trigger=trigger,
            principal=principal,
        )
        return record

    def import_memories(
        self,
        payloads: Sequence[MemoryCreate],
        *,
        principal,
        grants: Sequence[SignedGrant],
        trigger: str = "P1_IMPORT",
        task_id: str = "",
        run_id: str = "",
    ):
        """Batch import - each item needs its own verified grant and its own
        authorized context. A batch-wide ``*`` grant is accepted per item."""
        if len(payloads) != len(grants):
            raise GrantVerificationError("each imported item needs one signed grant")
        records = []
        for index, (payload, grant) in enumerate(zip(payloads, grants)):
            records.append(
                self.create_memory(
                    payload,
                    principal=principal,
                    grant=grant,
                    trigger=trigger,
                    task_id=task_id,
                    run_id=run_id,
                    object_ref=f"IMPORT:{index}",
                    idempotency_key=f"import:{run_id}:{index}" if run_id else "",
                )
            )
        return records

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        principal,
        grant,
        reason: str = "",
        trigger: str = "P1_APPROVAL",
        task_id: str = "",
        run_id: str = "",
    ):
        """Approval submission goes through the same door.

        ``ApprovalService`` still enforces its own 003-B rules (SCANNED status,
        content fingerprint, PII dual-authorization fail-closed). P1 does not
        weaken them; it adds the identity/grant/audit/cost layer in front.
        """
        from ..services.approval_service import ApprovalService

        result = self.governor.authorize(
            principal=principal,
            grant=grant,
            operation=OP_APPROVE,
            object_ref=candidate_id,
            trigger=trigger,
            task_id=task_id,
            run_id=run_id,
        )
        try:
            with authorized_write(result.context):
                response = ApprovalService(self.manager).review(
                    candidate_id, decision=decision, reason=reason
                )
        except Exception as exc:
            self._finish(
                result,
                operation=OP_APPROVE,
                object_ref=candidate_id,
                trigger=trigger,
                principal=principal,
                status=STATUS_FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._finish(
            result,
            operation=OP_APPROVE,
            object_ref=candidate_id,
            trigger=trigger,
            principal=principal,
        )
        return response
