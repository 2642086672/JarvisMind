"""Fine-grained agent permissions.

Permissions are attached to an agent and are the ceiling for every token the
agent holds. A token may narrow them with its own scopes but can never widen
them. Owner credentials implicitly hold every permission.
"""
from __future__ import annotations

from typing import Iterable, Tuple

READ = "read"
WRITE = "write"
UPDATE = "update"
DELETE = "delete"
SEARCH = "search"
CONTEXT = "context"
GRAPH = "graph"
SENSITIVE = "sensitive"

ALL_PERMISSIONS: Tuple[str, ...] = (READ, WRITE, UPDATE, DELETE, SEARCH, CONTEXT, GRAPH, SENSITIVE)

#: Least privilege by default. Writes, updates, deletes, graph access and
#: sensitive-memory access must be granted explicitly.
DEFAULT_AGENT_PERMISSIONS: Tuple[str, ...] = (READ, SEARCH, CONTEXT)

PERMISSION_DESCRIPTIONS = {
    READ: "List and read memories in granted spaces.",
    WRITE: "Submit new memories through the governed remember/extract paths.",
    UPDATE: "Request corrections to existing memories (owner approval required).",
    DELETE: "Request forgetting of memories (owner approval required).",
    SEARCH: "Run keyword and semantic recall.",
    CONTEXT: "Build injectable context blocks for a conversation.",
    GRAPH: "Read the knowledge graph of granted spaces.",
    SENSITIVE: "See memories classified as sensitive. Never granted by default.",
}

ACCESS_READ = "read"
ACCESS_READWRITE = "readwrite"
ACCESS_LEVELS = (ACCESS_READ, ACCESS_READWRITE)


def normalize_permissions(values: Iterable[str]) -> Tuple[str, ...]:
    """Validate and return permissions in canonical order without duplicates."""
    requested = set()
    for value in values:
        if value not in ALL_PERMISSIONS:
            raise ValueError(f"unknown permission: {value!r}")
        requested.add(value)
    return tuple(item for item in ALL_PERMISSIONS if item in requested)
