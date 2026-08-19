"""Query orchestration (PRD §14): embed → retrieve → floor → answer → audit."""

from __future__ import annotations

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion.embedder import embed_query
from app.rag.answerer import INSUFFICIENT, answer_question
from app.rag.retriever import Passage, retrieve
from app.schemas.query import QueryFilters, QueryRequest, QueryResponse, RelatedThread
from app.storage.db import session_scope
from app.storage.db.repositories import record_query_audit
from app.storage.qdrant.store import get_store

log = get_logger(__name__)


def answer_query(req: QueryRequest) -> QueryResponse:
    started = time.time()
    store = get_store()

    query_vector = embed_query(req.question)
    result = retrieve(store, query_vector, req)

    top_doc_ids = [p.doc_id for p in result.passages]

    # relevance floor: refuse rather than hallucinate (PRD §14.8).
    # Callers may raise the bar per-query (e.g. proactive replies) via req.min_score.
    floor = req.min_score if req.min_score is not None else settings.rag_relevance_floor
    if not result.had_hits or result.best_score < floor:
        response = QueryResponse(
            answer=INSUFFICIENT, citations=[], used_chunks=0, best_score=result.best_score
        )
        _audit(req.question, top_doc_ids, [], started)
        return response

    ans = answer_question(req.question, result.passages)
    cited_links = {c.permalink for c in ans.citations if c.permalink}
    response = QueryResponse(
        answer=ans.text,
        citations=ans.citations,
        used_chunks=len(ans.citations),
        best_score=result.best_score,
        related=_related_threads(result.passages, cited_links),
    )
    _audit(req.question, top_doc_ids, ans.used_doc_ids, started)
    return response


def find_sources(question: str, scope_id: str | None = None, limit: int = 3,
                 min_score: float | None = None) -> list[RelatedThread]:
    """On-demand: find relevant Slack threads AND files for "where was this discussed / do
    we have an example / share the link" — returns links, does NOT generate an answer.
    Includes files (PDFs/canvases) so an "example" doc surfaces too.
    """
    store = get_store()
    result = retrieve(store, embed_query(question),
                      QueryRequest(question=question, filters=QueryFilters(scope_id=scope_id)))
    floor = min_score if min_score is not None else settings.related_threads_min_score
    now = time.time()
    out: list[RelatedThread] = []
    seen: set[str] = set()
    for p in sorted(result.passages, key=lambda x: x.cos, reverse=True):
        if p.cos < floor or not p.permalink or p.permalink in seen:
            continue
        seen.add(p.permalink)
        age = int((now - p.created_epoch) / 86400.0) if p.created_epoch else 0
        out.append(RelatedThread(permalink=p.permalink, title=p.title, age_days=age))
        if len(out) >= limit:
            break
    return out


def _related_threads(passages: list[Passage], exclude: set[str]) -> list[RelatedThread]:
    """Pick older, relevant Slack *conversations* (not files) to surface as prior discussions.

    Excludes anything already cited, keeps one link per thread, and only counts a message as
    "prior" once it's older than the configured age (so a just-posted message isn't shown).
    """
    if not settings.enable_related_threads:
        return []
    now = time.time()
    seen = set(exclude)
    out: list[RelatedThread] = []
    for p in sorted(passages, key=lambda x: x.cos, reverse=True):
        if p.cos < settings.related_threads_min_score:  # genuinely relevant only
            continue
        if ":file:" in p.doc_id:  # only real conversations, not file/canvas chunks
            continue
        if not p.permalink or p.permalink in seen:
            continue
        age_days = (now - p.created_epoch) / 86400.0 if p.created_epoch else 0.0
        if age_days < settings.related_threads_min_age_days:
            continue
        seen.add(p.permalink)
        out.append(RelatedThread(permalink=p.permalink, title=p.title, age_days=int(age_days)))
        if len(out) >= settings.related_threads_max:
            break
    return out


def _audit(question: str, top: list[str], used: list[str], started: float) -> None:
    latency_ms = int((time.time() - started) * 1000)
    try:
        with session_scope() as session:
            record_query_audit(
                session, question=question, top_doc_ids=top,
                used_doc_ids=used, latency_ms=latency_ms,
            )
    except Exception as e:  # noqa: BLE001 - auditing must never break a query
        log.warning("query audit failed", extra={"error": str(e)})
