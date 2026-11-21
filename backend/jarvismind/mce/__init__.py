"""MCE-01A: read-only long-term project-context engine.

This package performs no I/O at import time. It is a **derived** module: JME
remains the single authoritative memory store, and JKG remains an optional
derived graph. No JME core module imports ``app.mce``.
"""
from .config import MceConfig
from .models import (
    ContextItem,
    ContextKind,
    ContextResolutionResult,
    DecisionStatus,
    ExcludedCounts,
    ProjectContextSnapshot,
    RelatedKnowledge,
    SourceRef,
)

__all__ = [
    "ContextItem",
    "ContextKind",
    "ContextResolutionResult",
    "DecisionStatus",
    "ExcludedCounts",
    "MceConfig",
    "ProjectContextSnapshot",
    "RelatedKnowledge",
    "SourceRef",
]
