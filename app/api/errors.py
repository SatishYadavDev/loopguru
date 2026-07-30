"""Uniform JSON error envelope {error, detail} — never leak stack traces (PRD §15)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id, get_logger

log = get_logger(__name__)


def _envelope(status_code: int, error: str, detail: object) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "detail": detail, "cid": get_correlation_id()},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException):
        return _envelope(exc.status_code, "http_error", exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError):
        return _envelope(422, "validation_error", exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.error("unhandled error", extra={"error": str(exc)})
        return _envelope(500, "internal_error", "An internal error occurred")
