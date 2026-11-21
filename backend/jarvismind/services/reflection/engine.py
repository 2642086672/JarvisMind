"""Reflection engine: LLM-backed with deterministic local fallback.

The engine only ever sends the explicitly selected memory set to a model. With
no provider configured (the default) it uses the offline local strategy, so no
personal memory leaves the machine unless an operator configures a model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...core.sensitive import redact
from ..llm import LLMError, LLMProvider
from . import local, prompt

logger = logging.getLogger("jme.reflection")


@dataclass
class SummaryDraft:
    title: str
    content: str
    confidence: float
    memory_ids: List[str] = field(default_factory=list)
    memory_type: Optional[str] = None
    engine: str = "local"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictDraft:
    memory_id_a: str
    memory_id_b: str
    reason: str
    confidence: float = 0.5
    engine: str = "local"


@dataclass
class MergeDraft:
    memory_ids: List[str]
    new_content: str
    new_summary: Optional[str]
    reason: str
    confidence: float = 0.5
    engine: str = "local"
    memory_type: Optional[str] = None
    source_id: Optional[str] = None


class ReflectionEngine:
    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        *,
        settings=None,
        budget=None,
        redaction_tokens: Sequence[str] = (),
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.budget = budget
        self.redaction_tokens = tuple(t for t in redaction_tokens if t)
        self.logger = logger or logging.getLogger("jme.reflection")

    @property
    def engine_name(self) -> str:
        return "llm" if self.provider is not None else "local"

    def _redact(self, text: str) -> str:
        if not self.redaction_tokens:
            return text
        return redact(text, self.redaction_tokens)

    def _timeout(self) -> float:
        return float(getattr(self.settings, "llm_timeout", 20.0)) if self.settings else 20.0

    def _max_tokens(self) -> int:
        return int(getattr(self.settings, "llm_max_tokens", 800)) if self.settings else 800

    def _check_budget(self, text: str) -> bool:
        if self.budget is None:
            return True
        try:
            estimate = self.budget.estimate(max(len(text) / 3.0, 200.0), self._max_tokens())
            self.budget.check(estimate)
            return True
        except Exception as exc:
            self.logger.warning("Reflection budget guard blocked the call: %s", exc)
            return False

    def _record_budget(self, result) -> None:
        if self.budget is None or self.provider is None:
            return
        try:
            self.budget.record(self.provider.estimate_cost_cny(result))
        except Exception:
            pass

    # ---------------------------------------------------------------- summaries
    def summarize(
        self,
        records: Sequence,
        *,
        summary_type: str,
        memory_type: Optional[str] = None,
    ) -> SummaryDraft:
        items = list(records)
        memory_ids = [r.id for r in items]
        source_ids = sorted({r.source_id for r in items if r.source_id})

        if self.provider is not None:
            messages = prompt.build_summary_messages(
                items, summary_type=summary_type, memory_type=memory_type
            )
            payload = self._redact(
                "\n".join(message["content"] for message in messages)
            )
            if self._check_budget(payload):
                try:
                    result = self.provider.complete(
                        messages, timeout=self._timeout(), max_tokens=self._max_tokens()
                    )
                    self._record_budget(result)
                    parsed = prompt.parse_summary_response(result.text)
                    if parsed:
                        return SummaryDraft(
                            title=parsed["title"],
                            content=parsed["content"],
                            confidence=parsed["confidence"],
                            memory_ids=memory_ids,
                            memory_type=memory_type,
                            engine="llm",
                            metadata={"source_ids": source_ids},
                        )
                except LLMError as exc:
                    self.logger.warning("LLM summary failed, using local: %s", exc)

        local_draft = local.build_summary(
            items, summary_type=summary_type, memory_type=memory_type
        )
        return SummaryDraft(
            title=local_draft["title"] or "记忆总结",
            content=local_draft["content"],
            confidence=float(local_draft["confidence"]),
            memory_ids=memory_ids,
            memory_type=memory_type,
            engine="local",
            metadata={"source_ids": source_ids},
        )

    # -------------------------------------------------------------- conflicts
    def detect_conflicts(self, records: Sequence) -> List[ConflictDraft]:
        items = list(records)
        if self.provider is not None:
            messages = prompt.build_conflict_messages(items)
            payload = self._redact(
                "\n".join(message["content"] for message in messages)
            )
            if self._check_budget(payload):
                try:
                    result = self.provider.complete(
                        messages, timeout=self._timeout(), max_tokens=self._max_tokens()
                    )
                    self._record_budget(result)
                    parsed = prompt.parse_conflict_response(result.text)
                    if parsed:
                        return [
                            ConflictDraft(
                                memory_id_a=item["memory_id_a"],
                                memory_id_b=item["memory_id_b"],
                                reason=item["reason"],
                                confidence=item["confidence"],
                                engine="llm",
                            )
                            for item in parsed
                        ]
                except LLMError as exc:
                    self.logger.warning("LLM conflict scan failed, using local: %s", exc)

        return [
            ConflictDraft(
                memory_id_a=item["memory_id_a"],
                memory_id_b=item["memory_id_b"],
                reason=item["reason"],
                confidence=float(item["confidence"]),
                engine="local",
            )
            for item in local.detect_conflicts(items)
        ]

    # ------------------------------------------------------------------ merge
    def suggest_merge(
        self,
        records: Sequence,
        *,
        reason: Optional[str] = None,
    ) -> MergeDraft:
        items = list(records)
        if self.provider is not None:
            messages = prompt.build_merge_messages(items)
            payload = self._redact(
                "\n".join(message["content"] for message in messages)
            )
            if self._check_budget(payload):
                try:
                    result = self.provider.complete(
                        messages, timeout=self._timeout(), max_tokens=self._max_tokens()
                    )
                    self._record_budget(result)
                    parsed = prompt.parse_merge_response(result.text)
                    if parsed:
                        return MergeDraft(
                            memory_ids=[r.id for r in items],
                            new_content=parsed["new_content"],
                            new_summary=parsed["new_summary"],
                            reason=parsed["reason"],
                            confidence=0.7,
                            engine="llm",
                            memory_type=items[0].memory_type,
                            source_id=items[0].source_id,
                        )
                except LLMError as exc:
                    self.logger.warning("LLM merge draft failed, using local: %s", exc)

        local_draft = local.build_merge_draft(items, reason=reason)
        return MergeDraft(
            memory_ids=[r.id for r in items],
            new_content=local_draft["new_content"],
            new_summary=local_draft["new_summary"],
            reason=local_draft["reason"],
            confidence=0.6,
            engine="local",
            memory_type=local_draft.get("memory_type"),
            source_id=local_draft.get("source_id"),
        )

