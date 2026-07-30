"""Query orchestration (PRD §14): embed → retrieve → floor → answer → audit."""

from __future__ import annotations

import time

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion.embedder import embed_query
from app.rag.answerer import INSUFFICIENT, answer_question
from app.rag.retriever import retrieve
from app.schemas.query import QueryRequest, QueryResponse
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

    # relevance floor: refuse rather than hallucinate (PRD §14.8)
    if not result.had_hits or result.best_score < settings.rag_relevance_floor:
        response = QueryResponse(answer=INSUFFICIENT, citations=[], used_chunks=0)
        _audit(req.question, top_doc_ids, [], started)
        return response

    ans = answer_question(req.question, result.passages)
    response = QueryResponse(
        answer=ans.text,
        citations=ans.citations,
        used_chunks=len(ans.citations),
    )
    _audit(req.question, top_doc_ids, ans.used_doc_ids, started)
    return response


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
