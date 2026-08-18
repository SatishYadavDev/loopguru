"""Admin endpoints: backfill, purge, sync-status (PRD §15)."""

from __future__ import annotations

import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.core.config import settings
from app.ingestion.pipeline import ingest_one
from app.schemas.document import Document
from app.storage.db import session_scope
from app.storage.db.repositories import (
    last_success_epoch,
    list_sync_states,
    recent_runs,
)
from app.storage.qdrant.store import get_store
from app.workers.sync import SOURCE, prioritized_channels

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_api_key)])


class BackfillRequest(BaseModel):
    source: str | None = None
    scope_id: str | None = None


class PurgeRequest(BaseModel):
    doc_id: str | None = None
    channel_id: str | None = None


class IngestRequest(BaseModel):
    """Manually ingest a document (bypasses Slack) — handy for testing procedural
    how-to answers from a local file (e.g. UAT setup steps) before it's in Slack.
    """

    text: str = Field(min_length=1)
    title: str | None = None
    doc_id: str | None = None


@router.post("/backfill")
def backfill(req: BackfillRequest) -> dict:
    from app.workers.tasks import full_backfill

    channels = [req.scope_id] if req.scope_id else prioritized_channels()
    for cid in channels:
        full_backfill.delay(cid)
    return {"enqueued": channels}


@router.post("/purge")
def purge(req: PurgeRequest) -> dict:
    from app.workers.tasks import purge as purge_task

    purge_task.delay(doc_id=req.doc_id, channel_id=req.channel_id)
    return {"enqueued": {"doc_id": req.doc_id, "channel_id": req.channel_id}}


@router.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    """Index a raw text/markdown document directly (source='manual'). Synchronous."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "text must not be empty")
    store = get_store()
    store.ensure_collection()
    doc_id = req.doc_id or "manual:" + hashlib.sha256(
        ((req.title or "") + text).encode("utf-8")
    ).hexdigest()[:16]
    now = int(time.time())
    doc = Document(
        doc_id=doc_id,
        source="manual",
        kind="file",
        scope_ids=["manual"],
        text=text,
        title=req.title,
        created_ts=str(now),
        created_epoch=now,
        permalink=None,
        metadata={
            "file_id": doc_id,
            "sharing_text": req.title or "",
            "mime": "text/markdown",
            "bytes": len(text),
        },
    )
    with session_scope() as session:
        stats = ingest_one(session, store, doc)
    return {
        "doc_id": doc_id,
        "chunks_upserted": stats.chunks_upserted,
        "embed_tokens": stats.embed_tokens,
    }


@router.get("/sync-status")
def sync_status() -> dict:
    now = time.time()
    with session_scope() as session:
        states = list_sync_states(session, SOURCE)
        runs = recent_runs(session, SOURCE, limit=20)
        scopes = []
        for st in states:
            last = last_success_epoch(session, SOURCE, st.scope_id)
            scopes.append(
                {
                    "scope_id": st.scope_id,
                    "backfill_status": st.backfill_status,
                    "backfill_completed_at": (
                        st.backfill_completed_at.isoformat()
                        if st.backfill_completed_at else None
                    ),
                    "last_checkpoint": st.last_checkpoint,
                    "locked_by": st.locked_by,
                    "days_since_last_success": (
                        round((now - last) / 86400, 2) if last else None
                    ),
                }
            )
        run_view = [
            {
                "scope_id": r.scope_id,
                "mode": r.mode,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "chunks_upserted": r.chunks_upserted,
                "chunks_deleted": r.chunks_deleted,
                "embed_tokens": r.embed_tokens,
            }
            for r in runs
        ]
    return {
        "configured_channels": settings.slack_channels,
        "scopes": scopes,
        "recent_runs": run_view,
        "stale_alert_days": settings.stale_scope_alert_days,
    }
