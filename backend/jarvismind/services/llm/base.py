"""Provider-agnostic LLM interface.

The extraction service depends only on this interface, so any vendor that
speaks the OpenAI chat-completions protocol can be used without changing
extraction logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    """Raised when a provider call fails or returns something unusable."""


@dataclass
class LLMResult:
    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Optional[Dict[str, Any]] = field(default=None)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMProvider(ABC):
    """Minimal chat-completion contract used by the extraction engine."""

    name: str = "abstract"

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        timeout: float = 20.0,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> LLMResult:
        """Run a chat completion. Implementations must raise LLMError on failure."""

    def estimate_cost_cny(self, result: LLMResult) -> float:
        """Return the estimated cost of a completed call in CNY."""
        return 0.0
