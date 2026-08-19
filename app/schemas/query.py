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
    # Override the relevance floor for this query (e.g. a higher bar for proactive replies).
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


class Citation(BaseModel):
    source: str
    title: str | None = None
    permalink: str | None = None
    scope_id: str | None = None
    snippet: str


class RelatedThread(BaseModel):
    """An earlier Slack discussion relevant to the question ("this was discussed before")."""
    permalink: str
    title: str | None = None
    age_days: int = 0


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    used_chunks: int = 0
    # Top retrieval score (cosine) — a confidence signal for callers (e.g. passive replies).
    best_score: float = 0.0
    # Older, relevant threads not necessarily cited — surfaced as "discussed before".
    related: list[RelatedThread] = Field(default_factory=list)
