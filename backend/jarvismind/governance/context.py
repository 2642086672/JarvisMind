"""Re-export of the standalone ORM gate state.

The implementation lives in ``app/p1_gate.py`` so ``models.py`` can import it
without pulling in this package (and therefore without a circular import).
Application code may import from either path; both refer to the same objects.
"""
from __future__ import annotations

from ..p1_gate import (  # noqa: F401
    WriteContext,
    activate,
    active_governor,
    authorized_write,
    current_write_context,
    deactivate,
    is_active,
)

__all__ = [
    "WriteContext",
    "current_write_context",
    "authorized_write",
    "activate",
    "deactivate",
    "active_governor",
    "is_active",
]
