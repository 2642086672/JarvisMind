"""FastAPI application factory and process entry point."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.staticfiles import StaticFiles

from .api.access_log import AccessLogMiddleware
from .api.auth import owner_auth, principal_auth
from .api.deps import build_token_dependency
from .api.errors import register_exception_handlers
from .api.routes import (
    context_router,
    embedding_router,
    extraction_router,
    forget_memory_router,
    forget_request_router,
    governance_router,
    health_router,
    hybrid_router,
    jmr_router,
    jmr_auto_execute_router,
    lifecycle_router,
    memories_router,
    modification_memory_router,
    modification_request_router,
    overview_router,
    sources_router,
    system_router,
)
from .api.v1.agent import router as agent_v1_router
from .api.v1.console import router as console_v1_router
from .api.v1.lab import router as lab_v1_router
from .api.v1.control import router as control_v1_router
from .config import Settings, load_settings
from .container import Container
from .control.database import CONTROL_DATABASE_FILENAME, ControlDatabase
from .control.ratelimit import SlidingWindowLimiter
from .control.service import ControlService
from .core.logging import configure_logging
from .database.connection import DatabaseManager
from .spaces import SpaceRegistry

def create_app(
    settings: Optional[Settings] = None,
    manager: Optional[DatabaseManager] = None,
) -> FastAPI:
    """Build the FastAPI app and wire routers without opening any DB connection.

    P0-03 第一阶段：启动阶段禁止数据库初始化、迁移与索引重建。
    """
    settings = settings or load_settings()
    logger = configure_logging(settings.log_level, settings.api_token)
    if manager is None:
        manager = DatabaseManager(settings)
    started_at = time.monotonic()
    container = Container(
        settings=settings,
        manager=manager,
        logger=logger,
        started_at=started_at,
    )
    # P0 fix (independent review): "enabled but not activated". Accessing the
    # lazy governance property at STARTUP installs the P1 schema and calls
    # activate() BEFORE any request is served. Without this, the first request
    # (possibly a malicious write) would arrive with is_active() == False and
    # the connection/DBAPI gates would be a no-op.
    if bool(getattr(settings, "p1_governance_enabled", False)):
        _ = container.governance

    control_db = ControlDatabase(settings.data_dir / CONTROL_DATABASE_FILENAME)
    control = ControlService(control_db, settings.data_dir)
    spaces = SpaceRegistry(settings, container, logger, started_at)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        spaces.close()
        container.close()
        manager.dispose()
        control_db.dispose()

    app = FastAPI(
        title="JarvisMind API",
        version=settings.version,
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.settings = settings
    app.state.control = control
    app.state.spaces = spaces
    app.state.rate_limiter = SlidingWindowLimiter(window_seconds=60.0)

    if settings.cors_origins:
        # CORS stays disabled unless the operator explicitly lists origins.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
            allow_headers=["Authorization", "X-API-Token", "Content-Type", "X-JarvisMind-Space"],
        )
    app.add_middleware(AccessLogMiddleware, target=app)
    app.add_middleware(ConsoleSecurityHeaders)

    register_exception_handlers(app)
    token_dependency = build_token_dependency(settings)

    app.include_router(health_router)
    # P1 unified governance surface. Always registered; endpoints that need an
    # active governance base return 409 until JARVISMIND_P1_GOVERNANCE_ENABLED=1.
    app.include_router(
        governance_router,
        prefix="/api/v1/p1",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        system_router,
        prefix="/api/v1",
        dependencies=[Depends(token_dependency)],
    )
    # JMR derived-memory surface. Registered outside /memories so it cannot be
    # captured by the CRUD `/{memory_id}` route. All endpoints are token-gated.
    app.include_router(
        jmr_router,
        prefix="/api/v1/jmr",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        embedding_router,
        prefix="/api/v1/embedding",
        dependencies=[Depends(token_dependency)],
    )
    # Extraction & hybrid routes are registered before the memory routes so that
    # /candidates, /extract, /hybrid-search, /index/* are not captured by /{memory_id}.
    # JME-MEMORY-OVERVIEW-01: /overview is a read-only summary endpoint and must
    # also be registered before the CRUD /{memory_id} route.
    app.include_router(
        overview_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        context_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        hybrid_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        extraction_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    # Lifecycle routes are registered before the CRUD memory router so that
    # /summaries, /corrections, /merges, /expiring, /archive-records, and the
    # per-memory sub-paths are not captured by /{memory_id}.
    app.include_router(
        lifecycle_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    # JW-01 forget-request sub-resource, registered before the CRUD memory
    # router so /{memory_id}/forget-requests is not captured by /{memory_id}.
    app.include_router(
        forget_memory_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    # JW-03 modification-request sub-resource, same ordering reason.
    app.include_router(
        modification_memory_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        memories_router,
        prefix="/api/v1/memories",
        dependencies=[Depends(token_dependency)],
    )
    # JW-01 top-level forget-request collection (list / detail / approve / reject).
    app.include_router(
        forget_request_router,
        prefix="/api/v1/forget-requests",
        dependencies=[Depends(token_dependency)],
    )
    # JW-03 top-level modification-request collection.
    app.include_router(
        modification_request_router,
        prefix="/api/v1/modification-requests",
        dependencies=[Depends(token_dependency)],
    )
    app.include_router(
        sources_router,
        prefix="/api/v1/memory-sources",
        dependencies=[Depends(token_dependency)],
    )
    # R3: owner-controlled automatic JMR execution settings.
    app.include_router(
        jmr_auto_execute_router,
        prefix="/api/v1/jmr",
        dependencies=[Depends(token_dependency)],
    )
    # JKG-INT-03: read-only knowledge-graph surface. Registered last and
    # after /api/v1/jmr so no earlier route is shadowed. Every endpoint is
    # token-gated. Registration itself opens no connection and installs no
    # schema; GET /status is the only endpoint that answers while the graph is
    # disabled or its tables are absent.
    #
    # Imported here rather than at module scope on purpose: main.py's top-level
    # import block must not depend on JKG at all (JKG is an optional module and
    # touching it at import time would put its schema in the startup path).
    from .api.routes.jkg import router as jkg_router

    app.include_router(
        jkg_router,
        prefix="/api/v1/jkg",
        dependencies=[Depends(token_dependency)],
    )
    # MCE-01A: read-only long-term project-context surface. Imported inside the
    # factory for the same reason as JKG: main.py's module-level imports must not
    # pull in a derived module, and registration itself must open no connection.
    from .api.routes.mce import router as mce_router

    app.include_router(
        mce_router,
        prefix="/api/v1/mce",
        dependencies=[Depends(token_dependency)],
    )
    # JarvisMind v1 surface. Owner-only control plane and console API, and the
    # agent facade that authenticates each request itself (principal_auth).
    app.include_router(
        control_v1_router,
        prefix="/api/v1/control",
        dependencies=[Depends(owner_auth)],
    )
    app.include_router(
        console_v1_router,
        prefix="/api/v1/console",
        dependencies=[Depends(owner_auth)],
    )
    app.include_router(
        agent_v1_router,
        prefix="/api/v1/agent",
        dependencies=[Depends(principal_auth)],
    )
    app.include_router(
        lab_v1_router,
        prefix="/api/v1/console",
        dependencies=[Depends(owner_auth)],
    )
    _mount_console(app, settings)
    return app


CONSOLE_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class SpaStaticFiles(StaticFiles):
    """Serve the console build. An unknown console route falls back to index.html, so a
    deep link such as /console/memories/abc can be reloaded. A missing asset is 404."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # Starlette builds `path` with the OS separator, so on Windows a missing
            # asset arrives as `assets\missing.js`. Normalise before the guard, or the
            # fallback would answer a missing asset with index.html and status 200.
            relative = path.replace("\\", "/")
            if exc.status_code != 404 or relative.startswith("assets/"):
                raise
            return await super().get_response("index.html", scope)


class ConsoleSecurityHeaders:
    """Adds browser hardening headers to every response under /console."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/console"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers["Content-Security-Policy"] = CONSOLE_CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _mount_console(app: FastAPI, settings: Settings) -> None:
    """Serve the built web console at /console when a build directory is configured.

    The static files contain no data. Every console request that touches data
    still goes through the owner-token API.
    """
    if not settings.console_dir:
        return
    directory = Path(settings.console_dir)
    if directory.is_dir() and (directory / "index.html").is_file():
        app.mount("/console", SpaStaticFiles(directory=str(directory), html=True), name="console")


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.api_token)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
