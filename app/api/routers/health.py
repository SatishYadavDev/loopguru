"""Health / readiness endpoints (no API key; PRD §15)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.storage.db import session_scope
from app.storage.qdrant.store import get_store

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness: process is up."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready() -> dict:
    """Readiness: downstream dependencies reachable."""
    from sqlalchemy import text

    checks = {"postgres": False, "qdrant": False}
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        get_store().client.get_collections()
        checks["qdrant"] = True
    except Exception:  # noqa: BLE001
        pass
    status = "ok" if all(checks.values()) else "degraded"
    return {"status": status, "checks": checks, "collection": settings.concrete_collection}
