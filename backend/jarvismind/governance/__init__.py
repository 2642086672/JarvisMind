"""Unified governance base for JarvisMind (P1 gate).

Public surface (all Python-level; HTTP routers are a later, separately
authorised batch):

    build_governance(manager, settings, config=None) -> Governance
    deactivate_governance()                          -> None
    is_active() / active_governor()                  -> bool / object

Modules:
    config       GovernanceConfig (all parameters externalised)
    context      ContextVar write context + process-level governor state
    identity     TrustedPrincipal + SignedGrant (HMAC, key_version, nonce)
    schema       append-only P1 tables (CREATE TABLE IF NOT EXISTS only)
    audit_bus    AuditBus, reusing memory_audit_logs
    cost_governor CostGovernor, reusing BudgetGuard / system_settings
    task_registry TaskRegistry (create/query/pause/resume/cancel/recover)
    projection   ReadOnlyProjection for JMR / JKG / JPA
    write_governor WriteGovernor + GovernedMemoryGateway (the only write door)

Frozen architecture decisions honoured here: JME stays the single memory
authority; JKG is Minimal; JPA is L0-L2 only; this spec is an Upgrade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..p1_gate import activate, active_governor, deactivate, is_active, set_denial_hook
from .config import GovernanceConfig
from .identity import GrantAuthority, SignedGrant, TrustedPrincipal
from .audit_bus import AuditBus
from .cost_governor import CostGovernor
from .projection import ReadOnlyProjection
from .schema import P1_SCHEMA_VERSION, install_p1_schema
from .task_registry import TaskRegistry
from .write_governor import GovernedMemoryGateway, WriteGovernor

__all__ = [
    "Governance",
    "GovernanceConfig",
    "build_governance",
    "deactivate_governance",
    "activate",
    "deactivate",
    "active_governor",
    "is_active",
    "TrustedPrincipal",
    "SignedGrant",
    "GrantAuthority",
    "WriteGovernor",
    "GovernedMemoryGateway",
    "AuditBus",
    "CostGovernor",
    "TaskRegistry",
    "ReadOnlyProjection",
    "install_p1_schema",
    "P1_SCHEMA_VERSION",
]


@dataclass
class Governance:
    """Bundle returned by build_governance(). Every component shares one DB."""

    manager: Any
    settings: Any
    config: GovernanceConfig
    grant_authority: GrantAuthority
    audit_bus: AuditBus
    cost_governor: CostGovernor
    task_registry: TaskRegistry
    projection: ReadOnlyProjection
    write_governor: WriteGovernor
    gateway: GovernedMemoryGateway
    enforce: bool = True
    schema_install_result: Optional[dict] = None


def build_governance(
    manager,
    settings,
    config: Optional[GovernanceConfig] = None,
    *,
    enforce: bool = True,
    install_schema: bool = True,
) -> Governance:
    """Assemble and (optionally) activate the governance base.

    ``enforce=True`` turns on the ORM-level write gate: from that point every
    INSERT/UPDATE of memory_records or memory_candidates must run inside an
    ``authorized_write`` context, regardless of which service produced it.
    Default is off for every existing process, so the JME regression suite is
    byte-for-byte unaffected.
    """
    cfg = config or GovernanceConfig.from_settings(settings)
    schema_result = install_p1_schema(manager) if install_schema else None

    authority = GrantAuthority(manager, settings, config=cfg)
    audit = AuditBus(manager)
    cost = CostGovernor(manager, settings, config=cfg)
    tasks = TaskRegistry(manager)
    projection = ReadOnlyProjection(manager)
    governor = WriteGovernor(
        manager,
        settings,
        config=cfg,
        grant_authority=authority,
        audit_bus=audit,
        cost_governor=cost,
        task_registry=tasks,
        enforce=enforce,
    )
    gateway = GovernedMemoryGateway(manager, governor)
    gov = Governance(
        manager=manager,
        settings=settings,
        config=cfg,
        grant_authority=authority,
        audit_bus=audit,
        cost_governor=cost,
        task_registry=tasks,
        projection=projection,
        write_governor=governor,
        gateway=gateway,
        enforce=enforce,
        schema_install_result=schema_result,
    )
    if enforce:
        activate(governor)

        def _denial_hook(*, table: str, operation: str) -> None:
            # Independent session: the refused write's transaction may be gone.
            audit.record(
                principal_id="UNTRUSTED",
                principal_type="",
                operation=operation,
                object_ref=table,
                trigger="ORM_GATE",
                status="DENIED",
                failure_reason="write_without_authorized_context",
            )

        set_denial_hook(_denial_hook)
    return gov


def deactivate_governance() -> None:
    """Restore the process to the pre-governance state (test teardown / shutdown)."""
    deactivate()
    set_denial_hook(None)
