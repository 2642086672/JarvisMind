"""The authenticated caller of a v1 request."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from .permissions import ACCESS_READ, ACCESS_READWRITE

KIND_OWNER = "owner"
KIND_AGENT = "agent"


@dataclass(frozen=True)
class Principal:
    kind: str
    label: str
    agent_id: Optional[str] = None
    token_id: Optional[str] = None
    default_space_id: str = "default"
    permissions: frozenset = field(default_factory=frozenset)
    #: space_id -> "read" | "readwrite". The owner's map is filled per request.
    spaces: Mapping[str, str] = field(default_factory=dict)
    all_spaces: bool = False

    @property
    def is_owner(self) -> bool:
        return self.kind == KIND_OWNER

    def has(self, permission: str) -> bool:
        return self.is_owner or permission in self.permissions

    def access_for(self, space_id: str) -> Optional[str]:
        if self.is_owner:
            return ACCESS_READWRITE
        return self.spaces.get(space_id)

    def can_read(self, space_id: str) -> bool:
        return self.access_for(space_id) in (ACCESS_READ, ACCESS_READWRITE)

    def can_write(self, space_id: str) -> bool:
        return self.access_for(space_id) == ACCESS_READWRITE

    def rate_key(self) -> str:
        if self.is_owner:
            return "owner"
        return f"agent:{self.agent_id}:{self.token_id}"

    def public_view(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "agent_id": self.agent_id,
            "token_id": self.token_id,
            "default_space_id": self.default_space_id,
            "permissions": sorted(self.permissions) if not self.is_owner else ["*"],
            "spaces": dict(self.spaces),
        }
