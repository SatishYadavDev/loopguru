"""Query request/response schemas (PRD §14, §15)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class QueryFilters(BaseModel):
    source: str | None = None
    scope_id: str | None = None
    since_epoch: int | None = None


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    filters: QueryFilters | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


class Citation(BaseModel):
    source: str
    title: str | None = None
    permalink: str | None = None
    scope_id: str | None = None
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    used_chunks: int = 0
