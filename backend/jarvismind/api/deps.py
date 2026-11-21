"""FastAPI dependencies shared by the routers."""
from __future__ import annotations

from typing import Callable

from fastapi import Request

from ..container import Container
from ..control.service import DEFAULT_SPACE_ID
from ..core.errors import ForbiddenError
from .auth import owner_auth


def get_container(request: Request) -> Container:
    """Return the container of the memory space selected by authentication.

    Authentication always selects a space before a route body runs. A request
    without a selection is refused instead of being served from the default
    space, so a route that forgets its dependency fails closed.
    """
    space_id = getattr(request.state, "space_id", None)
    if space_id is None:
        raise ForbiddenError(
            "A memory space must be selected for this request",
            detail={"code": "space_not_selected"},
        )
    default = getattr(request.app.state, "container", None)
    registry = getattr(request.app.state, "spaces", None)
    if space_id == DEFAULT_SPACE_ID or registry is None:
        if default is None:
            raise RuntimeError("Application container is not initialized")
        return default
    return registry.container_for(space_id)


def build_token_dependency(settings) -> Callable:
    """Owner-only route dependency for the internal (console) surface."""
    return owner_auth
