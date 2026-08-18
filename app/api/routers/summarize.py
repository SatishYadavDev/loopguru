"""Summarization endpoints: thread summary + channel digest."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.rag.summarizer import summarize_channel, summarize_thread

router = APIRouter(prefix="/summarize", tags=["summarize"], dependencies=[Depends(require_api_key)])


class ThreadRequest(BaseModel):
    channel_id: str
    thread_ts: str


class ChannelRequest(BaseModel):
    scope_id: str
    # up to ~100 years so a large value effectively means "whole channel"
    days: int = Field(default=7, ge=1, le=36500)


@router.post("/thread")
def thread(req: ThreadRequest) -> dict:
    if not req.channel_id or not req.thread_ts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "channel_id and thread_ts required")
    s = summarize_thread(req.channel_id, req.thread_ts)
    return {"summary": s.summary, "permalink": s.permalink, "message_count": s.item_count}


@router.post("/channel")
def channel(req: ChannelRequest) -> dict:
    s = summarize_channel(req.scope_id, req.days)
    return {"summary": s.summary, "days": s.period_days, "doc_count": s.item_count}
