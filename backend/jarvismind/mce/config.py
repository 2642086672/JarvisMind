"""MCE-01 configuration (environment only, fail-closed, zero I/O at import).

The engine is **read-only**: every knob below bounds a read or a rendered
block. None of them can create a table, install a schema or write a memory.
Values are resolved from ``JARVISMIND_MCE_*`` environment variables. A malformed
value falls back to the documented default instead of raising: an operator
typo must not turn the resolve endpoint into a 500 during an incident.

``JARVISMIND_MCE_ENABLED`` is the process-level kill switch. The default is ON because
the endpoint is read-only; an operator can still turn it off without a code
change. ``JARVISMIND_MCE_JKG_ENABLED`` only *permits* JKG enrichment; the graph still
has to be READY through ``JkgCompositionAdapter`` before any node is read.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

MCE_ENABLED_ENV = "JARVISMIND_MCE_ENABLED"
MCE_JKG_ENABLED_ENV = "JARVISMIND_MCE_JKG_ENABLED"
MCE_MAX_TOKENS_ENV = "JARVISMIND_MCE_MAX_TOKENS"
MCE_MAX_ITEMS_ENV = "JARVISMIND_MCE_MAX_ITEMS"
MCE_MAX_ITEM_CHARS_ENV = "JARVISMIND_MCE_MAX_ITEM_CHARS"
MCE_SCAN_LIMIT_ENV = "JARVISMIND_MCE_SCAN_LIMIT"
MCE_PAGE_SIZE_ENV = "JARVISMIND_MCE_PAGE_SIZE"
MCE_JKG_MAX_NODES_ENV = "JARVISMIND_MCE_JKG_MAX_NODES"
MCE_JKG_NEIGHBORS_ENV = "JARVISMIND_MCE_JKG_NEIGHBORS"

# Hard bounds. They are deliberately small: MCE is a chat-context surface, not
# an export tool. A config value can never widen them.
MIN_MAX_TOKENS = 128
MAX_MAX_TOKENS = 8000
MIN_ITEMS = 1
MAX_ITEMS = 50
MIN_ITEM_CHARS = 80
MAX_ITEM_CHARS = 2000
MIN_SCAN_LIMIT = 100
MAX_SCAN_LIMIT = 5000
MIN_PAGE_SIZE = 20
MAX_PAGE_SIZE = 500
MIN_JKG_NODES = 10
MAX_JKG_NODES = 500
MIN_JKG_NEIGHBORS = 1
MAX_JKG_NEIGHBORS = 10

DEFAULT_MAX_TOKENS = 1200
DEFAULT_MAX_ITEMS = 20
DEFAULT_MAX_ITEM_CHARS = 600
DEFAULT_SCAN_LIMIT = 2000
DEFAULT_PAGE_SIZE = 200
DEFAULT_JKG_MAX_NODES = 200
DEFAULT_JKG_NEIGHBORS = 4

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_bool(
    name: str, default: bool, environ: Optional[Mapping[str, str]] = None
) -> bool:
    raw = (environ if environ is not None else os.environ).get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().casefold()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    # Any other value is treated as OFF: a malformed switch must never turn a
    # capability on, and both current switches default to the permissive state.
    return False


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    raw = (environ if environ is not None else os.environ).get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class MceConfig:
    """Immutable bounds for one resolve call. Construction performs no I/O."""

    enabled: bool = True
    jkg_enabled: bool = True
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_items: int = DEFAULT_MAX_ITEMS
    max_item_chars: int = DEFAULT_MAX_ITEM_CHARS
    scan_limit: int = DEFAULT_SCAN_LIMIT
    page_size: int = DEFAULT_PAGE_SIZE
    jkg_max_nodes: int = DEFAULT_JKG_MAX_NODES
    jkg_neighbors_per_node: int = DEFAULT_JKG_NEIGHBORS

    def __post_init__(self) -> None:
        for name, low, high in (
            ("max_tokens", MIN_MAX_TOKENS, MAX_MAX_TOKENS),
            ("max_items", MIN_ITEMS, MAX_ITEMS),
            ("max_item_chars", MIN_ITEM_CHARS, MAX_ITEM_CHARS),
            ("scan_limit", MIN_SCAN_LIMIT, MAX_SCAN_LIMIT),
            ("page_size", MIN_PAGE_SIZE, MAX_PAGE_SIZE),
            ("jkg_max_nodes", MIN_JKG_NODES, MAX_JKG_NODES),
            ("jkg_neighbors_per_node", MIN_JKG_NEIGHBORS, MAX_JKG_NEIGHBORS),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer, got {value!r}")
            if not low <= value <= high:
                raise ValueError(
                    f"{name} must be between {low} and {high}, got {value}"
                )
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        if not isinstance(self.jkg_enabled, bool):
            raise ValueError(
                f"jkg_enabled must be a bool, got {self.jkg_enabled!r}"
            )

    @classmethod
    def from_env(
        cls, *, environ: Optional[Mapping[str, str]] = None
    ) -> "MceConfig":
        """Resolve every knob from the environment without raising."""
        return cls(
            enabled=_env_bool(MCE_ENABLED_ENV, True, environ),
            jkg_enabled=_env_bool(MCE_JKG_ENABLED_ENV, True, environ),
            max_tokens=_env_int(
                MCE_MAX_TOKENS_ENV,
                DEFAULT_MAX_TOKENS,
                minimum=MIN_MAX_TOKENS,
                maximum=MAX_MAX_TOKENS,
                environ=environ,
            ),
            max_items=_env_int(
                MCE_MAX_ITEMS_ENV,
                DEFAULT_MAX_ITEMS,
                minimum=MIN_ITEMS,
                maximum=MAX_ITEMS,
                environ=environ,
            ),
            max_item_chars=_env_int(
                MCE_MAX_ITEM_CHARS_ENV,
                DEFAULT_MAX_ITEM_CHARS,
                minimum=MIN_ITEM_CHARS,
                maximum=MAX_ITEM_CHARS,
                environ=environ,
            ),
            scan_limit=_env_int(
                MCE_SCAN_LIMIT_ENV,
                DEFAULT_SCAN_LIMIT,
                minimum=MIN_SCAN_LIMIT,
                maximum=MAX_SCAN_LIMIT,
                environ=environ,
            ),
            page_size=_env_int(
                MCE_PAGE_SIZE_ENV,
                DEFAULT_PAGE_SIZE,
                minimum=MIN_PAGE_SIZE,
                maximum=MAX_PAGE_SIZE,
                environ=environ,
            ),
            jkg_max_nodes=_env_int(
                MCE_JKG_MAX_NODES_ENV,
                DEFAULT_JKG_MAX_NODES,
                minimum=MIN_JKG_NODES,
                maximum=MAX_JKG_NODES,
                environ=environ,
            ),
            jkg_neighbors_per_node=_env_int(
                MCE_JKG_NEIGHBORS_ENV,
                DEFAULT_JKG_NEIGHBORS,
                minimum=MIN_JKG_NEIGHBORS,
                maximum=MAX_JKG_NEIGHBORS,
                environ=environ,
            ),
        )

    def as_dict(self) -> dict:
        """Plain-dict view for ``GET /api/v1/mce/status``."""
        return {
            "enabled": self.enabled,
            "jkg_enabled": self.jkg_enabled,
            "max_tokens": self.max_tokens,
            "max_items": self.max_items,
            "max_item_chars": self.max_item_chars,
            "scan_limit": self.scan_limit,
            "page_size": self.page_size,
            "jkg_max_nodes": self.jkg_max_nodes,
            "jkg_neighbors_per_node": self.jkg_neighbors_per_node,
        }


__all__ = [
    "DEFAULT_JKG_MAX_NODES",
    "DEFAULT_JKG_NEIGHBORS",
    "DEFAULT_MAX_ITEM_CHARS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_SCAN_LIMIT",
    "MCE_ENABLED_ENV",
    "MCE_JKG_ENABLED_ENV",
    "MceConfig",
]
