"""Owner-only control-plane API: memory spaces, agents, grants, tokens, access logs.

Every route is registered behind the owner credential in ``main``. A token value
is returned exactly once, in the response that issues or rotates it. It is never
listed, logged, or retrievable afterwards.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...control.permissions import ALL_PERMISSIONS, PERMISSION_DESCRIPTIONS
from ...control.service import DEFAULT_TOKEN_TTL_DAYS, MAX_TOKEN_TTL_DAYS, ControlService
from ...schemas.memory import MemoryStatus
from ...spaces import SpaceRegistry

router = APIRouter(tags=["control"])

AccessLevel = Literal["read", "readwrite"]
TOKEN_NOTICE = "Store this token now. It is shown once and cannot be retrieved later."


def _control(request: Request) -> ControlService:
    return request.app.state.control


def _registry(request: Request) -> SpaceRegistry:
    return request.app.state.spaces


class SpaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=2, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    space_type: str = Field(default="custom", max_length=20)


class SpaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = Field(default=None, max_length=20)


class AgentGrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(..., min_length=2, max_length=63)
    access: AccessLevel = "read"


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    agent_type: str = Field(default="custom", max_length=40)
    source: str = Field(default="manual", max_length=20)
    default_space_id: str = Field(default="default", min_length=2, max_length=63)
    permissions: Optional[List[str]] = Field(default=None, max_length=len(ALL_PERMISSIONS))
    grants: List[AgentGrantIn] = Field(default_factory=list, max_length=50)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    agent_type: Optional[str] = Field(default=None, max_length=40)
    status: Optional[str] = Field(default=None, max_length=20)
    permissions: Optional[List[str]] = Field(default=None, max_length=len(ALL_PERMISSIONS))
    default_space_id: Optional[str] = Field(default=None, min_length=2, max_length=63)


class GrantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: AccessLevel


class TokenIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="default", min_length=1, max_length=120)
    scopes: Optional[List[str]] = Field(default=None, max_length=len(ALL_PERMISSIONS))
    expires_in_days: int = Field(default=DEFAULT_TOKEN_TTL_DAYS, ge=1, le=MAX_TOKEN_TTL_DAYS)


def _memory_counts(container) -> dict:
    counts = {}
    for status in MemoryStatus:
        _rows, total = container.memory_service.list(
            status=status.value,
            page=1,
            page_size=1,
            sort_by="created_at",
            sort_order="desc",
        )
        counts[status.value.lower()] = int(total)
    return counts


@router.get("/permissions")
def list_permissions() -> dict:
    return {
        "data": [
            {"name": name, "description": PERMISSION_DESCRIPTIONS[name]}
            for name in ALL_PERMISSIONS
        ]
    }


@router.get("/spaces")
def list_spaces(request: Request, include_archived: bool = False) -> dict:
    return {"data": _control(request).list_spaces(include_archived=include_archived)}


@router.post("/spaces", status_code=201)
def create_space(payload: SpaceCreate, request: Request) -> dict:
    control = _control(request)
    registry = _registry(request)
    # Every check runs before storage exists, so a rejected request leaves nothing behind.
    control.check_new_space(
        space_id=payload.id,
        name=payload.name,
        description=payload.description,
        space_type=payload.space_type,
    )
    version = registry.provision_new(payload.id)
    try:
        space = control.create_space(
            space_id=payload.id,
            name=payload.name,
            description=payload.description,
            space_type=payload.space_type,
        )
    except Exception:
        registry.discard(payload.id)
        raise
    return {"data": {**space, "schema_version": version}}


@router.get("/spaces/{space_id}")
def get_space(space_id: str, request: Request) -> dict:
    control = _control(request)
    space = control.get_space(space_id)
    registry = _registry(request)
    provisioned = registry.is_provisioned(space_id)
    counts = None
    if provisioned and space["status"] == "active":
        counts = _memory_counts(registry.container_for(space_id))
    agents = [
        {"id": agent["id"], "name": agent["name"], "access": grant["access"]}
        for agent in control.list_agents()
        for grant in agent["grants"]
        if grant["space_id"] == space_id
    ]
    return {
        "data": {
            **space,
            "provisioned": provisioned,
            "memory_counts": counts,
            "agents": agents,
        }
    }


@router.patch("/spaces/{space_id}")
def update_space(space_id: str, payload: SpaceUpdate, request: Request) -> dict:
    control = _control(request)
    if payload.status == "active":
        control.get_space(space_id)
        _registry(request).ensure_provisioned(space_id)
    space = control.update_space(
        space_id,
        name=payload.name,
        description=payload.description,
        status=payload.status,
    )
    return {"data": space}


@router.get("/agents")
def list_agents(request: Request, include_deleted: bool = False) -> dict:
    return {"data": _control(request).list_agents(include_deleted=include_deleted)}


@router.post("/agents", status_code=201)
def create_agent(payload: AgentCreate, request: Request) -> dict:
    agent = _control(request).create_agent(
        name=payload.name,
        description=payload.description,
        agent_type=payload.agent_type,
        source=payload.source,
        default_space_id=payload.default_space_id,
        permissions=payload.permissions,
        grants=[(grant.space_id, grant.access) for grant in payload.grants],
    )
    return {"data": agent}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict:
    return {"data": _control(request).get_agent(agent_id)}


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: str, payload: AgentUpdate, request: Request) -> dict:
    agent = _control(request).update_agent(
        agent_id,
        name=payload.name,
        description=payload.description,
        agent_type=payload.agent_type,
        status=payload.status,
        permissions=payload.permissions,
        default_space_id=payload.default_space_id,
    )
    return {"data": agent}


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request) -> dict:
    _control(request).delete_agent(agent_id)
    return {"data": {"id": agent_id, "status": "deleted"}}


@router.put("/agents/{agent_id}/grants/{space_id}")
def set_grant(agent_id: str, space_id: str, payload: GrantUpdate, request: Request) -> dict:
    return {"data": _control(request).set_grant(agent_id, space_id, payload.access)}


@router.delete("/agents/{agent_id}/grants/{space_id}")
def revoke_grant(agent_id: str, space_id: str, request: Request) -> dict:
    return {"data": _control(request).revoke_grant(agent_id, space_id)}


@router.get("/tokens")
def list_tokens(request: Request, agent_id: Optional[str] = None) -> dict:
    return {"data": _control(request).list_tokens(agent_id=agent_id)}


@router.post("/agents/{agent_id}/tokens", status_code=201)
def issue_token(agent_id: str, payload: TokenIssue, request: Request) -> dict:
    token, plaintext = _control(request).issue_token(
        agent_id,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
    )
    return {"data": token, "token": plaintext, "notice": TOKEN_NOTICE}


@router.post("/tokens/{token_id}/revoke")
def revoke_token(token_id: str, request: Request) -> dict:
    return {"data": _control(request).revoke_token(token_id)}


@router.post("/tokens/{token_id}/rotate", status_code=201)
def rotate_token(token_id: str, request: Request) -> dict:
    token, plaintext = _control(request).rotate_token(token_id)
    return {"data": token, "token": plaintext, "notice": TOKEN_NOTICE}


@router.get("/access-logs")
def list_access_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    agent_id: Optional[str] = Query(default=None, max_length=64),
    status_family: Optional[str] = Query(default=None, pattern="^[245]xx$"),
) -> dict:
    items, total = _control(request).list_access_logs(
        limit=limit,
        offset=offset,
        agent_id=agent_id,
        status_family=status_family,
    )
    return {"data": items, "meta": {"total": total, "limit": limit, "offset": offset}}


@router.get("/access-logs/summary")
def access_log_summary(
    request: Request,
    hours: int = Query(default=24, ge=1, le=720),
) -> dict:
    return {"data": _control(request).access_summary(hours=hours)}
