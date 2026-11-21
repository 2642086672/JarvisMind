"""Extraction orchestration (003-B security gate).

Extraction NEVER creates a formal memory. Every candidate is persisted as
PENDING / PENDING_AUTHORIZATION and only ``ApprovalService.review`` may create
``memory_records`` inside one atomic transaction.

Credentials are BLOCKed before candidate persistence: only a de-identified
``block_events`` row is written (no original value, no digest, no reversible
representation). Non-candidate outcomes live in ``extraction_events``.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ...config import Settings
from ...core.dedup import find_matches
from ...core.errors import ExtractionError, NotFoundError, ValidationError
from ...core.fingerprint import content_fingerprint
from ...core.logging import get_logger
from ...core.security_gate import (
    RULESET_VERSION,
    assert_safe_text,
    classify_candidate,
    credential_categories,
    required_grants_for,
    safe_hit_metadata,
    scan_credentials,
)
from ...core.sensitive import CREDENTIAL_CATEGORIES, PII_CATEGORIES
from ...core.sensitive import categories as sensitive_categories
from ...core.sensitive import pii_categories, redact, scan
from ...core.timeutils import utc_now_iso
from ...database.connection import DatabaseManager
from ...database.models import MemoryCandidate
from ...repositories.candidate_repository import CandidateRepository
from ...repositories.memory_repository import MemoryRepository
from ...repositories.security_event_repository import SecurityEventRepository
from ...schemas.extraction import (
    BlockEventResponse,
    CandidateResponse,
    CandidateStatus,
    ExtractMode,
    ExtractRequest,
    ExtractResponse,
    ExtractionEventResponse,
    OutcomeCounts,
)
from ...schemas.memory import MemoryType
from ...services.extraction.local import (
    ExtractionUnit,
    LocalCandidate,
    extract_locally,
    extract_units_with_security,
)
from ...services.extraction.prompt import build_messages, parse_response
from ...services.llm import LLMError, LLMProvider, build_provider
from ...services.llm.budget import BudgetGuard
from ...services.memory_service import MemoryService

ACTOR_USER = "user"
MAX_LLM_CANDIDATES = 15


def _stored_required_grants(raw: Optional[str]) -> List[str]:
    if not raw:
        return ["USER_APPROVAL"]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ["USER_APPROVAL"]
    return [str(item) for item in parsed] if isinstance(parsed, list) else ["USER_APPROVAL"]


class ExtractionEngine:
    def __init__(
        self,
        manager: DatabaseManager,
        settings: Settings,
        *,
        provider: Optional[LLMProvider] = None,
        logger=None,
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.provider = provider if provider is not None else build_provider(settings)
        self.logger = logger or get_logger("jme.extraction")
        self.budget = BudgetGuard(
            manager,
            limit_cny=settings.llm_budget_cny,
            price_per_1k_input_cny=settings.llm_price_in,
            price_per_1k_output_cny=settings.llm_price_out,
        )
        self.memory_service = MemoryService(manager)

    # ------------------------------------------------------------------ extract
    def extract(self, payload: ExtractRequest) -> ExtractResponse:
        """P1-1: authorise at the SERVICE ENTRY, before any DB session opens.

        Doing this inside repository.create() deadlocked SQLite (the grant /
        audit writes use their own session while the caller holds a write lock).
        """
        from ...governance.integration import authorized_operation

        with authorized_operation(
            manager=self.manager,
            operation="CANDIDATE_CREATE",
            object_ref="EXTRACT",
            trigger="EXTRACTION_ENGINE",
        ):
            return self._extract_impl(payload)

    def _extract_impl(self, payload: ExtractRequest) -> ExtractResponse:
        if payload.source_id:
            with self.manager.session() as session:
                if not CandidateRepository(session).source_exists(payload.source_id):
                    raise ValidationError(
                        f"Memory source {payload.source_id} does not exist"
                    )

        engine_name = self._select_engine(payload.mode)
        # Keep the ORIGINAL text for credential classification: redaction
        # replaces values with [REDACTED], which would otherwise hide the very
        # secret we must BLOCK before persistence.
        original_conversation = payload.conversation
        exact = self._redaction_tokens()
        conversation = redact(original_conversation, exact)
        raw_categories = sorted(
            {hit.category for hit in scan(original_conversation)}
        )

        message_id = payload.message_id or ""
        turn_id = payload.turn_id or f"{payload.conversation_id}:{message_id}"
        # 003-B1 P0-2: schema validation covers HTTP callers; this covers
        # internal callers constructing ExtractRequest programmatically.
        for label, value in (
            ("conversation_id", payload.conversation_id),
            ("message_id", message_id),
            ("turn_id", payload.turn_id),
            ("source_id", payload.source_id),
            ("app_decision_reported", payload.app_decision_reported),
        ):
            assert_safe_text(value, field=label)

        # 003-B1 P1-3: EVERY processing unit is retained, including units with
        # no memory value, so the response can account for them honestly.
        if engine_name == "llm":
            raw_candidates = self._extract_with_llm(conversation, payload.context)
            units: List[ExtractionUnit] = []
            for index, item in enumerate(raw_candidates):
                flags = [
                    hit.category
                    for hit in scan_credentials(item.content, exact_tokens=exact)
                ] + pii_categories(item.content)
                item.unit_index = index
                units.append(
                    ExtractionUnit(
                        index=index,
                        statement=item.content,
                        redacted=item.content,
                        start=0,
                        end=0,
                        candidate=item,
                        credential_flags=[
                            c for c in flags if c in CREDENTIAL_CATEGORIES
                        ],
                        pii_flags=[c for c in flags if c in PII_CATEGORIES],
                    )
                )
        else:
            units, _ = extract_units_with_security(original_conversation, exact)

        # 1) BLOCK on the FULL ORIGINAL text (multi-line PEM / DB URIs span
        #    segment boundaries), including exact live server secrets.
        full_hits = scan_credentials(original_conversation, exact_tokens=exact)
        grouped: Dict[str, List[Any]] = {}
        for hit in full_hits:
            grouped.setdefault(hit.category, []).append(hit)
        block_events: List[BlockEventResponse] = []
        for cat_index, (category, hits) in enumerate(grouped.items()):
            metadata = safe_hit_metadata(hits)
            # Numeric suffix, NOT the category name: a server-derived id like
            # "...:cat:openai_key" would otherwise look like "api_key:<value>"
            # to the credential-assignment rule and trigger a false BLOCK.
            event_turn = f"{turn_id}:cat:{cat_index}"
            event_id = self._record_block_event(
                conversation_id=payload.conversation_id,
                message_id=message_id,
                turn_id=event_turn,
                category=category,
                hit_length=metadata["max_hit_length"],
            )
            block_events.append(
                BlockEventResponse(
                    block_event_id=event_id,
                    conversation_id=payload.conversation_id,
                    message_id=message_id,
                    turn_id=event_turn,
                    category=category,
                    hit_length=metadata["max_hit_length"],
                    ruleset_version=RULESET_VERSION,
                )
            )
        covered_categories = {event.category for event in block_events}

        # 2) Assign EXACTLY ONE outcome to each unit. A full-text credential
        #    hit is mapped back to the segments it overlaps; a per-unit flag
        #    covers LLM output that invented a secret absent from the source.
        unit_results: Dict[int, str] = {}
        blocked_units = set()
        for hit in full_hits:
            for unit in units:
                if (
                    unit.end > unit.start
                    and unit.start < hit.end
                    and hit.start < unit.end
                ):
                    blocked_units.add(unit.index)
        for unit in units:
            if unit.credential_flags:
                blocked_units.add(unit.index)

        extraction_events: List[ExtractionEventResponse] = []
        filtered: List[Tuple[int, LocalCandidate, List[str]]] = []
        for unit in units:
            if unit.index in blocked_units:
                unit_results[unit.index] = "blocked"
                for cred_index, category in enumerate(unit.credential_flags):
                    if category in covered_categories:
                        continue
                    event_turn = f"{turn_id}:u{unit.index}:c{cred_index}"
                    event_id = self._record_block_event(
                        conversation_id=payload.conversation_id,
                        message_id=message_id,
                        turn_id=event_turn,
                        category=category,
                        hit_length=0,
                    )
                    block_events.append(
                        BlockEventResponse(
                            block_event_id=event_id,
                            conversation_id=payload.conversation_id,
                            message_id=message_id,
                            turn_id=event_turn,
                            category=category,
                            hit_length=0,
                            ruleset_version=RULESET_VERSION,
                        )
                    )
                    covered_categories.add(category)
                continue
            if unit.candidate is None:
                unit_results[unit.index] = "low_value"
                event_id = self._record_extraction_event(
                    conversation_id=payload.conversation_id,
                    message_id=message_id,
                    turn_id=f"{turn_id}:u{unit.index}",
                    event_type="LOW_VALUE",
                    ignore_reason_enum="NO_LONG_TERM_VALUE",
                    app_decision_reported=payload.app_decision_reported,
                    server_security_result="ALLOWED",
                    final_result="IGNORED",
                )
                extraction_events.append(
                    ExtractionEventResponse(
                        event_id=event_id,
                        conversation_id=payload.conversation_id,
                        message_id=message_id,
                        turn_id=f"{turn_id}:u{unit.index}",
                        event_type="LOW_VALUE",
                        ignore_reason_enum="NO_LONG_TERM_VALUE",
                    )
                )
                continue
            filtered.append((unit.index, unit.candidate, unit.pii_flags))

        if not units:
            if not original_conversation.strip():
                event_id = self._record_extraction_event(
                    conversation_id=payload.conversation_id,
                    message_id=message_id,
                    turn_id=f"{turn_id}:u0",
                    event_type="EMPTY",
                    ignore_reason_enum="BLANK_INPUT",
                    app_decision_reported=payload.app_decision_reported,
                    server_security_result="ALLOWED",
                    final_result="IGNORED",
                )
                extraction_events.append(
                    ExtractionEventResponse(
                        event_id=event_id,
                        conversation_id=payload.conversation_id,
                        message_id=message_id,
                        turn_id=f"{turn_id}:u0",
                        event_type="EMPTY",
                        ignore_reason_enum="BLANK_INPUT",
                    )
                )
                unit_results[-1] = "empty"
            else:
                # A full-text credential hit with no attributable segment is
                # still one processing unit per BLOCK event.
                for index in range(len(block_events)):
                    unit_results[-(index + 2)] = "blocked"

        existing = self._dedup_corpus()
        created, outcome, persisted_events, extra_blocks, persist_results = (
            self._persist_candidates(
                filtered,
                conversation_id=payload.conversation_id,
                source_id=payload.source_id,
                engine=engine_name,
                existing=existing,
                message_id=message_id,
                turn_id=turn_id,
                app_decision_reported=payload.app_decision_reported,
            )
        )
        extraction_events.extend(persisted_events)
        block_events.extend(extra_blocks)
        unit_results.update(persist_results)

        def tally(name: str) -> int:
            return sum(1 for value in unit_results.values() if value == name)

        counts = self._count_by_status(created)
        return ExtractResponse(
            conversation_id=payload.conversation_id,
            engine=engine_name,
            candidates=[self._to_response(item) for item in created],
            counts=counts,
            sensitive_categories=raw_categories,
            outcome_counts=OutcomeCounts(
                processed_units=len(unit_results),
                extracted=len(created),
                pending_manual=outcome["pending_manual"],
                pending_pii_dual=outcome["pending_pii_dual"],
                conflict_pending=outcome["conflict_pending"],
                duplicate_rejected=outcome["duplicate_rejected"],
                low_value=tally("low_value"),
                empty=tally("empty"),
                ignored=tally("low_value") + tally("empty"),
                blocked=tally("blocked"),
                auto_saved=0,
                manually_approved=0,
                user_rejected=0,
            ),
            block_events=block_events,
            extraction_events=extraction_events,
        )

    def _select_engine(self, mode: ExtractMode) -> str:
        if mode == ExtractMode.LOCAL:
            return "local"
        if mode == ExtractMode.LLM:
            if self.provider is None:
                raise ExtractionError(
                    "Extraction mode 'llm' requires a configured LLM provider",
                    detail={"hint": "Set JARVISMIND_LLM_BASE_URL and JARVISMIND_LLM_MODEL"},
                )
            return "llm"
        if self.provider is not None:
            # Auto mode still respects the budget before spending anything.
            try:
                self.budget.check(self._estimate_cost(""))
            except Exception:
                self.logger.warning("LLM budget exhausted; using local engine")
                return "local"
            return "llm"
        return "local"

    def _extract_with_llm(
        self, conversation: str, context: Optional[str]
    ) -> List[LocalCandidate]:
        assert self.provider is not None
        estimated = self._estimate_cost(conversation)
        self.budget.check(estimated)
        messages = build_messages(conversation, context, self._redaction_tokens())
        try:
            result = self.provider.complete(
                messages,
                timeout=self.settings.llm_timeout,
                max_tokens=self.settings.llm_max_tokens,
            )
        except LLMError:
            self.logger.exception("LLM extraction failed; falling back to local engine")
            local, _ = extract_locally(conversation, self._redaction_tokens())
            return local
        self.budget.record(self.provider.estimate_cost_cny(result))
        try:
            parsed = parse_response(result.text)
        except (ValueError, json.JSONDecodeError):
            self.logger.warning("LLM response was not parseable; using local engine")
            local, _ = extract_locally(conversation, self._redaction_tokens())
            return local
        return [
            LocalCandidate(
                memory_type=item["memory_type"],
                content=item["content"],
                summary=item.get("summary"),
                importance=item["importance"],
                confidence=item["confidence"],
            )
            for item in parsed[:MAX_LLM_CANDIDATES]
        ]

    def _estimate_cost(self, conversation: str) -> float:
        input_tokens = max(len(conversation) / 3.0, 200.0)
        output_tokens = float(self.settings.llm_max_tokens)
        return self.budget.estimate(int(input_tokens), int(output_tokens))

    def _redaction_tokens(self) -> Tuple[str, ...]:
        return tuple(
            token
            for token in (self.settings.api_token, self.settings.llm_api_key)
            if token
        )

    # -------------------------------------------------------------- persistence
    def _dedup_corpus(self) -> List[Tuple[str, str]]:
        """Existing memories plus already-stored candidates, for dedup/conflict."""
        with self.manager.session() as session:
            memories = MemoryRepository(session).all_contents()
            candidates = CandidateRepository(session).all_contents()
        return list(memories) + list(candidates)

    def _record_block_event(
        self,
        *,
        conversation_id: str,
        message_id: str,
        turn_id: str,
        category: str,
        hit_length: int,
    ) -> str:
        with self.manager.session() as session:
            return SecurityEventRepository(session).record_block_event(
                conversation_id=conversation_id,
                message_id=message_id,
                turn_id=turn_id,
                category=category,
                hit_length=hit_length,
                ruleset_version=RULESET_VERSION,
            )

    def _record_extraction_event(
        self,
        *,
        conversation_id: str,
        message_id: str,
        turn_id: str,
        event_type: str,
        ignore_reason_enum: str,
        app_decision_reported: Optional[str],
        server_security_result: str,
        final_result: str,
    ) -> str:
        with self.manager.session() as session:
            return SecurityEventRepository(session).record_extraction_event(
                conversation_id=conversation_id,
                message_id=message_id,
                turn_id=turn_id,
                event_type=event_type,
                ignore_reason_enum=ignore_reason_enum,
                app_decision_reported=app_decision_reported,
                server_security_result=server_security_result,
                final_result=final_result,
                ruleset_version=RULESET_VERSION,
            )

    def _persist_candidates(
        self,
        units: List[Tuple[int, LocalCandidate, List[str]]],
        *,
        conversation_id: str,
        source_id: Optional[str],
        engine: str,
        existing: List[Tuple[str, str]],
        message_id: str,
        turn_id: str,
        app_decision_reported: Optional[str],
    ) -> Tuple[
        List[MemoryCandidate],
        Dict[str, int],
        List[ExtractionEventResponse],
        List[BlockEventResponse],
        Dict[int, str],
    ]:
        """Persist candidates/events in ONE session. Never creates memories.

        Each input unit gets exactly one result in ``unit_results`` so the
        caller can build a mutually exclusive OutcomeCounts without guessing.
        """
        created: List[MemoryCandidate] = []
        events: List[ExtractionEventResponse] = []
        extra_blocks: List[BlockEventResponse] = []
        unit_results: Dict[int, str] = {}
        outcome = {
            "pending_manual": 0,
            "pending_pii_dual": 0,
            "conflict_pending": 0,
            "duplicate_rejected": 0,
        }
        with self.manager.session() as session:
            repo = CandidateRepository(session)
            events_repo = SecurityEventRepository(session)
            for unit_index, item, pii_flags in units:
                event_turn = f"{turn_id}:u{unit_index}"
                # G1 final guard: re-check the exact text about to be stored.
                blocked, cred, detected_pii = classify_candidate(
                    item.content, summary=item.summary
                )
                if blocked:
                    block_turn = f"{event_turn}:g1"
                    event_id = events_repo.record_block_event(
                        conversation_id=conversation_id,
                        message_id=message_id,
                        turn_id=block_turn,
                        category=cred[0],
                        hit_length=0,
                        ruleset_version=RULESET_VERSION,
                    )
                    extra_blocks.append(
                        BlockEventResponse(
                            block_event_id=event_id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            turn_id=block_turn,
                            category=cred[0],
                            hit_length=0,
                            ruleset_version=RULESET_VERSION,
                        )
                    )
                    unit_results[unit_index] = "blocked"
                    continue
                # PII flags come from the ORIGINAL pre-redaction statement;
                # redaction would otherwise erase the category.
                pii = sorted(set(pii_flags) | set(detected_pii))

                duplicates, conflicts = find_matches(item.content, existing)
                if duplicates:
                    event_id = events_repo.record_extraction_event(
                        conversation_id=conversation_id,
                        message_id=message_id,
                        turn_id=event_turn,
                        event_type="DUPLICATE",
                        ignore_reason_enum="DUPLICATE_OF_EXISTING",
                        app_decision_reported=app_decision_reported,
                        server_security_result="ALLOWED",
                        final_result="IGNORED",
                        ruleset_version=RULESET_VERSION,
                    )
                    outcome["duplicate_rejected"] += 1
                    unit_results[unit_index] = "duplicate_rejected"
                    events.append(
                        ExtractionEventResponse(
                            event_id=event_id,
                            conversation_id=conversation_id,
                            message_id=message_id,
                            turn_id=event_turn,
                            event_type="DUPLICATE",
                            ignore_reason_enum="DUPLICATE_OF_EXISTING",
                        )
                    )
                    continue

                required = required_grants_for(pii, [])
                status = (
                    CandidateStatus.PENDING_AUTHORIZATION.value
                    if pii
                    else CandidateStatus.PENDING.value
                )
                server_result = "PII_REQUIRES_GRANTS" if pii else "ALLOWED"
                # Canonical metadata: the fingerprint is computed over EXACTLY
                # the string that will be persisted, so any later metadata
                # edit invalidates the approval basis (003-B1 P1-2).
                canonical_meta = json.dumps(
                    {
                        "ruleset_version": RULESET_VERSION,
                        "message_id": message_id,
                        "turn_id": turn_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                fingerprint = content_fingerprint(
                    self.settings,
                    content=item.content,
                    summary=item.summary,
                    metadata_json=canonical_meta,
                    decision_version=1,
                )
                candidate = repo.create(
                    MemoryCandidate(
                        id=uuid.uuid4().hex,
                        conversation_id=conversation_id,
                        source_id=source_id,
                        memory_type=item.memory_type,
                        content=item.content,
                        summary=item.summary,
                        importance=item.importance,
                        confidence=item.confidence,
                        status=status,
                        duplicate_of=None,
                        conflicts_with=conflicts[0] if conflicts else None,
                        extraction_engine=engine,
                        extracted_at=utc_now_iso(),
                        decided_at=None,
                        decided_by=None,
                        reject_reason=None,
                        metadata_json=canonical_meta,
                        scan_status="SCANNED",
                        decision_version=1,
                        content_fingerprint=fingerprint,
                        ruleset_version=RULESET_VERSION,
                        required_grants_json=json.dumps(required),
                        message_id=message_id,
                        turn_id=turn_id,
                        app_decision_reported=app_decision_reported,
                        server_security_result=server_result,
                        final_result=None,
                    )
                )
                created.append(candidate)
                # Units created in this same request also count for dedup, so
                # repeated statements inside one /extract become DUPLICATE.
                existing.append((candidate.id, item.content))
                if conflicts:
                    outcome["conflict_pending"] += 1
                    unit_results[unit_index] = "conflict_pending"
                elif pii:
                    outcome["pending_pii_dual"] += 1
                    unit_results[unit_index] = "pending_pii_dual"
                else:
                    outcome["pending_manual"] += 1
                    unit_results[unit_index] = "pending_manual"
        return created, outcome, events, extra_blocks, unit_results

    def _count_by_status(self, candidates: List[MemoryCandidate]) -> Dict[str, int]:
        """Legacy 3-key view. APPROVED is ALWAYS 0 during extraction."""
        pending = sum(
            1
            for candidate in candidates
            if candidate.status
            in (CandidateStatus.PENDING.value, CandidateStatus.PENDING_AUTHORIZATION.value)
        )
        return {"PENDING": pending, "APPROVED": 0, "REJECTED": 0}

    def _to_response(self, candidate: MemoryCandidate) -> CandidateResponse:
        metadata = None
        if candidate.metadata_json:
            try:
                metadata = json.loads(candidate.metadata_json)
            except (ValueError, TypeError):
                metadata = None
        return CandidateResponse(
            id=candidate.id,
            conversation_id=candidate.conversation_id,
            source_id=candidate.source_id,
            memory_type=candidate.memory_type,
            content=candidate.content,
            summary=candidate.summary,
            importance=candidate.importance,
            confidence=candidate.confidence,
            status=CandidateStatus(candidate.status),
            duplicate_of=candidate.duplicate_of,
            conflicts_with=candidate.conflicts_with,
            extraction_engine=candidate.extraction_engine,
            extracted_at=candidate.extracted_at,
            decided_at=candidate.decided_at,
            decided_by=candidate.decided_by,
            reject_reason=candidate.reject_reason,
            sensitive_categories=sorted(
                set(sensitive_categories(candidate.content))
            ),
            metadata=metadata,
            scan_status=candidate.scan_status,
            decision_version=int(candidate.decision_version or 1),
            content_fingerprint=candidate.content_fingerprint,
            ruleset_version=candidate.ruleset_version,
            required_grants=_stored_required_grants(candidate.required_grants_json),
            app_decision_reported=candidate.app_decision_reported,
            server_security_result=candidate.server_security_result,
            final_result=candidate.final_result,
        )

    # ---------------------------------------------------------------- querying
    def list_candidates(
        self,
        *,
        status: Optional[str] = None,
        memory_type: Optional[str] = None,
        conversation_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[CandidateResponse], int]:
        with self.manager.session() as session:
            rows, total = CandidateRepository(session).list(
                status=status,
                memory_type=memory_type,
                conversation_id=conversation_id,
                page=page,
                page_size=page_size,
            )
        return [self._to_response(row) for row in rows], total

    def get_candidate(self, candidate_id: str) -> CandidateResponse:
        with self.manager.session() as session:
            candidate = CandidateRepository(session).get_by_id(candidate_id)
            if candidate is None:
                raise NotFoundError(f"Candidate {candidate_id} not found")
            return self._to_response(candidate)

    # ----------------------------------------------------------------- review
    def review(
        self,
        candidate_id: str,
        *,
        decision: str,
        reason: Optional[str] = None,
        actor: Optional[str] = ACTOR_USER,
    ) -> CandidateResponse:
        """Delegate to the atomic approval service.

        003-B1: ``actor`` is retained for wire compatibility but is ONLY an
        unverified client claim (stored in client_actor_claim). The server
        writes ``decided_by`` itself and never treats a client-supplied value
        as an identity or an authorization result.
        """
        from ...services.approval_service import ApprovalService

        return ApprovalService(self.manager).review(
            candidate_id,
            decision=decision,
            reason=reason,
            actor_label=actor or "",
        )

    def budget_status(self) -> Dict[str, Any]:
        return self.budget.status()