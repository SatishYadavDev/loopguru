"""FastAPI application (PRD §6, §15). CORS disabled by default; JSON error envelope;
per-request correlation id; request body-size guard.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.errors import register_error_handlers
from app.api.routers import admin, health, query
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, new_correlation_id

configure_logging(settings.log_level)
log = get_logger(__name__)

app = FastAPI(
    title="LoopGuru",
    version="1.0.0",
    description="Internal RAG agent over organization sources (Slack v1).",
)

register_error_handlers(app)


@app.middleware("http")
async def correlation_and_size(request: Request, call_next):
    new_correlation_id()
    # Body-size guard (PRD §15) via Content-Length when present.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_request_bytes:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": "payload_too_large", "detail": "Request body too large"},
        )
    response = await call_next(request)
    from app.core.logging import get_correlation_id

    response.headers["X-Correlation-ID"] = get_correlation_id()
    return response


@app.on_event("startup")
def _startup() -> None:
    # Best-effort: ensure the Qdrant collection/alias exists so queries work.
    try:
        from app.storage.qdrant.store import get_store

        get_store().ensure_collection()
    except Exception as e:  # noqa: BLE001 - don't crash API if Qdrant is briefly down
        log.warning("qdrant ensure_collection failed at startup", extra={"error": str(e)})


app.include_router(health.router)
app.include_router(query.router)
app.include_router(admin.router)
