"""Control-plane schema: memory spaces, agents, grants, API tokens, access logs.

The control plane answers only one question: which principal may open which
memory space with which permissions. It never stores memory content. Every
memory lives in exactly one per-space JME database, so the control database is
never a second authoritative copy of memory data.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CONTROL_SCHEMA_VERSION = 1


class ControlBase(DeclarativeBase):
    """Declarative base for the control database (separate from JME's Base)."""


class ControlMeta(ControlBase):
    __tablename__ = "control_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class MemorySpace(ControlBase):
    __tablename__ = "memory_spaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    space_type: Mapped[str] = mapped_column(String, nullable=False, default="custom")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Agent(ControlBase):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agent_type: Mapped[str] = mapped_column(String, nullable=False, default="custom")
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    default_space_id: Mapped[str] = mapped_column(
        ForeignKey("memory_spaces.id"), nullable=False
    )
    permissions_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    deleted_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AgentSpaceGrant(ControlBase):
    __tablename__ = "agent_space_grants"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    space_id: Mapped[str] = mapped_column(
        ForeignKey("memory_spaces.id", ondelete="CASCADE"), primary_key=True
    )
    access: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ApiToken(ControlBase):
    """An agent credential. Only an HMAC-SHA256 digest of the token is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    expires_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    last_used_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    revoked_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    replaced_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (Index("ix_api_tokens_agent", "agent_id"),)


class AccessLog(ControlBase):
    """One row per API request. Holds no request body, no query string, no IP."""

    __tablename__ = "api_access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    principal_kind: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    token_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    space_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    method: Mapped[str] = mapped_column(String, nullable=False)
    route: Mapped[str] = mapped_column(String, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    client_digest: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_access_logs_time", "occurred_at"),
        Index("ix_access_logs_agent", "agent_id"),
    )


CONTROL_TABLES = (
    ControlMeta,
    MemorySpace,
    Agent,
    AgentSpaceGrant,
    ApiToken,
    AccessLog,
)
