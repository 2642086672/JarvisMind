"""Process-wide container holding the settings, database, and logger."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Settings
from .database.connection import DatabaseManager


@dataclass
class Container:
    settings: Settings
    manager: DatabaseManager
    logger: logging.Logger
    started_at: float = field(default=0.0)
    _embedding_provider: Any = field(default=None, init=False)
    _vector_store: Any = field(default=None, init=False)
    _governance: Any = field(default=None, init=False)
    _jmr_query: Any = field(default=None, init=False)
    _jmr_cost_governance: Any = field(default=None, init=False)
    _jmr_service: Any = field(default=None, init=False)
    _jmr_bridge: Any = field(default=None, init=False)
    _jmr_scheduler: Any = field(default=None, init=False)
    _auto_memory: Any = field(default=None, init=False)
    _jkg_adapter: Any = field(default=None, init=False)
    _jkg_lifecycle: Any = field(default=None, init=False)
    _jkg_ingestor: Any = field(default=None, init=False)
    _jkg_initializer: Any = field(default=None, init=False)
    _jkg_worker: Any = field(default=None, init=False)
    _mce_adapter: Any = field(default=None, init=False)
    _overview_cache: Any = field(default=None, init=False)

    def _overview_cache_instance(self):
        """One overview cache per container.

        A process-wide cache would let one memory space's AI summaries be served
        to another space whose source version happens to match.
        """
        if self._overview_cache is None:
            from .services.memory_overview_cache import MemoryOverviewCache

            self._overview_cache = MemoryOverviewCache()
        return self._overview_cache

    @property
    def embedding_provider(self):
        if self._embedding_provider is None:
            from .services.embedding import create_embedding_provider

            self._embedding_provider = create_embedding_provider(self.settings)
        return self._embedding_provider

    @property
    def vector_store(self):
        if self._vector_store is None:
            from .services.vector_store import VectorStore

            self._vector_store = VectorStore(
                path=self.settings.qdrant_path,
                collection_name=self.settings.qdrant_collection,
                dimension=self.settings.embedding_dimension,
            )
        return self._vector_store

    @property
    def memory_context_service(self):
        """JME-MEMORY-CONTEXT-01: read-only retrieval for chat injection.

        Reuses the existing hybrid search for candidates, then applies the
        expiry, sensitivity and relevance gates. Never writes anything.
        """
        from .services.memory_context_service import MemoryContextService

        return MemoryContextService(
            manager=self.manager,
            hybrid_search=self.hybrid_search_service,
            min_match_ratio=self.settings.memory_context_min_match_ratio,
            candidate_limit=self.settings.memory_context_candidate_limit,
        )

    @property
    def hybrid_search_service(self):
        from .services.hybrid_search_service import HybridSearchService

        return HybridSearchService(
            manager=self.manager,
            embedding_provider=self.embedding_provider,
            vector_store=self.vector_store,
            settings=self.settings,
        )

    @property
    def memory_service(self):
        from .services.memory_service import MemoryService

        return MemoryService(
            self.manager,
            embedding_provider=self.embedding_provider,
            vector_store=self.vector_store,
        )

    @property
    def memory_overview_service(self):
        """JME-MEMORY-OVERVIEW-01: read-only overview with derived AI summaries.

        The overview itself is always built deterministically from ACTIVE
        memories. When ``JARVISMIND_LLM_BASE_URL`` / ``JARVISMIND_LLM_MODEL`` are configured,
        an enricher is attached and its output is cached by ``source_version``
        so a page refresh costs nothing until memories actually change.
        """
        from .services.memory_overview import MemoryOverviewService
        from .services.memory_overview_enricher import build_overview_enricher

        return MemoryOverviewService(
            self.manager,
            enricher=build_overview_enricher(self.settings, logger_=self.logger),
            cache=self._overview_cache_instance(),
        )

    @property
    def system_service(self):
        from .services.system_service import SystemService

        return SystemService(self.manager, self.settings, self.started_at)

    @property
    def source_service(self):
        from .services.source_service import SourceService

        return SourceService(self.manager)

    @property
    def extraction_engine(self):
        from .services.extraction import ExtractionEngine

        return ExtractionEngine(self.manager, self.settings, logger=self.logger)

    @property
    def auto_memory_service(self):
        """JEA-01: non-sensitive automatic memory writes through ApprovalService.

        C-1: the wired ``memory_service`` (embedding + vector_store) is injected
        so auto_merge / auto_expire / auto-update really delete and reindex
        vector points instead of silently skipping the vector hooks.
        """
        if self._auto_memory is None:
            from .services.auto_memory import AutoMemoryService

            self._auto_memory = AutoMemoryService(
                self.manager, memory_service=self.memory_service
            )
        return self._auto_memory

    @property
    def lifecycle_service(self):
        from .services.lifecycle_service import LifecycleService
        from .services.llm import build_provider
        from .services.llm.budget import BudgetGuard
        from .services.reflection import ReflectionEngine

        provider = build_provider(self.settings)
        budget = None
        if provider is not None:
            budget = BudgetGuard(
                self.manager,
                limit_cny=self.settings.llm_budget_cny,
                price_per_1k_input_cny=self.settings.llm_price_in,
                price_per_1k_output_cny=self.settings.llm_price_out,
            )
        engine = ReflectionEngine(
            provider,
            settings=self.settings,
            budget=budget,
            redaction_tokens=(self.settings.api_token, self.settings.llm_api_key),
            logger=self.logger,
        )
        return LifecycleService(
            self.manager,
            memory_service=self.memory_service,
            reflection_engine=engine,
            settings=self.settings,
        )

    @property
    def forget_service(self):
        """JW-01: forget-request approval flow (request -> approve/reject)."""
        from .services.forget_service import ForgetApprovalService

        ttl = int(getattr(self.settings, "forget_token_ttl_seconds", 300))
        return ForgetApprovalService(
            self.manager,
            vector_store=self.vector_store,
            token_ttl_seconds=ttl,
        )

    @property
    def modification_service(self):
        """JW-03: controlled modification flow for approved memories."""
        from .services.modification_service import ModificationRequestService

        ttl = int(getattr(self.settings, "forget_token_ttl_seconds", 300))
        return ModificationRequestService(self.manager, token_ttl_seconds=ttl)

    @property
    def migration_service(self):
        from .services.migration_service import MigrationService

        return MigrationService(self.manager, memory_service=self.memory_service)

    @property
    def jmr_query_service(self):
        """JMR-02 read facade. Never writes authoritative memory."""
        if self._jmr_query is None:
            from .jmr.config import JMRConfig
            from .jmr.query import JMRQueryService

            self._jmr_query = JMRQueryService(
                self.manager, config=JMRConfig.from_env()
            )
        return self._jmr_query

    @property
    def jmr_service(self):
        """JMR-02 orchestration facade wrapped with JMR-06 cost governance.

        Does not install P1 tables and does not activate automatic execution.
        """
        if self._jmr_service is None:
            from .jmr import build_jmr_service
            from .jmr.config import JMRConfig
            from .jmr.cost_governance import (
                CostGovernance,
                CostGovernanceConfig,
                CostGovernedJMRService,
            )

            service = build_jmr_service(
                manager=self.manager, config=JMRConfig.from_env()
            )
            config = CostGovernanceConfig.from_sources(
                self.settings,
                jmr_config=service.config,
                governor=getattr(service, "cost_governor", None),
            )
            self._jmr_cost_governance = CostGovernance(self.manager, config)
            self._jmr_service = CostGovernedJMRService(
                service, self._jmr_cost_governance
            )
        return self._jmr_service

    @property
    def jmr_cost_governance(self):
        """JMR-06 reservation, audit and report facade (metadata only)."""
        if self._jmr_cost_governance is None:
            _ = self.jmr_service
        return self._jmr_cost_governance

    @property
    def jmr_scheduler(self):
        """JMR-05 scheduler bound to the cost-governed service (pull-based).

        No thread or daemon is started here: a host calls ``tick()`` (see
        POST /api/v1/jmr/scheduler/tick) so scheduling stays explicit.
        """
        if self._jmr_scheduler is None:
            from .jmr.scheduler_engine import SchedulerConfig, SchedulerEngine

            self._jmr_scheduler = SchedulerEngine(
                self.manager,
                service=self.jmr_service,
                config=SchedulerConfig.from_env(),
            )
        return self._jmr_scheduler

    @property
    def jmr_review_bridge(self):
        """JMR-03 only review path for JMR-derived proposals."""
        if self._jmr_bridge is None:
            from .jmr.bridge import JMRReviewBridge

            service = self.jmr_service
            self._jmr_bridge = JMRReviewBridge(
                manager=self.manager,
                query=self.jmr_query_service,
                lifecycle=self.lifecycle_service,
                audit_bus=service.audit_bus,
                projection=service.projection,
            )
        return self._jmr_bridge

    @property
    def jkg_adapter(self):
        """JKG-INT-02 / D21 plan A: composition-root adapter for the derived graph.

        Constructing it opens no connection and installs nothing.
        """
        if self._jkg_adapter is None:
            from .jkg.adapter import JkgCompositionAdapter

            self._jkg_adapter = JkgCompositionAdapter(self.manager)
        return self._jkg_adapter

    @property
    def jkg_query_service(self):
        """Read-only graph facade. Raises when disabled or schema is absent."""
        return self.jkg_adapter.query_service()

    @property
    def jkg_service(self):
        """Write facade for JKG-owned workers. Never called by JME request paths."""
        return self.jkg_adapter.write_service()

    @property
    def jkg_ingestor(self):
        """JKG-INT-05 single-memory extraction. Used by the lifecycle and the initializer.

        Constructing it opens no connection and installs nothing.
        """
        if self._jkg_ingestor is None:
            from .jkg.ingestor import JkgIngestor

            self._jkg_ingestor = JkgIngestor(
                self.manager,
                adapter=self.jkg_adapter,
                exact_tokens=self._jkg_exact_tokens,
            )
        return self._jkg_ingestor

    @property
    def jkg_lifecycle(self):
        """JKG-INT-04 lifecycle consumer. Passive: it acts only when ticked.

        Constructing it opens no connection, installs nothing and starts no
        thread; the admin routes are the only production callers. Each live memory
        it sees is extracted through ``jkg_ingestor``.
        """
        if self._jkg_lifecycle is None:
            from .jkg.lifecycle import JkgLifecycleSync

            self._jkg_lifecycle = JkgLifecycleSync(
                self.manager,
                adapter=self.jkg_adapter,
                ingest=self.jkg_ingestor.ingest_memory,
            )
        return self._jkg_lifecycle

    @property
    def jkg_worker(self):
        """P0-1 pull-based JKG worker. Passive: it acts only when run.

        Constructing it opens no connection, installs nothing and starts no
        thread; ``run_once`` is the only entry point that does work, and the
        admin route is its production driver. It wraps ``jkg_lifecycle`` so the
        worker and the admin ``POST /sync/tick`` route share one consumer (and
        therefore one cursor).
        """
        if self._jkg_worker is None:
            from .jkg.worker import JkgWorker

            self._jkg_worker = JkgWorker(
                self.manager,
                adapter=self.jkg_adapter,
                lifecycle=self.jkg_lifecycle,
                ingest=self.jkg_ingestor.ingest_memory,
                config=self.settings,
            )
        return self._jkg_worker

    @property
    def jkg_initializer(self):
        """JKG-INT-05 historical backfill. Its task runs only when a caller starts one.

        The lifecycle is handed over so that a finished backfill replays the audit
        window written during the scan.
        """
        if self._jkg_initializer is None:
            from .governance.task_registry import TaskRegistry
            from .jkg.initializer import JkgInitializer

            self._jkg_initializer = JkgInitializer(
                self.manager,
                adapter=self.jkg_adapter,
                ingestor=self.jkg_ingestor,
                lifecycle=self.jkg_lifecycle,
                registry=TaskRegistry(self.manager),
            )
        return self._jkg_initializer

    @property
    def mce_adapter(self):
        """MCE-01A: read-only long-term project-context adapter.

        Constructing it opens no connection, installs no schema and reads no
        setting. MCE consumes JME through ``ReadOnlyProjection.for_module("MCE")``
        and JKG through the existing composition adapter; neither dependency is
        ever imported by JME core.
        """
        if self._mce_adapter is None:
            from .mce.adapter import MceAdapter

            self._mce_adapter = MceAdapter(
                self.manager, jkg_adapter=self.jkg_adapter
            )
        return self._mce_adapter

    def _jkg_exact_tokens(self) -> list:
        """The API token, as an exact-match token for the governance check.

        Read on every call, so a memory carrying the token is refused even if the
        token changed after the ingestor was built. An empty token contributes none.
        """
        token = getattr(self.settings, "api_token", None)
        return [str(token)] if token else []

    @property
    def governance(self):
        """Lazy, explicitly-enabled unified governance base.

        Returns None when JARVISMIND_P1_GOVERNANCE_ENABLED is not set. Enabling it
        calls build_governance(enforce=True), which installs the P1 tables and
        switches on the connection-level write gate for this process.
        """
        if self._governance is None:
            if not bool(getattr(self.settings, "p1_governance_enabled", False)):
                return None
            from .governance import build_governance

            self._governance = build_governance(
                self.manager, self.settings, enforce=True
            )
        return self._governance

    def close(self) -> None:
        if self._vector_store is not None:
            try:
                self._vector_store.close()
            except Exception:
                pass
            self._vector_store = None
