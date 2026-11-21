"""Exception handlers translating domain and framework errors into JSON.

Every JSON error uses one envelope: ``{"error": {"code", "message", "details"}}``.
Responses never include tracebacks, SQL, file paths, request bodies or tokens.
The error code is also stored on the request state so the access log can record
it without ever recording the message.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError

from ..core.errors import JmeError
from ..jmr.errors import JMRError

logger = logging.getLogger("jarvismind")


def _envelope(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: Optional[dict] = None,
) -> JSONResponse:
    request.state.error_code = code
    return JSONResponse(
        status_code=status_code,
        content=_envelope(code, message, details),
        headers=headers,
    )


def _retry_headers(detail: Any) -> Optional[dict]:
    if isinstance(detail, dict) and "retry_after_seconds" in detail:
        return {"Retry-After": str(int(detail["retry_after_seconds"]))}
    return None


def _validation_details(exc: Exception) -> list:
    # Only location and message are echoed; request bodies are never reflected
    # back, so stored content cannot leak through an error.
    return [
        {"loc": list(error.get("loc", [])), "msg": error.get("msg", "")}
        for error in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(JmeError)
    async def _handle_jme_error(request: Request, exc: JmeError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("Handled error %s: %s", exc.code, exc.message)
        return _error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            exc.detail or None,
            _retry_headers(exc.detail),
        )

    @app.exception_handler(JMRError)
    async def _handle_jmr_error(request: Request, exc: JMRError) -> JSONResponse:
        # JMR errors are domain conflicts by default (409); missing dependencies
        # and stale sources keep their explicit code and message.
        if exc.status_code >= 500:
            logger.error("Handled JMR error %s: %s", exc.code, exc.message)
        return _error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            exc.detail or None,
            _retry_headers(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            _validation_details(exc),
        )

    @app.exception_handler(PydanticValidationError)
    async def _handle_pydantic(
        request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        return _error_response(
            request,
            422,
            "validation_error",
            "Request validation failed",
            _validation_details(exc),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _handle_database(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("Database failure")
        return _error_response(
            request, 503, "database_error", "Database operation failed"
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        return _error_response(request, 500, "internal_error", "Internal server error")
