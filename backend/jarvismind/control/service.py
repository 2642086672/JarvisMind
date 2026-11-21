"""Control-plane operations: memory spaces, agents, grants, API tokens, access logs.

Authorization decisions made here are the only source of truth for who may open
which space. Callers (HTTP dependencies) must never re-derive them from request
headers.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..core.errors import ValidationError
from .database import ControlDatabase
from .errors import (
    AgentNotFoundError,
    ControlConflictError,
    SpaceNotFoundError,
    TokenNotFoundError,
)
from .models import AccessLog, Agent, AgentSpaceGrant, ApiToken, MemorySpace
from .permissions import (
    ACCESS_LEVELS,
    ACCESS_READWRITE,
    ALL_PERMISSIONS,
    DEFAULT_AGENT_PERMISSIONS,
    normalize_permissions,
)
from .principal import KIND_AGENT, KIND_OWNER, Principal
from .tokens import (
    digest_client,
    digest_token,
    generate_token,
    load_or_create_control_key,
    new_entity_id,
)

DEFAULT_SPACE_ID = "default"
SPACE_TYPES = ("personal", "work", "project", "shared", "custom")
SPACE_STATUSES = ("active", "archived")
AGENT_TYPES = (
    "dsh",
    "deepseek",
    "openai_compatible",
    "claude",
    "openclaw",
    "langchain",
    "llamaindex",
    "custom",
)
AGENT_SOURCES = ("manual", "template", "sdk")
AGENT_STATUSES = ("active", "disabled")
DEFAULT_TOKEN_TTL_DAYS = 90
MAX_TOKEN_TTL_DAYS = 3650
ACCESS_LOG_RETENTION_ROWS = 100_000

_SPACE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _check_name(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not 1 <= len(cleaned) <= 120:
        raise ValidationError(f"{field} must be 1-120 characters", detail={"field": field})
    return cleaned


def _check_description(value: Optional[str]) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > 500:
        raise ValidationError(
            "description must be at most 500 characters",
            detail={"field": "description"},
        )
    return cleaned


def _check_ttl(expires_in_days: Any) -> int:
    if (
        isinstance(expires_in_days, bool)
        or not isinstance(expires_in_days, int)
        or not 1 <= expires_in_days <= MAX_TOKEN_TTL_DAYS
    ):
        raise ValidationError(
            f"expires_in_days must be an integer between 1 and {MAX_TOKEN_TTL_DAYS}",
            detail={"field": "expires_in_days"},
        )
    return expires_in_days


def validate_space_id(space_id: str) -> str:
    """Strict id rule. The id is also a directory name, so it must never allow traversal."""
    if not isinstance(space_id, str) or _SPACE_ID_PATTERN.fullmatch(space_id) is None:
        raise ValidationError(
            "space id must be 2-63 lowercase letters, digits, '-' or '_'",
            detail={"field": "id"},
        )
    return space_id


def _lifetime_of(row: ApiToken) -> timedelta:
    """The original lifetime, measured exactly rather than in whole days, so rotation keeps it."""
    if row.expires_at:
        span = _parse_time(row.expires_at) - _parse_time(row.created_at)
        seconds = min(MAX_TOKEN_TTL_DAYS * 86400, max(1, round(span.total_seconds())))
        return timedelta(seconds=seconds)
    return timedelta(days=DEFAULT_TOKEN_TTL_DAYS)


def space_to_dict(row: MemorySpace) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "space_type": row.space_type,
        "status": row.status,
        "is_default": row.id == DEFAULT_SPACE_ID,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def token_to_dict(row: ApiToken) -> Dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "status": row.status,
        "scopes": json.loads(row.scopes_json),
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "revoked_at": row.revoked_at,
        "replaced_by": row.replaced_by,
    }


class ControlService:
    def __init__(self, database: ControlDatabase, data_dir: Path) -> None:
        self.database = database
        self._data_dir = Path(data_dir)
        self._key_cache: Optional[bytes] = None
        self._key_lock = threading.Lock()
        database.set_on_ready(self._seed_default_space)

    def _signing_key(self) -> bytes:
        # Loaded on first use so that building the application writes no files.
        with self._key_lock:
            if self._key_cache is None:
                self._key_cache = load_or_create_control_key(self._data_dir)
            return self._key_cache

    # ------------------------------------------------------------------ spaces
    def _seed_default_space(self, session: Session) -> None:
        if session.get(MemorySpace, DEFAULT_SPACE_ID) is None:
            now = _now_iso()
            session.add(
                MemorySpace(
                    id=DEFAULT_SPACE_ID,
                    name="Default",
                    description="Storage root of this JarvisMind installation.",
                    space_type="personal",
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )

    def ensure_default_space(self) -> None:
        with self.database.session() as session:
            self._seed_default_space(session)

    def list_spaces(self, *, include_archived: bool = False) -> List[Dict[str, Any]]:
        with self.database.session() as session:
            statement = select(MemorySpace).order_by(MemorySpace.created_at, MemorySpace.id)
            if not include_archived:
                statement = statement.where(MemorySpace.status == "active")
            return [space_to_dict(row) for row in session.scalars(statement)]

    def get_space(self, space_id: str) -> Dict[str, Any]:
        with self.database.session() as session:
            row = session.get(MemorySpace, space_id)
            if row is None:
                raise SpaceNotFoundError()
            return space_to_dict(row)

    def require_active_space(self, space_id: str) -> None:
        with self.database.session() as session:
            row = session.get(MemorySpace, space_id)
            if row is None or row.status != "active":
                raise SpaceNotFoundError()

    def check_new_space(
        self,
        *,
        space_id: str,
        name: str,
        description: str = "",
        space_type: str = "custom",
    ) -> None:
        """Validate a space request and confirm the id is free, before storage is created."""
        validate_space_id(space_id)
        if space_id == DEFAULT_SPACE_ID:
            raise ControlConflictError("the default space already exists")
        _check_name(name, "name")
        _check_description(description)
        if space_type not in SPACE_TYPES:
            raise ValidationError("unknown space type", detail={"field": "space_type"})
        with self.database.session() as session:
            if session.get(MemorySpace, space_id) is not None:
                raise ControlConflictError("a memory space with this id already exists")

    def create_space(
        self,
        *,
        space_id: str,
        name: str,
        description: str = "",
        space_type: str = "custom",
    ) -> Dict[str, Any]:
        validate_space_id(space_id)
        name = _check_name(name, "name")
        description = _check_description(description)
        if space_type not in SPACE_TYPES:
            raise ValidationError("unknown space type", detail={"field": "space_type"})
        with self.database.session() as session:
            if session.get(MemorySpace, space_id) is not None:
                raise ControlConflictError("a memory space with this id already exists")
            now = _now_iso()
            row = MemorySpace(
                id=space_id,
                name=name,
                description=description,
                space_type=space_type,
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return space_to_dict(row)

    def update_space(
        self,
        space_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.database.session() as session:
            row = session.get(MemorySpace, space_id)
            if row is None:
                raise SpaceNotFoundError()
            if name is not None:
                row.name = _check_name(name, "name")
            if description is not None:
                row.description = _check_description(description)
            if status is not None:
                if status not in SPACE_STATUSES:
                    raise ValidationError("unknown space status", detail={"field": "status"})
                if status == "archived" and space_id == DEFAULT_SPACE_ID:
                    raise ControlConflictError("the default space cannot be archived")
                if status == "archived":
                    in_use = session.scalar(
                        select(func.count())
                        .select_from(Agent)
                        .where(Agent.default_space_id == space_id, Agent.status != "deleted")
                    )
                    if int(in_use or 0) > 0:
                        raise ControlConflictError(
                            "agents still use this space as their default; change their default space first",
                            detail={"code": "space_in_use"},
                        )
                row.status = status
            row.updated_at = _now_iso()
            session.flush()
            return space_to_dict(row)

    # ------------------------------------------------------------------ agents
    def _agent_grants(self, session: Session, agent_id: str) -> List[AgentSpaceGrant]:
        statement = (
            select(AgentSpaceGrant)
            .where(AgentSpaceGrant.agent_id == agent_id)
            .order_by(AgentSpaceGrant.space_id)
        )
        return list(session.scalars(statement))

    def _agent_dict(self, session: Session, row: Agent) -> Dict[str, Any]:
        grants = self._agent_grants(session, row.id)
        active_tokens = session.scalar(
            select(func.count())
            .select_from(ApiToken)
            .where(ApiToken.agent_id == row.id, ApiToken.status == "active")
        )
        return {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "agent_type": row.agent_type,
            "source": row.source,
            "status": row.status,
            "default_space_id": row.default_space_id,
            "permissions": json.loads(row.permissions_json),
            "grants": [
                {"space_id": grant.space_id, "access": grant.access} for grant in grants
            ],
            "active_token_count": int(active_tokens or 0),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "deleted_at": row.deleted_at,
        }

    def list_agents(self, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        with self.database.session() as session:
            statement = select(Agent).order_by(Agent.created_at, Agent.id)
            if not include_deleted:
                statement = statement.where(Agent.status != "deleted")
            return [self._agent_dict(session, row) for row in session.scalars(statement)]

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        with self.database.session() as session:
            row = session.get(Agent, agent_id)
            if row is None or row.status == "deleted":
                raise AgentNotFoundError()
            return self._agent_dict(session, row)

    def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        agent_type: str = "custom",
        source: str = "manual",
        default_space_id: str = DEFAULT_SPACE_ID,
        permissions: Optional[Iterable[str]] = None,
        grants: Sequence[Tuple[str, str]] = (),
    ) -> Dict[str, Any]:
        name = _check_name(name, "name")
        description = _check_description(description)
        if agent_type not in AGENT_TYPES:
            raise ValidationError("unknown agent type", detail={"field": "agent_type"})
        if source not in AGENT_SOURCES:
            raise ValidationError("unknown agent source", detail={"field": "source"})
        try:
            perms = normalize_permissions(
                DEFAULT_AGENT_PERMISSIONS if permissions is None else permissions
            )
        except ValueError as exc:
            raise ValidationError(str(exc), detail={"field": "permissions"}) from exc
        with self.database.session() as session:
            default_space = session.get(MemorySpace, default_space_id)
            if default_space is None or default_space.status != "active":
                raise SpaceNotFoundError()
            now = _now_iso()
            agent = Agent(
                id=new_entity_id("agt"),
                name=name,
                description=description,
                agent_type=agent_type,
                source=source,
                status="active",
                default_space_id=default_space_id,
                permissions_json=json.dumps(list(perms)),
                created_at=now,
                updated_at=now,
            )
            session.add(agent)
            session.flush()
            session.add(
                AgentSpaceGrant(
                    agent_id=agent.id,
                    space_id=default_space_id,
                    access=ACCESS_READWRITE,
                    created_at=now,
                )
            )
            for space_id, access in grants:
                if space_id == default_space_id:
                    continue
                self._check_grant_values(session, space_id, access)
                session.add(
                    AgentSpaceGrant(
                        agent_id=agent.id,
                        space_id=space_id,
                        access=access,
                        created_at=now,
                    )
                )
            session.flush()
            return self._agent_dict(session, agent)

    def update_agent(
        self,
        agent_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        agent_type: Optional[str] = None,
        status: Optional[str] = None,
        permissions: Optional[Iterable[str]] = None,
        default_space_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self.database.session() as session:
            row = session.get(Agent, agent_id)
            if row is None or row.status == "deleted":
                raise AgentNotFoundError()
            if name is not None:
                row.name = _check_name(name, "name")
            if description is not None:
                row.description = _check_description(description)
            if agent_type is not None:
                if agent_type not in AGENT_TYPES:
                    raise ValidationError("unknown agent type", detail={"field": "agent_type"})
                row.agent_type = agent_type
            if status is not None:
                if status not in AGENT_STATUSES:
                    raise ValidationError("unknown agent status", detail={"field": "status"})
                row.status = status
            if permissions is not None:
                try:
                    perms = normalize_permissions(permissions)
                except ValueError as exc:
                    raise ValidationError(str(exc), detail={"field": "permissions"}) from exc
                row.permissions_json = json.dumps(list(perms))
                self._clamp_token_scopes(session, row.id, set(perms))
            if default_space_id is not None:
                grant_ids = {grant.space_id for grant in self._agent_grants(session, row.id)}
                if default_space_id not in grant_ids:
                    raise ControlConflictError("the default space must be a granted space")
                row.default_space_id = default_space_id
            row.updated_at = _now_iso()
            session.flush()
            return self._agent_dict(session, row)

    def delete_agent(self, agent_id: str) -> None:
        with self.database.session() as session:
            row = session.get(Agent, agent_id)
            if row is None or row.status == "deleted":
                raise AgentNotFoundError()
            now = _now_iso()
            row.status = "deleted"
            row.deleted_at = now
            row.updated_at = now
            for token in session.scalars(
                select(ApiToken).where(ApiToken.agent_id == agent_id, ApiToken.status == "active")
            ):
                token.status = "revoked"
                token.revoked_at = now
            session.flush()

    def _check_grant_values(self, session: Session, space_id: str, access: str) -> None:
        if access not in ACCESS_LEVELS:
            raise ValidationError("unknown access level", detail={"field": "access"})
        space = session.get(MemorySpace, space_id)
        if space is None or space.status != "active":
            raise SpaceNotFoundError()

    def set_grant(self, agent_id: str, space_id: str, access: str) -> Dict[str, Any]:
        with self.database.session() as session:
            agent = session.get(Agent, agent_id)
            if agent is None or agent.status == "deleted":
                raise AgentNotFoundError()
            self._check_grant_values(session, space_id, access)
            grant = session.get(AgentSpaceGrant, (agent_id, space_id))
            if grant is None:
                session.add(
                    AgentSpaceGrant(
                        agent_id=agent_id,
                        space_id=space_id,
                        access=access,
                        created_at=_now_iso(),
                    )
                )
            else:
                grant.access = access
            agent.updated_at = _now_iso()
            session.flush()
            return self._agent_dict(session, agent)

    def revoke_grant(self, agent_id: str, space_id: str) -> Dict[str, Any]:
        with self.database.session() as session:
            agent = session.get(Agent, agent_id)
            if agent is None or agent.status == "deleted":
                raise AgentNotFoundError()
            if space_id == agent.default_space_id:
                raise ControlConflictError(
                    "the default space grant cannot be removed; change the default space first"
                )
            grant = session.get(AgentSpaceGrant, (agent_id, space_id))
            if grant is not None:
                session.delete(grant)
            agent.updated_at = _now_iso()
            session.flush()
            return self._agent_dict(session, agent)

    # ------------------------------------------------------------------ tokens
    def _clamp_token_scopes(self, session: Session, agent_id: str, allowed: set) -> None:
        for token in session.scalars(
            select(ApiToken).where(ApiToken.agent_id == agent_id, ApiToken.status == "active")
        ):
            scopes = json.loads(token.scopes_json)
            token.scopes_json = json.dumps([scope for scope in scopes if scope in allowed])

    def issue_token(
        self,
        agent_id: str,
        *,
        name: str = "default",
        scopes: Optional[Iterable[str]] = None,
        expires_in_days: int = DEFAULT_TOKEN_TTL_DAYS,
    ) -> Tuple[Dict[str, Any], str]:
        name = _check_name(name, "name")
        lifetime_days = _check_ttl(expires_in_days)
        with self.database.session() as session:
            agent = session.get(Agent, agent_id)
            if agent is None or agent.status == "deleted":
                raise AgentNotFoundError()
            if agent.status != "active":
                raise ControlConflictError("tokens cannot be issued for a disabled agent")
            agent_perms = set(json.loads(agent.permissions_json))
            try:
                requested = normalize_permissions(
                    sorted(agent_perms) if scopes is None else scopes
                )
            except ValueError as exc:
                raise ValidationError(str(exc), detail={"field": "scopes"}) from exc
            if not requested:
                raise ValidationError("a token needs at least one scope", detail={"field": "scopes"})
            if not set(requested) <= agent_perms:
                raise ValidationError(
                    "token scopes must be a subset of the agent permissions",
                    detail={"field": "scopes"},
                )
            plaintext, public_prefix = generate_token()
            now = datetime.now(timezone.utc)
            row = ApiToken(
                id=new_entity_id("tok"),
                agent_id=agent_id,
                name=name,
                token_prefix=public_prefix,
                token_hash=digest_token(plaintext, self._signing_key()),
                scopes_json=json.dumps(list(requested)),
                status="active",
                expires_at=(now + timedelta(days=lifetime_days)).isoformat(),
                created_at=now.isoformat(),
            )
            session.add(row)
            session.flush()
            return token_to_dict(row), plaintext

    def list_tokens(self, *, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.database.session() as session:
            statement = select(ApiToken).order_by(ApiToken.created_at, ApiToken.id)
            if agent_id is not None:
                statement = statement.where(ApiToken.agent_id == agent_id)
            return [token_to_dict(row) for row in session.scalars(statement)]

    def revoke_token(self, token_id: str) -> Dict[str, Any]:
        with self.database.session() as session:
            row = session.get(ApiToken, token_id)
            if row is None:
                raise TokenNotFoundError()
            if row.status == "active":
                row.status = "revoked"
                row.revoked_at = _now_iso()
            session.flush()
            return token_to_dict(row)

    def rotate_token(self, token_id: str) -> Tuple[Dict[str, Any], str]:
        with self.database.session() as session:
            old = session.get(ApiToken, token_id)
            if old is None:
                raise TokenNotFoundError()
            if old.status != "active":
                raise ControlConflictError("only an active token can be rotated")
            now = datetime.now(timezone.utc)
            if old.expires_at and _parse_time(old.expires_at) <= now:
                raise ControlConflictError("an expired token cannot be rotated; issue a new one")
            agent = session.get(Agent, old.agent_id)
            if agent is None or agent.status != "active":
                raise ControlConflictError("the agent is not active")
            plaintext, public_prefix = generate_token()
            new_row = ApiToken(
                id=new_entity_id("tok"),
                agent_id=old.agent_id,
                name=old.name,
                token_prefix=public_prefix,
                token_hash=digest_token(plaintext, self._signing_key()),
                scopes_json=old.scopes_json,
                status="active",
                expires_at=(now + _lifetime_of(old)).isoformat(),
                created_at=now.isoformat(),
            )
            session.add(new_row)
            old.status = "revoked"
            old.revoked_at = now.isoformat()
            old.replaced_by = new_row.id
            session.flush()
            return token_to_dict(new_row), plaintext

    def authenticate_token(self, plaintext: str) -> Optional[Principal]:
        """Resolve an agent token to a principal, or ``None`` if it is not usable."""
        if not plaintext:
            return None
        digest = digest_token(plaintext, self._signing_key())
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.scalars(select(ApiToken).where(ApiToken.token_hash == digest)).first()
            if row is None or not hmac.compare_digest(row.token_hash, digest):
                return None
            if row.status != "active":
                return None
            if row.expires_at and _parse_time(row.expires_at) <= now:
                return None
            agent = session.get(Agent, row.agent_id)
            if agent is None or agent.status != "active":
                return None
            permissions = frozenset(
                set(json.loads(row.scopes_json)) & set(json.loads(agent.permissions_json))
            )
            active_spaces = {
                space.id
                for space in session.scalars(
                    select(MemorySpace).where(MemorySpace.status == "active")
                )
            }
            spaces = {
                grant.space_id: grant.access
                for grant in self._agent_grants(session, agent.id)
                if grant.space_id in active_spaces and grant.access in ACCESS_LEVELS
            }
            if row.last_used_at is None or (now - _parse_time(row.last_used_at)) > timedelta(seconds=60):
                row.last_used_at = now.isoformat()
            return Principal(
                kind=KIND_AGENT,
                label=agent.name,
                agent_id=agent.id,
                token_id=row.id,
                default_space_id=agent.default_space_id,
                permissions=permissions,
                spaces=spaces,
            )

    def owner_principal(self) -> Principal:
        spaces = {space["id"]: ACCESS_READWRITE for space in self.list_spaces()}
        return Principal(
            kind=KIND_OWNER,
            label="owner",
            default_space_id=DEFAULT_SPACE_ID,
            permissions=frozenset(ALL_PERMISSIONS),
            spaces=spaces,
            all_spaces=True,
        )

    # ------------------------------------------------------------- access logs
    def client_digest(self, host: Optional[str]) -> Optional[str]:
        if not host:
            return None
        return digest_client(host, self._signing_key())

    def record_access(
        self,
        *,
        principal_kind: str,
        agent_id: Optional[str],
        token_id: Optional[str],
        space_id: Optional[str],
        method: str,
        route: str,
        status_code: int,
        latency_ms: int,
        error_code: Optional[str],
        client_digest: Optional[str],
    ) -> None:
        with self.database.session() as session:
            session.add(
                AccessLog(
                    occurred_at=_now_iso(),
                    principal_kind=principal_kind,
                    agent_id=agent_id,
                    token_id=token_id,
                    space_id=space_id,
                    method=method[:10],
                    route=route[:200],
                    status_code=int(status_code),
                    latency_ms=max(0, int(latency_ms)),
                    error_code=error_code[:64] if error_code else None,
                    client_digest=client_digest,
                )
            )

    def prune_access_logs(self, keep: int = ACCESS_LOG_RETENTION_ROWS) -> int:
        """Keep only the newest ``keep`` access rows. Returns the number deleted."""
        with self.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(AccessLog)) or 0)
            if total <= keep:
                return 0
            cutoff = session.scalar(
                select(AccessLog.id).order_by(AccessLog.id.desc()).offset(keep).limit(1)
            )
            if cutoff is None:
                return 0
            result = session.execute(delete(AccessLog).where(AccessLog.id <= cutoff))
            return int(result.rowcount or 0)

    def list_access_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        agent_id: Optional[str] = None,
        status_family: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        with self.database.session() as session:
            statement = select(AccessLog)
            count_statement = select(func.count()).select_from(AccessLog)
            if agent_id:
                statement = statement.where(AccessLog.agent_id == agent_id)
                count_statement = count_statement.where(AccessLog.agent_id == agent_id)
            if status_family in {"2xx", "4xx", "5xx"}:
                low = int(status_family[0]) * 100
                statement = statement.where(
                    AccessLog.status_code >= low, AccessLog.status_code < low + 100
                )
                count_statement = count_statement.where(
                    AccessLog.status_code >= low, AccessLog.status_code < low + 100
                )
            total = int(session.scalar(count_statement) or 0)
            statement = statement.order_by(AccessLog.id.desc()).offset(offset).limit(limit)
            items = [
                {
                    "id": row.id,
                    "occurred_at": row.occurred_at,
                    "principal_kind": row.principal_kind,
                    "agent_id": row.agent_id,
                    "space_id": row.space_id,
                    "method": row.method,
                    "route": row.route,
                    "status_code": row.status_code,
                    "latency_ms": row.latency_ms,
                    "error_code": row.error_code,
                }
                for row in session.scalars(statement)
            ]
            return items, total

    def access_summary(self, *, hours: int = 24) -> Dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AccessLog)
                    .where(AccessLog.occurred_at >= since)
                    .order_by(AccessLog.id.desc())
                    .limit(50_000)
                )
            )
        families: Counter = Counter()
        errors: Counter = Counter()
        routes: Counter = Counter()
        latencies: List[int] = []
        for row in rows:
            families[f"{row.status_code // 100}xx"] += 1
            if row.error_code:
                errors[row.error_code] += 1
            routes[f"{row.method} {row.route}"] += 1
            latencies.append(row.latency_ms)
        average = round(sum(latencies) / len(latencies), 1) if latencies else 0.0
        return {
            "window_hours": int(hours),
            "total": len(rows),
            "by_status_family": dict(sorted(families.items())),
            "average_latency_ms": average,
            "top_error_codes": [
                {"code": code, "count": count} for code, count in errors.most_common(10)
            ],
            "top_routes": [
                {"route": route, "count": count} for route, count in routes.most_common(10)
            ],
        }

    def _signing_key_for_tests(self) -> bytes:
        return self._signing_key()
